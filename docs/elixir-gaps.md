# Where Elixir's isolation is still ahead, and the plan to close it

**Where it stands (2026-11-21, ceesafec 0.84.1, CeeSafe.Web at #58's merge `9c38d14`).** Gaps 1, 3, 4 and 5 are
closed as planned, and gap 2 is as small as recycling makes it. Measured against Bandit in docs/elixir-benchmark.md:
with a 2 ms slice, a healthy request beside computing ones waits less than under Bandit (p99.9 3.1 against 9.5 ms);
at 1 000 crashes a second with per-request isolation, 22 healthy requests in 100 000 are retried on a new connection
(Bandit: none) and their p99 is 1.7 against 1.8 ms. What stays different by design: a crash's memory is freed by
replacing the worker, not with a heap (gap 2), and there are no supervision trees beyond a worker's `Services`.
The fixes the benchmark needed outside this repository: CeeSafe ADR 0191 (0.83.0, a busy executor runs its timers
and the tasks woken behind a paused one), ADR 0193 (0.84.1, the bound under a slice is the time a request ran), and
CeeSafe.Web's Web ADR 0019 (a drain's linger) and 0020 (an isolated request's file goes out with `sendfile`).
The rest of this document is the plan as accepted, and its notes as built.

Status: **accepted** by the owner, 2026-11-20 ("I accept all the above ADRs as written", together with CeeSafe ADRs
0189 and 0190 and CeeSafe.Web ADR 0017; §6's questions as recommended). Proposed the same day ("Draft ADRs for all
gaps"). **Implemented** the same day, on branch `plan/elixir-gaps`: gap 4 (`Services`), gap 2 (recycling that
refuses no connection) and gap 3's steps 1 to 3, 5 and 6 (isolation and a bound by default, httpserver 0.2.0); notes
in §7. Then, with ceesafec 0.82.0 (ADRs 0189 and 0190) and CeeSafe.Web at Web ADR 0017's
pull request (#53): gap 3's step 4 (`polls = true`), gap 1's slice (§2 step 2) and gap 5's isolated requests (§7,
"After 0.82.0"). **Waiting:** the tag v0.82.0, which CI installs, and #53's merge, whose commit the manifests then
pin in place of its head. It starts from httpserver at ceesafec 0.80.0:
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

## 7. Implementation notes (2026-11-20)

Built and checked with ceesafec 0.80.0 on macOS (Apple M1), under other sessions' load (averages 12 to 155).
Linux was not available: what is Linux's alone (`SO_REUSEPORT` and `tcp_migrate_req`, `memoryBytes`) was not run.

### Gap 4: `Services` (commit `0714adc`)

- `src/services.cee`: `Service`, `Restart` (permanent, transient, temporary), `Strategy` (one for one, one for all),
  `Intensity`, `ServiceEvent` and `Services` (`create`, `add`, `watch`, `run`, `name`, `count`, `lookEvery`), as §3.
  Each start is `set.spawnIsolated` of the service's `run`; a start's end reaches the supervisor through a channel
  (a return) or the set's watcher (a trap), and the supervisor looks at both every 20 ms (`lookEvery`). One for all
  runs each generation of the services in a task set of its own, which the body's `Err` cancels.
- **Departures from §3, each forced by the language or the code:**
  - `NetError` has no `Crashed`: past the intensity `run` returns `NetError.LimitExceeded`.
  - An interface cannot name `Send + Share` as its bases (N0204): `Service` is a plain interface, and `add` takes a
    `Box<dyn Service + Send + Share>`. httpserver's private `Service` (the connection handler in `src/app.cee`) is
    now `Serving`, to free the name.
  - A `ServiceEvent` names a service by its number, not its name (a `StringView` in a channel would need a region):
    `services.name(id)` gives the name.
  - The intensity counts every restart within its window, as Erlang's does: a service restarted once, then
    trapping at every run, gives up one restart sooner.
  - **The Hub has no task of its own**: what it does outside a session is `deliverEncoded` from the bus's sink, so it
    is under `Services` through the bus's reading. The scheduler is a service through `Scheduler.service()`, whose
    jobs are now shared `Arc`s so that each start runs them from a fresh schedule (the reports begin again). The
    bus's reading is `BusCarrier<K>`, which takes a `Bus.reader` in each start (a reader that leaves the worker's
    ring as it is, where `join` drops what was being written).
- The benchmark server runs the bus's reading and, with `CW_JOBS=1`, a scheduler with a job every second under
  `Services` beside `tend` (`CW_SERVICE_RESTARTS`, 5 in 30 s); `/job` counts its runs and `/job/trap/{n}` makes it
  trap.
