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
  in a table of fixed size, 429 with `Retry-After`; `RateLimit.shared` counts in `SharedWords`, so the supervisor's
  workers enforce one limit together).
- **What middleware leaves for the handler.** `scope.put("user", …)` in a filter, `ctx.value("user")` after it.
- **Cookies.** `Cookies.get(request, name)` and `SetCookie` (path, domain, max age, `Secure`, `HttpOnly`,
  `SameSite`).
- **Health.** `Health.answer`: 200, or 503 once the server drains.
- **Tokens** (`auth/`, the package `httpserver_auth`, on CeeSafeLibs' sha256, encoding, json and pkey): `JwtVerifier`
  checks HS256, RS256, EdDSA and ES256 tokens (`exp`, `nbf`, issuer, audience, leeway) against secrets, PEM public
  keys, or a JSON Web Key Set as an identity provider publishes it, by `kid`. A key is used for its own algorithm
  only, and `alg: none` is refused. `BearerAuth` answers 401 with `WWW-Authenticate` or leaves the token's `sub` and
  claims for the handler; `RequireClaim` after it asks for a role or a scope, or answers 403. The package links
  OpenSSL's libcrypto through pkey.
- **Sessions and CSRF** (`auth/`): `Sessions` keeps a session's bytes in a cookie the server signed (HMAC-SHA-256),
  so nothing is stored, nothing is shared between workers, and there is no session id to fix; `Csrf` is the signed
  double-submit token: a cookie on the first visit, and 403 for a request that changes something without the same
  token in `x-csrf-token`.
- **Compression** (`compress/`, the package `httpserver_compress`, on libzstd and libbrotli): `Codings.send`
  answers with Zstandard (`zstd`) where the request accepts it, else Brotli (`br`), when the body is not small and
  the coding makes it smaller, and marks such responses `vary: accept-encoding`. A client that takes neither gets
  the body as it is, or gzip with `CodingOptions.gzip`. The package needs the two libraries (`brew install zstd
  brotli`; `apt install libzstd-dev libbrotli-dev`).
- **gzip without a library.** `Compression.send` in the core package does the same with gzip alone, from an encoder
  written here (fixed Huffman codes), for a program that must not depend on C libraries. `tools/gzip_check.py`
  checks it against Python's gzip.
- **Replies.** `Reply.text`, `html`, `json`, `bytes`, `empty`, `redirect` and `problem` (RFC 9457).
- **The query.** `Query.raw`, `Query.value` (decoded) and `Query.number`.
- **Uploads as they arrive.** `Upload` reads a multipart/form-data body through the exchange, part by part and
  piece by piece through a buffer of a fixed size, so a handler writes a file to disk or hashes it without holding
  it; `Multipart` remains for bodies that fit in memory.
- **Request timeouts.** `app.timeout(ms)` bounds every request and `app.timeoutIn(group, ms)` a group's (longer for
  uploads, shorter for an API): a request whose middleware and handler have not returned is cancelled and answered
  504, or its connection is closed if part of the response had left. The time runs while a handler waits, not
  while it computes: one that never awaits is the supervisor's to stop.
- **Access log.** `app.logTo(log)` tells an `AccessLog` of every request once it is answered (the request, its
  scope with the connection and what the filters left, such as the trace id, the status, the microseconds), a 404
  or a timeout included; a request that timed out comes without what the filters left. `ConsoleLog` writes a line a request to
  standard output; an application's own can count, sample, or write somewhere else.
- **Trace context.** The filter `Trace` reads the caller's W3C `traceparent` or starts a trace, gives the request
  a span id of its own, and leaves `"trace"`, `"span"`, `"parent"` and `"traceparent"` (the header to send on) for
  the handler; with `respond`, the response says them in `traceresponse`. Spans are not exported to a collector.
- **Output cache.** A handler that answers through `OutputCache.send(request, exchange, status, type, body,
  seconds)` has its response kept in memory; the filter `Cached` before it answers the next GET and HEAD requests
  for the same path and query without running it, with `age`, and with 304 for a client that sends the `etag` back
  (`If-None-Match`). The table has a fixed number of places and a largest body, so its memory is bounded;
  `forget(target)` and `clear()` drop what changed. The key is the target and the request fields named with
  `varyBy` (`accept-language`), which the responses list in `Vary`: do not keep what depends on who asks. Each
  worker keeps its own.
