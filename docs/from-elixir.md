# httpserver for Elixir developers

You know Plug, Phoenix and OTP. This page maps what you use there to what httpserver has, says where the model is
different, and lists the mistakes Elixir habits make in CeeSafe. How the two compare when things go wrong, measured:
the README's [Compared with Elixir](../README.md#compared-with-elixir) and [elixir-benchmark.md](elixir-benchmark.md).

## The model, in one paragraph

A Phoenix app is one BEAM VM: every connection is a process with its own heap, the VM's schedulers run them on every
core and take the CPU from one that computes too long, and supervisors restart what crashes. An httpserver app is a
native program: a **supervisor process** starts one **worker process per core**, each worker runs an event loop on one
thread, and each connection (with `app.isolateRequests(true)`, each request) is an **isolated task**, CeeSafe's
equivalent of a process for failure: a trap in it ends that task alone and the worker goes on. There is no garbage
collector; memory is freed when its owner goes out of scope, which the compiler checks. What a crashed task held is
leaked and counted, and a worker that has crashed too often is replaced, without dropping connections, the way you
would restart a node.

## The map

| Elixir, Plug, Phoenix | httpserver | Notes |
|---|---|---|
| `mix new`, `mix.exs`, `mix deps.get` | `ceesafe.toml`, `ceesafec fetch` | dependencies are git revisions, pinned in `ceesafe.lock` |
| `mix compile`, `iex -S mix` | `ceesafec check`, `ceesafec build`, `ceesafec run --interp` | `check` is the fast loop: types and borrows, no C |
| `mix release` | `ceesafec build` | one native binary, `out/<name>`; nothing to install beside it |
| `mix format` | `ceesafec fmt` | one canonical style, `fmt --check` in CI |
| `mix test`, ExUnit | `ceesafec test` | `--cflag -fsanitize=address,undefined` for the sanitizers |
| `Plug.Router`, `get "/items/:id"` | `app.get("/items/{id:int}", handler)` | kinds in the pattern (`int`, `long`, `alpha`, `bool`, `guid`); `{*rest}` for the rest |
| Phoenix `scope "/api"` | `let api = app.group("/api")?` and `app.routeIn(api, …)` | groups nest and have their own middleware, timeout and budget |
| a controller action, a `Plug` module's `call/2` | a struct that implements `Handler<S>` (`handle`) | no reflection: what a handler needs is in its fields or in the state |
| `%Plug.Conn{}` | `request` (what came) and `exchange` (what goes back) | the request is read-only; the response is written through the exchange |
| `conn.params["id"]`, path params | `ctx.param("id")`, `ctx.paramInt("id")` | optionals: `?? { … }` for the missing case |
| `fetch_query_params`, `conn.query_params` | `Query.raw`, `Query.value` (decoded into a buffer you give), `Query.number` | read from the target on demand, nothing parsed ahead |
| `get_req_header(conn, "x-api-key")` | `request.headers().getNamed("x-api-key")` | `get(HeaderName.Authorization)` for the common ones |
| `send_resp`, `json/2`, `text/2`, `redirect/2` | `Reply.text`, `Reply.json`, `Reply.html`, `Reply.redirect`, `Reply.problem` | `exchange.send(status, bytes)` underneath |
| `put_resp_header` | `exchange.header(name, value)` | before the head is sent; a middleware's `after` adds fields last |
| a plug pipeline, `pipe_through` | `app.use(middleware)`, `app.useIn(group, middleware)` | `before` in order, `after` in reverse |
| `halt(conn)` | `return Ok(Flow.Responded)` from `before` | after answering through the exchange |
| `assign(conn, :user, u)`, `conn.assigns.user` | `ctx.put("user", bytes)`, `ctx.value("user")` | bytes, not terms: encode what you keep |
| application config, an `Agent` holding state | the application's state `S`, `ctx.state()` | shared by every request of a worker; atomics or locks for what changes |
| ETS | `SharedWords` (numbers across workers), `OutputCache` (responses, per worker) | there is no shared term store; a database or a cache server across machines |
| `Phoenix.PubSub` on one node | `Bus` between the workers of one machine | across machines: a broker |
| Phoenix Channels, `WebSock` | `httpserver_ws`'s `Hub`: sessions, groups, `broadcast`, `toGroup` | the hub reaches other workers over the bus |
| `Plug.Static` | `httpserver_static`'s `Files` | ranges, validators, `sendfile`, a single-page app's fallback |
| `Plug.Session`, `Plug.CSRFProtection` | `httpserver_auth`'s `Sessions` (signed cookies) and `Csrf` | nothing stored on the server |
| Guardian, Joken | `httpserver_auth`'s `JwtVerifier`, `BearerAuth`, `RequireClaim` | HS256, RS256, EdDSA, ES256, JWKS |
| `Plug.Logger`, Telemetry | `app.logTo(log)` (`ConsoleLog`, or your own `AccessLog`), `Trace`, `httpserver_otel` | spans as OTLP/JSON |
| `Plug.Parsers` multipart | `Upload` (streamed), `Multipart` (in memory) | |
| Quantum, `:timer.send_interval` | `Scheduler` with cron expressions, as a `Service` | run it in one worker: `worker.index() == 0` |
| a `Supervisor` with workers | `Services`: `add(name, service, Restart.Permanent)`, `Strategy.OneForAll`, an `Intensity` | the worker's own tree; past the intensity the worker ends and the process supervisor restarts it |
| the application's top supervisor, `:heart` | `Supervisor` (the process supervisor) | restarts workers, kills one whose loop stops beating |
| `Task.async`, `Task.Supervisor` | `Async.tasks(n, …)` and `set.spawn`, `set.spawnIsolated` | `spawnIsolated` is a task that crashes alone |
| `GenServer` with a mailbox | a task that reads a `Channel<T>` | `sendAsync`, `recvAsync`; values move, they are not copied |
| "let it crash" | isolated tasks, and errors as values | see below |
| a process's `max_heap_size` | `app.budget(bytes)`, `app.budgetIn(group, bytes)` | an allocation past it fails in that request alone |
| a request timeout of your own | `app.timeout(ms)` | 504, or the connection closed if part of the answer had left |
| preemption by reductions | `app.slice(ms)` (10 ms) and `app.bound(ms)` (30 s), in a build with `polls = true` | paused every slice at the compiler's polls; ended past the bound |
| hot code upgrade | drain and restart | SIGTERM to the supervisor drains every worker |

