"""Load test: N agents over HTTP - claims, messages, reads - plus SSE listeners.

    uv run tools/load_test.py                       # its own server on a temp database
    uv run tools/load_test.py --url http://127.0.0.1:1337
    uv run tools/load_test.py --agents 20 --iterations 50 --json

What it is for: to see where SQLite starts to feel it. Every mutation runs under
BEGIN IMMEDIATE, so writes serialize on purpose; the question a load test answers is
what that costs - the latency each agent sees as the writer queue grows, whether any
caller ever sees `busy`/`locked`, and how far the WAL runs ahead of the checkpoint.

It measures, it does not assert: exit code 1 means something errored, which is the only
failure this tool reports.
"""

import argparse
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coordination.client import RemoteCoord  # noqa: E402
from coordination.service import CoordError  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(db: Path, port: int) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COORD_")}
    env.update({"PYTHONPATH": str(REPO)})
    proc = subprocess.Popen([sys.executable, "-m", "coordination.server", "--port", str(port),
                             "--db", str(db)], cwd=str(REPO), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        if "coord-server on" in line:
            return proc
        if proc.poll() is not None:
            raise SystemExit(f"server exited: {line}")
    raise SystemExit("server did not start")


def pct(values: list[float], q: float) -> float:
    """Percentile by index: no dependency on how statistics.quantiles handles short samples."""
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, max(0, int(round(q / 100 * (len(v) - 1)))))]


def agent_loop(i: int, url: str, iterations: int, stop: threading.Event,
               stats: dict, lock: threading.Lock) -> None:
    rc = RemoteCoord(url)
    me = rc.whoami(family=f"agent{i}", project="load")
    sid = me["session_id"]
    for n in range(iterations):
        if stop.is_set():
            return
        for op in ("claim", "post", "locks"):
            t0 = time.perf_counter()
            try:
                if op == "claim":
                    rc.claim(session=sid, scope=f"src/a{i}/{n}.py")
                elif op == "post":
                    rc.post(session=sid, body=f"agent{i} round {n}")
                else:
                    rc.locks(project="load")
                err = None
            except CoordError as e:
                err = e.code
            except Exception as e:                          # transport: a reset, a timeout
                err = type(e).__name__
            dt = (time.perf_counter() - t0) * 1000
            with lock:
                slot = stats.setdefault(op, {"ok": [], "err": {}})
                if err:
                    slot["err"][err] = slot["err"].get(err, 0) + 1
                else:
                    slot["ok"].append(dt)


