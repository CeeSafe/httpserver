# httpserver against Elixir (Bandit), 2026-11-20

`tools/elixir_bench.py` against httpserver's bench server (`bench/`) and the same routes on Bandit with Plug
(`bench/elixir/`, no Phoenix). Raw results: `bench/elixir/results/2026-11-20-rq/` (one speed run, three fault runs;
the fault table gives the median of the three).

## The machine and the settings

- **rq**: Intel Xeon D-2123IT (4 cores, 8 threads, 2.2 GHz), 31 GB, Ubuntu 24.04.5, Linux 6.8.0-149. It also runs
  production services (a .NET site on 80/443, an API service, Docker containers), lightly loaded: the 1-minute load
  was 0.2 to 1.5 before each run, recorded in every row.
- **Each server** has CPUs 0, 1, 4, 5 (two physical cores, both hyperthreads): httpserver as 4 worker processes
  (`CW_PROCS=4`), Bandit as 4 schedulers (`+S 4:4`). **The load** (h2load, the clients, the script) has CPUs 2, 3, 6, 7.
- httpserver: ceesafec 0.82.0, CeeSafe.Web `9e83e4d`, its defaults (each connection an isolated task, a 10 ms slice,
  a 30 s bound, a worker replaced after 100 crashes) unless a row says otherwise.
- Bandit 1.15.0, Thousand Island 1.6.0, Plug 1.20.4, Jason 1.4.5 (`bench/elixir/mix.lock`), Elixir 1.20.5 on
  Erlang/OTP 29.1.2, a `mix release` in prod, Logger at `:error`.
- Both servers pass CeeSafe.Web's `benchmarks/servers/conform.py` (HTTP/1.1 and h2c) before anything is measured.

## Speed

(These are 0.82.0's numbers; the h2c rows in bold are fixed by CeeSafe's ADR 0191, below.)

h2load, 10 s after a 1 s warm-up, one request at a time on each connection (`-m 1`).

| mode | route | conns | httpserver req/s | Bandit req/s | ratio | httpserver p99 µs | Bandit p99 µs | RSS MiB (hs / Bandit) |
|---|---|---|---|---|---|---|---|---|
| h1 | small | 1 | 12691 | 9368 | 1.4× | 120 | 142 | 16 / 162 |
| h1 | small | 16 | 136674 | 47066 | 2.9× | 171 | 583 | 17 / 161 |
| h1 | small | 128 | 156978 | 48273 | 3.3× | 3721 | 3168 | 22 / 174 |
| h1 | 4k | 16 | 129293 | 46166 | 2.8× | 246 | 601 | 17 / 161 |
| h1 | 64k | 16 | 63620 | 34263 | 1.9× | 501 | 837 | 16 / 168 |
| h1 | stream (1 MiB) | 16 | 3173 | 1876 | 1.7× | 13814 | 11355 | 17 / 168 |
| h1 | json | 16 | 127010 | 41340 | 3.1× | 186 | 632 | 17 / 167 |
| h1 | json | 128 | 154764 | 41938 | 3.7× | 3575 | 3398 | 22 / 169 |
| h1 | echo (4 KiB) | 16 | 101859 | 42024 | 2.4× | 525 | 642 | 17 / 166 |
| h2c | small | 1 | 8284 | 5975 | 1.4× | 176 | 192 | 16 / 162 |
| h2c | small | 16 | 73524 | 22084 | 3.3× | 315 | 1394 | 19 / 172 |
| h2c | json | 16 | 69533 | 20162 | 3.4× | 368 | 1514 | 19 / 170 |
| h2c | echo | 16 | 61480 | 18908 | 3.3× | 434 | 1608 | 19 / 166 |
| h2c | small | 128 | **39831** | 24597 | 1.6× | 1928 | 9359 | 38 / 166 |
| h2c | 64k | 128 | **3615** | 21094 | 0.2× | 7777 | 9676 | 44 / 174 |
| h2c | stream | 16 | **804** | 1440 | 0.6× | 7222 | 19025 | 20 / 166 |
| h2c | stream | 128 | **166** | 1364 | 0.1× | 83685 | 101655 | 45 / 172 |

