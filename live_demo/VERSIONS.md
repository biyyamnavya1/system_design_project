# Live demo — version history

Every time we finish a concept, the working app gets frozen into a new
`vN_*.py` file in [`versions/`](versions/). Old versions are never edited —
if you want to see "caching, step 1" again, open `v1`, not the current one.
[`app.py`](app.py) (one folder up) is always the ACTIVE file — equal to the
newest version, plus whatever we're currently building on top of it.

Run any version directly: `python versions/v1_cache_aside_dict.py`, etc.
(v2+ also needs [`redis_server.py`](redis_server.py) running — see each
file's own docstring for exact run instructions.)

---

## v1 — [`v1_cache_aside_dict.py`](versions/v1_cache_aside_dict.py)
**Topic: caching basics, single process.**
- Cache-aside (lazy-loading) pattern: miss → read DB → populate cache → return
- Manual TTL tracking (Python timestamps, compared by hand on every read)
- Explicit invalidation ("Clear cache" button)
- Why stopping a process ≠ pausing it (cache wiped on restart — it's just a dict in RAM)
- Simulated slow database via `time.sleep()`

## v2 — [`v2_real_redis_resilience.py`](versions/v2_real_redis_resilience.py)
**Topic: a real separate cache process, and what happens when it dies.**
- Cache becomes a REAL separate OS process (`redis_server.py`, real Redis wire protocol, real port 6379) instead of a dict inside the app
- Process independence — proved live by killing only Redis and watching Flask survive
- Redis-native TTL (`SETEX`/`TTL` commands) replacing manual tracking
- Failure mode 1: no error handling → Redis dying crashes every request (raw HTTP 500)
- Failure mode 2 (the fix): graceful degradation — `try/except` around Redis calls, fall back to the database instead of crashing
- Hidden retry/timeout layers: measured a "1 second" timeout actually taking 15–25 real seconds because of redis-py's own internal connection-retry logic sitting on top of it; fixed by explicitly disabling that retry layer (`Retry(NoBackoff(), 0)`)
- Discussed (not yet built): absolute TTL (what we have — resets only on write) vs. sliding/rolling TTL (resets on every read — a different pattern for different data, e.g. session expiry)

## v3 — [`v3_circuit_breaker.py`](versions/v3_circuit_breaker.py)
**Topic: stop re-trying a dependency you already know is dead.**
- Circuit breaker pattern: `CLOSED` → (2 consecutive failures) → `OPEN` (skip Redis entirely for an 8s cooldown, straight to DB, ~0ms wasted) → cooldown expires → `HALF_OPEN` (one test probe) → success → `CLOSED` again, or failure → back to `OPEN`
- Proved live: a request during `OPEN` took ~1522ms (pure DB latency only) vs. ~2527ms in v2's equivalent scenario (where every request re-paid the connect-timeout tax)
- Full lifecycle watched end to end: tripped it, watched a failed recovery probe re-open it, brought Redis back, watched a successful probe close it again, confirmed normal MISS then HIT afterward
- A real bug found and fixed along the way: `/api/cache-state` crashed (`WRONGTYPE`) on any non-string Redis key; fixed by catching `ResponseError` per key instead of letting one bad key crash the whole status panel
- Scoped out (tested, not faithfully demoable with `fakeredis`): Redis's own `maxmemory-policy` eviction (config accepted but silently not enforced — tested with a 1-byte limit, nothing evicted), persistence (no disk involved at all), replication/clustering (single-node emulation only), pub/sub (messages didn't reliably arrive in testing). All four need the real `redis-server` binary (Docker/WSL — neither installed here) to demo honestly.

## Up next (not yet built)
- Redis data structures (lists/hashes/sets) — confirmed these DO work correctly through `fakeredis`, just not wired into the UI yet
- The real-Redis-only topics above, if/when Docker or WSL gets installed
- Sliding/rolling TTL as a toggle (discussed in v2, not yet built)
- Load balancing — multiple visible "server" tiles, a routing algorithm, kill one and watch the others keep serving
- Scaling — a button to add/remove a server tile live
