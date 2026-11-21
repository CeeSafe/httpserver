# CeeSafe findings

Bugs and gaps in CeeSafe and CeeSafe.Web met while writing httpserver, with ceesafec 0.62.2 (now built with 0.77.2) and CeeSafe.Web at
`4bb64c6` (now `79ae138`, with Web ADR 0016's isolated connections and streams). Each has what was seen, a reduction where there is one, and what httpserver does instead.

| | What | Where | Status |
|---|---|---|---|
| S1 | An integer literal inside a parenthesised `if` expression does not take the type the outer `if` expects | ceesafec | fixed in ceesafec 0.65.1 |
| S2 | A server runs on one thread: nothing serves one listener from several cores | CeeSafe.Web, runtime | measured; the supervisor's worker processes are the way round it |
| S3 | A trap ends the process: a task cannot fail alone | ceesafec, by design (panic = abort) | ceesafec 0.75.0 (ADR 0175) and CeeSafe.Web's Web ADR 0016: under `app.runIsolated`, which is `app.run` from httpserver 0.2.0 (docs/elixir-gaps.md §2), a trap costs its connection; the supervisor still replaces a worker that ends, and recycles one past `recycleCrashes`, starting another in its place while it drains (§4). With `app.isolateRequests(true)` (CeeSafe.Web's Web ADR 0017) a trap costs its HTTP/1.1 request, answered 500 when no byte of the response had left, and the connection goes on; HTTP/2 and HTTP/3 streams that trap before their head are answered 500 always |
| S4 | lib/std cannot start a process without waiting for it, signal one, or wait for any child | lib/std | fixed in 0.70.0 and 0.73.1 (ADRs 0169, 0173): the supervisor spawns its workers and hands them what they share (below) |
| S5 | No allocation budget for a task, and no allocator for `Vector` or `String` but the system's | runtime | fixed in 0.70.0 (`Budget`): `app.budget(bytes)`; a worker's `RLIMIT_AS` is the backstop |
| S6 | `ProcessSignals` does not survive `fork` | runtime | fixed in ceesafec 0.65.1: a forked child starts with no subscription |
| S7 | A struct without type parameters cannot implement a generic interface for every argument (`impl<S> Middleware<S> for Cors`) | ceesafec (N0267) | fixed in ceesafec 0.68.0 (ADR 0165, for one state type a program): `Filter` is gone, and `Cors` and the others are `impl<S: Send + Share> Middleware<S>` |
| S8 | A connection does not say whether it came over TLS | CeeSafe.Web | `HttpsRedirect` redirects everything; the application on the plain port uses it |
| S9 | `Application.beforeHead` gets the request and the head, not the handler's context | CeeSafe.Web | a group's middleware has no `after`; the access log and the timeout are in `App.handle`, around the whole chain |
| S10 | Nothing can change a response's body after the handler sends it | CeeSafe.Web | compression is a call the handler makes (`Compression.send`), not a middleware |
| S11 | `for (x in it)` over an iterator that is Copy walks a copy, and says nothing | ceesafec | ceesafec 0.67.0 warns (N0407, ADR 0167); `for (part in borrow mut parts)` walks the variable itself, as it always did |
| S12 | A struct that holds a raw pointer is not Send or Share, and `@threadSafe` is for trusted packages only | ceesafec, by design | the address is kept as a `usize` and cast where it is used |
| S13 | An array literal of borrows is refused with a message about the borrowed type not being Copy | ceesafec | fixed in ceesafec 0.65.1: it is N0246, an array of references |
| S14 | A method that is given the context by `borrow mut` cannot call a `dyn Handler<S>` with it (N0272) | ceesafec, by design: the handler and `Context<S>` share the flow name `S`, whatever its bounds (§25.6) | fixed in ceesafec 0.67.0 (ADR 0168) once the interfaces ask `S: Send + Share`, which they do now: `App.handle` owns the context and hands it to the dispatcher |
| S15 | A cancelled task's function runs on to its end, and cannot tell without asking (`AsyncControl<E>.checkpoint`) | runtime, by design | the dispatcher asks before it tells the access log; `Arc.create(v)` and its like are inferred since ceesafec 0.67.0 (ADR 0166) |
| S16 | A closure that stores into a captured local and also captures part of a `borrow mut` parameter is refused as if it stored into the parameter (N0272) | ceesafec, looks like a fault of the checker; reduced, not understood | no longer met: with `S: Send + Share` on the interfaces (S14) the closure is accepted in 0.67.0; without the bound it is refused as before, and why is still not known |
| S17 | A request that computes without waiting holds its worker's event loop until the supervisor kills the worker | runtime, by design (cooperative tasks) | ceesafec 0.80.0 (ADR 0186) and 0.82.0 (ADRs 0189, 0190): with `polls = true` in the program's manifest, `App.run` pauses a request every 10 ms slice, so its neighbours wait a few slices, and ends one past its bound (30 s with the slice, 1 s without) with `Overran`, alone; the watchdog stays for C code and blocking calls |