(Every row is in `speed.json`.) Over HTTP/1.1, httpserver serves 1.4× Bandit's rate on one connection and 1.7× to 3.7×
under concurrency, with a lower p99 up to 16 connections and about the same at 128, in a tenth of the memory
(15 to 25 MiB against 160 to 190). Bandit uses more CPU for one connection (1.07 cores against 0.6).

**The h2c rows in bold are not httpserver's speed: its workers were killed under them.** At 128 h2c connections (and
at 16 for `/stream`) the workers' heartbeats stop, and the supervisor kills each one 2 s later (`has not beaten:
killed after ms 2011`, signal 9), so the run is mostly reconnects. gdb on a worker that is about to be killed shows it
busy serving (`web_http2_handle`, `send`), not stuck. The cause is in CeeSafe's executor
(`runtime/hosted/ceesafe_rt_async.c`, `cee_exec_tasks`): it looks at its timers only when no task is ready, or after
a task is paused past its slice. A worker whose HTTP/2 tasks keep its queue full never runs its heartbeat's timer.
Under the same load Bandit loses nothing. (The `cpu_cores` of those rows, 0.02 to 0.04, is wrong too: a killed
worker's CPU time leaves the process tree that the script sums.)

## Faults

A fixed healthy load, 10 000 GET /small a second over 64 keep-alive connections, each request's latency counted from
when it was due, while a fault client runs beside it. A healthy request whose connection ends before any answer is
sent again once on a new connection, and counted as retried; none failed twice, in any run.

| server | fault | healthy p50 µs | p99 µs | p99.9 µs | healthy retried (of 100 000) | what the faulty requests got | peak RSS MiB | workers replaced |
|---|---|---|---|---|---|---|---|---|
| httpserver | none | 375 | 1534 | 2681 | 0 | | 19 | 0 |
| httpserver | 100 crashes/s | 484 | 1634 | 8831 | 186 | 1100 resets, each costing its connection | 38 | 8 |
| httpserver | 1000 crashes/s | 663 | 6706 | 14444 | 1565 | 11 000 resets | 53 | 83 |
| httpserver | 4 × /spin/100 in a loop | 545 | 1687 | 69123 | 0 | 336 spins, p50 100 ms | 19 | 0 |
| httpserver | 2 × /spin forever | 553 | 1530 | 2432 | 0 | still running | 19 | 0 |
| httpserver, per-request isolation | none | 352 | 1476 | 2316 | 0 | | 21 | 0 |
| httpserver, per-request isolation | 100 crashes/s | 371 | 1554 | 5638 | 147 | 1010 × 500, 90 resets; 118 connections for 1100 | 42 | 8 |
| httpserver, per-request isolation | 1000 crashes/s | 525 | 3421 | 8520 | 1180 | 10 287 × 500, 713 resets; 745 connections | 45 | 68 |
| httpserver, per-request isolation | 4 × /spin/100 | **6 213 650** | **9 889 994** | **9 959 145** | 0 | 440 spins, p50 101 ms | 22 | 0 |
| httpserver, 1 ms slice | 4 × /spin/100 | **3 632 544** | **6 101 054** | **6 139 100** | 0 | 440 spins, p50 100 ms | 19 | 0 |
| httpserver, 1 ms slice | 2 × /spin forever | 445 | 1464 | 2446 | 0 | still running | 19 | 0 |
| Bandit | none | 344 | 1441 | 4370 | 0 | | 166 | 0 |
| Bandit | 100 crashes/s | 354 | 1446 | 4123 | 0 | 1100 × 500, the connection closed after each | 166 | 0 |
| Bandit | 1000 crashes/s | 421 | 2533 | 5763 | 0 | 11 000 × 500, closed after each | 170 | 0 |
| Bandit | 4 × /spin/100 | 428 | 3721 | 7533 | 0 | 440 spins, p50 100 ms | 164 | 0 |
| Bandit | 2 × /spin forever | 451 | 3720 | 5028 | 0 | still running | 166 | 0 |

(The 1 ms slice's crash rows match the default's: `fault-*.json`.) The healthy client is Python, so the baseline's
p50 (340 to 380 µs) is mostly the client's own; the differences between rows are what the faults add.

- **A crash.** Bandit answers 500 and closes the connection; httpserver resets the connection (its default), or with
  per-request isolation (Web ADR 0017) answers 500 and keeps the connection, which Bandit does not. Neither lets a
  crash touch another client's request directly.
- **What crashes cost the healthy clients.** Bandit: nothing measurable at 100 a second, p99 1.4 → 2.5 ms at 1000.
  httpserver: every 100 crashes a worker is replaced (gap 2, the memory a crash leaks), and the worker it replaces
  drains, closing its healthy keep-alive connections. At 1000 crashes a second that is 83 replacements in 11 s and
  1.6 % of healthy requests retried on a new connection, with p99 rising to 6.7 ms (3.4 ms with per-request
  isolation). The memory is reclaimed (peak 53 MiB, back to about 20). Bandit's processes leak nothing, so it never
  replaces anything.
- **A request that computes.** Two requests that never end cost neither server anything measurable. Four that
  compute 100 ms at a time, back to back, cost Bandit a p99 of 3.7 ms. With httpserver's defaults they cost the
  p99.9 69 ms (two runs of three; 3.8 ms in the third). **With per-request isolation or a 1 ms slice, healthy
  requests waited 3.6 to 10 s, in all three runs**, while the spinning clients were served at once (440 spins,
  every one they asked for). The spinner's requests keep each worker's queue full, so like the h2c rows this looks
  like the executor not getting to what waits on sockets, or on timers, while some task is always ready; the exact
  path is not yet found.

## What this says about the gaps (docs/elixir-gaps.md)

*As first measured, on ceesafec 0.82.0. Points 2 and 3 are answered by the two sections after this one: CeeSafe's
ADR 0191 for the executor, and Web ADR 0019 with a 2 ms slice for recycling and latency. A third fault, found by
httpserver's CI with the 2 ms slice, is fixed by CeeSafe's ADR 0193 (0.84.1): the bound under a slice counted slices
times the slice, not the time a request ran.*

1. **Speed and memory** are httpserver's: 1.4 to 3.7× the rate over HTTP/1.1 and h2c at moderate concurrency, in a
   tenth of the memory.
2. **Gap 1 (fair preemption) is not closed.** Bandit's scheduler keeps every healthy p99 under 4 ms whatever the
   faulty requests do. httpserver's slice helps for a task that computes without end, but a busy executor still
   starves its socket waits and timers: seconds of latency with four spinners under per-request isolation or a short
   slice, and its supervisor killing healthy workers under h2c load. The fix is in CeeSafe's executor: collect events
   and due timers every so many resumes, or every slice's worth of time, whether or not a task was paused, not only
   when the queue is empty.
3. **Gap 2 (a crash's memory)** costs healthy clients reconnects under a high crash rate, through recycling. Recycling
   that drains a worker without closing its idle keep-alive connections at once (or hands them to the new worker)
   would remove most of the retries.
4. **Gap 5 (a 500 for a crash)** is closed by per-request isolation, which keeps the connection where Bandit closes
   it.

## After CeeSafe's ADR 0191 (2026-11-21)

The two starvations above were one cause in CeeSafe's executor, fixed by its ADR 0191 (ceesafec 0.83.0): a busy executor now collects socket events, wakes and due timers every
millisecond, and a task paused past its slice waits in a queue of its own, so the tasks woken behind it run first.
The same runs again, the bench built with that compiler, Bandit unchanged (`bench/elixir/results/2026-11-21-rq-adr0191/`):

| | before (0.82.0) | after (ADR 0191) | Bandit |
|---|---|---|---|
| h2c `/small`, 128 connections | 39 831 req/s, workers killed | **73 325 req/s**, none killed | 24 661 |
| h2c `/64k`, 128 connections | 3 615 req/s | **21 390 req/s** | 21 062 |
| h2c `/stream`, 16 connections | 804 req/s | **2 354 req/s** | 1 420 |
| h2c `/stream`, 128 connections | 166 req/s | **1 721 req/s** | 1 328 |
| h2c `/json`, 128 connections | 12 459 req/s | **68 931 req/s** | 22 802 |
| 4 × /spin/100, defaults: healthy p99 / p99.9 | 1.7 / 69 ms | 7.9 / **9.8 ms** | 3.9 / 7.5 ms |
| 4 × /spin/100, per-request isolation | 6.2 s p50, 9.9 s p99.9 | **0.4 ms p50, 1.5 / 4.6 ms** | |
| 4 × /spin/100, 1 ms slice | 3.6 s p50, 6.1 s p99.9 | **0.6 ms p50, 2.8 / 6.5 ms** | |

Every h2c row is now at least Bandit's rate (1.0× to 3.4×), with no worker killed in any run, and the HTTP/1.1 rows
are as before (1.1× to 3.7×). Under computing neighbours httpserver's healthy p99.9 is now Bandit's order (4.6 to
9.8 ms against 7.5), where it was 69 ms to 10 s. What is left of the gaps is recycling under a high crash rate
(1.5 % of healthy requests retried at 1000 crashes a second, unchanged) and the default slice's p99 (7.9 ms, a
neighbour waiting up to one 10 ms slice, against Bandit's 3.9).

## After CeeSafe.Web's Web ADR 0019 and a 2 ms slice (2026-11-21)

The two gaps left above, run again (three fault runs, medians; `bench/elixir/results/2026-11-21-rq-web0019/`; the
bench built with ceesafec 0.83.0 and CeeSafe.Web at Web ADR 0019's branch):

- **Recycling.** Web ADR 0019: when a worker drains, an idle keep-alive connection waits up to a second for its next
  request and answers it with `connection: close`, instead of closing under a client that had just sent on it.
- **The slice.** A 2 ms slice instead of 10, now `App`'s default.

| | before | after | Bandit |
|---|---|---|---|
| 1000 crashes/s: healthy requests retried (of 100 000) | 1 545 | **22** | 0 |
| 1000 crashes/s, per-request isolation: healthy p99 / p99.9 | 3.7 / 9.4 ms | **1.7 / 6.5 ms** | 1.8 / 5.2 ms |
| 1000 crashes/s, per-request isolation: crashes reset rather than answered 500 | 721 | **5** | 0 |
| 100 crashes/s: healthy requests retried | 152 | **5** | 0 |
| 4 × /spin/100: healthy p99 / p99.9, 2 ms slice | 7.9 / 9.8 ms (10 ms slice) | **1.5 / 3.1 ms** | 3.8 / 9.5 ms |
| 4 × /spin/100: how long each spin took | 100 ms | 105 ms | 100 ms |

`/small` over HTTP/1.1 at 128 connections answered 161 000 to 175 000 requests a second with a 10 ms slice and
155 000 to 175 000 with 2 ms (three alternating runs each): the same within the noise.

With these, httpserver's healthy requests are at Bandit's level or better in every fault run: a crash costs the
others about what it costs under Bandit, and a computing neighbour costs them less. What still differs is where
the crash's memory goes: Bandit frees it with the process, and httpserver replaces the worker every 100 crashes,
which now costs a handful of retried requests rather than one or two in a hundred.

## An independent benchmark, and CeeSafe.Web's Web ADR 0021 (2026-11-21)

An independent benchmark (not in this repository) ran httpserver at `f44fc0d` against Bandit and Cowboy on Linux,
each server on one core, under an open-loop healthy load of 3 000 requests a second over 30 keep-alive
connections, with its own load client written in Go. httpserver ran under the supervisor with one worker.

- **Speed and memory, as here:** HTTP/1.1 85 000 to 94 000 requests a second against Bandit's 20 000 to 21 000
  and Cowboy's 13 000 to 16 000; h2c 3.8 to 6.4 times Bandit's; 5 MiB idle against 133.
- **Memory a request takes** (two requests a second that each hold 256 MiB): with `app.budget(64 MiB)` the healthy
  p99 was 1.8 ms, and without a budget 154 ms. Bandit's was 33 s, and it died in one run of two; Cowboy's was 116 s.
- **Crashes: a second's tail.** At 20, 200 and 1 000 crashes a second, and with one, two or four workers, the healthy
  p99 was 0.8 to 0.9 s and the p99.9 1.0 s, against Bandit's 3.5 to 4.4 ms p99, and every worst case was 1 000 ms.

The tail was not recycling itself. Reproduced on the Xeon above, `bench` with one worker pinned to one core, with a
probe that logs each slow request beside the server's lines: at every replacement, the one request a client sent in
the turn the drain began was taken from its socket and dropped, and the connection closed when Web ADR 0019's
linger ended, a second later. The idle read was raced against the drain, and when both finished in one turn of the
executor the drain's error decided the race and the bytes were lost (on Linux's epoll; not on macOS). Before Web ADR
0019 that connection closed at once, and the request was sent again: the 1.2 to 1.5 % of healthy requests retried
above, and the 22 left after it, were most likely the same loss. Web ADR 0021 keeps a value that comes in the drain's
turn, for the idle read and for the accept loops (`drainBegun`, `drainTaken`).

The probe, before → after, at 3 000 healthy requests a second over 30 connections (10 to 12 s each):

| | healthy p99 | p99.9 | worst | requests over 100 ms |
|---|---|---|---|---|
| 20 crashes/s (three runs) | 0.4 → 0.3 ms | 823 → 2.0 to 4.3 ms | 1 003 → 7 to 12 ms | 182 → 0 |
| 200 crashes/s | 805 → 6.2 ms | 988 → 13.5 ms | 1 007 → 18 ms | 1 292 → 0 |
| 1 000 crashes/s | 767 → 13.2 ms | 987 → 18.4 ms | 1 015 → 24 ms | 958 → 0 |
| 200 crashes/s, per-request isolation | 675 → 0.4 ms | 973 → 1.4 ms | 1 003 → 3 ms | 787 → 0 |

The same benchmark also measured two other costs.

- **Beside requests that compute,** the 2 ms slice made the healthy median 2.4 ms against Bandit's 0.8: a request
  waited for the rest of the slice and the monitor's millisecond. CeeSafe's ADR 0203 (ceesafec 0.89.0) ends a slice
  when the worker has I/O to serve. Reproduced and measured on the Xeon, one worker on one core, 3 000 healthy
  requests a second beside two clients computing `/spin/200`: p50 1.81 → 0.30 ms, p99 3.41 → 1.35 to 1.56 ms (0.60 ms
  with two cores), the computing requests 201 ms, `/small`'s rate the same (71 881 against 72 178 a second).
- **Per-request isolation** serves 30 to 40 % fewer HTTP/1.1 requests a second than isolation per connection (57 000
  against 90 000 on one core; 47 000 against 72 800 here). Profiled on the Xeon, the extra 6 µs a request was the
  request task itself, not the messages between it and its connection. Two changes take part of it:
  - CeeSafe.Web's Web ADR 0022: a small response goes back with the request's kit, one message in place of three
    and no allocation for its body;
  - CeeSafe's ADR 0204 (ceesafec 0.91.0): the C locals of an async function's resume function are no longer zeroed
    at every resume (the relay's had 56, 3 042 bytes, each its own store); in this server's C, zeroed locals fell
    from 5 279 to 717.

  One worker on one core, `/small` at 64 connections, four alternating runs each: isolated 46 691 requests a second
  before, 48 534 with ADR 0204, 49 286 with Web ADR 0022, 51 280 with both (+10 %); direct 73 278 against 72 718, the
  same. What is left (about 4.5 µs a request) is spawning and waking the request's task and parsing its head a second
  time in it.

## Measurement notes

- h2load is not used under faults: when the server closes connections under it, as a worker being replaced does, it
  reported 0 requests for a run in which the server answered about 94 000.
- One speed run, so the speed rows carry the run-to-run noise of a shared machine (the three fault baselines differ by
  up to 2.5× at p99.9). The ratios are large enough not to depend on it; the latencies of the 128-connection rows do.
