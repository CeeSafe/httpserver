#!/usr/bin/env python3
"""Checks the span export (otel/) end to end: this script is the collector. The demo server (otel/demo) is asked
for pages, with and without a `traceparent`, and what it posts to /v1/traces must be OTLP/JSON with a span for
each request: the caller's trace and parent where one was sent, the method, the path, the status, and times that
enclose the request.

usage: python3 tools/otel_check.py [--port 18494]   (after `ceesafec build` in otel/demo/)
"""
import argparse, http.client, http.server, json, os, pathlib, subprocess, sys, threading, time

HERE = pathlib.Path(__file__).resolve().parent.parent
args = argparse.ArgumentParser()
args.add_argument("--port", type=int, default=18494)
a = args.parse_args()
received, kinds = [], []

class Collector(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        kinds.append((self.path, self.headers.get("content-type")))
        received.append(json.loads(body))
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")
    def log_message(self, *_):
        pass

collector = http.server.ThreadingHTTPServer(("127.0.0.1", a.port + 1), Collector)
threading.Thread(target=collector.serve_forever, daemon=True).start()
env = dict(os.environ, OTEL_PORT=str(a.port), OTEL_ENDPOINT=f"http://127.0.0.1:{a.port + 1}/v1/traces")
proc = subprocess.Popen([str(HERE / "otel" / "demo" / "out" / "otel_demo")], env=env, stdout=subprocess.DEVNULL)
problems = []
def expect(what, ok):
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        problems.append(what)

def get(path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", a.port, timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    r.read()
    c.close()
    return r

try:
    time.sleep(1.0)
    before = time.time_ns()
    parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    first = get("/a", {"traceparent": parent})
    second = get('/odd"path"\\x')
    third = get("/fail")
    for i in range(40):
        get(f"/many/{i}")
    after = time.time_ns()
    time.sleep(1.0)
    spans = [s for doc in received for rs in doc["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]
    attrs = lambda s: {x["key"]: next(iter(x["value"].values())) for x in s["attributes"]}
    by_path = {attrs(s)["url.path"]: s for s in spans}
    expect(f"the batches were posted to /v1/traces as JSON ({len(received)} batches)",
           len(received) >= 1 and all(k == ("/v1/traces", "application/json") for k in kinds))
    services = {x["value"]["stringValue"] for doc in received for rs in doc["resourceSpans"]
                for x in rs["resource"]["attributes"] if x["key"] == "service.name"}
    expect("the service is named", services == {"demo"})
    expect(f"a span for each of the 43 requests ({len(spans)})", len(spans) == 43 and len(by_path) == 43)
    s = by_path.get("/a", {})
    expect("the caller's trace and span are the span's trace and parent",
           s.get("traceId") == "0af7651916cd43dd8448eb211c80319c" and s.get("parentSpanId") == "b7ad6b7169203331")
    expect("the span is the one the response named",
           first.getheader("traceresponse") == f"00-{s.get('traceId')}-{s.get('spanId')}-01")
    s = by_path.get('/odd"path"\\x', {})
    expect("a request without a traceparent starts a trace, with no parent",
           len(s.get("traceId", "")) == 32 and "parentSpanId" not in s
           and second.getheader("traceresponse") == f"00-{s.get('traceId')}-{s.get('spanId')}-00")
    s = by_path.get("/fail", {})
    expect("a 500 is an error span, a 200 is not",
           s.get("status") == {"code": 2} and attrs(s)["http.response.status_code"] == "500"
           and "status" not in by_path["/a"] and attrs(by_path["/a"])["http.response.status_code"] == "200")
    expect("every span is a server's, named by its method", all(x["kind"] == 2 and x["name"] == "GET" for x in spans))
    expect("ids are well formed and spans differ",
           all(len(x["traceId"]) == 32 and len(x["spanId"]) == 16 for x in spans)
           and len({x["spanId"] for x in spans}) == 43)
    times = [(int(x["startTimeUnixNano"]), int(x["endTimeUnixNano"])) for x in spans]
    expect("the times enclose the requests", all(before <= st <= en <= after for st, en in times))
finally:
    proc.terminate()
    proc.wait(10)
    collector.shutdown()

# With no collector listening the server goes on answering: batches are dropped, requests are not held up.
env["OTEL_ENDPOINT"] = f"http://127.0.0.1:{a.port + 2}/v1/traces"
proc = subprocess.Popen([str(HERE / "otel" / "demo" / "out" / "otel_demo")], env=env, stdout=subprocess.DEVNULL)
try:
    time.sleep(1.0)
    statuses = []
    for i in range(30):
        statuses.append(get(f"/alone/{i}").status)
        time.sleep(0.03)
    expect("with the collector away, every request is still answered", statuses == [200] * 30 and proc.poll() is None)
finally:
    proc.terminate()
    proc.wait(10)
sys.exit(1 if problems else 0)
