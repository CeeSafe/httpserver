#!/usr/bin/env python3
"""Checks `Compression` against Python's gzip: the benchmark server's /gz/{n} is fetched with and without
`Accept-Encoding: gzip` for many sizes, and what comes back must inflate to the bytes this script builds itself.

usage: python3 tools/gzip_check.py [--port 18491]   (after `ceesafec build` in bench/)
"""
import argparse, gzip, http.client, os, pathlib, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent.parent
args = argparse.ArgumentParser()
args.add_argument("--port", type=int, default=18491)
a = args.parse_args()
WORDS = [b'"status":', b'"ok",', b'"id":', b'"name":', b'"value":', b"true,", b"false,", b"null,"]

def body(n):
    out = bytearray()
    x = 12345
    while len(out) < n:
        x = (x * 1103515245 + 12345) & 0xFFFFFFFF
        out += WORDS[(x >> 16) % 8]
    return bytes(out[:n])

def get(n, accept):
    c = http.client.HTTPConnection("127.0.0.1", a.port, timeout=20)
    c.request("GET", f"/gz/{n}", headers={"Accept-Encoding": accept} if accept else {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data

proc = subprocess.Popen([str(HERE / "bench" / "out" / "bench")], env=dict(os.environ, CW_PORT=str(a.port)),
                        stdout=subprocess.DEVNULL)
problems = 0
try:
    time.sleep(1.0)
    for n in [0, 1, 2, 3, 1023, 1024, 1025, 4096, 32767, 32768, 32769, 65536, 100000, 262144, 1000003, 4194304]:
        want = body(n)
        r, plain = get(n, None)
        r2, packed = get(n, "gzip")
        encoded = r2.getheader("content-encoding") == "gzip"
        ok = plain == want and r.getheader("content-encoding") is None
        ok = ok and encoded == (n >= 1024) and (r2.getheader("vary") == "accept-encoding") == (n >= 1024)
        ok = ok and (gzip.decompress(packed) if encoded else packed) == want
        ratio = f"{len(packed) / n:.2f}" if encoded else "-"
        print(f"{'ok  ' if ok else 'FAIL'} {n:>8} bytes  gzip {len(packed) if encoded else '-':>8}  ratio {ratio}")
        problems += 0 if ok else 1
finally:
    proc.terminate()
    proc.wait(10)
sys.exit(1 if problems else 0)
