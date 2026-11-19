#!/usr/bin/env python3
"""Checks the supervisor with the benchmark server: workers that crash, stall or run out of memory are replaced, the
others keep answering, and a SIGTERM to the supervisor ends them all.

usage: python3 tools/supervisor_check.py [--port 18490] [--procs 3] [--memory-mb 0] [--budget-mb 0] [--isolated]
       (after `ceesafec build` in bench/)

--isolated serves each connection in an isolated task (CW_ISOLATED=1): a trap then costs its connection and no
worker, and a worker whose tasks have trapped twice (CW_RECYCLE_CRASHES=2) drains and is started again.
"""
import argparse, http.client, os, pathlib, signal, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent.parent
args = argparse.ArgumentParser()
args.add_argument("--port", type=int, default=18490)
args.add_argument("--procs", type=int, default=3)
args.add_argument("--memory-mb", type=int, default=0)
args.add_argument("--budget-mb", type=int, default=0)
args.add_argument("--inherit", action="store_true")
args.add_argument("--isolated", action="store_true")
a = args.parse_args()

def get(path, timeout=3.0):
    """(status, body), or None if the connection failed or timed out."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", a.port, timeout=timeout)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body
    except (OSError, http.client.HTTPException):
        return None

def answering(tries=40):
    """How many of `tries` requests for /small are answered."""
    return sum(1 for _ in range(tries) if get("/small") == (200, b"hello, world\n"))

problems = []
def expect(what, ok):
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        problems.append(what)

env = dict(os.environ, CW_PORT=str(a.port), CW_PROCS=str(a.procs), CW_STALL_MS="1500")
if a.inherit:
    env["CW_INHERIT"] = "1"
if a.memory_mb:
    env["CW_MEMORY_MB"] = str(a.memory_mb)
if a.budget_mb:
    env["CW_BUDGET_MB"] = str(a.budget_mb)
if a.isolated:
    env["CW_ISOLATED"] = "1"
    env["CW_RECYCLE_CRASHES"] = "2"
log = open("/tmp/supervisor_check.log", "w")
proc = subprocess.Popen([str(HERE / "bench" / "out" / "bench")], env=env, stdout=log, stderr=subprocess.STDOUT)
try:
    time.sleep(1.0)
    expect("every request is answered at the start", answering() == 40)
    workers = {get("/worker")[1] for _ in range(200)}
    print(f"     workers seen: {sorted(workers)}")

    for n in range(5):
        expect(f"crash {n + 1}: the request that traps gets no answer", get("/crash") is None)
        expect(f"crash {n + 1}: the server still answers", answering() >= 39)
    time.sleep(0.5)

    t0 = time.time()
    expect("spin: the request that never ends gets no answer", get("/spin", timeout=6.0) is None)
    print(f"     the stuck worker was gone after {time.time() - t0:.1f} s")
    expect("spin: the server still answers", answering() >= 39)

    if a.memory_mb:
        r = get("/bloat", timeout=20.0)
        expect(f"bloat: the allocation fails inside the request, which is answered ({r})",
               r is not None and r[0] == 200 and 0 < int(r[1]) <= a.memory_mb)
        expect("bloat: the server still answers", answering() == 40)

    if a.budget_mb:
        # The request's own budget stops it, on any system and well short of a worker's limit.
        for n in range(3):
            r = get("/bloat", timeout=20.0)
            expect(f"budget {n + 1}: the allocation fails at the request's budget, and it is answered ({r})",
                   r is not None and r[0] == 200 and a.budget_mb - 2 <= int(r[1]) < a.budget_mb)
        expect("budget: the server still answers", answering() == 40)

    time.sleep(1.0)
    expect("every request is answered at the end", answering() == 40)
    proc.send_signal(signal.SIGTERM)
    code = proc.wait(10)
    expect(f"the supervisor ends on SIGTERM (status {code})", code == 0)
finally:
    if proc.poll() is None:
        proc.kill()
log.close()
text = open("/tmp/supervisor_check.log").read()
print("--- the supervisor's log")
print(text.strip())
lines = text.split("\n")
report = lines[lines.index("bench: started, crashed, stalled, killed") + 1:][:4] if "bench: started, crashed, stalled, killed" in lines else []
if a.isolated:
    # Five traps over three workers: at least one worker took two and was replaced, and none crashed.
    recycled = text.count("passed the bound: draining, to be replaced")
    expect(f"the report counts no crash and 1 stall ({report})", len(report) == 4 and report[1] == "0" and report[2] == "1")
    expect(f"each trap ended alone ({text.count('ended alone)')} of 5)", text.count("ended alone)") == 5)
    expect(f"a worker whose tasks trapped twice was replaced ({recycled})", recycled >= 1 and int(report[0]) >= 5)
else:
    expect(f"the report counts 5 crashes and 1 stall ({report})", len(report) == 4 and report[1] == "5" and report[2] == "1")
sys.exit(1 if problems else 0)
