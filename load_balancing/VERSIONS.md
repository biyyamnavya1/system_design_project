# Load balancing — version history

Same convention as the caching demo: every concept milestone gets frozen
into `versions/vN_*.py`, never edited again. [`load_balancer.py`](load_balancer.py)
(one folder up) is always the ACTIVE file. [`backend_server.py`](backend_server.py)
is shared infrastructure (like `redis_server.py` was for caching) — it's
the generic "one backend copy" script, not itself a versioned concept.

Run any version directly, after starting the three backends:
```
python backend_server.py A 6101
python backend_server.py B 6102
python backend_server.py C 6103
python versions/v1_round_robin.py
```

---

## v1 — [`v1_round_robin.py`](versions/v1_round_robin.py)
**Topic: the physical shape of load balancing, plus the simplest routing algorithm.**
- One process (the load balancer) in front of several identical backend processes — client only ever talks to the load balancer
- Proxying: the load balancer makes its own HTTP request to the chosen backend and relays the response back
- Round robin algorithm: a counter modulo the number of backends — request 1 → backend 0, request 2 → backend 1, wrapping around
- Verified live: strict A → B → C → A rotation across 4 real requests, each backend's own `request_count` and PID staying independent and consistent

## v2 — [`v2_retry_on_failure.py`](versions/v2_retry_on_failure.py)
**Topic: a failed request gets redirected, not lost.**
- The v1 gap, proven live: round robin has zero health awareness — killed Server B, watched its counter freeze while A/C kept climbing, confirmed via the actual error (`ConnectTimeoutError`, a 2s wasted timeout) in the log
- The fix: round robin still picks the starting backend, but on failure the load balancer walks forward through the rest of the list and tries each one, instead of giving up immediately
- Answers "lost or redirected?": redirected, as long as at least one backend is still healthy; genuinely lost (HTTP 502) only if ALL backends are down
- Verified live, full cycle: killed B → next request round-robin'd to B → timed out, auto-skipped to C (UI flashes B red, lights up C) → succeeded in ~2080ms, logged as "NOT lost" → restarted B → confirmed it rejoined rotation normally on its next turn (fresh PID, counter reset — a genuinely new process)

## v3 — [`v3_least_connections.py`](versions/v3_least_connections.py)
**Topic: route by real-time load, not blind rotation — and a real bug found live.**
- Least connections: track in-flight request counts per backend (`active_connections`), always route to whichever has the fewest right now
- Needed real infrastructure changes to test meaningfully: `threaded=True` on both the load balancer and `backend_server.py`, plus an optional artificial-delay argument on `backend_server.py` to simulate an "overloaded" backend
- **Bug #1 (browser quirk, not a code bug):** firing 6 "concurrent" requests via the browser's `Promise.all` all landed on Server A. Diagnosed by firing the identical 6 requests from Python's `ThreadPoolExecutor` against the same server — correctly spread A/B/C/B/A/C — proving the algorithm was right and the browser's same-origin connection handling was the actual cause (confirmed via batch duration: ~3s total ≈ 6× sequential, not ~0.65s parallel)
- **Bug #2 (real algorithm bug, found live):** even single sequential clicks always routed to Server A — because every click saw a perfect 3-way tie (0 active everywhere), and plain `min()` always resolves ties to the first list item. B and C were *structurally* unreachable under normal, non-concurrent load.
- **The fix:** round-robin tie-breaking among equally-least-busy backends (same approach nginx's `least_conn` uses). Verified live: 4 sequential clicks correctly cycled A → B → C → A. Re-verified genuine concurrency still works after the fix via the same Python test.

## v4 — [`v4_sticky_ip_hash.py`](versions/v4_sticky_ip_hash.py)
**Topic: the SAME client always lands on the SAME backend — sticky sessions via hashing.**
- IP hashing: hash a client identifier into a number, use it to always pick the same backend for that client — a pure function, no shared counters or state needed (unlike least-connections)
- Hashed a simulated "client ID" instead of the real IP, since every request in this local demo shares one IP — same mechanism, honest substitution
- Avoided a real, easy-to-miss bug: Python's built-in `hash()` is randomized per process run for strings; used `hashlib.md5` instead for a stable, restart-proof hash
- Verified live via a dedicated sticky-history panel in the UI: client-1 → always Server A, client-2 → always Server C, client-3 → always Server C, across multiple separate requests each
- Observed a real (non-bug) limitation: client-2 and client-3 collided onto the same backend — expected with only 3 buckets (pigeonhole principle), one of the standard motivations for consistent hashing in bigger/more dynamic clusters
- Named but not built: no fallback when a client's assigned backend is down (deliberately, to keep the concept focused) — this is the real tradeoff consistent hashing exists to soften

## Up next (not yet built)
- Proactive health checks (`backend_server.py` already exposes `/api/health`) — right now a dead backend is only discovered reactively, costing up to ~2s per request until its turn comes up
- Consistent hashing (the standard refinement over plain modulo hashing)
- Weighted round robin
- Eventually: combine with the caching demo into one integrated system
