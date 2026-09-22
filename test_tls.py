"""Integration test: does ACP_server.py actually serve HTTPS end to end.

Unlike smoke_test.py (which calls agents in-process and never opens a
socket), this starts the real server as a subprocess, over real TLS, on
a scratch port (ACP_PORT) so it never collides with a live dev server on
the usual 1337. Needs `openssl` on PATH (cert generation and the
independent negotiated-group check both use it) - skips with a note,
exit 0, if it's missing. Run: uv run python test_tls.py
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
STARTUP_TIMEOUT_SECONDS = 15


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(base_url: str, agent: str, text: str, verify) -> httpx.Response:
    return httpx.post(
        f"{base_url}/runs",
        json={
            "agent_name": agent,
            "input": [{"role": "user", "parts": [{"content_type": "text/plain", "content": text}]}],
            "mode": "sync",
        },
        timeout=10.0,
        verify=verify,
    )


def main() -> None:
    if shutil.which("openssl") is None:
        print("SKIP test_tls: openssl not on PATH (needed for cert gen + verification)")
        return

    tmp = Path(tempfile.mkdtemp(prefix="acp-tls-test-"))
    # Copy just the two files the server needs into an isolated dir, so
    # its coord.db (relative to ACP_server.py's own __file__) is a scratch
    # file here, never the real repo's coord.db.
    shutil.copy(HERE / "ACP_server.py", tmp / "ACP_server.py")
    shutil.copy(HERE / "store.py", tmp / "store.py")

    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-nodes", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True, capture_output=True, text=True,
    )

    port = _free_port()
    base_url = f"https://127.0.0.1:{port}"
    env = {
        **os.environ,
        "ACP_TLS_CERT": str(cert),
        "ACP_TLS_KEY": str(key),
        "ACP_PORT": str(port),
    }
    proc = subprocess.Popen(
        [sys.executable, str(tmp / "ACP_server.py")],
        cwd=tmp, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        last_error = None
        up = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"server exited early (code {proc.returncode}):\n{proc.stdout.read()}"
                )
            try:
                _call(base_url, "status", "", verify=str(cert))
                up = True
                break
            except httpx.TransportError as e:
                last_error = e
                time.sleep(0.3)
        if not up:
            raise AssertionError(f"server never came up on {base_url}: {last_error}")

        # A self-signed cert must NOT be silently trusted by default -
        # this is the exact scenario ACP_client.py's ACP_TLS_CA/
        # ACP_TLS_INSECURE knobs exist for.
        try:
            _call(base_url, "status", "", verify=True)
        except httpx.ConnectError as e:
            assert "certificate" in str(e).lower() or "ssl" in str(e).lower(), e
        else:
            raise AssertionError("self-signed cert was trusted without ACP_TLS_CA - unexpected")

        # Pinning the cert (ACP_TLS_CA's job) must work, and the agent
        # round-trip must behave exactly as it does over plain HTTP.
        r = _call(base_url, "post", "tls-test: hello over https", verify=str(cert))
        assert r.status_code == 200, r.text
        posted = r.json()["output"][0]["parts"][0]["content"]
        assert posted == "posted #1 from tls-test", posted

        r = _call(base_url, "inbox", "", verify=str(cert))
        inbox = r.json()["output"][0]["parts"][0]["content"]
        assert "hello over https" in inbox, inbox

        # Independent confirmation of what actually got negotiated, via
        # the openssl CLI rather than anything ACP_client.py/httpx report.
        s_client = subprocess.run(
            ["openssl", "s_client", "-connect", f"127.0.0.1:{port}",
             "-tls1_3", "-CAfile", str(cert)],
            input="", capture_output=True, text=True, timeout=10,
        )
        transcript = s_client.stdout + s_client.stderr
        assert "Cipher is TLS_AES_256_GCM_SHA384" in transcript, transcript
        if "Negotiated TLS1.3 group: X25519MLKEM768" in transcript:
            print("PQ hybrid group X25519MLKEM768 confirmed (OpenSSL 3.5+)")
        else:
            print(
                "note: X25519MLKEM768 not negotiated - fine on OpenSSL < 3.5, "
                "just no post-quantum hybrid available here"
            )

        print("PASS TLS end-to-end (real subprocess, real handshake)")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
