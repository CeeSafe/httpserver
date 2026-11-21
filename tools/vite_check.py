#!/usr/bin/env python3
"""Checks httpserver_vite with examples/vite and a real Vite+ toolchain: the frontend on the API's origin.

1. Built: `vp build` writes frontend/dist, and out/vite_app serves it: the page at `/` and for the frontend's own
   routes (`/about`) not cached, hashed assets immutable, a missing asset 404, `/api/hello` the API's, and an
   unrouted path under `/api` 404.
2. Development (HTTPSERVER_FRONTEND=dev): out/vite_app starts `vp dev` itself and proxies to it. The page, a module
   (`/src/main.ts`) and Vite's client come through the server's port; Vite's client is told no HMR port of its own,
   so the browser's HMR WebSocket comes to the server too, whose upgrade is tunnelled: 101, Vite's `connected`, and
   an `update` once a component's source changes. SIGTERM ends the server, and the dev server with every process it
   started: its port is free after.

usage: python3 tools/vite_check.py [--port 18610] [--vite-port 18611]
       (needs `vp` on the PATH, https://viteplus.dev; runs `vp install` and `ceesafec build` itself)
"""
import argparse, base64, http.client, os, pathlib, re, signal, socket, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = HERE / "examples" / "vite"
FRONTEND = EXAMPLE / "frontend"
args = argparse.ArgumentParser()
args.add_argument("--port", type=int, default=18610)
args.add_argument("--vite-port", type=int, default=18611)
a = args.parse_args()
problems = []


def expect(what, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {what}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        problems.append(what)


def get(path, accept="*/*"):
    c = http.client.HTTPConnection("127.0.0.1", a.port, timeout=20)
    c.request("GET", path, headers={"Accept": accept})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, body


def listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), 0.3).close()
        return True
    except OSError:
        return False


def start(env):
    log = open(f"/tmp/vite_check_{env.get('HTTPSERVER_FRONTEND', 'built')}.log", "w")
    proc = subprocess.Popen([str(EXAMPLE / "out" / "vite_app")], cwd=EXAMPLE, stdout=log, stderr=subprocess.STDOUT,
                            env=dict(os.environ, CW_PORT=str(a.port), CW_VITE_PORT=str(a.vite_port), **env))
    for _ in range(600):
        if listening(a.port) or proc.poll() is not None:
            break
        time.sleep(0.1)
    return proc


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
        return None


def frames(sock, until, seconds):
    """Reads text frames from a server (unmasked, at most 64 KiB each) until one contains `until`."""
    sock.settimeout(seconds)
    data, seen = b"", []
    deadline = time.time() + seconds
    while time.time() < deadline:
        while len(data) >= 2:
            n, at = data[1] & 127, 2
            if n == 126:
                if len(data) < 4:
                    break
                n, at = int.from_bytes(data[2:4], "big"), 4
            if len(data) < at + n:
                break
            seen.append(data[at:at + n])
            data = data[at + n:]
            if until in seen[-1]:
                return seen
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
    return seen


def run(command, cwd):
    r = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:])
    return r.returncode == 0


expect("vp install", run(["vp", "install", "--frozen-lockfile"], FRONTEND))
expect("vp build", run(["vp", "build"], FRONTEND))
expect("ceesafec build", run(["ceesafec", "build"], EXAMPLE))
if problems:
    sys.exit(1)

print("--- built")
proc = start({})
try:
    status, head, page = get("/", "text/html")
    expect("/ is the built page", status == 200 and b'<div id="app">' in page and b"/@vite/client" not in page)
    expect("/ is not cached", head.get("cache-control") == "no-cache", head.get("cache-control"))
    script = re.search(rb'src="(/assets/[^"]+\.js)"', page)
    expect("the page names a hashed script", script is not None)
    if script:
        status, head, body = get(script.group(1).decode())
        expect("the script is served", status == 200 and len(body) > 1000 and "javascript" in head.get("content-type", ""))
        expect("the script is immutable", "immutable" in head.get("cache-control", ""), head.get("cache-control"))
    status, head, body = get("/about", "text/html")
    expect("/about, a route of the frontend's, is the page", status == 200 and body == page)
    expect("the page for a route is not cached", head.get("cache-control") == "no-cache", head.get("cache-control"))
    expect("a missing asset is 404", get("/assets/missing-0000.js")[0] == 404)
    status, head, body = get("/api/hello")
    expect("/api/hello is the API's", status == 200 and b"Hello from httpserver" in body, body[:80])
    expect("an unrouted /api path is 404", get("/api/missing")[0] == 404)
finally:
    expect("built: SIGTERM ends the server (status 0)", stop(proc) == 0)

print("--- development")
proc = start({"HTTPSERVER_FRONTEND": "dev"})
view = FRONTEND / "src" / "views" / "AboutView.vue"
original = view.read_text()
try:
    expect("the server listens once vp dev is up", listening(a.port) and listening(a.vite_port))
    status, head, page = get("/", "text/html")
    expect("/ is Vite's page, through the server", status == 200 and b"/@vite/client" in page and b"/src/main.ts" in page)
    status, head, body = get("/src/main.ts")
    expect("a module comes through the server", status == 200 and "javascript" in head.get("content-type", ""))
    status, head, client = get("/@vite/client")
    port = re.search(rb"const hmrPort = ([^;]+);", client)
    expect("Vite's client has no HMR port of its own: it uses the page's", port is not None and port.group(1) == b"null",
           port.group(1) if port else None)
    status, head, body = get("/about", "text/html")
    expect("/about is Vite's page", status == 200 and b"/@vite/client" in body)
    status, head, body = get("/api/hello")
    expect("/api/hello is still the API's", status == 200 and b"Hello from httpserver" in body)
    expect("an unrouted /api path is 404, not Vite's", get("/api/missing")[0] == 404)

    # The HMR WebSocket, as Vite's client opens it, on the server's port.
    token = re.search(rb'const wsToken = "([^"]*)"', client)
    sock = socket.create_connection(("127.0.0.1", a.port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    target = "/?token=" + token.group(1).decode() if token else "/"
    sock.sendall((f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{a.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: vite-hmr\r\n"
                  f"Origin: http://127.0.0.1:{a.port}\r\n\r\n").encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    response, _, rest = data.partition(b"\r\n\r\n")
    expect("the HMR upgrade is tunnelled (101, vite-hmr)",
           response.startswith(b"HTTP/1.1 101") and b"vite-hmr" in response.lower(), response.split(b"\r\n")[0])
    seen = frames(sock, b"connected", 10) if not rest else [rest[2:]]
    expect("Vite says connected through the tunnel", any(b'"connected"' in f for f in seen), seen[:2])
    # A component the page has loaded changes: Vite pushes an update through the tunnel.
    get("/src/views/AboutView.vue")
    view.write_text(original.replace("This is an about page", "This is the about page"))
    seen = frames(sock, b'"type":"update"', 20)
    expect("a change to a component comes as an HMR update", any(b'"type":"update"' in f for f in seen), seen[-2:])
    sock.close()
finally:
    view.write_text(original)
    expect("development: SIGTERM ends the server (status 0)", stop(proc) == 0)
    time.sleep(1.0)
    expect("and the dev server with it: its port is free", not listening(a.vite_port))

print("vite_check:", "0 problems" if not problems else f"{len(problems)} problems")
sys.exit(1 if problems else 0)