## S1: a nested `if` expression's literals are `i32`

```ceesafe
fn pick(a: bool, b: bool) -> u16 {
    let flat: u16 = if (a) 308 else 307;                        // accepted
    let nested: u16 = if (a) (if (b) 308 else 307) else 302;    // N0205: expected 'u16', found 'i32'
    return flat + nested;
}
```

The expected type reaches the branches of the outer `if`, but not through the parentheses into the inner one.
`src/reply.cee` (`Reply.redirect`) writes the literals with their suffix (`308u16`).

## S2: one thread

CeeSafe.Web's connection server runs every connection as a task of one event loop, so a server uses one core
(its `docs/milestones/M1.md` lists serving from several as not done). `ListenOptions.reusePort` exists, and on Linux
several processes that set it share a port, the kernel spreading connections over them. Measured on an 8-core
machine (README.md, "Measured"): one process answers about 105 000 small requests a second on one core, two about
190 000, four about 270 000.

That works for a stateless application. What it leaves open is state the processes would have to share: a rate
limiter's counters, sessions kept in memory, an output cache. Inside one process this needs either the runtime's
workers (`Async.runWorkers`) with a listener each, or one accepting thread that hands connections over.

## S3: a trap ends the process

`spec/type-system.md` §14.5: "`Process.exit` and panics do not run destructors or deferred statements (panic =
abort)". An index out of bounds in one request's handler ends every connection of the process. The panic hook
(§28, "Panics in a host") must end the process or never return, and may not unwind, so a library cannot turn a trap
into a failed task.

What a per-task trap would need from the language: the task's memory in a region the executor can free at once, and
the task's other resources (sockets, locks, reference counts it holds) known to the executor so it can release them.
httpserver does not attempt it: `src/supervisor.cee` puts each event loop in a process, so a trap costs that worker's
connections and nothing else, and the worker is started again.

## S4: processes in lib/std

`Process.run` and `Process.capture` start a program and wait for it. There is no handle to a running child, no
`kill`, no process id, and no way to continue as a copy of the running program. The supervisor declares `fork`,
`waitpid`, `kill`, `mmap`, `signal`, `setrlimit` and `usleep` itself, with their constants read from the system's
headers (`@header`, `const NAME: i32;`), which worked on macOS and Linux without a C file.

Two things met on the way:
- `sigemptyset`, `sigaddset` and `sigismember` are macros on macOS, so an `extern "C"` declaration of them does not
  compile there; the supervisor uses `signal` and a `@callback` that sets a static `AtomicU32`.
- An imported C struct cannot be made as a local (N0294), so `struct rlimit` is passed as two `u64` in an array,
  cast to its pointer.

## S5: no budget for a task's allocations

`Vector`, `String`, `Arena` and the others take `SystemHeap` or `FailingAllocator` only (§30.2), and nothing counts
what one task has allocated. A request that grows without bound takes the process's memory, and the first
allocation to fail may be another request's. With a limit on the worker's address space (`RLIMIT_AS`, Linux) the
failure at least stays in the worker, and since CeeSafe's allocations return errors the request that asked is
answered instead of the process ending (measured: a handler that takes memory until it fails got 495 MiB under a
512 MiB limit, answered, and the worker went on).

Wanted: a byte budget an executor can give a task, checked by the runtime's allocator, so the request that grows is
the one whose allocation fails.

## S6: `ProcessSignals` and `fork`

