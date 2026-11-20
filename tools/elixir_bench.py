#!/usr/bin/env python3
"""httpserver against Elixir: the bench server (bench/out/bench) and Bandit with Plug (bench/elixir), on one Linux
machine, the servers pinned to some CPUs and the load to the others.

usage: python3 tools/elixir_bench.py [--speed] [--faults] [--duration 10] [--conns 1,16,128] [--modes h1,h2c]
                                     [--routes small,4k,64k,stream,json,echo] [--server-cpus 0,1,4,5]
                                     [--load-cpus 2,3,6,7] [--port 18090] [--json out.json]

Build first: `ceesafec build` in bench/, and `MIX_ENV=prod mix release` in bench/elixir (PATH with OTP and Elixir).
Each server gets the server CPUs: httpserver a worker process per CPU (CW_PROCS), Bandit a scheduler per CPU
(`+S n:n`). Everything else, h2load, the fault clients and this script, runs on the load CPUs. Neither server is
measured unless CeeSafe.Web's conform.py (its path in CONFORM, or beside this checkout) passes on it first.

--speed: for each mode, route and connection count, h2load for `duration` seconds after a one-second warm-up, one
request at a time on each connection (-m 1); latency percentiles from its --log-file; the server's CPU (all its
processes' user and system time over the run, as cores and as microseconds a request) and peak RSS (all its
processes, polled every 100 ms).

--faults: healthy load at a fixed rate while a fault client runs beside it, a fresh server for each. The healthy
load is HEALTHY_RATE requests a second of GET /small, spread over 64 keep-alive connections in HEALTHY_PROCS
processes; each request's latency counts from when it was due, so time spent queued behind a busy server counts (no
coordinated omission). A request whose connection ends before any answer is sent again once on a new connection, as
a browser does for a request on a reused connection, and counted as retried; one that fails again is failed. (h2load
is not used here: when the server closes connections under it, as a worker being replaced does, it can report no
requests at all for a run in which the server answered thousands.) Every row records the healthy latency
percentiles, its retried and failed requests and the connections it opened, the fault client's outcomes, the server's
RSS before, at peak and after, and its process count (httpserver replaces a worker after 100 crashes). Scenarios:
  baseline        no faults
  crash-100/s     GET /crash 100 times a second, from 32 keep-alive connections that reconnect when closed
  crash-1000/s    the same, 1000 a second
  spin-100ms      as many clients as server CPUs, each asking for /spin/100 (100 ms of computing) in a loop
  spin-forever    two requests to /spin, which never end, made at the start
Servers: `httpserver` (each connection an isolated task: a crash costs its connection), `httpserver-req`
(CW_ISOLATE_REQUESTS=1: a crash is answered 500 and the connection goes on), `httpserver-slice1` (CW_RUN_SLICE_MS=1
instead of the default 10), `bandit`.
"""
import argparse, asyncio, json, os, pathlib, platform, signal, socket, subprocess, sys, tempfile, threading, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench" / "out" / "bench"
RELEASE = ROOT / "bench" / "elixir" / "_build" / "prod" / "rel" / "bench_ex" / "bin" / "bench_ex"
ROUTES = {"small": "/small", "4k": "/4k", "64k": "/64k", "stream": "/stream", "json": "/json", "echo": "/echo"}
TICK = os.sysconf("SC_CLK_TCK")
PAGE_KIB = os.sysconf("SC_PAGE_SIZE") // 1024


# --- the servers -----------------------------------------------------------------------------------------------------

def server_command(name, cpus, port):
    n = len(cpus)
    env = dict(os.environ, CW_PORT=str(port))
    if name.startswith("httpserver"):
        env["CW_PROCS"] = str(n)
        if name == "httpserver-req":
            env["CW_ISOLATE_REQUESTS"] = "1"
        elif name == "httpserver-slice1":
            env["CW_RUN_SLICE_MS"] = "1"
        cmd = [str(BENCH)]
    elif name == "bandit":
        env.update(RELEASE_DISTRIBUTION="none", ERL_FLAGS=f"+S {n}:{n}")
        cmd = [str(RELEASE), "start"]
    else:
        raise SystemExit(f"elixir_bench: no server {name}")
    return ["taskset", "-c", ",".join(map(str, cpus)), *cmd], env


