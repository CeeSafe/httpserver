# CeeSafe findings

Bugs and gaps in CeeSafe and CeeSafe.Web met while writing httpserver, with ceesafec 0.62.2 and CeeSafe.Web at
`4bb64c6`. Each has what was seen, a reduction where there is one, and what httpserver does instead.

| | What | Where | Status |
|---|---|---|---|
| S1 | An integer literal inside a parenthesised `if` expression does not take the type the outer `if` expects | ceesafec | worked around |
| S2 | A server runs on one thread: nothing serves one listener from several cores | CeeSafe.Web, runtime | measured; the supervisor's worker processes are the way round it |
| S3 | A trap ends the process: a task cannot fail alone | ceesafec, by design (panic = abort) | the supervisor starts the worker again |
| S4 | lib/std cannot start a process without waiting for it, signal one, or wait for any child | lib/std | `fork`, `waitpid`, `kill` declared through `extern "C"` |
| S5 | No allocation budget for a task, and no allocator for `Vector` or `String` but the system's | runtime | a worker's `RLIMIT_AS`; open |
| S6 | `ProcessSignals` does not survive `fork` | runtime | the supervisor installs its own handler and never subscribes |
| S7 | A struct without type parameters cannot implement a generic interface for every argument (`impl<S> Middleware<S> for Cors`) | ceesafec (N0267) | a second interface without the parameter (`Filter`) |
| S8 | A connection does not say whether it came over TLS | CeeSafe.Web | `HttpsRedirect` redirects everything; the application on the plain port uses it |
| S9 | `Application.beforeHead` gets the request and the head, not the handler's context | CeeSafe.Web | a group's middleware has no `after`; a response cannot be timed from one hook |

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
request's duration from `before` and `after` alone. Wanted: the `Exchange` (or a slot in it the application owns)
passed to `beforeHead`.