A subscription is a thread that reads a pipe (`runtime/hosted/ceesafe_rt_async.c`). A process forked from one that
has subscribed inherits the "subscribed" state without the thread, so its own `subscribe` would answer
`AlreadyExists` and no signal would reach it. This is read from the runtime's code, not run. The supervisor
therefore never subscribes: it installs a handler with `signal`, and each worker subscribes for itself (`tend`).

## S7: no blanket implementation of a generic interface

```ceesafe
public interface Middleware<S> { ... }
public struct Cors { ... }
impl<S> Middleware<S> for Cors { ... }   // N0267: type parameter 'S' does not appear in 'Cors'
```

CORS, security headers, the rate limit and authentication read nothing of the application's state, so one type
should serve every `App<S>`. ADR 0054's generic impls need the parameter in the implementing type. httpserver has a
second interface, `Filter`, whose `before` takes the state-free part of the context (`Scope`), and the application
keeps two lists and one chain of tagged indexes. The same limit stopped a stock `Health` handler (`impl<S>
Handler<S> for Health`), which is a function a handler calls instead.

## S8: is this connection TLS?

`ConnectionContext` has the addresses, the peer's certificate and the drain; `Transport` has `alpn()`. Neither says
whether the bytes came over TLS: HTTP/1.1 over TLS without ALPN and HTTP/1.1 in clear look the same to a handler
(`web_ws`'s `accept` takes `secure` as an argument for the same reason). A filter cannot redirect only plain
requests, mark a cookie `Secure` by itself, or add HSTS only where it is allowed.

## S9: `beforeHead` has no context

`Application.beforeHead(request, head)` runs when the handler sends, with no way back to what `handle` knew: the
matched route, its group, what middleware left in the context, or when the request started. So `after` hooks are
the application's only (a group's would need the route looked up again), and an access log cannot give a
request's duration from `before` and `after` alone. httpserver's access log (`App.logTo`) and request timeout
therefore sit in `App.handle`, around the middleware and the handler, not in a middleware. Wanted: the `Exchange` (or a slot in it the application owns)
passed to `beforeHead`.

## S10: no hook on the response body

`Exchange.send` and `write` go to the connection; `beforeHead` can add fields, not replace the body. Compression,
an output cache and a response logger are therefore not middleware in httpserver: a handler that wants its body
compressed answers through `Compression.send`. Wanted: a body filter an application can put between the handler's
writes and the connection.

## S11: a `for` loop over a Copy iterator walks a copy

```ceesafe
var parts = Multipart.of(body, boundary);   // every field Copy, so the struct is Copy
for (part in parts) { ... }                  // walks a copy of `parts`
parts.failed()                               // the original: never advanced, never failed
```

Seen in tests/form.cee: a body cut short was reported as well-formed, because `failed()` was asked of the value the
loop had not touched. Nothing warns. A struct with `fn next(borrow mut this)` whose state matters after the loop
has to be `@noncopy`, after which the loop is refused and the caller writes `parts.next()` in a `loop`. Wanted: a
warning for a `for` over a Copy value that is read again afterwards, or the loop borrowing a named variable.

## S12: a raw pointer makes a struct thread-bound

`SharedWords` holds the address of a mapping every process and thread may use, and a filter that holds it must be
`Send + Share` to go in an application. A `*mut AtomicU64` field makes the struct neither (N0334), and `@threadSafe`
is refused outside the libraries and packages marked `trusted = true` (N0290). The field is a `usize`, cast to the
pointer inside the one `unsafe` expression that uses it (`n as *mut T` is allowed, §27.2). That works, but the type
no longer says it is a pointer. What did work without any C: `Ptr.toBorrow` over the mapping gives a
`borrow AtomicU64`, whose `compareExchange` is the shared memory's only access.

## S13: an array of borrows

```ceesafe
for (app in [borrow two, borrow one]) { ... }   // N0259: array elements must be Copy; 'App<St>' is not Copy
```

A `borrow T` is Copy (§5), but the element type reported is the borrowed `App<St>`. Whether arrays of references
are meant to be refused or not, the message names the wrong type. tests/filters.cee writes the two calls out.

## S14: a closure over a context and the exchange

```ceesafe
async fn handle(borrow this, request: borrow Request, exchange: borrow mut Exchange, connection: borrow ConnectionContext) -> … {
    var ctx = Context<S>.create(this.appState, connection, this.routes, request.path(), params);
    r = await Async.timeout(ms, late, async () => await this.dispatch(found, mut ctx, request, exchange));
    //  N0272: this stores a value borrowed from 'this' into what 'exchange' refers to, which outlives this call
    await log.record(request, ctx.scope(), …);   // what the context was wanted for after the call
}
```

