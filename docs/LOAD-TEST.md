# Load test - 20 agents, and where SQLite feels it

The tool is `tools/load_test.py`; this is what it measured on 25 September 2026 (Windows, one
machine, `0.13.0`), and what the numbers say. Run it yourself:

```sh
uv run tools/load_test.py                     # its own server on a temp database
uv run tools/load_test.py --url http://127.0.0.1:1337
uv run tools/load_test.py --agents 20 --iterations 50 --json
```

## Method

20 agent threads against a real `coord-server` over HTTP (not in-process), each doing 50 rounds
of **claim → post → locks** - one write, one write, one read - so 1 000 of each. Four listeners
sit on `/events/stream` for the duration, the way a window or a following agent would. The WAL
size is sampled four times a second. One run, 22 s, plain loopback without mTLS or OIDC.

```
22.25s  134.8 ops/s  (3000 ok, 0 errored, 7975 SSE events)
  claim    ok=1000   p50= 36.42ms  p95= 557.81ms  p99=2516.20ms  max= 5836.81ms  >1s=37
  locks    ok=1000   p50= 29.30ms  p95=  94.11ms  p99= 156.49ms  max=  253.95ms  >1s=0
  post     ok=1000   p50= 27.17ms  p95= 270.58ms  p99=2324.59ms  max=11109.74ms  >1s=27

  WAL peaked at 4,194,192 bytes over 89 samples
  SQLite: page_size=4096 journal=wal
```

## What the numbers say

**1. Reads do not degrade.** `locks` - a read - holds p50 29 ms, p99 156 ms and **zero**
operations over a second, while writes around it are reaching seconds. WAL readers do not queue
behind a writer; this is the whole reason the database is in WAL mode (`core.py`).

**2. Writes convoy, and the damage is in the tail.** p50 is 27-36 ms - three agents would never
notice - but p99 is 2.3-2.5 s and the worst write of the run took 11 s. 64 of 2 000 writes (3.2%)
exceeded one second. Every mutation runs under `BEGIN IMMEDIATE`, so exactly one write happens at
a time by design; what 20 agents produce is a queue, and the queue's length varies with whatever
else the database is doing.

**3. Nothing failed.** 3 000 writes, **zero** `busy` or `locked` errors: the server's
`PRAGMA busy_timeout=30000` (`core.py`) absorbed every wait. Under this load coord degrades in
*latency*, never in correctness - no caller sees a rejected write.

**4. The WAL stops at exactly the checkpoint threshold.** It peaked at 4 194 192 bytes against
`page_size=4096` and SQLite's default `wal_autocheckpoint` of **1 000 pages = 4 096 000 bytes** -
the file grows to the threshold and is then checkpointed. That is the obvious suspect for the
multi-second outliers: an automatic checkpoint runs on the connection that triggered it, and
that connection is an agent waiting for its reply.

> **Hypothesis, not a measurement.** The WAL ceiling and the tail correlate; nothing here proves
> the checkpoint *causes* the stalls. The experiment is one line: set `PRAGMA wal_autocheckpoint`
> higher (or 0, checkpointing from maintenance instead) and re-run. That is the first thing
> **T36** should try, and this tool is how it gets an answer.

**5. SSE is a 1 Hz fan-out, and it scales.** 7 975 events reached 4 listeners in 22 s with no
reader falling behind. `/events/stream` polls the event log every `STREAM_POLL_SECONDS` (1.0) and
closes after `STREAM_MAX_SECONDS` (3600) for the client to resume on `Last-Event-ID` - so the cost
per subscriber is one query per second, not one per event, and it did not move the write numbers.

## What this run does not cover

- **No mTLS, no OIDC, no UI** - plain loopback HTTP. Certificate verification per request and
  token introspection are both outside these numbers.
- **One machine, 22 seconds.** This is a burst, not a soak: **T35** owns the 24 h run, where
  renewal, checkpoint accumulation and memory are the questions.
- **No A2A push and no wake hooks**, and a single SQLite file with no `coord-db prune` running
  against it (events grew for 22 s and were never pruned).

## Where it leaves the roadmap

T34 is done when this exists and the limits are written down. What it found goes to **T36** -
"fix what the review, load and soak tests find": the checkpoint hypothesis above is the one
concrete, cheap experiment. Nothing here blocks **T37** (hardened); the 24 h soak (T35) still has
to show that a longer run does not turn a tail into a trend.
