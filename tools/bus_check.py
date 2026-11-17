#!/usr/bin/env python3
"""Checks the bus between the supervisor's workers (`Bus`, `carry`) with four workers of the benchmark server:
a number said to one worker is heard by all four, and a cached response forgotten through one is forgotten by all.
Without the bus only the worker asked hears and forgets.

usage: python3 tools/bus_check.py [--port 18493] [--inherit]   (after `ceesafec build` in bench/)
"""
import argparse, http.client, os, pathlib, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent.parent
args = argparse.ArgumentParser()
args.add_argument("--port", type=int, default=18493)
args.add_argument("--inherit", action="store_true", help="the workers accept from one socket (needed on macOS)")
a = args.parse_args()

def get(path):
    c = http.client.HTTPConnection("127.0.0.1", a.port, timeout=20)  # a connection each: any worker's
    c.request("GET", path)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data

def heard(tries=400):
    """What each worker says it heard last: {worker: number}."""
    seen = {}
    for _ in range(tries):
        worker, number = get("/bus/heard")[1].split()
        seen[worker] = int(number)
        if len(seen) == 4 and _ > 40:
            break
    return seen

def misses(count=400):
    """How many of `count` requests for /kept/4096 ran the handler (a kept response has `age`)."""
    return sum(1 for _ in range(count) if get("/kept/4096")[0].getheader("age") is None)

def run(bus):
    env = dict(os.environ, CW_PORT=str(a.port), CW_PROCS="4")
    if bus:
        env["CW_BUS"] = "1"
    if a.inherit:
        env["CW_INHERIT"] = "1"
    proc = subprocess.Popen([str(HERE / "bench" / "out" / "bench")], env=env, stdout=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        results = []
        for number in (42, 7, 123456):
            said = get(f"/bus/say/{number}")[1]
            time.sleep(0.1)
            seen = heard()
            results.append((said, len(seen), sum(1 for v in seen.values() if v == number)))
        first = misses()        # each worker makes the response once
        again = misses()        # and then answers from what it kept
        get("/forget/4096")
        time.sleep(0.1)
        after = misses()        # every worker that forgot makes it once more
        return results, (first, again, after)
    finally:
        proc.terminate()
        proc.wait(10)

problems = 0
said, kept = run(True)
ok = all(s == (b"ok", 4, 4) for s in said) and kept == (4, 0, 4)
print(f"{'ok  ' if ok else 'FAIL'} with the bus:    said/workers/heard {said}  handler runs before, kept, after forget {kept}")
problems += 0 if ok else 1
said, kept = run(False)
ok = all(s == (b"ok", 4, 1) for s in said) and kept == (4, 0, 1)
print(f"{'ok  ' if ok else 'FAIL'} without the bus: said/workers/heard {said}  handler runs before, kept, after forget {kept}")
problems += 0 if ok else 1
sys.exit(1 if problems else 0)