`ctx` holds borrows of `this`; the closure's environment holds `ctx` and `exchange`, and the checker then reports
that a borrow of `this` is stored into what `exchange` refers to, though nothing stores anything. With the closure
in the function, the same call written without a closure a few lines above is reported too. A second form of the
same refusal: a method `dispatch(borrow this, ctx: borrow mut Context<S>, …)` that calls
`this.routes.handler(index).handle(mut ctx, request, exchange)` is N0272 ("a value borrowed from a temporary
value into what 'ctx' refers to"), while the same call in the function that owns `ctx` is accepted. A region
parameter on the method did not help, and `borrow(r) this` is not accepted in a method's parameters.

Looked into for 0.65.1, the two forms are two things.

The second form (the method that is handed `ctx`) is the language's rule, not a fault (type-system.md §25.6, ADR
0131): in `interface Handler<S>` the receiver has the interface's type parameter as a flow name, and so has a
parameter of type `Context<S>`, so a call of `handle` may store a loan of the handler into the context. Reduced:
the same method with a context type that does not mention `S` is accepted, and bounding `S` by `Send + Share`
changes nothing, though a value of such a type holds no loan. ADR 0168 (proposed) is that a type parameter bounded
by `Send` or `Share` be no flow name.

The first form (the closure) is S16.

So `App.dispatch` makes the context, calls the handler and tells the access log itself; the closure captures only
`handle`'s parameters. The cost: a request that was cancelled is told to the log by `handle`, which has no context,
so its log entry lacks what the filters left (the trace id).

## S15: a cancelled task runs to its end

