# Where Elixir's isolation is still ahead, and the plan to close it

Status: **accepted** by the owner, 2026-11-20 ("I accept all the above ADRs as written", together with CeeSafe ADRs
0189 and 0190 and CeeSafe.Web ADR 0017; §6's questions as recommended). Proposed the same day ("Draft ADRs for all
gaps"). Implementation notes: §7. It starts from httpserver at ceesafec 0.80.0:
isolated connections and streams (ceesafec ADR 0175, Web ADR 0016), request budgets (ADR 0170), and a bound on how
long a task runs without waiting (ADR 0186), on branch `adr0186-run-bound`.

## 1. The comparison

Elixir's web servers (Phoenix on Cowboy or Bandit) run each connection, and each HTTP/2 stream, as a BEAM process
with its own heap, preempted by reduction counting and supervised in trees. httpserver runs one OS process a core
under its supervisor, and in each, every connection and stream as an isolated task. For the common failures, a
trap, a runaway loop, a request that takes too much memory, both now cost one request. Five differences remain:

| # | Gap | Elixir | httpserver at 0.80.0 | Plan | Document |
|---|---|---|---|---|---|
| 1 | A long computation | Preempted and resumed: neighbours barely notice | Ended with `Overran` past the bound; neighbours wait up to it | A unit past its slice is paused at the poll, on a C stack of its own, and resumed later; a total-CPU bound ends a runaway | CeeSafe ADR 0189 |
| 5 | What the client sees of a crash | A 500, from the connection process | The connection closed with no answer, and its other requests lost | Each request isolated apart from its connection: the connection answers 500 and goes on | CeeSafe.Web ADR 0017 |
| 3 | Isolation by default | Always | Opt-in three times: `runIsolated`, a build with polls, the bound | The manifest asks for polls; httpserver's defaults become isolation and a bound | CeeSafe ADR 0190, §2 here |
| 4 | Supervision of long-lived work | Trees, restart strategies, links, monitors | One level: OS workers restarted; a trap in a job, the bus carrier or `tend` ends the worker | A supervisor for tasks over ADR 0175's `spawnIsolated` and `watch` | §3 here |
| 2 | Memory a crash leaves | Freed with the process's heap | Leaked and counted; the worker recycled past a bound | Not freeing (unsound here); recycling that refuses no connection, and leaks of one request, not one connection | §4 here |

The order of work is in §5, the questions in §6.

## 2. Gap 3: isolation and a bound by default

### What is opt-in today

- `App.run` (`src/app.cee:386`) serves connections in plain tasks: a trap ends the worker. `App.runIsolated`
  (`src/app.cee:404`) serves each in an isolated task, and takes its acceptor as `Box<dyn Acceptor + Send + Share>`
  where `run` borrows it.
- The bound is the program's: the benchmark server calls `Async.runBound(bound, RunBound.Count)` when
  `CW_RUN_BOUND_MS` is set (`bench/src/main.cee:878`), and it does anything only in a build with
  `--cflag -DCEE_POLLS=1`.
- The supervisor's recycle bounds are on by default (`SupervisorOptions.defaults`, `src/supervisor.cee:63`:
  `recycleCrashes` 100, `recycleLeakedBytes` 64 MiB, `stallMs` 5 000).

### The change

1. **`App.run` becomes the isolated server**, and the present one is renamed `App.runShared`, for a program that
   wants its connections in plain tasks (a trusted internal service measured to need the last few percent). `run`
   then takes the acceptor as `runIsolated` does; `runIsolated` stays one release as a name for `run`, then goes.
   httpserver's version goes to 0.2.0: it is a change of a public signature.