def sse_loop(url: str, project: str, stop: threading.Event, stats: dict, lock: threading.Lock) -> None:
    """One listener on /events/stream, as a human's window or an agent following the feed does.

    It stops on `stop` rather than a deadline: the count is written in `finally`, so a reader
    still blocked in readline() when the report is printed would report zero it never delivered.
    """
    req = urllib.request.Request(f"{url}/events/stream?project={project}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    seen = 0
    try:
        with opener.open(req, timeout=5) as r:
            if r.headers.get("Content-Type") != "text/event-stream":
                return
            while not stop.is_set():
                try:
                    line = r.readline()
                except (TimeoutError, OSError):             # idle stream: the server polls once a second
                    continue
                if not line:
                    return                                  # the server closed (or was killed)
                if line.startswith(b"data: "):
                    seen += 1
    except Exception:
        pass
    finally:
        with lock:
            stats["sse"] = stats.get("sse", 0) + seen


def wal_sampler(db: Path, stop: threading.Event, out: list[int]) -> None:
    wal = Path(str(db) + "-wal")
    while not stop.is_set():
        try:
            out.append(wal.stat().st_size)
        except OSError:
            out.append(0)
        time.sleep(0.25)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agents", type=int, default=20)
    ap.add_argument("--iterations", type=int, default=50)
    ap.add_argument("--sse", type=int, default=4, help="listeners on /events/stream")
    ap.add_argument("--url", help="an already running coord-server; otherwise one is started")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    a = ap.parse_args()

    tmp = Path(os.environ.get("COORD_LOAD_TMP") or Path(__file__).resolve().parents[1] / ".load")
    tmp.mkdir(exist_ok=True)
    own, proc = False, None
    port = free_port()
    db = tmp / f"load-{port}.db"                  # a path either way: only --url does not create it
    if a.url:
        url = a.url.rstrip("/")
    else:
        own = True
        proc = start_server(db, port)
        url = f"http://127.0.0.1:{port}"

    stats: dict = {}
    lock = threading.Lock()
    stop = threading.Event()
    wals: list[int] = []
    sampler = threading.Thread(target=wal_sampler, args=(db, stop, wals), daemon=True) if own else None
    if sampler:
        sampler.start()

    print(f"{a.agents} agents x {a.iterations} rounds against {url}"
          + ("" if a.url else f" (database {db.name})"))
    # the listeners subscribe to a project, so it has to exist before they attach
    RemoteCoord(url).whoami(family="warmup", project="load")
    started = time.time()
    sse_threads = [threading.Thread(target=sse_loop, args=(url, "load", stop, stats, lock), daemon=True)
                   for _ in range(a.sse)]
    for t in sse_threads:
        t.start()
    time.sleep(1.5)                                         # one poll round, then the writes

    agents = [threading.Thread(target=agent_loop,
                               args=(i, url, a.iterations, stop, stats, lock), daemon=True)
              for i in range(a.agents)]
    for t in agents:
        t.start()
    for t in agents:
        t.join()
    elapsed = time.time() - started
    stop.set()

    for t in sse_threads:                       # a reader may be up to one socket timeout in readline()
        t.join(timeout=12)

    report = {"url": url, "agents": a.agents, "iterations": a.iterations,
              "seconds": round(elapsed, 2), "ops": {}}
    total_ok = total_err = 0
    lines = []
    for op, s in sorted(stats.items()):
        if op == "sse":
            continue
        ok, err = s["ok"], s["err"]
        total_ok += len(ok)
        total_err += sum(err.values())
        report["ops"][op] = {"ok": len(ok), "errors": err,
                             "p50_ms": round(pct(ok, 50), 2), "p95_ms": round(pct(ok, 95), 2),
                             "p99_ms": round(pct(ok, 99), 2),
                             "max_ms": round(max(ok), 2) if ok else 0,
                             "over_1s": sum(1 for x in ok if x > 1000)}
        lines.append(f"  {op:<8} ok={len(ok):<6} p50={pct(ok,50):6.2f}ms  p95={pct(ok,95):7.2f}ms  "
                     f"p99={pct(ok,99):7.2f}ms  max={max(ok) if ok else 0:8.2f}ms  "
                     f">1s={sum(1 for x in ok if x > 1000):<4} errors={err or '-'}")
    report["throughput_ops_s"] = round((total_ok + total_err) / elapsed, 1) if elapsed else 0
    report["sse_events"] = stats.get("sse", 0)
    report["errors"] = total_err
    if wals:
        report["wal_bytes"] = {"max": max(wals), "samples": len(wals)}

    if a.json:
        print(json.dumps(report, indent=1))
    else:
        print(f"\n{elapsed:.2f}s  {report['throughput_ops_s']} ops/s  "
              f"({total_ok} ok, {total_err} errored, {report['sse_events']} SSE events)")
        print("\n".join(lines))
        if wals:
            print(f"\n  WAL peaked at {max(wals):,} bytes over {len(wals)} samples "
                  f"(checkpoint keeps up when it does not climb without bound)")

    if own and proc:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)   # the server is gone: read the file
        try:
            # only what the database file itself records: busy_timeout and wal_autocheckpoint are
            # per-connection, so reading them here would report this connection's defaults, not the
            # server's (core.py sets busy_timeout=30000 and leaves autocheckpoint at SQLite's 1000)
            report["sqlite"] = {k: con.execute(f"PRAGMA {p}").fetchone()[0] for k, p in
                                (("page_size", "page_size"), ("journal_mode", "journal_mode"))}
        finally:
            con.close()
        if not a.json:
            print(f"  SQLite: page_size={report['sqlite']['page_size']} "
                  f"journal={report['sqlite']['journal_mode']} "
                  f"(connection settings: core.py busy_timeout=30000, autocheckpoint left at 1000 pages)")
        with contextlib.suppress(OSError):
            db.unlink()
            Path(str(db) + "-wal").unlink(missing_ok=True)
            Path(str(db) + "-shm").unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            tmp.rmdir()
    return 1 if total_err else 0


if __name__ == "__main__":
    sys.exit(main())
