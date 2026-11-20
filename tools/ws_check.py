#!/usr/bin/env python3
"""Checks the hub end to end: WebSocket clients written here from RFC 6455 (the handshake and masked frames) join
the benchmark server's /chat, and a message from one must reach all of them.

With --workers n the server runs n worker processes with a bus between them, the sessions fall on different
workers, and a message must still reach all of them.

usage: python3 tools/ws_check.py [--port 18492] [--workers 4] [--inherit]   (after `ceesafec build` in bench/)
"""
import argparse, base64, hashlib, http.client, os, pathlib, socket, struct, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent.parent
args = argparse.ArgumentParser()
args.add_argument("--port", type=int, default=18492)
args.add_argument("--workers", type=int, default=0)
args.add_argument("--inherit", action="store_true", help="the workers accept from one socket (needed on macOS)")
a = args.parse_args()
env = dict(os.environ, CW_PORT=str(a.port))
if a.workers:
    env.update(CW_PROCS=str(a.workers), CW_BUS="1")
env["CW_INHERIT"] = "1" if a.inherit else "0"  # the benchmark server's default is 1
COUNT = 12 if a.workers else 5

class Client:
    def __init__(self):
        self.s = socket.create_connection(("127.0.0.1", a.port), 5)
        self.s.settimeout(5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /chat HTTP/1.1\r\nHost: 127.0.0.1:{a.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.s.recv(1)
        want = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        self.ok = head.startswith(b"HTTP/1.1 101") and want in head

    def send(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def exactly(self, n):
        out = b""
        while len(out) < n:
            piece = self.s.recv(n - len(out))
            if not piece:
                raise EOFError
            out += piece
        return out

    def receive(self):
        b0, b1 = self.exactly(2)
        n = b1 & 127
        if n == 126:
            n = struct.unpack(">H", self.exactly(2))[0]
        return b0 & 15, self.exactly(n)

proc = subprocess.Popen([str(HERE / "bench" / "out" / "bench")], env=env, stdout=subprocess.DEVNULL)
problems = []
def expect(what, ok):
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        problems.append(what)
try:
    time.sleep(1.5)
    clients = [Client() for _ in range(COUNT)]
    expect(f"{COUNT} sessions are accepted", all(c.ok for c in clients))
    if a.workers:
        seen = set()
        for _ in range(200):
            c = http.client.HTTPConnection("127.0.0.1", a.port, timeout=5)
            c.request("GET", "/worker")
            seen.add(c.getresponse().read())
            c.close()
        expect(f"connections fall on several workers ({len(seen)})", len(seen) > 1)
    clients[0].send(1, b"hello, room")
    expect("a text message reaches all", all(c.receive() == (1, b"hello, room") for c in clients))
    big = bytes(range(256)) * 20
    clients[3].send(2, big)
    expect("a 5120-byte binary message reaches all", all(c.receive() == (2, big) for c in clients))
    clients[4].send(8, struct.pack(">H", 1000))
    expect("a session that closes gets its Close back", clients[4].receive() == (8, struct.pack(">H", 1000)))
    time.sleep(0.2)
    clients[1].send(1, b"still here")
    expect("the others go on", all(c.receive() == (1, b"still here") for c in clients[:4] + clients[5:]))
    clients[2].s.close()   # gone without a Close
    time.sleep(0.2)
    clients[0].send(1, b"three left")
    expect("a session that vanished is left out", all(c.receive() == (1, b"three left") for c in [clients[0], clients[1], clients[3]] + clients[5:]))
finally:
    proc.terminate()
    proc.wait(10)
sys.exit(1 if problems else 0)
