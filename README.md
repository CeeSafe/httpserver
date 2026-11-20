# httpserver

[![ci](https://github.com/CeeSafe/httpserver/actions/workflows/ci.yml/badge.svg)](https://github.com/CeeSafe/httpserver/actions/workflows/ci.yml)

An HTTP application server written in [CeeSafe](https://ceesafe.github.io/CeeSafe/), a memory-safe systems language
that compiles ahead of time to C: routing, route groups, middleware, replies, request timeouts and memory budgets,
an output cache, scheduled jobs, WebSockets, static files, tokens and sessions, compression, tracing, and a
supervisor that runs one worker process a core.

It stands on [CeeSafe.Web](https://github.com/CeeSafe/CeeSafe.Web)'s connection server and its HTTP/1.1 and HTTP/2
drivers: the protocol work is CeeSafe.Web's, and this package is what an application is written against. It is a
port of CosmoApiServer (.NET) by what it does, not line by line. There is no garbage collector and no runtime to
install: a server is one native program, which answers about 100 000 small requests a second on one core in 8 to
22 MB of memory ([Measured](#measured)).

**Status:** version 0.1.0. The API is not settled, and the language under it is young: what was met while writing
this is in [CEESAFE_FINDINGS.md](CEESAFE_FINDINGS.md). Linux and macOS; the supervisor is POSIX only.

## Contents

- [Quick start](#quick-start)
- [Using it in a program](#using-it-in-a-program)
- [The packages](#the-packages)
- [What is here](#what-is-here)
- [One process per core, supervised](#one-process-per-core-supervised)
- [What is not here yet](#what-is-not-here-yet)
- [Measured](#measured)
- [The benchmark server](#the-benchmark-server)
- [Working on it](#working-on-it)
- [License](#license)

## Quick start

You need:

- `ceesafec` 0.77.2 or later ([releases](https://github.com/CeeSafe/CeeSafe/releases), or the
  [guide](https://ceesafe.github.io/CeeSafe/)'s installation page) and a C compiler (`cc`);
- for `auth/` only, OpenSSL's libcrypto; for `compress/` only, libzstd and libbrotli
  (`brew install zstd brotli`; `apt install libzstd-dev libbrotli-dev`). The core package needs no C library.

```sh
git clone https://github.com/CeeSafe/httpserver.git
cd httpserver/examples/hello
ceesafec fetch          # CeeSafe.Web's packages, at the revision in ceesafe.lock
ceesafec build          # compiles through C to out/hello
out/hello               # hello: listening on http://127.0.0.1:8080
```

```sh
$ curl http://127.0.0.1:8080/
hello
$ curl http://127.0.0.1:8080/api/items/42
{"id":42,"visits":1}
$ curl -i http://127.0.0.1:8080/api/items/x      # {id:int} does not match
HTTP/1.1 404 Not Found
```

That program, whole ([examples/hello/src/main.cee](examples/hello/src/main.cee)):

```ceesafe
// The smallest server: two routes, a group with a parameter, state shared by every request.
//
//   ceesafec build && out/hello        then:  curl -i http://127.0.0.1:8080/api/items/42
import web_core;
import web_transport;
import web_http;
import httpserver;

/// What every request can reach: `ctx.state()`.
struct Shop {
    visits: AtomicU64;
}

struct Home {}

impl Handler<Shop> for Home {
    async fn handle(
        borrow this,
        ctx: borrow mut Context<Shop>,
        request: borrow Request,
        exchange: borrow mut Exchange,
    ) -> Result<void, HandlerError> => await Reply.text(exchange, Status.ok(), "hello\n");
}

struct Item {}

impl Handler<Shop> for Item {
    async fn handle(
        borrow this,
        ctx: borrow mut Context<Shop>,
        request: borrow Request,
        exchange: borrow mut Exchange,
    ) -> Result<void, HandlerError> {
        let seen = ctx.state().visits.fetchAdd(1) + 1;
        // The route says `{id:int}`, so a request that reaches this handler has one.
        let id = ctx.paramInt("id") ?? { return await Reply.problem(exchange, Status.badRequest(), "No id", ""); };
        var text = [0u8; 96];
        var w = TextWriter.create(mut text[..]);
        w.text("{\"id\":");
        w.decimal(truncate<u64>(id));
        w.text(",\"visits\":");
        w.decimal(seen);
        w.text("}\n");
        return await Reply.json(exchange, Status.ok(), w.written());
    }
}

/// A handler as the application keeps it.
fn boxed<H: Handler<Shop> + Send + Share>(handler: H) -> Result<Box<dyn Handler<Shop> + Send + Share>, RouteError> {
    match (Box.create(handler)) {
        Ok(b) => {
            let d: Box<dyn Handler<Shop> + Send + Share> = b;
            return Ok(d);
        }
        Err(_) => { return Err(RouteError.OutOfMemory); }
    }
}

fn application() -> Result<App<Shop>, RouteError> {
    var app = App<Shop>.create(Shop { visits: AtomicU64.create(0) }, Limits.defaults())?;
    app.get("/", boxed(Home {})?)?;
    let api = app.group("/api")?;
    app.routeIn(api, Method.Get, "/items/{id:int}", boxed(Item {})?)?;
    app.timeout(5000);       // a request that takes longer is answered 504
    app.budget(8 * 1048576); // and one that would hold more than 8 MiB has its allocation fail
    return Ok(app);
}

async fn serve(app: App<Shop>, listener: borrow Listener) -> Result<ServerReport, NetError> {
    let drain = Drain.create()?;
    let limits = ConnectionLimits.defaults();
    return await app.run(listener, PlainAcceptor {}, limits, drain);
}

fn main() {
    let app = match (application()) {
        Ok(a) => a,
        Err(_) => {
            Console.writeLine("hello: could not build the routes");
            Process.exit(1);
        },
    };
    let options = ListenOptions { backlog: 0, reuseAddress: true, reusePort: false, v6Only: false };
    let listener = match (Listener.bindWith(SocketAddresses.v4Loopback(8080), options)) {
        Ok(l) => l,
        Err(e) => {
            Console.writeLine(NetErrors.describe(e));
            Process.exit(1);
        },
    };
    Console.writeLine("hello: listening on http://127.0.0.1:8080");
    Console.flush();
    match (Async.run(serve(app, listener))) {
        Ok(_) => {}
        Err(e) => {
            Console.writeLine(NetErrors.describe(e));
            Process.exit(1);
        }
    }
}
```

What to notice:

- **A handler is a struct** that implements `Handler<S>`, where `S` is the application's state. There is no
  reflection and no container: what a handler needs is in its fields or in the state.
- **`ctx`** has the state (`ctx.state()`), the route's parameters (`ctx.param`, `ctx.paramInt`), the connection, and
  what middleware left for the handler (`ctx.value("user")`).
- **Every failure is a value.** A route that cannot be added, an allocation that fails, a client that went away:
  each is a `Result`, and `?` passes it on. A handler that returns `Err(HandlerError.Status(code))` is answered
  with that status.
- **`app.run` serves until the drain starts** (`drain.start()`, which `tend` does on SIGTERM under the supervisor):
  it stops accepting, lets the requests in flight finish, and returns a report.

[bench/src/main.cee](bench/src/main.cee) is a larger program: every feature below behind a route, the supervisor,
the bus and the shared rate limit.

## Using it in a program

In the program's `ceesafe.toml`, name this repository and CeeSafe.Web's packages at the revision this one is built
against (the one in [ceesafe.toml](ceesafe.toml)):

```toml
[package]
name = "myserver"
version = "0.1.0"
kind = "executable"
sources = ["src"]
ceesafec = ">=0.77.2"

[dependencies]
httpserver = { git = "https://github.com/CeeSafe/httpserver.git", rev = "<a commit of this repository>" }
web_core = { git = "https://github.com/CeeSafe/CeeSafe.Web.git", rev = "44299461d0e7ba3d1d7ffb982fc7fa288af96433", dir = "packages/web_core" }
web_transport = { git = "https://github.com/CeeSafe/CeeSafe.Web.git", rev = "44299461d0e7ba3d1d7ffb982fc7fa288af96433", dir = "packages/web_transport" }
web_http = { git = "https://github.com/CeeSafe/CeeSafe.Web.git", rev = "44299461d0e7ba3d1d7ffb982fc7fa288af96433", dir = "packages/web_http" }
```

The other packages are directories of this repository: add `dir`, as in
`httpserver_ws = { git = "https://github.com/CeeSafe/httpserver.git", rev = "…", dir = "ws" }`. Run `ceesafec fetch`
once to resolve them and write `ceesafe.lock`, then `ceesafec build`.

For TLS, give `app.run` `web_tls`'s acceptor in place of `PlainAcceptor {}` (HTTP/2 is then chosen by ALPN); for
HTTP/3, give `applicationOf(app)` to `web_http3`'s server. Both are CeeSafe.Web's packages.

## The packages

| Directory | Package | What it is | C libraries |
|---|---|---|---|
| `.` | `httpserver` | The application, routes, groups, middleware, replies, cookies, forms and uploads, timeouts, budgets, the access log, the output cache, trace context, scheduled jobs, gzip, the supervisor, shared words and the bus | none |
| `auth/` | `httpserver_auth` | JWT verification (HS256, RS256, EdDSA, ES256, JWKS), bearer authentication, claims, signed-cookie sessions, CSRF | OpenSSL's libcrypto |
| `static/` | `httpserver_static` | A handler that serves the files under a root, with validators, conditional requests and ranges, and a single-page application's fallback page | none |
| `ws/` | `httpserver_ws` | A WebSocket hub: sessions, groups, broadcast, delivery across workers over the bus | none |
| `compress/` | `httpserver_compress` | Zstandard and Brotli responses by what the request accepts | libzstd, libbrotli |
| `otel/` | `httpserver_otel` | Traced requests as spans, exported to a collector as OTLP/JSON | none |
| `bench/` | `bench` | The benchmark server: a whole program | as `compress/` |
| `examples/hello/` | `hello` | The quick start's program | none |

## What is here

- **Routes.** `/`, literal segments, `{name}`, `{name:int}` (also `long`, `alpha`, `bool`, `guid`) and, last,
  `{*rest}`. Matching is by method: `GET /users/new` goes to `GET /users/{id}` when `/users/new` has only a POST.
  Literal text wins over a parameter with a kind, that over a plain parameter, that over the rest of the path. Routes
  without a parameter are found in a hash table, the others in a tree over the path's segments. A path with routes
  but none for the method is 405 with `Allow`; OPTIONS is 204 with `Allow`; HEAD goes through GET.
- **Groups.** A prefix and its middleware; groups nest.
- **Middleware.** `before` runs in the order added, for the application and then for the route's group, and may
  answer itself; the application's `after` runs in reverse order just before the head is sent.
- **Stock middleware.** Each is written once for every application state (`impl<S: Send + Share> Middleware<S> for
  Cors`) and added with `use` or `useIn` like any other: `Cors` (any or listed
  origins, credentials, preflights answered), `FixedHeaders` and `SecurityHeaders` (nosniff, frame options, referrer
  policy, HSTS, a content security policy), `HttpsRedirect`, and `RateLimit` (requests per window per client address,
  in a table of fixed size, 429 with `Retry-After`; `RateLimit.shared` counts in `SharedWords`, so the supervisor's
  workers enforce one limit together).
- **What middleware leaves for the handler.** `ctx.put("user", …)` in a middleware, `ctx.value("user")` after it.
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
- **Request memory budgets.** `app.budget(bytes)` bounds what every request may hold at once and
  `app.budgetIn(group, bytes)` a group's: an allocation that would take the request past it fails as any failed
  allocation does, and the handler answers for it. The request that grows is the one that is refused, not its
  neighbours. Counted: the request's own blocks, and those of the tasks it starts; not what C libraries take.
- **Request timeouts.** `app.timeout(ms)` bounds every request and `app.timeoutIn(group, ms)` a group's (longer for
  uploads, shorter for an API): a request whose middleware and handler have not returned is cancelled and answered
  504, or its connection is closed if part of the response had left. The time runs while a handler waits, not
  while it computes: one that never awaits is stopped by `Async.runBound` under `app.runIsolated` (below), else by
  the supervisor.
- **Access log.** `app.logTo(log)` tells an `AccessLog` of every request once it is answered (the request, its
  scope with the connection and what the middleware left, such as the trace id, the status, the microseconds), a 404
  or a timeout included. `ConsoleLog` writes a line a request to
  standard output; an application's own can count, sample, or write somewhere else.
- **Trace context.** The middleware `Trace` reads the caller's W3C `traceparent` or starts a trace, gives the request
  a span id of its own, and leaves `"trace"`, `"span"`, `"parent"` and `"traceparent"` (the header to send on) for
  the handler; with `respond`, the response says them in `traceresponse`.
- **Spans for a collector** (`otel/`, the package `httpserver_otel`, on CeeSafe.Web's `web_client`): `SpanLog` is an
  access log that keeps each traced request as a span in a bounded queue, and `export`, raced against the server,
  posts them in batches as OTLP/JSON to a collector's `/v1/traces` over plain HTTP. A batch the collector does not
  take is dropped and counted; requests are never held up.
- **Output cache.** A handler that answers through `OutputCache.send(request, exchange, status, type, body,
  seconds)` has its response kept in memory; the middleware `Cached` before it answers the next GET and HEAD requests
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
- **Static files** (`static/`): `Files<S>.create(root, FilesOptions.defaults())` is a handler for GET and HEAD
  that serves the regular files under `root`. The path is decoded once and checked lexically (a refused path, a
  FIFO or a device is 404), the validators are read from the open file, conditional requests and one range are
  honoured, and the body goes out with `sendfile`. `FilesOptions.singlePage(page)` answers every path that is not a
  file with a single-page application's page.
- **WebSockets** (`ws/`): a `Hub` keeps the sessions of a process. A session joins (`hub.join(sender)`), enters
  groups (`membership.enter("room")`), and the hub sends to all (`broadcast`), to a group (`toGroup`) or to one
  session (`to`). A session that does not read is not waited for: what it missed is counted. Under the supervisor,
  `Hub.encode` and `Hub.deliverEncoded` carry a message to the sessions of the other workers over the bus.
- **HTTP/1.1 and HTTP/2** on one port: `h2` by ALPN over TLS (`web_tls`'s acceptor), or by its preface in clear.
  `applicationOf(app)` gives the same application to `web_http3`'s server.

## One process per core, supervised

A trap in CeeSafe ends its process (CEESAFE_FINDINGS.md, S3), and an event loop is one thread. A `Supervisor` runs
the server as worker processes, each with its own memory, loop and core, under a parent that does nothing but watch.
A worker is the same program started again (`Process.spawnWith`), so `main` runs in every process, makes the same
`Supervisor`, and asks it for what the workers share in the same order; the parent makes each thing and hands it to
the workers, and a worker takes it back:

```ceesafe
struct Server { listener: SharedListener; words: SharedWords; }

impl WorkerMain for Server {
    fn run(borrow this, worker: Worker) -> i32 { /* Async.run of the server; see bench/src/main.cee */ }
}

fn supervised() -> Result<SupervisorReport, SupervisorError> {
    var supervisor = Supervisor.create(SupervisorOptions.defaults(4))?;
    let words = supervisor.words(4096)?;                           // made in the parent, taken in a worker
    let listener = supervisor.listener(address, listenOptions)?;   // bound in the parent, taken in a worker
    return supervisor.run(Server { listener: listener, words: words });   // in a worker, never returns
}
```

A worker is told which one it is by its last argument, `--httpserver-worker=<index>`; `supervisor.worker()` is null
in the parent.

- A worker that ends, however it ends, is started again, after a growing pause if it keeps ending at once.
- A worker whose loop stops turning (a computation that never awaits, where no bound ends it first: below) stops
  beating and is killed after `stallMs`.
- `memoryBytes` limits a worker's address space (Linux), so one that grows fails its own allocations. It is the
  backstop for `app.budget`, which bounds each request on every system.
- SIGTERM or SIGINT to the parent is passed on; the workers drain (`tend`), and those still there after `graceMs`
  are killed.
- The workers either accept from one listening socket the parent bound (`supervisor.listener`), so connections
  waiting to be accepted outlive a worker, or each bind the shared port (`reusePort`, Linux), which spreads
  connections evenly.

**A trap can cost one connection instead of a worker.** `app.runIsolated(incoming, acceptor, limits, drain)` serves each
connection in an isolated task (ceesafec ADR 0175, CeeSafe.Web's `ConnectionServer.runIsolated`, Web ADR 0016). A
handler that traps ends its connection, whose socket the server shuts, and the worker goes on with the rest. What the
connection held, memory and its socket, is leaked, and `Isolation.report()` counts it. So `tend` drains a worker whose
isolated tasks have trapped `recycleCrashes` times (100 by default) or leaked `recycleLeakedBytes` (64 MiB), and the
supervisor starts it again. HTTP/2 and HTTP/3 streams are isolated one by one (Web ADR 0016 §2.3): a trap costs its
stream, not the connection's others. A computation that never awaits costs only its connection or stream in a build with
polls (`--cflag -DCEE_POLLS=1`) once `Async.runBound` is set (ceesafec ADR 0186; the benchmark server's
`CW_RUN_BOUND_MS`): past the bound it traps with `Overran` and ends alone, its neighbours having waited at most that
long. Still ending the worker: a trap under a C library's callback or inside a `Critical`, and, without the bound, or in
C code or a blocking system call, a computation that does not return to the loop (the stall below).

What it costs: the requests in flight on a worker that ends get no answer, and the workers share no memory, so a
counter or a cache is per worker unless it is put somewhere they share. `SharedWords` is such a place for numbers:
words the parent maps and hands to every worker, changed only atomically, with no locks and no pointers in them, so
a worker that traps cannot leave the others anything half written. `RateLimit.shared` counts there.

For events there is `Bus`: each worker publishes into a ring of its own in shared memory and the others read it on
a timer (`carry`, raced against the server as `tend` is), so a message reaches them within that interval (5 ms in
the benchmark server). Nothing blocks: a reader that falls a whole ring behind skips to the newest and counts it
(`missed`). An application gives each topic a number and a `BusSink` that acts on it, such as
`OutputCache.forget` for a target another worker changed, or `Hub.deliverEncoded` for a WebSocket message that
another worker's session sent (`Hub.encode` makes it). The bus is one machine's; across machines a broker is
needed.

`python3 tools/supervisor_check.py [--inherit] [--memory-mb 512] [--budget-mb 16] [--isolated]` checks all of this
against the benchmark server: five traps, a handler that never returns and one that takes memory until it fails,
each followed by the server still answering. With `--isolated` the traps cost their requests and no worker, and a
worker whose tasks trapped twice is replaced; two more traps hold the output cache's lock and the gzip encoder's,
which the next uses repair, the cache's from the bus. Those two need the trap hooks, which only a build with
`--cflag -DHTTPSERVER_TRAP_HOOKS=1` has. `--isolated` also bounds how long a request may compute without waiting
(`CW_RUN_BOUND_MS=250`, `Async.runBound`, ceesafec ADR 0186): the handler that never returns is then ended alone with
`Overran`, its worker answering the other requests meanwhile, where without the bound the supervisor kills the stalled
worker. The bound needs polls compiled in, `--cflag -DCEE_POLLS=1`: `--isolated` runs `bench/out/bench-traps`, built
with both macros. On macOS add `--inherit`: without it the kernel gives every connection to one worker.

The shared memory is ceesafec's `SharedMemory` with no name (`SharedMemory.anonymous`), which a process reaches
only by being handed it, so a supervisor that is killed leaves nothing behind. The supervisor starts, watches and
stops its workers with lib/std's `Process.spawnWith`, `ChildProcess` and `ProcessSignals`; the one thing it still
declares from C is `setrlimit`, for `memoryBytes`.

## What is not here yet

Of CosmoApiServer, nothing that was planned. Spans go to a collector over plain HTTP only (TLS through an agent),
and a span is named by its method, not its route's pattern. Not planned: reflection controllers and
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
- The stock middleware a public API would run together (security headers, CORS for any origin, the rate limit) cost
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
- The trace middleware costs about 10% on `/small` (90 000 to 93 000 a second, against 98 000 to 105 000): four values
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

## The benchmark server

`bench/` answers the routes of CeeSafe.Web's comparison contract, so its `conform.py` checks this server and the
same load compares it with the others, and it has a route for every feature above. It reads its settings from the
environment:

| Variable | What it does |
|---|---|
| `CW_PORT` | The port on 127.0.0.1 (8090) |
| `CW_PROCS=n` | n worker processes under the supervisor; unset, one process and no supervisor |
| `CW_INHERIT=1` | The workers accept from one socket the supervisor bound, instead of each binding the port |
| `CW_REUSEPORT=1` | One process that binds the port with `reusePort`, to run several by hand (Linux) |
| `CW_WORKERS` | The most connections served at once (256) |
| `CW_STALL_MS` | How long a worker may go without beating before it is killed (2000) |
| `CW_MEMORY_MB` | A limit on each worker's address space (Linux) |
| `CW_BUDGET_MB` | A memory budget for each request |
| `CW_TIMEOUT_MS` | A time limit for each request |
| `CW_LIMIT=n` | A rate limit of n requests an hour for each client address; with `CW_SHARED=1` the workers count together |
| `CW_BUS=1` | A bus between the workers: `/bus/say/{n}`, `/bus/heard`, `/forget/{n}`, and `/chat` across workers |
| `CW_TRACE=1` | The trace-context middleware |
| `CW_LOG=1` or `2` | An access log that counts, or one that writes a line a request |
| `CW_HOOKS=n` | n middleware that do nothing, to measure what one costs |
| `CW_STOCK=1` | The stock middleware a public API would run: security headers, CORS, a rate limit |

Its routes are listed at the top of [bench/src/main.cee](bench/src/main.cee): `/small`, `/json`, `/echo`,
`/users/{id:int}`, `/stream`, `/kept/{n}` (the output cache), `/gz/{n}` and `/z/{n}` (compression), `/chat` (a
WebSocket), and `/crash`, `/spin` and `/bloat`, which a server should survive.

```sh
(cd bench && ceesafec build)
CW_PROCS=4 CW_BUDGET_MB=16 bench/out/bench &
h2load --h1 -c 64 -t 4 -D 8 http://127.0.0.1:8090/small
```

## Working on it

The layout: each package has `ceesafe.toml`, `src/` and `tests/`; `tools/` has the checks that need several
processes or a client written from the RFC, in Python with nothing to install. [AGENTS.md](AGENTS.md) is for coding
agents: the commands, and the mistakes that code written from another language's habits makes in CeeSafe.


```sh
ceesafec check && ceesafec fmt --check
ceesafec test --cflag -DHTTPSERVER_TRAP_HOOKS=1   # the macro gives tests/poisoned.cee its traps
ceesafec test --cflag -DHTTPSERVER_TRAP_HOOKS=1 --cflag -fsanitize=address,undefined
(cd auth && ceesafec test)
(cd static && ceesafec test)
(cd ws && ceesafec test)
(cd compress && ceesafec test)   # needs libzstd and libbrotli
(cd otel && ceesafec test)
(cd examples/hello && ceesafec build)
(cd bench && ceesafec build)      # then: python3 <CeeSafe.Web>/benchmarks/servers/conform.py --port 8090
python3 tools/supervisor_check.py # the supervisor, against bench/out/bench
(cd bench && ceesafec build --cflag -DHTTPSERVER_TRAP_HOOKS=1 --cflag -DCEE_POLLS=1 -o out/bench-traps)
python3 tools/supervisor_check.py --isolated   # isolated connections, against bench/out/bench-traps
python3 tools/gzip_check.py       # compressed responses, against Python's gzip
python3 tools/ratelimit_check.py  # one rate limit for four workers (add --inherit on macOS)
python3 tools/bus_check.py        # the bus between four workers (add --inherit on macOS)
python3 tools/otel_check.py       # exported spans, with a collector written here (build otel/demo first)
python3 tools/ws_check.py         # the hub, with WebSocket clients written from RFC 6455
python3 tools/ws_check.py --workers 4   # the same over four workers and the bus (add --inherit on macOS)
```

The compiler: ceesafec 0.77.2 or later. Isolated tasks (ADR 0175), which `app.runIsolated` serves connections in,
came with 0.75.0; `Process.spawnWith` and `SharedMemory.anonymous` (ADR 0173, 0.73.1) start the workers and hand them
what they share; `Budget`, for a request's memory, came with 0.70.0; and the stock middleware are impls for every
application state, which 0.68.0 accepts. CeeSafe.Web at the pinned revision, whose HTTP/2 and HTTP/3 streams are
isolated one by one, needs 0.77.2: `trySpawnIsolated` (0.77.0), and the leak counts of a crashed task in tests and
under LeakSanitizer (0.77.1, 0.77.2).
The supervisor is POSIX only: it declares `setrlimit` from `sys/resource.h` for `memoryBytes`.

## License

httpserver, httpserver_auth, httpserver_static, httpserver_ws, httpserver_compress and httpserver_otel are licensed under the GNU
General Public License, version 2 only ([COPYING](COPYING)), as CeeSafe and CeeSafe.Web are, with an exception that
permits linking them with OpenSSL ([COPYING.OPENSSL](COPYING.OPENSSL)), which httpserver_auth does through `pkey`
and a TLS server does through `web_tls`. httpserver_compress links libzstd (BSD or GPL v2) and libbrotli (MIT).
