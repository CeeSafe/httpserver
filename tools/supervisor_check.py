#!/usr/bin/env python3
"""Checks the supervisor with the benchmark server: workers that crash, stall or run out of memory are replaced, the
others keep answering, and a SIGTERM to the supervisor ends them all.

usage: python3 tools/supervisor_check.py [--port 18490] [--procs 3] [--memory-mb 0] [--budget-mb 0] [--isolated]
       (after `ceesafec build` in bench/)

Then, in either mode, the worker's services (src/services.cee) on ports --port + 2: a scheduled job that traps is
started again by its `Services` supervisor, and one that traps at every run makes it give up and the worker end.

--isolated serves each connection in an isolated task (CW_ISOLATED=1): a trap then costs its connection and no
worker, and a worker whose tasks have trapped twice (CW_RECYCLE_CRASHES=2) drains and is started again. Two of its
traps hold a lock: the output cache's, then the gzip encoder's. The next uses repair and heal them, the cache's from
the bus's reading (CW_BUS=1), a service of the worker's `Services`. Those two traps are
hooks only a build with the C macro HTTPSERVER_TRAP_HOOKS has. --isolated also bounds how long a request may compute
without waiting (CW_RUN_BOUND_MS=250, ceesafec ADR 0186), which needs polls compiled in (CEE_POLLS): a request that
spins then traps with `Overran` and costs its connection, and its worker, not stalled, answers the others meanwhile.
So --isolated runs bench/out/bench-traps, built with both macros:
       (cd bench && ceesafec build --cflag -DHTTPSERVER_TRAP_HOOKS=1 --cflag -DCEE_POLLS=1 -o out/bench-traps)
"""
import argparse, gzip, http.client, os, pathlib, signal, subprocess, sys, threading, time

HERE = pathlib.Path(__file__).resolve().parent.parent
args = argparse.ArgumentParser()
args.add_argument("--port", type=int, default=18490)
args.add_argument("--procs", type=int, default=3)
args.add_argument("--memory-mb", type=int, default=0)
args.add_argument("--budget-mb", type=int, default=0)
args.add_argument("--inherit", action="store_true")
args.add_argument("--isolated", action="store_true")
a = args.parse_args()

def get(path, timeout=3.0, port=None):
    """(status, body), or None if the connection failed or timed out."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port or a.port, timeout=timeout)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body
    except (OSError, http.client.HTTPException):
        return None

def gzipped(path, timeout=3.0):
    """The body of `path` asked for as gzip, inflated; None if it was not answered with a gzip member."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", a.port, timeout=timeout)
        c.request("GET", path, headers={"accept-encoding": "gzip"})
        r = c.getresponse()
        body = r.read()
        c.close()
        if r.status != 200 or r.getheader("content-encoding") != "gzip":
            return None
        return gzip.decompress(body)
    except (OSError, http.client.HTTPException, EOFError, gzip.BadGzipFile):
        return None

def ask(c, path):
    """(status, body) of `path` on the kept-alive connection `c`, or None if it failed or timed out."""
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()
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
env["CW_INHERIT"] = "1" if a.inherit else "0"  # the benchmark server's default is 1
if a.memory_mb:
    env["CW_MEMORY_MB"] = str(a.memory_mb)
if a.budget_mb:
    env["CW_BUDGET_MB"] = str(a.budget_mb)
if a.isolated:
    env["CW_ISOLATED"] = "1"
    env["CW_RECYCLE_CRASHES"] = "2"
    env["CW_BUS"] = "1"
    env["CW_RUN_BOUND_MS"] = "250"