When `Async.timeout`'s time is up the child is cancelled and awaited: its awaits return errors, and its function
runs on with them. The dispatcher saw the handler return `Err(Internal)` (its `Timer.sleep` had failed) and told
the access log 500 for a request the client got 504 for. Nothing in the result says "cancelled";
`AsyncControl<HandlerError>.checkpoint(…)` does, and the dispatcher asks it before logging. (The call needs its
type argument written: `AsyncControl.checkpoint(e)` is N0265, though `E` is the argument's type.)

## S16: a closure that stores into a captured local

```ceesafe
struct Out<region r> { sink: borrow(r) Vector<u8>; n: u32; }
struct Ctx<region r, S> { state: borrow(r) S; seen: u32; }
interface App { fn serve(borrow this, out: borrow mut Out) -> u32; }
struct Server<S: Send + Share> { state: S; }

impl<S: Send + Share> Server<S> {
    fn dispatch(borrow this, ctx: borrow mut Ctx<S>) -> u32 { ctx.seen += 1; return ctx.seen; }
    fn plain(borrow this, seen: borrow mut u32) -> u32 { seen += 1; return seen; }
}

fn twice(f: borrow mut fn() -> u32) -> u32 => f() + f();

impl<S: Send + Share> App for Server<S> {
    fn serve(borrow this, out: borrow mut Out) -> u32 {
        var ctx = Ctx { state: borrow this.state, seen: 0 };
        return twice(() => {
            out.n += 1;
            return this.dispatch(mut ctx);      // N0272: this stores a value borrowed from 'out.n' into
        });                                     //        what 'out' refers to, which outlives this call
    }
}
```

The closure stores nothing into `out`: it adds one to a number in it. Three changes each make the program
accepted (ceesafec 0.65.1): calling `this.plain(mut ctx.seen)`, which has no flow into the context; leaving
`out.n += 1` out, so `out` is not captured; or writing the two statements in `serve` without a closure. So the
store that `dispatch` may make into `ctx` (its receiver shares `S` with it, S14) is charged to `out` when both are
captures of one closure. This is what refused `Async.timeout(ms, late, async () => await this.dispatch(…, mut ctx,
…, exchange))` in `App.handle`. One guess was tried in the checker and was wrong (a captured local written "through"
as a reference is, `collectWrites`): the cause is not known.

## S17: a request that computes holds its worker

A task runs until it awaits, so a handler that computes without waiting (`GET /spin`, a loop that never ends; in a
real server a quadratic parse of a hostile input) holds every connection of its worker's event loop. Isolation
(S3) does not help: nothing traps. The supervisor's watchdog was the remedy: it sees the loop stop and, after
`stallMs` (1.5 s in `tools/supervisor_check.py`), kills the worker and every connection on it. It is the largest
gap left between httpserver and Erlang, whose scheduler takes the CPU from a process after a count of reductions.

ceesafec ADR 0186 (written from this review, released in 0.80.0) bounds how long one resume of a task may run without waiting. A build
with `--cflag -DCEE_POLLS=1` polls at loop headers and at the entry of functions that may recurse, and a monitor
thread marks a thread whose task has run past `Async.runBound(ms, mode)`; the next poll traps an isolated task with
`PanicKind.Overran`, which ends it alone. The benchmark server sets it from `CW_RUN_BOUND_MS`, and
`supervisor_check.py --isolated` (250 ms) checks that `/spin` and `/spin/2000` are ended in under a second, that
`/spin/50` is answered, that every other request is answered meanwhile, and that no worker stalls or is killed. The
spin so moves from the supervisor to the unit. The watchdog stays for what polls cannot see: computing in C, or a
blocking system call.

The price is the polls' (ADR 0186 §10): measured on the benchmark server, `/small` and `/json` within noise and
`/gz/16384` 6% fewer requests a second, deflate's inner loops being where it goes. A build without the macro has
no polls and the same code as before.

ceesafec 0.82.0 goes further (ADR 0189, from the comparison with Elixir in docs/elixir-gaps.md): under
`Async.runSlice(ms)`, each resume of an isolated task runs on a stack of its own, and a poll past the slice pauses
the task there and puts it at the back of the queue, where the bound alone could only end it. `App.run` sets a 10 ms
slice and, with it, a bound of 30 s, for a request that never ends (1 s where no slice can pause a task: under
`runShared`, on a 32-bit target). `supervisor_check.py` checks, with one worker, that a request beside one computing
for 300 ms waits a median of under 0.2 s (0.07 s here), that the 300 ms one is answered, and that one without end is
ended at its bound. ADR 0190 lets the manifest ask for the polls, `polls = true`, in place of a flag on every build
command; httpserver's servers do.

ceesafec 0.83.0 (ADR 0191, from the benchmark against Bandit in docs/elixir-benchmark.md) fixes what the slice still
left: a paused task went back in the one ready queue, so every task woken behind it (a connection handing a request
to its isolated task, and back) waited a slice at each hand-off, and a worker whose queue never emptied (h2c under
load) never ran its timers, so its heartbeat stopped and the supervisor killed it. Paused tasks now wait in a queue
of their own and a busy executor collects every millisecond. On the benchmark machine, h2c at 128 connections went
from 19 439 requests a second with workers killed to 74 521 with none, and a healthy request's p99.9 beside four
computing ones from 69 ms (9.9 s with per-request isolation) to 9.8 ms (4.6 ms). The check above that failed on
macOS's CI under 0.82.0 (a median of 0.232 s beside `/spin`) is the first symptom.

ceesafec 0.84.1 (ADR 0193) makes the bound mean what it says under a slice. It counted a sliced resume's slices times
the slice, and the monitor sees every slice late, so with `App`'s default slice lowered from 10 to 2 ms a 250 ms bound
ended a spin on macOS's CI after 1.5 s, past the supervisor's stall. The time a resume ran is now measured at each
pause: the same check ends it after 0.26 s here, where 0.84.0 took 0.39 s.

ceesafec 0.89.0 (ADR 0203, from an independent benchmark against Bandit and Cowboy) ends a slice when the worker has
I/O to serve, not only when the slice has run: a request beside one that computes waited for the rest of the slice
and the monitor's lateness, a median of 1.8 ms on one Linux core with the 2 ms slice, against Bandit's 0.8. On the
Xeon, one worker on one core beside two `/spin/200` clients: p50 0.30 ms and p99 1.35 to 1.56 ms (1.81 and 3.41 before),
with `/small`'s rate unchanged.

ceesafec 0.91.0 (ADR 0204) declares an async function's C locals as a sync function's: zeroed only when written in
part or borrowed. Every one had been zeroed at every resume, each with a store of its own: 56 of them in the isolated
relay's resume function. With CeeSafe.Web's Web ADR 0022, isolated `/small` on one core went from 46 691 to 51 280
requests a second.

## What ceesafec 0.65.1 to 0.71.0 changed

Released 2026-11-18 from these findings and httpclient's. 0.65.1 fixed S1, S6 and S13. 0.67.0 carries three ADRs:
0166 (S15: a generic type's arguments inferred in a static call), 0167 (S11: warning N0407) and 0168 (S14: a type
parameter bounded by `Send` or `Share` is no flow name). With 0168 and the bound written on `Handler<S>` and
`Middleware<S>`, `App.handle` makes the context and passes it to `dispatch`, inside the time limit's closure too, so
the access log has what the filters left for every request: a request whose time was up used to be logged without
its trace id. 0.68.0 carries ADR 0165 (S7), for one argument list a type: an impl whose parameter the interface chooses. With it
the second interface, `Filter`, and the application's second list are gone: each stock middleware is one
`impl<S: Send + Share> Middleware<S>`. A program whose applications have two state types cannot use one such
middleware for both (N0267). S2, S3, S8, S9, S10 and S12 are as they were.

0.70.0 carries the last two ADRs.

- **0170 (S5), an allocation budget**, as a guard: `Budget.enter(bytes)`, not the `Async.budget(bytes, body)` the
  ADR had. `App.handle` enters one for each request where `app.budget` or `app.budgetIn` set a bound, before the
  time limit's child task is made, so that task is under it. Measured with the benchmark server's `/bloat`, which
  takes a MiB at a time until an allocation fails: under a budget of 16 MiB it gets 15 and is answered, on macOS
  and on Linux, and on Linux well short of the worker's 512 MiB (`tools/supervisor_check.py --budget-mb 16`). A
  budget for every request cost nothing that two runs of the same thing do not differ by (ADR 0170 §6).
- **0169 (S4), child processes**: `Process.spawn`, `ChildProcess`, `Process.id`, `Process.executable`. **The
  supervisor does not use it yet.** It still forks: a forked worker inherits the listening socket and the shared
  mappings (`SharedWords`, `Bus`), and a spawned one would have to be handed them by name, which lib/std has
  nothing for. Without them a spawned worker can bind with `reusePort` on Linux, not spread connections on macOS,
  and share no counters. The ADR says so (§4) and leaves named shared memory and descriptor passing to a later
  one; until then `fork` through `extern "C"` stays.

0.71.0 carries ADR 0172, named shared memory: `SharedMemory.create(name, bytes)`, `open`, `remove`, words of
`AtomicU64`. The three mappings here (`SharedWords`, `Bus`, the supervisor's page of heartbeats) are made with it
and no longer declare `mmap`: each is created under a name of this process's own and the name is taken away at
once, so the forked workers have the memory, nothing finds it by name, and a supervisor that is killed leaves
nothing in `/dev/shm`. Two things met on the way:

- **Nothing keeps a value without dropping it.** The mapping must outlive every copy of the address taken from it,
  for the life of the process, and `SharedMemory`'s drop unmaps. `sharedBytes` pushes the value into a vector,
  takes the vector apart (`Vector.intoRaw`) and rebuilds it empty (`Vector.fromRaw`, `unsafe`), which frees the
  buffer and forgets the element. Leaving the buffer too is what `ceesafec test` reports as "leaked". A
  `forget(value)` in lib/core, or a `SharedMemory` that can be told to stay, would say it in one line.
- **The supervisor still forks**, and `fork`, `waitpid`, `kill` and `setrlimit` are still declared. Workers that
  are spawned would open the memory by a name they are told, which is now possible, but on macOS they would also
  need the listening socket handed to them, which is not (ADR 0172 §4).

0.73.1 carries ADR 0173, what a child is handed: `Handover`, `Process.spawnWith`, `Process.inherited()` and
`SharedMemory.anonymous`. With it the supervisor no longer forks (`src/supervisor.cee`, checked with 0.75.0). A
worker is the program started again (`Process.executable()`, its arguments and `--httpserver-worker=<index>`); it
runs `main`, makes the same `Supervisor`, and takes back, place by place, what the parent made through it: the page
of beats, the shared words, the bus and the listening socket. The parent's loop is an `Async.run` that polls each
`ChildProcess` and a `ProcessSignals` subscription on a 20 ms timer, and ends a worker with `terminate` or `kill`.
`fork`, `waitpid`, `kill`, `signal`, `usleep` and the `@callback` are gone; `setrlimit` stays, for `memoryBytes`,
which the worker now sets on itself before it serves. `tools/supervisor_check.py --inherit --budget-mb 16`,
`ratelimit_check.py --inherit`, `bus_check.py --inherit` and `ws_check.py --workers 3 --inherit` pass on macOS
arm64 (2026-11-19). Three things met on the way:

- **`Process.executable()` may block** (N0295 in the parent's async loop: `ceesafe_host_executable` is
  `@effects(sleep)`), so the supervisor reads the path once before its loop. `spawnWith` and `poll` do not block.
- **web_transport's `Listener` cannot be handed over or made from a socket that was**: its `AsyncTcpListener` is
  private, and `Handover.listener` takes an `AsyncTcpListener`. httpserver has `SharedListener`, its own `Incoming`
  over an `AsyncTcpListener`, which does what `Listener.accept` does (TCP_NODELAY on). A `Listener.adopt(l)` and a
  `listener.socket()` in web_transport would let a program use the one type.
- **A worker is told which one it is by an argument**, since `ChildOptions` has no environment of its own for a
  child. The program then sees one argument more than it was given. An environment variable for one child, or a
  number in the handover, would leave the arguments alone.
- **gcc 13.3 warns about the 0.75.0 runtime** (`-Wclobbered`) when the benchmark server is built on Linux
  (Ubuntu 24.04): `ceesafe_rt_async.c:3126: variable 'outer' might be clobbered by 'longjmp'`, in
  `cee_exec_loop`'s jump point for isolated tasks (ADR 0175). Read, not tested: `outer` is set before the
  `setjmp` and not changed before a `longjmp` can come back to it, which C11 §7.13.2.1 says keeps its value, so the
  warning looks like gcc's known false positive for a variable that lives across loop iterations. Declaring it
  (and `outer_jump`, `outer_depth`) `volatile` would quiet it at no cost worth measuring. Built against CeeSafe.Web
  with `runIsolated`, gcc also names `t` (`:3105`), the task being resumed, which is likewise set before the
  `setjmp`.

0.75.0 carries ADR 0175, isolated tasks, and CeeSafe.Web's Web ADR 0016 (accepted 2026-11-19) builds
`ConnectionServer.runIsolated` on it. httpserver has `app.runIsolated`, and `tend` drains a worker past
`SupervisorOptions.recycleCrashes` or `recycleLeakedBytes`, read from `Isolation.report()`.
`tools/supervisor_check.py --isolated` (benchmark server with `CW_ISOLATED=1`, `CW_RECYCLE_CRASHES=2`) passes on macOS
(`--inherit --budget-mb 16`) and Linux (`--memory-mb 512 --budget-mb 16`). Five traps cost five requests and no
worker, the report counts no crash, two workers were replaced after their second crash, and the stall is still
caught. CeeSafe.Web's own list (`docs/ceesafe-upstream-work.md`) has what this met in CeeSafe:

- U25, no `trySpawnIsolated`, so HTTP/2 and HTTP/3 streams are not isolated one by one;
- B21, on macOS a closer cannot end a connection whose peer has already half-closed;
- B22, `ceesafec test` reports a test whose isolated task trapped as panicked;
- B23, the interpreter leaks a watched channel made inside its task set's body.

All four were fixed in 0.77.0 (CeeSafe ADR 0175 amendment 1), and CeeSafe.Web then isolated HTTP/2 and HTTP/3
streams one by one (Web ADR 0016 §2.3, merged as `79ae138`). Two more came with that, both fixed:

- B24, `ceesafec test` reported what a crashed isolated task captured as the test's leak (0.77.1, amendment 2);
- B25, LeakSanitizer reported what a crashed isolated task left, so a sanitized program whose task trapped exited
  with status 1 and every trap test failed CI's sanitizer jobs (0.77.2, amendment 3: the runtime marks the dead
  unit's roots with `__lsan_ignore_object`, and every other leak is still reported). It applies here too: a
  sanitized build of the benchmark server under `CW_ISOLATED=1` needs 0.77.2 for a trap not to fail its exit.

httpserver asks for 0.77.2 and pins CeeSafe.Web at `79ae138`.