- **Tests:** `tests/services.cee` (4 tests: started again after a trap, transient done after Ok, giving up past the
  intensity, one for all), native: 4 passed. The package's tests do not run interpreted (every one of them, the
  cron tests included, exits 70 under `--interp`: the package calls C), so they are not run there.
  `supervisor_check.py`'s services phase: a trap of the job is started again and the job runs on, the worker
  answers throughout; at every run it traps, the supervisor gives up, the worker ends and is started again (report
  2 started, 1 crashed); four traps ended alone.

### Gap 2: recycling that refuses no connection (commit `65bbf92`)

- A worker past its bound asks for a replacement (`tend` sets its state word to 1); the parent starts another in its
  place at once and tells the old one to drain (2): at once with the parent's socket, once the new one has beaten
  with sockets of their own. One that outlives `graceMs` after it was told is killed. `SupervisorReport.recycled`;
  the benchmark server prints it after its four numbers (`bench: recycled`).
- **Departure from §4:** a place has two seats, one for each generation, each with a beat, a state and a ring of the
  bus (`Worker.ring()`, `--httpserver-worker=<index>.<generation>`), where §4 had the replacement beat into the
  place's word. With one word the old worker, still beating while it drains, would hide a replacement that stalls.
  So the pages are 8 KiB (a beat and a state for each of 256 places and two generations), not 4 KiB. A supervisor's
  bus has two rings for each place, which doubles its memory, as §4 said.
- The parent's socket is the benchmark server's default (`CW_INHERIT=0` for sockets of their own); every tool sets
  `CW_INHERIT` both ways.
- **Tests:** `supervisor_check.py` (isolated, `--inherit`): two clients ask for `/small` on new connections every
  10 ms while three workers are replaced: 0 of 484 failed, 3 recycled, each started while the old one drained.
  **What this does not show:** with the parent's socket the old scheme (start the replacement after the exit)
  would likely have refused nothing either, its queue waiting for the others; the change is felt in the time a
  place has no worker, and with sockets of their own on Linux, where `tcp_migrate_req` decides. Neither was
  measured here.

### Gap 3: isolation and a bound by default (commit `c730964`)

- `App.run` is the isolated server, `runShared` the former `run`, `runIsolated` a name for `run` until the release
  after 0.2.0; `app.bound(ms)` (1 000 ms, 0 for none) is set as the program's bound (`RunBound.Count`) when `run` or
  `runShared` starts. httpserver is 0.2.0, every manifest asks for ceesafec 0.80.0, and CI installs v0.80.0 and builds
  `bench-traps` with `CEE_POLLS` (it did not since step 7 of ADR 0186).