class Server:
    def __init__(self, name, cpus, port, log):
        cmd, env = server_command(name, cpus, port)
        self.name, self.port = name, port
        self.proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        if not wait_port(port, 20):
            self.stop()
            raise SystemExit(f"elixir_bench: {name} did not listen on {port}")
        time.sleep(1.0)  # every worker or scheduler up

    def stop(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()
        wait_closed(self.port)


def wait_port(port, seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def wait_closed(port, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            time.sleep(0.1)
        except OSError:
            return


# --- the server's processes ------------------------------------------------------------------------------------------

def proc_table():
    """{pid: (ppid, rss KiB, CPU seconds)} from /proc."""
    table = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            stat = open(f"/proc/{d}/stat").read()
        except OSError:
            continue
        f = stat[stat.rindex(")") + 2:].split()
        table[int(d)] = (int(f[1]), int(f[21]) * PAGE_KIB, (int(f[11]) + int(f[12])) / TICK)
    return table


def tree(root):
    """The pids under `root` (itself included), their RSS KiB and CPU seconds."""
    table = proc_table()
    pids, grew = {root}, True
    while grew:
        more = {p for p, (pp, _, _) in table.items() if pp in pids and p not in pids}
        pids |= more
        grew = bool(more)
    pids = {p for p in pids if p in table}
    return pids, sum(table[p][1] for p in pids), sum(table[p][2] for p in pids)


class Watch(threading.Thread):
    """Polls the server's processes every 100 ms: peak RSS, and every pid seen (replaced workers)."""

    def __init__(self, root):
        super().__init__(daemon=True)
        self.root, self.peak, self.seen, self.stopped = root, 0, set(), threading.Event()

    def run(self):
        while not self.stopped.is_set():
            pids, rss, _ = tree(self.root)
            self.peak, self.seen = max(self.peak, rss), self.seen | pids
            time.sleep(0.1)

    def finish(self):
        self.stopped.set()
        self.join()


# --- the load --------------------------------------------------------------------------------------------------------

def h2load(cpus, mode, path, conns, duration, scratch, body=None):
    log = scratch / "latency.log"
    threads = max(1, min(len(cpus), conns))
    args = ["taskset", "-c", ",".join(map(str, cpus)), "h2load", "-D", str(duration), "--warm-up-time", "1",
            "-c", str(conns), "-t", str(threads), "-m", "1", "--log-file", str(log),
            *(["--h1"] if mode == "h1" else [])]
    if body:
        args += ["-d", str(body)]
    r = subprocess.run(args + [f"http://127.0.0.1:{PORT}{path}"], capture_output=True, text=True)
    done = failed = errored = 0
    for line in r.stdout.splitlines():
        if line.startswith("requests:"):
            parts = line.replace(",", "").split()
            done = int(parts[parts.index("succeeded") - 1])
            failed = int(parts[parts.index("failed") - 1])
            errored = int(parts[parts.index("errored") - 1])
    samples = []
    if log.exists():
        for line in log.read_text().splitlines():
            f = line.split()
            if len(f) >= 3 and f[1] == "200":
                samples.append(int(f[2]))
        log.unlink()
    samples.sort()

    def pct(q):
        return samples[min(len(samples) - 1, int(q * len(samples)))] if samples else None
    return {"requests_per_s": round(done / duration), "succeeded": done, "failed": failed, "errored": errored,
            "p50_us": pct(0.50), "p99_us": pct(0.99), "p999_us": pct(0.999)}


# --- the fault clients (run in a process of their own, on the load CPUs) ---------------------------------------------

async def one_request(state, path):
    """Sends GET path on the connection in `state` (opening one if there is none) and reads the answer. Returns the
    status, or None when the connection ended or reset first."""
    if state.get("w") is None:
        state["r"], state["w"] = await asyncio.open_connection("127.0.0.1", PORT)
        state["opened"] = state.get("opened", 0) + 1
    r, w = state["r"], state["w"]
    try:
        w.write(f"GET {path} HTTP/1.1\r\nhost: x\r\n\r\n".encode())
        await w.drain()
        head = await r.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {k.strip().lower(): v.strip().lower() for k, _, v in (l.partition(":") for l in lines[1:] if l)}
        if "content-length" in headers:
            await r.readexactly(int(headers["content-length"]))
        elif headers.get("transfer-encoding") == "chunked":
            while True:
                size = int((await r.readuntil(b"\r\n")).split(b";")[0], 16)
                await r.readexactly(size + 2)
                if size == 0:
                    break
        if headers.get("connection") == "close":
            close(state)
        return status
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, IndexError):
        close(state)
        return None


def close(state):
    if state.get("w") is not None:
        state["w"].close()
    state["w"] = None


async def crash_client(rate, duration, conns=32):
    counts = {"sent": 0, "answered_500": 0, "other_status": 0, "no_answer": 0}
    states = [{} for _ in range(conns)]
    queue = asyncio.Queue()

    async def worker(state):
        while True:
            await queue.get()
            status = await one_request(state, "/crash")
            counts["sent"] += 1
            if status == 500:
                counts["answered_500"] += 1
            elif status is None:
                counts["no_answer"] += 1
            else:
                counts["other_status"] += 1

    tasks = [asyncio.create_task(worker(s)) for s in states]
    start = time.monotonic()
    k = 0
    while time.monotonic() - start < duration:
        k += 1
        queue.put_nowait(k)
        await asyncio.sleep(max(0.0, start + k / rate - time.monotonic()))
    await asyncio.sleep(1.0)
    for t in tasks:
        t.cancel()
    counts["connections_opened"] = sum(s.get("opened", 0) for s in states)
    counts["unsent"] = queue.qsize()
    return counts


async def spin_client(clients, ms, duration):
    lat = []
    counts = {"answered": 0, "no_answer": 0}

    async def loop():
        state = {}
        end = time.monotonic() + duration
        while time.monotonic() < end:
            t = time.monotonic()
            status = await one_request(state, f"/spin/{ms}")
            if status == 200:
                counts["answered"] += 1
                lat.append(time.monotonic() - t)
            else:
                counts["no_answer"] += 1
    await asyncio.gather(*(loop() for _ in range(clients)))
    lat.sort()
    counts["spin_p50_ms"] = round(lat[len(lat) // 2] * 1000) if lat else None
    counts["spin_max_ms"] = round(lat[-1] * 1000) if lat else None
    return counts


async def forever_client(count, duration):
    """Starts `count` requests to /spin and holds them for the run; then reports what became of them."""
    states = [{} for _ in range(count)]
    tasks = [asyncio.create_task(one_request(s, "/spin")) for s in states]
    done, pending = await asyncio.wait(tasks, timeout=duration + 1)
    ended = sum(1 for t in done if t.result() is None)
    for t in pending:
        t.cancel()
    return {"started": count, "still_running": len(pending), "ended_by_server": ended}


HEALTHY_RATE = 10000
HEALTHY_CONNS = 64
HEALTHY_PROCS = 2


async def healthy_client(rate, conns, duration):
    """Open loop: request k is due at start + k / rate and goes to the next idle connection; its latency counts from
    when it was due."""
    idle = asyncio.Queue()
    for _ in range(conns):
        idle.put_nowait({})
    lat, counts = [], {"done": 0, "retried": 0, "failed": 0}

    async def one(due):
        state = await idle.get()
        status = await one_request(state, "/small")
        if status is None:
            counts["retried"] += 1
            status = await one_request(state, "/small")
        idle.put_nowait(state)
        if status == 200:
            counts["done"] += 1
            lat.append(time.monotonic() - due)
        else:
            counts["failed"] += 1

    start = time.monotonic() + 0.2
    tasks, k = set(), 0
    while True:
        due = start + k / rate
        if due - start >= duration:
            break
        delay = due - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        t = asyncio.create_task(one(due))
        tasks.add(t)
        t.add_done_callback(tasks.discard)
        k += 1
    if tasks:
        await asyncio.wait(tasks, timeout=10)
    counts["unfinished"] = sum(1 for t in tasks if not t.done())
    counts["connections_opened"] = 0
    while not idle.empty():
        counts["connections_opened"] += idle.get_nowait().get("opened", 0)
    counts["latencies_us"] = [round(x * 1e6) for x in lat]
    return counts


def fault_main(kind, arg, duration):
    if kind == "crash":
        out = asyncio.run(crash_client(arg, duration))
    elif kind == "spin":
        out = asyncio.run(spin_client(int(arg), 100, duration))
    elif kind == "healthy":
        out = asyncio.run(healthy_client(arg, HEALTHY_CONNS // HEALTHY_PROCS, duration))
    else:
        out = asyncio.run(forever_client(int(arg), duration))
    print(json.dumps(out))


# --- the runs --------------------------------------------------------------------------------------------------------

def loadavg():
    return open("/proc/loadavg").read().split()[0]


def speed(args, servers, scratch, log):
    body = scratch / "body"
    body.write_bytes(bytes(ord("a") + i % 26 for i in range(4096)))
    rows = []
    for name in servers:
        for mode in args.modes:
            for route in args.routes:
                for c in args.conns:
                    srv = Server(name, args.server_cpus, PORT, log)
                    watch = Watch(srv.proc.pid)
                    watch.start()
                    before, la = tree(srv.proc.pid)[2], loadavg()
                    t0 = time.monotonic()
                    r = h2load(args.load_cpus, mode, ROUTES[route], c, args.duration, scratch,
                               body if route == "echo" else None)
                    wall = time.monotonic() - t0
                    after = tree(srv.proc.pid)[2]
                    watch.finish()
                    srv.stop()
                    cores = (after - before) / wall
                    row = {"server": name, "mode": mode, "route": route, "conns": c, **r,
                           "cpu_cores": round(cores, 2),
                           "cpu_us_per_request": round(cores * 1e6 / r["requests_per_s"], 1) if r["requests_per_s"] else None,
                           "peak_rss_kib": watch.peak, "load_before": la}
                    rows.append(row)
                    print(f"{name:15} {mode:4} {route:7} c={c:<4} {r['requests_per_s']:>8} req/s  p50 {r['p50_us']} "
                          f"p99 {r['p99_us']} us  cpu {row['cpu_cores']}  rss {watch.peak // 1024} MiB  "
                          f"fail {r['failed']}", flush=True)
    return rows


FAULTS = [("baseline", None, None), ("crash-100/s", "crash", 100), ("crash-1000/s", "crash", 1000),
          ("spin-100ms", "spin", None), ("spin-forever", "forever", 2)]


def faults(args, servers, scratch, log):
    rows = []
    for name in servers:
        for label, kind, arg in FAULTS:
            srv = Server(name, args.server_cpus, PORT, log)
            pids0, rss0, _ = tree(srv.proc.pid)
            watch = Watch(srv.proc.pid)
            watch.start()
            client = None
            if kind:
                a = arg if arg is not None else len(args.server_cpus)
                client = subprocess.Popen(["taskset", "-c", ",".join(map(str, args.load_cpus)), sys.executable,
                                           __file__, "--fault-client", kind, str(a), str(args.duration + 1),
                                           "--port", str(PORT)], stdout=subprocess.PIPE, text=True)
            la = loadavg()
            r = healthy(args)
            outcome = json.loads(client.communicate()[0]) if client else {}
            time.sleep(0.5)
            pids1, rss1, _ = tree(srv.proc.pid)
            watch.finish()
            srv.stop()
            row = {"server": name, "fault": label, "healthy": r, "fault_client": outcome,
                   "rss_kib_before": rss0, "rss_kib_peak": watch.peak, "rss_kib_after": rss1,
                   "processes_seen": len(watch.seen), "processes_at_start": len(pids0), "load_before": la}
            rows.append(row)
            print(f"{name:17} {label:13} p50 {r['p50_us']} p99 {r['p99_us']} p99.9 {r['p999_us']} max {r['max_us']} us"
                  f"  done {r['done']} retried {r['retried']} failed {r['failed']} conns {r['connections_opened']}"
                  f"  rss {rss0 // 1024}->{watch.peak // 1024}->{rss1 // 1024} MiB  procs {len(pids0)}/{len(watch.seen)}"
                  f"  {outcome}", flush=True)
    return rows


def healthy(args):
    """The healthy load: HEALTHY_PROCS clients on the load CPUs, their counts summed and latencies pooled."""
    procs = [subprocess.Popen(["taskset", "-c", ",".join(map(str, args.load_cpus)), sys.executable, __file__,
                               "--fault-client", "healthy", str(HEALTHY_RATE / HEALTHY_PROCS), str(args.duration),
                               "--port", str(PORT)], stdout=subprocess.PIPE, text=True) for _ in range(HEALTHY_PROCS)]
    parts = [json.loads(p.communicate()[0]) for p in procs]
    lat = sorted(x for p in parts for x in p.pop("latencies_us"))
    total = {k: sum(p[k] for p in parts) for k in parts[0]}

    def pct(q):
        return lat[min(len(lat) - 1, int(q * len(lat)))] if lat else None
    return {**total, "rate": HEALTHY_RATE, "p50_us": pct(0.5), "p99_us": pct(0.99), "p999_us": pct(0.999),
            "max_us": lat[-1] if lat else None}


def conform(servers, cpus, log):
    script = pathlib.Path(os.environ.get("CONFORM", ROOT.parent / "conform.py"))
    for name in servers:
        if name in ("httpserver-req", "httpserver-slice1"):
            continue
        srv = Server(name, cpus, PORT, log)
        r = subprocess.run([sys.executable, str(script), "--port", str(PORT), "--name", name], capture_output=True,
                           text=True)
        srv.stop()
        print(r.stdout.strip() or r.stderr.strip(), flush=True)
        if r.returncode != 0:
            raise SystemExit(f"elixir_bench: {name} fails the contract; nothing measured")


def environment(args):
    def first(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.strip().splitlines()[0]
        except (OSError, IndexError):
            return None
    cpu = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), None)
    return {"cpu": cpu, "threads": os.cpu_count(), "kernel": platform.release(),
            "os": first(["sh", "-c", ". /etc/os-release; echo $PRETTY_NAME"]),
            "server_cpus": args.server_cpus, "load_cpus": args.load_cpus,
            "ceesafec": first(["ceesafec", "--version"]),
            "elixir": first(["sh", "-c", "elixir --version | tail -1"]),
            "otp": first(["erl", "-noshell", "-eval", "io:put_chars(erlang:system_info(otp_release)), halt()."]),
            "h2load": first(["h2load", "--version"]),
            "bandit_lock": (ROOT / "bench" / "elixir" / "mix.lock").read_text() if (ROOT / "bench" / "elixir" / "mix.lock").exists() else None,
            "duration_s": args.duration, "healthy": {"rate": HEALTHY_RATE, "conns": HEALTHY_CONNS,
                                                      "procs": HEALTHY_PROCS}, "date": time.strftime("%Y-%m-%d %H:%M %Z")}


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", action="store_true")
    ap.add_argument("--faults", action="store_true")
    ap.add_argument("--duration", type=int, default=10)
    ap.add_argument("--conns", default="1,16,128")
    ap.add_argument("--modes", default="h1,h2c")
    ap.add_argument("--routes", default="small,4k,64k,stream,json,echo")
    ap.add_argument("--servers", default="httpserver,bandit")
    ap.add_argument("--fault-servers", default="httpserver,httpserver-req,httpserver-slice1,bandit")
    ap.add_argument("--server-cpus", default="0,1,4,5")
    ap.add_argument("--load-cpus", default="2,3,6,7")
    ap.add_argument("--port", type=int, default=18090)
    ap.add_argument("--json")
    ap.add_argument("--fault-client", nargs=3, metavar=("KIND", "ARG", "DURATION"))
    args = ap.parse_args()
    PORT = args.port
    if args.fault_client:
        kind, arg, duration = args.fault_client
        fault_main(kind, float(arg), float(duration))
        return
    args.conns = [int(c) for c in args.conns.split(",")]
    args.modes = args.modes.split(",")
    args.routes = args.routes.split(",")
    args.server_cpus = [int(c) for c in args.server_cpus.split(",")]
    args.load_cpus = [int(c) for c in args.load_cpus.split(",")]
    servers = args.servers.split(",")
    with tempfile.TemporaryDirectory() as tmp, open(pathlib.Path(tmp) / "servers.log", "w") as log:
        scratch = pathlib.Path(tmp)
        conform(sorted(set(servers) | set(args.fault_servers.split(",") if args.faults else [])), args.server_cpus, log)
        out = {"environment": environment(args)}
        if args.speed:
            out["speed"] = speed(args, [s for s in servers if s in ("httpserver", "bandit")], scratch, log)
        if args.faults:
            fs = args.fault_servers.split(",")
            out["faults"] = faults(args, fs, scratch, log)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(out, indent=1))


PORT = 18090
if __name__ == "__main__":
    main()