- **Scheduled jobs.** `Scheduler` runs a `Job` by a cron expression (`0 9 * * mon-fri`, `*/15 * * * *`, `@daily`;
  on UTC, the system's time zone with its daylight saving, or a fixed offset) or every so many seconds. An
  expression is parsed once into bit sets and asked for its next time, so an idle scheduler does one comparison a
  job each second. Jobs run one at a time on the server's event loop; one that is late runs once, not once for each
  time it missed. `jobs.run` is raced against the server as `tend` is, and stops running jobs when the drain
  starts. Under the supervisor, start it in one worker (`worker.index() == 0`).
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
counter or a cache is per worker unless it is put somewhere they share. `SharedWords` is such a place for numbers:
words mapped before the workers are forked and changed only atomically, with no locks and no pointers in them, so a
worker that traps cannot leave the others anything half written. `RateLimit.shared` counts there.

For events there is `Bus`: each worker publishes into a ring of its own in shared memory and the others read it on
a timer (`carry`, raced against the server as `tend` is), so a message reaches them within that interval (5 ms in
the benchmark server). Nothing blocks: a reader that falls a whole ring behind skips to the newest and counts it
(`missed`). An application gives each topic a number and a `BusSink` that acts on it, such as
`OutputCache.forget` for a target another worker changed, or `Hub.deliverEncoded` for a WebSocket message that
another worker's session sent (`Hub.encode` makes it). The bus is one machine's; across machines a broker is
needed.

`python3 tools/supervisor_check.py [--inherit] [--memory-mb 512]` checks all of this against the benchmark server:
five traps, a handler that never returns and one that takes memory until it fails, each followed by the server
still answering.

## What is not here yet

Of CosmoApiServer: spans exported to a collector. Not planned: reflection controllers and
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
- Compressing 64 KiB of JSON-like text for each request, one core answers 2 800 a second with Zstandard (level 3),
  1 100 with Brotli (quality 4) and 710 with the core's gzip, against 3 700 uncompressed; at 4 KiB, 25 300, 12 000 and
  10 500 against 36 800. All three send about a sixth of the bytes (0.16, 0.17, 0.17). The text is made of a few
  repeated words, which flatters every coding.
- Under the supervisor, four workers answer about 275 000 `/small` a second in either listener mode. While a
  worker was made to trap twice a second, no request was answered wrongly; the load generator does not reconnect, so
  its rate fell as it lost the connections of each worker that ended.
- A request timeout costs about 5% on `/small` (94 000 to 102 000 a second with one set, against 102 000 to
  105 000 without; the runs vary by that much), an access log that only counts nothing measurable, and `ConsoleLog`
  about 20% (81 000) with standard output sent to `/dev/null`.
- The output cache answers 4 KiB of JSON-like text 83 000 to 85 000 times a second against 33 000 when the handler
  makes it each time, and 64 KiB 45 000 times against 3 600 to 4 000.
- The trace filter costs about 10% on `/small` (90 000 to 93 000 a second, against 98 000 to 105 000): four values
  left for the handler and one more response field.
- With four workers and every request from one address (so every request changes the same shared word), a limit
  counted in `SharedWords` answers 240 000 to 259 000 `/small` a second, one each worker counts for itself 261 000,
  and no limit 254 000 to 275 000. `tools/ratelimit_check.py`: of 992 requests against a limit of 200, exactly 200
  are answered with the shared words, 727 to 800 without.
- `tools/bus_check.py`, four workers: a number said to one is heard by all four, and a cached response forgotten
  through one is made again by all four (by one, without the bus); after three workers trap and are started
  again, eight numbers said through whichever worker answers are each heard by all four. A bus nobody publishes on costs nothing
  measurable (272 000 to 276 000 `/small` a second with it, 263 000 to 278 000 without).
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
(cd compress && ceesafec test)   # needs libzstd and libbrotli
(cd bench && ceesafec build)      # then: python3 <CeeSafe.Web>/benchmarks/servers/conform.py --port 8090
python3 tools/supervisor_check.py # the supervisor, against bench/out/bench
python3 tools/gzip_check.py       # compressed responses, against Python's gzip
python3 tools/ratelimit_check.py  # one rate limit for four workers (add --inherit on macOS)
python3 tools/bus_check.py        # the bus between four workers (add --inherit on macOS)
python3 tools/ws_check.py         # the hub, with WebSocket clients written from RFC 6455
python3 tools/ws_check.py --workers 4   # the same over four workers and the bus (add --inherit on macOS)
```

The compiler: ceesafec 0.62.2 or later. The supervisor is POSIX only.

## License

httpserver, httpserver_auth, httpserver_static, httpserver_ws and httpserver_compress are licensed under the GNU
General Public License, version 2 only ([COPYING](COPYING)), as CeeSafe and CeeSafe.Web are, with an exception that
permits linking them with OpenSSL ([COPYING.OPENSSL](COPYING.OPENSSL)), which httpserver_auth does through `pkey`
and a TLS server does through `web_tls`. httpserver_compress links libzstd (BSD or GPL v2) and libbrotli (MIT).