log = open("/tmp/supervisor_check.log", "w")
binary = HERE / "bench" / "out" / ("bench-traps" if a.isolated else "bench")
proc = subprocess.Popen([str(binary)], env=env, stdout=log, stderr=subprocess.STDOUT)
try:
    time.sleep(1.0)
    expect("every request is answered at the start", answering() == 40)
    workers = {get("/worker")[1] for _ in range(200)}
    print(f"     workers seen: {sorted(workers)}")

    # Recycling that refuses no connection (docs/elixir-gaps.md §4): while the traps below make workers pass their
    # bound (CW_RECYCLE_CRASHES=2) and be replaced, two clients ask for /small on a new connection every 10 ms each.
    # A worker that drains has another started in its place at once, and the queue of the socket they share is the
    # others' meanwhile: none of those requests may fail.
    steady = {"stop": False, "asked": 0, "failed": 0}

    def client():
        while not steady["stop"]:
            r = get("/small", timeout=5.0)
            steady["asked"] += 1
            if r != (200, b"hello, world\n"):
                steady["failed"] += 1
            time.sleep(0.01)

    clients = [threading.Thread(target=client) for _ in range(2)] if a.isolated else []
    for c in clients:
        c.start()

    if a.isolated:
        # A trap holding the cache's lock. `/bus/forget/7`, answered by another worker, reaches the one whose lock
        # is poisoned through the bus, in its bus service: it empties the table and heals the lock, where a panic
        # would cost a start of that service. Then `/forget/7` forgets in the request too.
        kept = [get("/kept/7") for _ in range(30)]
        expect("kept: /kept/7 is answered", all(r is not None and r[0] == 200 for r in kept))
        expect("lock 1: the request that traps holding the cache's lock gets no answer", get("/crash/kept") is None)
        published = []
        for _ in range(30):
            published.append(get("/bus/forget/7"))
            time.sleep(0.02)
        expect("lock 1: /bus/forget/7 is answered and published", all(r == (200, b"ok") for r in published))
        time.sleep(0.3)
        expect("lock 1: /forget/7 is answered and published",
               all(get("/forget/7") == (200, b"ok") for _ in range(10)))
        time.sleep(0.3)
        kept = [get("/kept/7") for _ in range(30)]
        expect("lock 1: /kept/7 is still answered", all(r is not None and r[0] == 200 for r in kept))
        # A trap holding the encoder's lock, half way through a body: the next gzip makes the encoder new.
        expected = get("/gz/20000")
        expect("lock 2: the request that traps holding the encoder's lock gets no answer",
               get("/crash/gz/60000") is None)
        bodies = [gzipped("/gz/20000") for _ in range(40)]
        expect(f"lock 2: every gzip after it inflates to the body ({sum(1 for b in bodies if b is not None)} of 40)",
               expected is not None and all(b == expected[1] for b in bodies))
        expect("lock 1 and 2: the server still answers", answering() == 40)

    for n in range(5):
        expect(f"crash {n + 1}: the request that traps gets no answer", get("/crash") is None)
        expect(f"crash {n + 1}: the server still answers", answering() >= 39)
    time.sleep(0.5)
    if a.isolated:
        time.sleep(1.0)  # the last recycle's drain
        steady["stop"] = True
        for c in clients:
            c.join()
        what = f"recycling: {steady['failed']} of {steady['asked']} steady requests failed while workers were replaced"
        if a.inherit:
            expect(what, steady["asked"] > 100 and steady["failed"] == 0)
        else:
            # Sockets of their own: what waits in a drained worker's queue is reset unless Linux's
            # net.ipv4.tcp_migrate_req is 1, which this check does not set; it is told, not asked.
            print(f"     {what} (sockets of their own)")

    if a.isolated:
        # The bound (ADR 0186): a request that computes past 250 ms traps with Overran and costs its connection, well
        # before the supervisor's 1.5 s stall would kill its worker; meanwhile every other request is answered.
        expect("bound: a request that computes 50 ms, under the bound, is answered",
               all(get("/spin/50") == (200, b"spun") for _ in range(5)))
        spun = {}

        def spin():
            t = time.time()
            spun["answer"] = get("/spin", timeout=6.0)
            spun["seconds"] = time.time() - t

        spinner = threading.Thread(target=spin)
        spinner.start()
        time.sleep(0.05)
        waits = []
        for _ in range(40):
            t = time.time()
            waits.append((get("/small") == (200, b"hello, world\n"), time.time() - t))
        spinner.join()
        slowest = max(w for _, w in waits)
        expect(f"bound: the request that never ends is ended, after {spun['seconds']:.2f} s (the stall is 1.5 s)",
               spun["answer"] is None and spun["seconds"] < 1.0)
        expect(f"bound: every other request is answered meanwhile, the slowest after {slowest:.2f} s",
               all(ok for ok, _ in waits) and slowest < 1.0)
        t0 = time.time()
        r = get("/spin/2000", timeout=6.0)
        seconds = time.time() - t0
        expect(f"bound: a request that computes 2 s is ended, after {seconds:.2f} s", r is None and seconds < 1.0)
        expect("bound: the server still answers", answering() == 40)
    else:
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
    # Seven traps over three workers (two of them holding a lock) and two requests ended by the run bound: at least
    # one worker took two and was replaced, none crashed, and none stalled or was killed.
    recycled = text.count("passed the bound: draining, to be replaced")
    replaced = lines[lines.index("bench: recycled") + 1] if "bench: recycled" in lines else "?"
    expect(f"the report counts the workers started in the place of one that drained ({replaced})",
           replaced.isdigit() and int(replaced) == recycled and recycled >= 1)
    expect("each replacement was started while the worker it replaced drained",
           text.count("started in the place of one that drains") == recycled)
    overran = text.count("past its bound of 250 ms")
    expect(f"the report counts no crash, no stall and no kill ({report})",
           len(report) == 4 and report[1] == "0" and report[2] == "0" and report[3] == "0")
    expect(f"each trap ended alone ({text.count('ended alone)')} of 9)", text.count("ended alone)") == 9)
    expect(f"the two spins were ended by the run bound ({overran} of 2)", overran == 2)
    expect(f"a worker whose tasks trapped twice was replaced ({recycled})", recycled >= 1 and int(report[0]) >= 5)
