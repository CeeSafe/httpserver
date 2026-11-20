#!/usr/bin/env python3
"""Checks the rate limit the supervisor's workers share (`RateLimit.shared`, `SharedWords`): with four workers and a
limit of 200, 1000 requests on 1000 connections from 16 threads must be answered 200 exactly 200 times, by more
than one worker. Without the shared words each worker counts for itself, and more than 200 pass.

usage: python3 tools/ratelimit_check.py [--port 18492] [--inherit]   (after `ceesafec build` in bench/)
"""
import argparse, collections, http.client, os, pathlib, subprocess, sys, threading, time

HERE = pathlib.Path(__file__).resolve().parent.parent
args = argparse.ArgumentParser()
args.add_argument("--port", type=int, default=18492)
args.add_argument("--inherit", action="store_true", help="the workers accept from one socket (needed on macOS)")
a = args.parse_args()
LIMIT, REQUESTS, THREADS = 200, 1000, 16

def run(shared):
    env = dict(os.environ, CW_PORT=str(a.port), CW_PROCS="4", CW_LIMIT=str(LIMIT))
    if shared:
        env["CW_SHARED"] = "1"
    env["CW_INHERIT"] = "1" if a.inherit else "0"  # the benchmark server's default is 1
    proc = subprocess.Popen([str(HERE / "bench" / "out" / "bench")], env=env, stdout=subprocess.DEVNULL)
    statuses, workers, lock = collections.Counter(), collections.Counter(), threading.Lock()
    def ask(count):
        for _ in range(count):
            c = http.client.HTTPConnection("127.0.0.1", a.port, timeout=20)  # a connection each: any worker's
            c.request("GET", "/worker")
            r = c.getresponse()
            data = r.read()
            c.close()
            with lock:
                statuses[r.status] += 1
                if r.status == 200:
                    workers[data] += 1
    try:
        time.sleep(1.5)
        threads = [threading.Thread(target=ask, args=(REQUESTS // THREADS,)) for _ in range(THREADS)]
        for t in threads: t.start()
        for t in threads: t.join()
    finally:
        proc.terminate()
        proc.wait(10)
    return statuses, workers

problems = 0
statuses, workers = run(True)
sent = REQUESTS // THREADS * THREADS
ok = statuses[200] == LIMIT and statuses[429] == sent - LIMIT and len(workers) > 1
print(f"{'ok  ' if ok else 'FAIL'} shared:      {statuses[200]} answered, {statuses[429]} refused, by {len(workers)} workers {sorted(workers.values())}")
problems += 0 if ok else 1
statuses, workers = run(False)
ok = statuses[200] > LIMIT and statuses[200] + statuses[429] == sent
print(f"{'ok  ' if ok else 'FAIL'} each its own: {statuses[200]} answered, {statuses[429]} refused, by {len(workers)} workers {sorted(workers.values())}")
problems += 0 if ok else 1
sys.exit(1 if problems else 0)