- **Departure from §2 step 3:** the line says `build with --cflag -DCEE_POLLS=1`, not `polls = true`, which no
  released ceesafec reads yet. Step 4 (`polls = true` in httpserver's manifests) waits for ADR 0190's release.
- **Question 2's measurement:** the benchmark server's slowest routes, with polls, ten requests each:
  `/gz/60000` (gzip) at most 5.5 ms, `/z/60000` (Brotli) 7.1 ms, zstd 1.5 ms, `/64k` 0.9 ms; none overran. 1 000 ms
  is over a hundred times the slowest.
- **Tests:** `tests/run.cee` (`App.run`: a trap costs its connection, the next is served, `Isolation.report()`
  counts one). `supervisor_check.py` is isolated by default (`--shared` for plain tasks) and runs a build without
  polls, which says so and whose spin the stall catches.

### Checks, at the end

- `ceesafec check` and `fmt --check` in `.`, `auth`, `static`, `ws`, `compress`, `otel`, `otel/demo`,
  `examples/hello`, `bench`: clean. `ceesafec fetch` changed no lock.
- `ceesafec test --cflag -DHTTPSERVER_TRAP_HOOKS=1`: 32 passed; with `-fsanitize=address,undefined`: 32 passed;
  `auth` 9, `static` 1, `ws` 3, `compress` 3, `otel` 1 passed.
- `supervisor_check.py --inherit --budget-mb 16`: 64 ok, 0 failed; `--shared`: 31 ok, 0 failed (the crash steps that
  failed under load before this work, on httpserver `c95e9e3` with 0.79.2 as well, passed in each run here).
- `gzip_check.py` 16 ok, `ratelimit_check.py --inherit` 2, `ws_check.py` 6, `ws_check.py --workers 3 --inherit` 7,
  `bus_check.py --inherit` 3, `otel_check.py` 11: all passed.

### After 0.82.0 and Web ADR 0017

Built and checked with ceesafec 0.82.0 (CeeSafe's release branch at `8ba415aa`, first cut as 0.81.0 and renumbered
0.82.0 when another session tagged a 0.81.0 meanwhile; same ADR 0189 and 0190 code) and CeeSafe.Web at #53's head
(`91ef113`), on macOS (Apple M1) at load averages of 6 to 12.

- **The pin** (commit `2616409`): every manifest names CeeSafe.Web at `91ef113`, and the locks are fetched again.
  After #53 merged, every manifest names its merge commit, `9e83e4d`, and the checks below were run again on it.
- **Gap 3, step 4: `polls = true`** (commit `a1440dd`): `bench`, `examples/hello` and `otel/demo` ask for the polls
  in their manifests (ADR 0190), as does the README's example manifest; a library's key does nothing for its users,
  so the root package does not. The start-up line of a build without polls says `add polls = true to ceesafe.toml`.
  `bench-traps` is built with the trap hooks' macro alone, and `supervisor_check.py`'s phase without polls runs
  `bench/out/bench-nopolls`, built with `--no-polls`. CI installs v0.82.0.
- **Gap 1: the slice** (commit `e42e4f9`): `App.slice(ms)`, 10 ms by default (ADR 0189's measurement on this
  server), which `run` sets with `Async.runSlice` before the bound. The bound's default follows §2 step 2: 30 000 ms
  when the slice took, a request that has computed that long in one resume while sharing its worker being taken to
  never end, and 1 000 ms when it did not (`runShared`, where no task is paused; a 32-bit target; a build without
  polls), which keeps the bound well below the supervisor's 5 000 ms stall. A target without a slice says so once.
  `app.bound(ms)` given is used as it is. The benchmark server takes `CW_RUN_SLICE_MS` and `CW_RUN_BOUND_MS` only
  when they are set, and answers `/pauses` with `Async.pauses()`. `supervisor_check.py`: the one-worker bound phase
  now runs with `CW_RUN_SLICE_MS=0`, where a request beside a spin waits for its end (0.1 to 1.0 s); a new phase with
  a 10 ms slice and a 1 000 ms bound checks that the median wait of five requests beside `/spin/300` and beside
  `/spin` is under 0.2 s (0.073 and 0.088 s), that `/spin/300` is answered, that `/spin` is ended at its bound, and
  that the worker counted pauses (122). Every manifest asks for ceesafec 0.82.0, since `App` calls `Async.runSlice`.
- **Gap 5: isolated requests** (commit `0712cc9`): `App.isolateRequests(on)` sets `Limits.isolateRequests`, off by
  default as Web ADR 0017 has it until the benchmark machine measures its cost, and the connection handler then
  serves HTTP/1.1 through `web_http1.serveIsolated`. `Application.crashed` reaches the access log through a new
  default method, `AccessLog.crashed(request, crash)`; `ConsoleLog` writes `GET /crash crashed index`. The benchmark
  server takes `CW_ISOLATE_REQUESTS=1`, has `/crash/after/{n}` (sends n bytes, then traps) and prints its
  `ServerReport.crashed` at the end. `supervisor_check.py`: on one kept-alive connection `/crash`, `/crash/kept`,
  `/spin` (`Overran`) and `/crash/gz/60000` are each answered 500 and the next request goes on the same socket;
  `/crash/after/4096` loses its connection; the report counts the five, and the log has a line for each.
  **Departure from Web ADR 0017's note:** it said `/crash/gz/60000` would still lose its connection. It does not:
  `Compression.send` gzips the body into a buffer before the head is sent, so when it traps no byte has left, and
  the 500 is the ADR's own rule. `/crash/after/{n}` is the route whose bytes have left.
- **Checks, at the end:** `check` and `fmt --check` clean in all 9 packages; the core tests 32 passed with the trap
  hooks, and 32 under AddressSanitizer and UndefinedBehaviorSanitizer; auth 9, static 1, ws 3, compress 3, otel 1;
  `examples/hello` and `otel/demo` build; `supervisor_check.py --inherit --budget-mb 16` 84 ok (the slice and
  isolated-requests phases among them) and with `--shared` 31 ok, no failures; `gzip_check.py` 16, `ratelimit_check.py
  --inherit` 2, `bus_check.py --inherit` 3, `ws_check.py` 6 and with 3 workers 7, `otel_check.py` 11, all ok.