2. **`App` sets a bound** in `run`: `app.bound(ms)` (default **1 000 ms** with today's ADR 0186, an ended task;
   0 for none), applied with `Async.runBound(ms, RunBound.Count)` when `run` starts. Why 1 000: it must sit well
   below the supervisor's stall (5 000) so the bound, not the watchdog, catches a loop, and well above any
   legitimate single resume of a handler (the benchmark server's largest, a gzip of 60 000 bytes, is far below
   it; to be measured, question 2). Once ADR 0189 is in, the default becomes its slice (pause, about 10 ms) plus a
   total-CPU bound on a request (end, about 30 s, under the request timeout's spirit), and `bound` names the
   latter.
3. **A build without polls is told.** `run` checks `Async.runBound`'s answer; false (a build without polls) makes
   the server write one line at start, `httpserver: no polls in this build: a request that computes without
   waiting holds its worker (add polls = true to ceesafe.toml)`, once a worker. `app.bound(0)` silences it.
4. **Polls are the application's to ask for.** By ADR 0190 the root manifest decides, and a library's
   `polls = true` does nothing for its users (a library cannot impose the cost on their loops). So httpserver's
   own manifests (`bench/ceesafe.toml`, `examples/`) say `polls = true`, the README's first example manifest
   does, and the line of step 3 tells everyone else.
5. **The benchmark server** keeps `CW_ISOLATED` as `CW_ISOLATED=0` for the shared server (default 1), and
   `CW_RUN_BOUND_MS` overrides `app.bound` (0 for none). `tools/supervisor_check.py` runs isolated by default,
   with `--shared` for the old mode.
6. **README**: the isolation paragraph becomes the default's description, and the shared server its exception.

### What a user who wants the old behaviour does

`app.runShared(...)` in place of `app.run(...)`, and `app.bound(0)`. Nothing else changes for them.

### Depends on

ADR 0190 (`polls = true`). Without it, step 4 is the command-line flag in every build, which is today's state.
Step 1 can go first: isolation by default needs no polls.

### Tests

- `supervisor_check.py` (default now isolated): the present isolated run, plus a run of a build without polls that
  expects step 3's line and the watchdog's kill for `/spin` (the bound absent, the stall still caught).
- `supervisor_check.py --shared`: the present non-isolated run.
- A test (`tests/`) that `App.run` serves through an isolated task: a handler that traps answers no request, and the
  next request on another connection is answered (`MockIncoming`), with `Isolation.report().crashes == 1`.

## 3. Gap 4: a supervisor for tasks

### What runs outside any isolated task today

Each worker races its server against its housekeeping (`bench/src/main.cee:752` and `:795`): `tend`
(`src/supervisor.cee:631`), the bus carrier `carry` (`src/bus.cee:226`) and, where a program has one, the
scheduler's `run` (`src/schedule.cee:473`; "race `run` against the server, as `tend` is", `:362`). None is isolated:
a trap in a scheduled job, in a `BusSink` (`OutputCache.forget`, `Hub.deliverEncoded`), or in `tend` ends the
worker and every connection on it. A job that computes for long holds the loop (`src/schedule.cee:8`). Nothing
restarts one of them alone. The Hub's lock already heals after a trap of a session that held it
(`ws/src/hub.cee:53`), so a restarted part finds it usable.

### The design

ADR 0175 §3 left this to "a library over `watch`". Every piece it needs exists: `Async.tasks`, `spawnIsolated`
(captures `Send`, no loans), `set.watch(crashes)` (a `TaskCrash` per crash, never waited for), a set's cancellation
(ADR 0053), and `Drain.share()` for a task that may hold no loan (CeeSafe.Web's `web_core/src/drain.cee:27`).

```ceesafe
/// A long-lived part of a worker: started, and started again after a crash, by a `Services` supervisor.
public interface Service: Send + Share {
    /// Runs until `drain` starts (then returns Ok) or fails. Each start is a new isolated task: what the last one
    /// held is gone (leaked, ADR 0175 §2.4), so it builds its state again here.
    async fn run(borrow this, drain: Drain) -> Result<void, NetError>;
}

public enum Restart { Permanent, Transient, Temporary }    // always; only after a crash or an Err; never
public enum Strategy { OneForOne, OneForAll }

public struct Intensity { public restarts: u32; public withinMs: u64; }   // more than this: give up

var services = Services.create(Strategy.OneForOne, Intensity { restarts: 5, withinMs: 10000 })?;
services.add("scheduler", Box.create(Jobs { … })?, Restart.Permanent)?;
services.add("bus", Box.create(Carrier { bus: bus.share(), sink: … })?, Restart.Permanent)?;
let events = services.watch()?;       // Channel<ServiceEvent>: Started, Crashed(name, kind), GaveUp(name)

// Raced beside the server, as tend is today:
await Async.race(server, async move () => await services.run<ServerReport>(drain.share()));
```

- **Each start is `set.spawnIsolated`** of `async move () => await spec.run(drain)`, `spec` an
  `Arc<Box<dyn Service + Send + Share>>` clone: the spec is the factory, the task its one life. State that must
  survive a crash lives outside the task (`SharedWords`, the cache's `Arc`), as in Erlang it lives in ETS or
  another process.
- **One-for-one**: the crashed child is started again after a pause that grows as the supervisor's own does
  (`src/supervisor.cee`, 0, 100 ms, … 3.2 s, 5 s), so a child that cannot start does not spin the loop.
- **One-for-all**: the children run in a nested task set; a crash cancels the set (ADR 0053), waits for the others
  to return (each sees the cancellation at its next wait; a computing one at the bound), and starts all again. For
  parts that must restart together (a carrier and the sink whose state it feeds).
- **Rest-for-one** is left out: no part of httpserver needs an order of dependence among its services yet.
- **Escalation.** Past `Intensity`, `run` returns `Err(NetError.Crashed)`: the race ends the server, the worker
  ends with a status that is not 0, and the OS supervisor starts it again and counts a crash, which is the next
  level of the tree. That is Erlang's escalation to the parent supervisor, with the worker process as the parent.
- **Monitors** are `watch()`'s channel; **links** are one-for-all. A task that wants to know when a service it
  depends on restarts reads the channel.
- **`tend` stays outside it**: it is what beats and drains, and a crash in it should end the worker (the
  supervisor's stall would catch it anyway).
- **Its own crashes are the program's crashes**: `Isolation.report()` counts them with the connections', so the
  recycle bounds see them.

### Where it lives

**In httpserver first** (`src/services.cee`), then proposed to lib/std. It needs nothing CeeSafe lacks, and
httpserver is its first user with three services to move onto it (jobs, the bus carrier, the Hub's housekeeping).
Once it has run there, the same API in lib/std (as `std.tasks.Supervisor`) is a small ADR with a working
implementation behind it. Question 4.

### What CeeSafe would need

Nothing to start. Two things would make it better, to be raised with ADR 0189's measurements: a way to end one
isolated task of a set at once (today a set's cancellation is cooperative and set-wide), and `TaskCrash` naming
the service (today the task's `u64` id, which `Services` maps to its name itself).

### Tests

- `tests/services_test.cee` (`ceesafec test`, interpreted and native): a service that traps on its first start is
  started again and its second start runs; a `Transient` service that returns Ok is not; past the intensity, `run`
  returns `Err(Crashed)`; one-for-all restarts a sibling.
- `supervisor_check.py`: a route of the benchmark server that makes its scheduled job trap (behind
  `HTTPSERVER_TRAP_HOOKS`): the worker answers on, the report counts no worker crash, the job runs again.

## 4. Gap 2: memory a crash leaves

### Why it is not freed

ADR 0175 §2.4 and §4: what a crashed unit allocated may be reachable from elsewhere (an `Arc` the parent also
holds, a value sent through a channel), and a frame may still be written by the kernel (a read in flight). The
runtime cannot know who else reaches a block, so freeing it could leave live code holding freed memory. Unwinding
with destructors was rejected too (landing pads in every function; destructors on broken invariants). Erlang can
free a dead process's heap because a message is **copied** into the receiver's heap: nothing outside a process
points into it. CeeSafe **moves** a value, which is what makes it fast, and is why the same is not available. A
sound free would need copy-on-send for every value that leaves a unit, a change of the language's model, not a
plan of httpserver's.

### The plan: make the cost of a leak small, and its cure invisible

1. **Leak one request, not one connection.** With Web ADR 0017 the isolated unit is the request: a crash leaks the
   request's memory, not the connection's buffers, TLS state and every request before it. The connection itself
   goes on.
2. **Recycle without refusing a connection.** Today a spent worker (`Worker.spent`, `src/supervisor.cee:120`)
   starts its drain from `tend` (`:631`); the drain stops its accepting at once (CeeSafe.Web
   `web_transport/src/server.cee:8`); the parent starts the replacement only when the old process has exited
   (`watch`, the restart after an exit, `src/supervisor.cee:532`). Meanwhile the port has one worker fewer,
   and, without `CW_INHERIT`, the old worker's own `SO_REUSEPORT` socket still receives its share of connections
   until it closes, which then resets those waiting in its queue. The change:
   - **A worker says it is draining** before it drains: a second word for each worker beside its beat (the beats
     are `maxWorkers()` words, `src/supervisor.cee:230`; a second row makes them 4 KiB, still one page), set by
     `tend`.
   - **The parent starts the replacement at once**, at the same index, while the old one drains. A place has two
     children for that time: the old is watched until it exits (killed past `graceMs`), its beat no longer read;
     the new beats into the place's word.
   - **The same index twice** is safe for what `index()` decides today: the scheduler of worker 0
     (`src/schedule.cee:8`) runs nothing once its drain starts (`:473`), so jobs never run twice. The bus is not:
     its ring has one writer by design (`src/bus.cee:3`). Each index gets two rings, the generation's parity
     choosing which one a worker writes, and readers read both. That doubles the bus's memory (64 KiB a worker in
     the benchmark server).
   - **Listening:** with the inherited socket (`CW_INHERIT=1`, `Supervisor.listener`, `src/supervisor.cee:290`),
     a draining worker that stops accepting leaves the queue to the others and to its replacement: nothing is
     refused. With `SO_REUSEPORT` sockets of their own, the old worker keeps accepting until its replacement's
     first beat, then closes its listener, then drains; what lands in its queue in between is reset unless Linux's
     `net.ipv4.tcp_migrate_req = 1` (5.14 and later) moves it to another socket of the group. macOS gives every
     connection to one of the sockets, so it needs the inherited socket already.
   - **Recommendation:** the inherited socket becomes the supervised default (`Supervisor.listener` in the
     README's example and the benchmark server), and `SO_REUSEPORT` stays a Linux option with the sysctl named in
     the README.
3. **Report it.** `SupervisorReport` gains `recycled` beside `started`, `crashed`, `stalled` and `killed`, so a
   program sees how often its leaks cost a process.

### Tests

`supervisor_check.py --isolated` with `CW_RECYCLE_CRASHES=2`: while workers are recycled, a client sends a steady
stream of requests on new connections (two threads, 200 requests a second); none may be refused or reset, and
`recycled` is at least 1. Once with `--inherit`, and on Linux once with `SO_REUSEPORT` and the sysctl.

## 5. Order of work

1. **Gap 3, step 1** (isolation by default in `App.run`): httpserver alone, no CeeSafe change.
2. **Gap 2's recycling** (§4 item 2): httpserver alone.
3. **Gap 4's `Services`** in httpserver, then the scheduler, the bus carrier and the Hub moved onto it.
4. **ADR 0190** in CeeSafe, then gap 3's steps 2 to 6.
5. **Web ADR 0017** (requests isolated apart from connections) in CeeSafe.Web, then httpserver's update.
6. **ADR 0189** (pause at the slice) in CeeSafe, then httpserver's default bound becomes slice plus total (§2 step 2).
7. Upstream `Services` to lib/std (question 4).

Items 1 to 3 need no release of CeeSafe or CeeSafe.Web and can start at once.

## 6. Questions for the owner

1. `App.run` isolated by default, the present `run` renamed `runShared`, httpserver 0.2.0 (§2 step 1)?
   Recommended: yes.
2. A default bound of 1 000 ms until ADR 0189, measured first against the benchmark server's slowest routes
   (§2 step 2)? Recommended: yes.
3. A line at start when the build has no polls, rather than refusing to start (§2 step 3)? Recommended: a line:
   a server that runs without the bound still runs as it does today.
4. `Services` in httpserver first, proposed to lib/std after (§3)? Recommended: yes.
5. One-for-one and one-for-all only, rest-for-one left out (§3)? Recommended: yes.
6. Recycling with the replacement started before the old worker drains, two bus rings for each index, and the
   inherited socket as the supervised default (§4)? Recommended: yes.
7. No attempt to free a crashed unit's memory, given ADR 0175 §2.4 (§4)? Recommended: yes; to be revisited only
   if CeeSafe ever copies values between units.
