# CeeSafe findings

Bugs and gaps in CeeSafe and CeeSafe.Web met while writing httpserver, with ceesafec 0.62.2 (now built with 0.65.1) and CeeSafe.Web at
`4bb64c6`. Each has what was seen, a reduction where there is one, and what httpserver does instead.

| | What | Where | Status |
|---|---|---|---|
| S1 | An integer literal inside a parenthesised `if` expression does not take the type the outer `if` expects | ceesafec | fixed in ceesafec 0.65.1 |
| S2 | A server runs on one thread: nothing serves one listener from several cores | CeeSafe.Web, runtime | measured; the supervisor's worker processes are the way round it |
| S3 | A trap ends the process: a task cannot fail alone | ceesafec, by design (panic = abort) | the supervisor starts the worker again |
| S4 | lib/std cannot start a process without waiting for it, signal one, or wait for any child | lib/std | `fork`, `waitpid`, `kill` declared through `extern "C"` |
| S5 | No allocation budget for a task, and no allocator for `Vector` or `String` but the system's | runtime | a worker's `RLIMIT_AS`; open |
| S6 | `ProcessSignals` does not survive `fork` | runtime | fixed in ceesafec 0.65.1: a forked child starts with no subscription |
| S7 | A struct without type parameters cannot implement a generic interface for every argument (`impl<S> Middleware<S> for Cors`) | ceesafec (N0267) | a second interface without the parameter (`Filter`) |
| S8 | A connection does not say whether it came over TLS | CeeSafe.Web | `HttpsRedirect` redirects everything; the application on the plain port uses it |
| S9 | `Application.beforeHead` gets the request and the head, not the handler's context | CeeSafe.Web | a group's middleware has no `after`; the access log and the timeout are in `App.handle`, around the whole chain |
| S10 | Nothing can change a response's body after the handler sends it | CeeSafe.Web | compression is a call the handler makes (`Compression.send`), not a middleware |
| S11 | `for (x in it)` over an iterator that is Copy walks a copy, and says nothing | ceesafec | `Multipart` is `@noncopy` |
| S12 | A struct that holds a raw pointer is not Send or Share, and `@threadSafe` is for trusted packages only | ceesafec, by design | the address is kept as a `usize` and cast where it is used |
| S13 | An array literal of borrows is refused with a message about the borrowed type not being Copy | ceesafec | fixed in ceesafec 0.65.1: it is N0246, an array of references |
| S14 | A method that is given the context by `borrow mut` cannot call a `dyn Handler<S>` with it (N0272) | ceesafec, by design: the handler and `Context<S>` share the flow name `S`, whatever its bounds (§25.6) | the function that makes the context calls the handler; ADR 0168 proposes that a `Send` parameter be no flow name |
| S15 | A cancelled task's function runs on to its end, and cannot tell without asking (`AsyncControl<E>.checkpoint`) | runtime, by design | the dispatcher asks before it tells the access log |
| S16 | A closure that stores into a captured local and also captures part of a `borrow mut` parameter is refused as if it stored into the parameter (N0272) | ceesafec, looks like a fault of the checker; reduced, not understood | the closure captures only the parameters; a cancelled request's log entry lacks what the filters left |

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
