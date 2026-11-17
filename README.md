# httpserver

An HTTP application server in [CeeSafe](https://ceesafe.github.io/CeeSafe/): routing, route groups, middleware and
replies, on [CeeSafe.Web](https://github.com/CeeSafe/CeeSafe.Web)'s connection server and its HTTP/1.1 and HTTP/2
drivers. It is a port of CosmoApiServer (.NET) by what it does, not line by line: the protocol work is CeeSafe.Web's,
and this package is what an application is written against.

```ceesafe
import web_core;
import web_transport;
import web_http;
import httpserver;

struct Shop { visits: AtomicU64; }

struct Item {}

impl Handler<Shop> for Item {
    async fn handle(
        borrow this,
        ctx: borrow mut Context<Shop>,
        request: borrow Request,
        exchange: borrow mut Exchange,
    ) -> Result<void, HandlerError> {
        ctx.state().visits.fetchAdd(1);
        let id = ctx.paramInt("id") ?? { return await Reply.problem(exchange, Status.badRequest(), "", "no id"); };
        return await Reply.json(exchange, Status.ok(), "{\"ok\":true}".bytes());
    }
}

// var app = App<Shop>.create(Shop { visits: AtomicU64.create(0) }, Limits.defaults())?;
// app.use(logging)?;                                           // for every request
// let api = app.group("/api")?;
// app.useIn(api, authentication)?;                             // for the group's routes
// app.routeIn(api, Method.Get, "/items/{id:int}", item)?;      // GET /api/items/42
// app.get("/health", health)?;
// await app.run(listener, PlainAcceptor {}, ConnectionLimits.defaults(), drain)
```

`bench/` is a whole program.

## What is here

- **Routes.** `/`, literal segments, `{name}`, `{name:int}` (also `long`, `alpha`, `bool`, `guid`) and, last,
  `{*rest}`. Matching is by method: `GET /users/new` goes to `GET /users/{id}` when `/users/new` has only a POST.
  Literal text wins over a parameter with a kind, that over a plain parameter, that over the rest of the path. Routes
  without a parameter are found in a hash table, the others in a tree over the path's segments. A path with routes
  but none for the method is 405 with `Allow`; OPTIONS is 204 with `Allow`; HEAD goes through GET.
- **Groups.** A prefix and its middleware; groups nest.
- **Middleware.** `before` runs in the order added, for the application and then for the route's group, and may
  answer itself; the application's `after` runs in reverse order just before the head is sent.
- **Filters.** Middleware that needs nothing of the application's state, in the same chains: `Cors` (any or listed
  origins, credentials, preflights answered), `FixedHeaders` and `SecurityHeaders` (nosniff, frame options, referrer
  policy, HSTS, a content security policy), `HttpsRedirect`, and `RateLimit` (requests per window per client address,
  in a table of fixed size, 429 with `Retry-After`).
- **What middleware leaves for the handler.** `scope.put("user", …)` in a filter, `ctx.value("user")` after it.
- **Cookies.** `Cookies.get(request, name)` and `SetCookie` (path, domain, max age, `Secure`, `HttpOnly`,
  `SameSite`).
- **Health.** `Health.answer`: 200, or 503 once the server drains.
- **Tokens** (`auth/`, the package `httpserver_auth`, on CeeSafeLibs' sha256, encoding and json): `JwtVerifier` for
  HS256 tokens (`exp`, `nbf`, issuer, audience, leeway; `alg: none` and every other algorithm refused), `Jwt.signHs256`,
  and `BearerAuth`, which answers 401 with `WWW-Authenticate` or leaves the token's `sub` and claims for the handler.
- **Sessions and CSRF** (`auth/`): `Sessions` keeps a session's bytes in a cookie the server signed (HMAC-SHA-256),
  so nothing is stored, nothing is shared between workers, and there is no session id to fix; `Csrf` is the signed
  double-submit token: a cookie on the first visit, and 403 for a request that changes something without the same
  token in `x-csrf-token`.
- **Compression.** `Compression.send` answers with gzip when the request accepts it, the body is not small and gzip
  makes it smaller, and marks such responses `vary: accept-encoding`. The encoder uses fixed Huffman codes: JSON-like
  text comes out at about a sixth of its size. `tools/gzip_check.py` checks it against Python's gzip.
- **Forms.** `Form.value` for a URL-encoded body, and `Multipart` for `multipart/form-data` (RFC 7578): the parts
  of a body already read, as views of it (name, file name, content type, bytes), with `failed()` for a body that is
  not multipart or was cut.
- **Static files** (`static/`, the package `httpserver_static`, on CeeSafe.Web's `web_static`): `Files` serves a
  root with media types, ETag and Last-Modified, conditional requests, one range, and `sendFile`; a path that leaves
  the root is 404. `FilesOptions.singlePage("index.html")` answers a single-page application's routes with its page.
- **A WebSocket hub** (`ws/`, the package `httpserver_ws`, on CeeSafe.Web's `web_ws`): the sessions of an
  application, and messages to all of them, to a group or to one. A message is queued for each session without
  waiting, so a client that does not read holds up nobody; what its full queue missed is counted. A membership leaves
  the hub when it is dropped. The hub is one process's: under the supervisor a broadcast reaches the sessions of the
  worker that makes it. `tools/ws_check.py` checks it end to end with clients written from RFC 6455.
- **Replies.** `Reply.text`, `html`, `json`, `bytes`, `empty`, `redirect` and `problem` (RFC 9457).
- **The query.** `Query.raw`, `Query.value` (decoded) and `Query.number`.
- **HTTP/1.1 and HTTP/2** on one port: `h2` by ALPN over TLS (`web_tls`'s acceptor), or by its preface in clear.
  `applicationOf(app)` gives the same application to `web_http3`'s server.

## One process per core, supervised

A trap in CeeSafe ends its process (CEESAFE_FINDINGS.md, S3), and an event loop is one thread. `supervise` runs the
server as worker processes, each with its own memory, loop and core, under a parent that does nothing but watch:

```ceesafe
struct Server { listener: Listener?; }

impl WorkerMain for Server {
    fn run(borrow this, worker: Worker) -> i32 { /* Async.run of the server; see bench/src/main.cee */ }
}

// supervise(Server { listener: Some(bound) }, SupervisorOptions.defaults(4))
```

- A worker that ends, however it ends, is started again, after a growing pause if it keeps ending at once.
- A worker whose loop stops turning (a computation that never awaits) stops beating and is killed after `stallMs`.
- `memoryBytes` limits a worker's address space (Linux), so one that grows fails its own allocations.
- SIGTERM or SIGINT to the parent is passed on; the workers drain (`tend`), and those still there after `graceMs`
  are killed.
- The workers either inherit one listening socket from the parent, so connections waiting to be accepted outlive a
  worker, or each bind the shared port (`reusePort`, Linux), which spreads connections evenly.

What it costs: the requests in flight on a worker that ends get no answer, and the workers share no memory, so a
counter or a cache is per worker unless it is put somewhere they share.

`python3 tools/supervisor_check.py [--inherit] [--memory-mb 512]` checks all of this against the benchmark server:
five traps, a handler that never returns and one that takes memory until it fails, each followed by the server
still answering.

## What is not here yet

Of CosmoApiServer: request timeouts, logging and tracing, Brotli, response and output caching, tokens signed with RSA or Ed25519 and authorisation policies, the scheduler. The rate limit counts in one process: under the supervisor, each worker has its own counters. Not planned: reflection controllers and
dependency injection (handlers are structs), Razor and Blazor, the front-end development helpers, SignalR's wire
protocol; gRPC and OpenAPI unless asked for.

## Measured

Ubuntu 24.04, 8 cores, loopback, HTTP/1.1, `h2load -m 1` with 4 threads on the same machine, 8 seconds after a
warm-up, on 2026-11-17. `cores` is the server's CPU time over the run's time. CosmoApiServer 4.0.22 on .NET 10
answers the same routes (server GC). One machine and one run each: read the ratios, not the last digit.

| Server | Route | Connections | Requests/s | Cores | Requests per CPU second | Peak RSS |
|---|---|---|---|---|---|---|
| httpserver | `/small` | 1 | 37 250 | 0.57 | 65 300 | 4 MB |
| httpserver | `/small` | 64 | 108 000 | 1.00 | 108 500 | 8 MB |
| httpserver | `/small` | 256 | 104 300 | 1.00 | 104 800 | 22 MB |
| httpserver | `/json` | 256 | 108 100 | 0.99 | 108 600 | 22 MB |
| httpserver | `/users/{id:int}` | 256 | 104 000 | 1.00 | 104 500 | 22 MB |
| httpserver, 5 middleware | `/small` | 256 | 98 700 | 0.99 | 99 200 | 21 MB |
| CeeSafe.Web's own server | `/small` | 256 | 107 000 | 1.00 | 107 500 | 23 MB |
| CosmoApiServer | `/small` | 1 | 27 600 | 2.90 | 9 500 | 65 MB |
| CosmoApiServer | `/small` | 64 | 161 600 | 4.32 | 37 400 | 70 MB |
| CosmoApiServer | `/small` | 256 | 177 800 | 4.10 | 43 300 | 77 MB |
| CosmoApiServer | `/json` | 256 | 171 600 | 4.29 | 40 000 | 80 MB |
| CosmoApiServer | `/users/{id:int}` | 256 | 172 400 | 4.27 | 40 400 | 82 MB |
| httpserver, 2 processes | `/small` | 256 | 183 800 | 1.99 | 92 500 | |
| httpserver, 4 processes | `/small` | 256 | 275 400 | 3.87 | 71 200 | |
| httpserver, 4 processes | `/json` | 256 | 261 600 | 3.92 | 66 800 | |

- This package costs nothing measurable over CeeSafe.Web's own router and server, and five middleware cost about
  5 % on the smallest response.
- One process is one core (CEESAFE_FINDINGS.md, S2). For each CPU second it answers about 2.5 times the requests
  CosmoApiServer does; CosmoApiServer answers more in all because it uses four cores.
- Several processes sharing the port (`CW_REUSEPORT=1`, Linux) pass it. With four, the load generator's four threads
  and the servers fill the machine, so that row is a floor.
- The stock filters a public API would run together (security headers, CORS for any origin, the rate limit) cost
  about 15 % on the smallest response: 81 000 `/small` a second against 97 000 on one core, at 64 connections with an
  `Origin` on each request. They add seven response fields and read the clock once.
- Under the supervisor, four workers answer about 275 000 `/small` a second in either listener mode. While a
  worker was made to trap twice a second, no request was answered wrongly; the load generator does not reconnect, so
  its rate fell as it lost the connections of each worker that ended.
- CosmoApiServer answered 56 and 16 of about 1.3 million `/users/42` requests with a status that was not 2xx; not
  looked into.

## Working on it

```sh
ceesafec check && ceesafec fmt --check
ceesafec test
ceesafec test --cflag -fsanitize=address,undefined
(cd auth && ceesafec test)
(cd static && ceesafec test)
(cd ws && ceesafec test)
(cd bench && ceesafec build)      # then: python3 <CeeSafe.Web>/benchmarks/servers/conform.py --port 8090
python3 tools/supervisor_check.py # the supervisor, against bench/out/bench
python3 tools/gzip_check.py       # compressed responses, against Python's gzip
python3 tools/ws_check.py         # the hub, with WebSocket clients written from RFC 6455
```

The compiler: ceesafec 0.62.2 or later. The supervisor is POSIX only.