## The same API in both

A JSON API behind an API key, as a Phoenix router with a pipeline would have it:

```elixir
pipeline :api do
  plug :require_api_key
end

scope "/api" do
  pipe_through :api
  post "/orders", OrderController, :create
end

defp require_api_key(conn, _opts) do
  case get_req_header(conn, "x-api-key") do
    [key] -> assign(conn, :caller, key)
    _ -> conn |> send_resp(401, "") |> halt()
  end
end
```

In httpserver ([examples/shop/src/main.cee](../examples/shop/src/main.cee), which CI builds):

```ceesafe
/// `plug :require_api_key`: answers 401 itself, or leaves who called for the handler, as `assign/3` does.
struct ApiKey {}

impl<S: Send + Share> Middleware<S> for ApiKey {
    async fn before(
        borrow this,
        ctx: borrow mut Context<S>,
        request: borrow Request,
        exchange: borrow mut Exchange,
    ) -> Result<Flow, HandlerError> {
        let key = request.headers().getNamed("x-api-key") ?? {
            let unauthorized = StatusCode.of(401) ?? { return Err(HandlerError.Internal); };
            await Reply.problem(exchange, unauthorized, "No API key", "")?;
            return Ok(Flow.Responded); // halt/1
        };
        let _ = ctx.put("caller", key); // assign(conn, :caller, key)
        return Ok(Flow.Continue);
    }

    async fn after(borrow this, request: borrow Request, head: borrow mut ResponseHead) -> Result<void, HandlerError> =>
        Ok();
}

/// `def create(conn, _params)`: a handler is a struct with a `handle` method.
struct CreateOrder {}

impl Handler<Shop> for CreateOrder {
    async fn handle(
        borrow this,
        ctx: borrow mut Context<Shop>,
        request: borrow Request,
        exchange: borrow mut Exchange,
    ) -> Result<void, HandlerError> {
        let n = ctx.state().orders.fetchAdd(1) + 1;
        var text = [0u8; 64];
        var w = TextWriter.create(mut text[..]);
        w.text("{\"order\":");
        w.decimal(n);
        w.text("}");
        return await Reply.json(exchange, Status.created(), w.written());
    }
}

/// `scope "/api" do pipe_through :api; post "/orders", OrderController, :create end`
fn application() -> Result<App<Shop>, RouteError> {
    var app = App<Shop>.create(Shop { orders: AtomicU64.create(0) }, Limits.defaults())?;
    let api = app.group("/api")?;
    let check: Box<dyn Middleware<Shop> + Send + Share> = match (Box.create(ApiKey {})) {
        Ok(b) => b,
        Err(_) => { return Err(RouteError.OutOfMemory); }
    };
    app.useIn(api, check)?;
    app.routeIn(api, Method.Post, "/orders", boxed(CreateOrder {})?)?;
    app.timeout(5000);         // a request still running after 5 s is answered 504
    app.isolateRequests(true); // a request that crashes is answered 500, and its connection goes on
    return Ok(app);
}
```

```sh
$ cd examples/shop && ceesafec build && out/shop &
$ curl -s -X POST http://127.0.0.1:8080/api/orders
{"type":"about:blank","title":"No API key","status":401}
$ curl -s -X POST -H 'x-api-key: k1' http://127.0.0.1:8080/api/orders
{"order":1}
```