else:
    expect(f"the report counts 5 crashes and 1 stall ({report})", len(report) == 4 and report[1] == "5" and report[2] == "1")

# Services (src/services.cee): one worker whose scheduler (CW_JOBS=1) runs a job every second under its `Services`
# supervisor. A job that traps ends that start of the scheduler alone: the worker answers on, the scheduler is started
# again and the job runs again. A job that traps at every run is then restarted until three restarts lie within 30 s,
# and at the next end the supervisor gives up: the worker ends, and the process supervisor starts it again, with a job
# that runs.
print("--- services: one worker, CW_JOBS=1, CW_SERVICE_RESTARTS=3")
sport = a.port + 2
senv = dict(os.environ, CW_PORT=str(sport), CW_PROCS="1", CW_STALL_MS="1500", CW_JOBS="1", CW_SERVICE_RESTARTS="3")
senv["CW_INHERIT"] = "1" if a.inherit else "0"
slog_path = "/tmp/supervisor_check_services.log"
slog = open(slog_path, "w")
proc = subprocess.Popen([str(binary)], env=senv, stdout=slog, stderr=subprocess.STDOUT)

def runs():
    r = get("/job", port=sport)
    return int(r[1]) if r is not None and r[0] == 200 else -1

def wait_for(test, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if test():
            return True
        time.sleep(0.1)
    return test()

try:
    time.sleep(1.0)
    expect("services: the job runs", wait_for(lambda: runs() >= 2, 5.0))
    before = runs()
    expect("services: the job is told to trap at its next run", get("/job/trap/1", port=sport) == (200, b"ok"))
    expect("services: the job runs again after its trap, the scheduler started again",
           wait_for(lambda: runs() >= before + 2, 6.0))
    expect("services: the worker answered throughout",
           all(get("/small", port=sport) == (200, b"hello, world\n") for _ in range(20)))
    expect("services: the job is told to trap at every run", get("/job/trap/2", port=sport) == (200, b"ok"))
    gave_up = wait_for(lambda: "a service restarted too often" in open(slog_path).read(), 15.0)
    expect("services: past the intensity the supervisor gives up and the worker ends", gave_up)
    time.sleep(1.0)
    expect("services: the worker started in its place answers",
           wait_for(lambda: get("/small", port=sport) == (200, b"hello, world\n"), 5.0))
    expect("services: and its job runs", wait_for(lambda: runs() >= 1, 5.0))
    proc.send_signal(signal.SIGTERM)
    code = proc.wait(10)
    expect(f"services: the supervisor ends on SIGTERM (status {code})", code == 0)
finally:
    if proc.poll() is None:
        proc.kill()
slog.close()
text = open(slog_path).read()
lines = text.split("\n")
report = lines[lines.index("bench: started, crashed, stalled, killed") + 1:][:4] if "bench: started, crashed, stalled, killed" in lines else []
expect(f"services: one worker replaced, as a crash, no stall, no kill ({report})", report == ["2", "1", "0", "0"])
# Four traps: the one asked for once, restarted (one restart within the 30 s), then three at every run, restarted
# twice more; at the third the restarts would pass three, and the supervisor gives up.
expect(f"services: each of the job's traps ended alone ({text.count('ended alone)')} of 4)", text.count("ended alone)") == 4)

if a.isolated:
    # The bound on one event loop: a single worker, which a crash does not recycle. A request on a second connection
    # to it waits for the spin to be ended at the bound and is then answered by the same worker, three times over.
    print("--- one worker, CW_RUN_BOUND_MS=250")
    port = a.port + 1
    one = dict(os.environ, CW_PORT=str(port), CW_PROCS="1", CW_STALL_MS="1500", CW_ISOLATED="1",
               CW_RUN_BOUND_MS="250")
    log = open("/tmp/supervisor_check_one.log", "w")
    proc = subprocess.Popen([str(binary)], env=one, stdout=log, stderr=subprocess.STDOUT)
    try:
        time.sleep(1.0)
        neighbour = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
        expect("one worker: answered at the start", ask(neighbour, "/small") == (200, b"hello, world\n"))
        for n, path in enumerate(("/spin", "/spin/2000", "/spin")):
            spinning = http.client.HTTPConnection("127.0.0.1", port, timeout=6.0)
            spun = {}
            spinner = threading.Thread(target=lambda: spun.update(answer=ask(spinning, path)))
            spinner.start()
            time.sleep(0.05)
            t = time.time()
            r = ask(neighbour, "/small")
            wait = time.time() - t
            spinner.join()
            spinning.close()
            expect(f"one worker {n + 1}: {path} is ended and gets no answer", spun.get("answer", 0) is None)
            expect(f"one worker {n + 1}: the request beside it waits for its end, then is answered, after {wait:.2f} s",
                   r == (200, b"hello, world\n") and 0.1 < wait < 1.0)
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(10)
        expect(f"one worker: the supervisor ends on SIGTERM (status {code})", code == 0)
    finally:
        if proc.poll() is None:
            proc.kill()
    log.close()
    text = open("/tmp/supervisor_check_one.log").read()
    lines = text.split("\n")
    report = lines[lines.index("bench: started, crashed, stalled, killed") + 1:][:4] if "bench: started, crashed, stalled, killed" in lines else []
    expect(f"one worker: started once, no crash, no stall, no kill ({report})", report == ["1", "0", "0", "0"])
    expect(f"one worker: the three spins were ended by the bound ({text.count('past its bound of 250 ms')} of 3)",
           text.count("past its bound of 250 ms") == 3 and text.count("ended alone)") == 3)
sys.exit(1 if problems else 0)