What is different from the Elixir:

- **Everything that can fail says so.** Adding a route, allocating a box, sending a response: each returns a
  `Result`, and `?` passes the error up, as `with` does. There are no exceptions to rescue; an `Err` from a handler
  is answered with its status (`HandlerError.Status(code)`).
- **The state is a type.** `App<Shop>` carries one `Shop` for the whole worker; what changes in it is an atomic or
  behind a lock, since requests run concurrently on the worker's loop. Where Phoenix code would reach for an Agent or
  ETS, put a field here.
- **Strings are bytes.** `ctx.put` takes bytes and `ctx.value` gives them back; `TextWriter` writes text into a
  buffer you own. There is no string interpolation.
- **Handlers are values.** A route takes a boxed handler (`boxed(CreateOrder {})`); a handler's configuration is its
  fields, given when the route is added.

## "Let it crash", here

What Elixir gives a request for free, and how httpserver gets it:

| In Elixir | In httpserver |
|---|---|
| A crash ends the request's process; Plug answers 500 | A trap (an index out of bounds, an unwrapped `Err`) ends the connection's isolated task; with `app.isolateRequests(true)` only the request's, answered 500 with the connection kept |
| The process's heap goes with it | What the task held is leaked and counted; after 100 crashes or 64 MiB leaked the worker is replaced, while the others keep serving |
| The scheduler preempts a process that computes | The compiler puts polls in loops and calls (`polls = true` in the manifest); past a 10 ms slice the task is paused and the worker's other requests run; past 30 s it is ended with `Overran` |
| A supervisor restarts a crashed process | `Services` restarts a worker's long-lived tasks; the `Supervisor` restarts a worker process, and kills one whose loop stops beating |

Some failures still end a worker rather than a task: a trap inside a C library's callback or a `Critical`, and a
computation in C code or a blocking system call (no polls there). The supervisor restarts the worker; the requests in
flight on it get no answer. Elixir has the same edge with NIFs.

Prefer errors to traps for what you expect: a bad request is an `Err`, answered with its status; a trap is for bugs.
That is Elixir's advice too (`{:error, reason}` for the expected, a crash for the impossible).

## What you will miss

- **A REPL on the running system.** There is no `iex --remsh`. A native debugger (lldb, gdb) attaches to a worker;
  the access log, `Isolation.report()` and the supervisor's report say what happened.
- **Hot code upgrade.** Deploy by draining: SIGTERM to the supervisor lets every request in flight finish.
- **Distribution.** No nodes, no `:global`, no PubSub across machines: the `Bus` is one machine's. Put a broker or a
  database between machines.
- **Shared terms.** No ETS. Workers share only `SharedWords` (atomic numbers); a response cache is per worker.
- **Ecto, LiveView, Phoenix's generators.** httpserver is the HTTP layer; a database is a client library you call.

## What you get

- **Speed and memory:** 1.1 to 3.7 times Bandit's requests a second on the same cores, in a tenth of the memory
  (15 to 45 MiB for four workers against 160 to 190 MiB): [elixir-benchmark.md](elixir-benchmark.md).
- **Limits per request** that Bandit leaves to you: a memory budget whose allocation fails in that request alone, a
  timeout, a bound on computing.
- **HTTP/3** (`web_http3`), and one binary to ship.
- **Errors in the types:** a failure is a `Result` you handle or pass on with `?`, and ownership and every borrow
  are checked at compile time, so much of what you would let crash in Elixir is a compile error here.

## Mistakes Elixir habits make in CeeSafe

- **Pattern matching on maps and tuples.** There are no tuples and no maps of terms; a struct per shape, `match` on
  enums and optionals (`Some(x)`, `null`), and `??` for a fallback.
- **Pipes.** There is no `|>`; methods chain (`request.headers().getNamed("x")`), and `?` is the `with` of errors.
- **Immutability by default.** `let` cannot be assigned and `var` can. Values move: after
  `set.spawn(async move () => …)` the task owns what it captured.
- **Copying freely.** Passing a struct moves it, or lends it (`borrow`, `borrow mut`); `clone()` is explicit, and an
  `Arc` is how several tasks share one value.
- **`receive`.** A task waits on what it awaits: a channel's `recvAsync`, a timer, a socket. `Async.race` and
  `Async.timeout` are the `after` clause.
- **Atoms as tags.** Use an enum; a message's reason that is fixed text is an enum's variant, not a string.
- **Long work in a handler.** It is paused every slice, but it still takes its worker's time: move it to a task set
  started from the handler, or to a `Service`, as you would to a `Task.Supervisor`.

The language itself, for anyone new to it: the [guide for agents](https://ceesafe.github.io/CeeSafe/guide/for-agents)
and the [CeeSafe guide](https://ceesafe.github.io/CeeSafe/). [AGENTS.md](../AGENTS.md) lists the commands and the
common mistakes.
