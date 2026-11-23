"""
VERSION 3 — Caching: circuit breaker (stop re-trying a known-dead Redis).

=====================================================================
TOPICS COVERED IN THIS VERSION (on top of everything in v1 + v2)
=====================================================================
  1. The circuit breaker pattern — three states, same names used in
     every real circuit-breaker library (Polly in .NET, resilience4j
     in Java, pybreaker in Python):
       CLOSED     - normal operation, try Redis every time
       OPEN       - Redis failed enough times; stop even TRYING to
                    reach it for a cooldown window, go straight to
                    the database, 0ms wasted on a doomed connection
       HALF_OPEN  - cooldown just expired; let exactly ONE request
                    probe Redis again. Success -> CLOSED. Failure ->
                    OPEN again, with a fresh cooldown.
  2. The problem it solves, proven live: in v2, EVERY request during
     an outage re-paid the ~1s connect-timeout tax individually.
     Measured here: after the breaker trips OPEN, a request during
     the cooldown completed in ~1522ms (pure database latency, 0ms
     spent even attempting Redis) vs. ~2527ms in v2's equivalent
     scenario.
  3. Full lifecycle verified live, end to end: CLOSED -> (2 failures)
     -> OPEN -> (cooldown elapses) -> HALF_OPEN probe fails -> OPEN
     again with a fresh cooldown -> Redis restarted -> HALF_OPEN
     probe succeeds -> back to CLOSED -> normal MISS then HIT.
  4. A real bug found and fixed while building this: /api/cache-state
     crashed with `WRONGTYPE` the moment a non-string Redis key (e.g.
     a list or hash) existed, because it called a plain GET on every
     key returned by KEYS *. Fixed by catching
     redis.exceptions.ResponseError per-key and skipping it, instead
     of letting one odd key take down the whole status panel.

WHAT THIS VERSION DOES **NOT** YET HAVE:
  - Sliding/rolling TTL (still absolute-TTL only, same as v2).
  - Redis's OWN eviction policies (maxmemory-policy / LRU-LFU inside
    Redis itself) — investigated for v3/v4 and found NOT demoable
    with fakeredis: CONFIG SET maxmemory is accepted but silently
    not enforced (tested: wrote 20 keys with maxmemory=1 byte, all
    20 survived). Needs the real redis-server binary (Docker/WSL,
    neither installed on this machine) to show honestly.
  - Redis persistence (RDB/AOF) or replication/clustering — same
    reason: fakeredis is pure in-memory, single-node only, no disk
    involved at all, so there's nothing real to demonstrate.
  - Redis pub/sub — commands are accepted but a quick test showed
    published messages not actually reaching a subscriber through
    fakeredis; not trustworthy enough to build a demo on.
  - Non-string Redis data structures (lists/hashes/sets) in the UI —
    confirmed these DO work correctly through fakeredis (LPUSH/HSET/
    SADD all behave correctly), just not wired into this demo yet.

DEPENDENCY: needs redis_server.py (one folder up) running on 6379.

This file is a frozen SNAPSHOT at this point in the lesson. It won't
be edited further — new concepts get their own vN file from here.
See ../VERSIONS.md for the full index and ../app.py for the current,
actively-edited version (always equal to the newest vN).

Run it with (from the live_demo/ folder, redis_server.py first):
    python redis_server.py
    python versions/v3_circuit_breaker.py
Then open:    http://127.0.0.1:5050
=====================================================================
"""

import time
import json
import redis
from flask import Flask, jsonify, request

app = Flask(__name__)

# -----------------------------------------------------------------------
# The "database" — slow on purpose (simulated disk/network latency).
# -----------------------------------------------------------------------
DB_LATENCY_SECONDS = 1.5

PRODUCTS = {
    "laptop":      {"name": "Laptop",      "price": "$999"},
    "phone":       {"name": "Phone",       "price": "$699"},
    "headphones":  {"name": "Headphones",  "price": "$199"},
}

# -----------------------------------------------------------------------
# The cache — a REAL Redis client, talking over the network (even though
# that network is just 127.0.0.1) to a SEPARATE process (redis_server.py)
# on port 6379.
#
# retry=Retry(NoBackoff(), 0): disables redis-py's own internal connection
# retries, which otherwise silently turn a configured 1s timeout into a
# real 15-25s wait (measured live in v2). Without this, the circuit
# breaker below would still "work" logically, but every individual Redis
# attempt would itself be needlessly slow.
# -----------------------------------------------------------------------
TTL_SECONDS = 12
redis_client = redis.Redis(
    host="127.0.0.1",
    port=6379,
    decode_responses=True,
    socket_connect_timeout=1,
    socket_timeout=1,
    retry=redis.retry.Retry(redis.backoff.NoBackoff(), 0),
    retry_on_error=[],
)


def read_from_database(product_id: str):
    time.sleep(DB_LATENCY_SECONDS)       # <-- this is the "slow" part, felt for real
    return PRODUCTS.get(product_id)


# =============================================================================
# CIRCUIT BREAKER — stop paying the connect-timeout tax on EVERY request
# once we already know Redis is down.
#
# Without this (v2's behaviour): every single request, for as long as Redis
# stays down, re-attempts the connection and re-pays the ~1s timeout before
# falling back to the database. Request #2 has no memory of request #1's
# failure.
#
# Three states, same names used in every real circuit-breaker library
# (Polly in .NET, resilience4j in Java, pybreaker in Python):
#   CLOSED     - normal operation. Try Redis every time.
#   OPEN       - Redis has failed enough times. Stop even TRYING to reach
#                it for COOLDOWN_SECONDS. Go straight to the database,
#                0ms wasted on a connection we already know is dead.
#   HALF_OPEN  - cooldown just expired. Let exactly ONE request try Redis
#                again, as a test probe. Success -> back to CLOSED.
#                Failure -> back to OPEN for another full cooldown.
# =============================================================================
FAILURE_THRESHOLD = 2       # consecutive failures before the breaker trips OPEN
COOLDOWN_SECONDS = 8        # how long OPEN skips Redis entirely before testing again

circuit_state = "CLOSED"
circuit_failure_count = 0
circuit_opened_at = None    # epoch seconds when we tripped to OPEN


def circuit_may_attempt_redis() -> bool:
    """Decide, BEFORE touching the network, whether this request should even
    try Redis. This is what saves the wasted time — we never call
    redis_client.get() at all while the breaker is OPEN and still cooling
    down, which is exactly what v2 was missing."""
    global circuit_state

    if circuit_state == "OPEN":
        if time.time() - circuit_opened_at >= COOLDOWN_SECONDS:
            circuit_state = "HALF_OPEN"   # cooldown elapsed -> allow one probe
            return True
        return False                      # still cooling down -> skip Redis, 0ms wasted
    return True                           # CLOSED or HALF_OPEN -> attempt as normal


def circuit_record_success():
    """A Redis call just succeeded. Any failure streak is forgiven, and if
    we were testing recovery (HALF_OPEN), we're confirmed healthy again."""
    global circuit_state, circuit_failure_count
    circuit_state = "CLOSED"
    circuit_failure_count = 0


def circuit_record_failure():
    """A Redis call just failed. Count it, and trip the breaker OPEN either
    once we hit the threshold, or immediately if our single HALF_OPEN probe
    failed (no point allowing more probes right away)."""
    global circuit_state, circuit_failure_count, circuit_opened_at
    circuit_failure_count += 1
    if circuit_state == "HALF_OPEN" or circuit_failure_count >= FAILURE_THRESHOLD:
        circuit_state = "OPEN"
        circuit_opened_at = time.time()


@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/fetch/<product_id>")
def fetch(product_id):
    # GRACEFUL DEGRADATION + CIRCUIT BREAKER: Redis is an OPTIMIZATION, not
    # the source of truth. If it's unreachable, fall back to the database —
    # but ALSO remember that it's unreachable, so we stop wasting time
    # trying it on every subsequent request until the cooldown passes.
    start = time.time()
    redis_ok = True
    cached_json = None
    circuit_skipped = False

    if circuit_may_attempt_redis():
        try:
            cached_json = redis_client.get(product_id)   # real Redis GET command, over the network
            circuit_record_success()
        except redis.exceptions.RedisError:
            redis_ok = False
            circuit_record_failure()
    else:
        # Breaker is OPEN and still cooling down: we deliberately do NOT
        # call redis_client.get() at all. This is the entire point — zero
        # network time spent on a connection we already know is dead.
        redis_ok = False
        circuit_skipped = True

    if cached_json is not None:
        # CACHE HIT: Redis still had it (and Redis itself enforces the
        # TTL we set below — it deletes expired keys on its own, we don't
        # track expiry ourselves at all anymore).
        value = json.loads(cached_json)
        elapsed_ms = round((time.time() - start) * 1000, 1)
        return jsonify(source="cache", product=value, elapsed_ms=elapsed_ms)

    # CACHE MISS (or Redis is down/skipped, which looks the same from here:
    # "no value available") — pay the slow database cost either way.
    value = read_from_database(product_id)
    elapsed_ms = round((time.time() - start) * 1000, 1)

    if redis_ok:
        try:
            redis_client.setex(product_id, TTL_SECONDS, json.dumps(value))
            circuit_record_success()
        except redis.exceptions.RedisError:
            redis_ok = False   # it died between the GET and here — still fine, just skip caching it
            circuit_record_failure()

    if circuit_skipped:
        source = "database (circuit OPEN — Redis skipped entirely, no time wasted)"
    elif not redis_ok:
        source = "database (redis is down — degraded mode)"
    else:
        source = "database"
    return jsonify(
        source=source, product=value, elapsed_ms=elapsed_ms,
        redis_ok=redis_ok, circuit_skipped=circuit_skipped,
    )


@app.route("/api/circuit-state")
def circuit_state_endpoint():
    remaining = 0
    if circuit_state == "OPEN" and circuit_opened_at is not None:
        remaining = max(0, round(COOLDOWN_SECONDS - (time.time() - circuit_opened_at), 1))
    return jsonify(state=circuit_state, failure_count=circuit_failure_count, cooldown_remaining=remaining)


@app.route("/api/cache-state")
def cache_state():
    # This endpoint DOES catch errors, so the side panel can clearly show
    # "Redis is down" instead of also crashing — the goal is for the
    # failure to be visible and explained, not merely a broken page.
    try:
        redis_client.ping()           # the standard "are you alive?" Redis command
    except redis.exceptions.RedisError:
        return jsonify(redis_up=False, items=[], ttl_seconds=TTL_SECONDS)

    items = []
    for key in redis_client.keys("*"):
        # Defensive: GET only works on string-type keys. If a list/hash/set
        # key ever ends up in Redis (we'll do this deliberately later when
        # exploring data structures), a plain GET raises WRONGTYPE — don't
        # let one odd key crash the whole status panel.
        try:
            ttl_remaining = redis_client.ttl(key)     # Redis tells us directly, no math needed
            raw = redis_client.get(key)
        except redis.exceptions.ResponseError:
            continue   # not a string-type key (or similar) — just skip it in this panel
        if raw is None or ttl_remaining is None or ttl_remaining < 0:
            continue
        value = json.loads(raw)
        items.append({
            "product_id": key,
            "name": value["name"],
            "ttl_remaining": ttl_remaining,
            "expired": False,
        })
    return jsonify(redis_up=True, items=items, ttl_seconds=TTL_SECONDS)


@app.route("/api/clear-cache", methods=["POST"])
def clear_cache():
    product_id = request.json.get("product_id") if request.is_json else None
    if product_id:
        redis_client.delete(product_id)     # real Redis DEL command
    else:
        redis_client.flushdb()              # real Redis FLUSHDB command: wipe everything
    return jsonify(ok=True)


INDEX_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Caching — v3: Circuit breaker</title>
<style>
  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; background:#0f1115; color:#e8e8e8; margin:0; padding:24px; }
  h1 { font-size:20px; margin-bottom:4px; }
  .sub { color:#9aa0a6; margin-bottom:20px; font-size:14px; }
  .row { display:flex; gap:16px; flex-wrap:wrap; }
  .panel { background:#181b21; border:1px solid #2a2e37; border-radius:10px; padding:16px; flex:1; min-width:280px; }
  .buttons button {
      background:#2a3142; color:#e8e8e8; border:1px solid #3a4258; border-radius:8px;
      padding:10px 16px; margin:4px 6px 4px 0; cursor:pointer; font-size:14px;
  }
  .buttons button:hover { background:#3a4258; }
  #result { margin-top:14px; padding:14px; border-radius:8px; font-size:15px; min-height:20px; transition:background 0.2s; }
  .hit  { background:#123b24; border:1px solid #1f6b3e; }
  .miss { background:#3b1414; border:1px solid #6b1f1f; }
  .degraded { background:#3b2c14; border:1px solid #6b4f1f; }
  .pending { background:#2a2e37; border:1px solid #3a4258; }
  .cache-item { display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px solid #2a2e37; font-size:13px; }
  .bar-bg { background:#2a2e37; border-radius:4px; height:6px; width:100px; overflow:hidden; }
  .bar-fill { background:#4caf87; height:100%; }
  #log { font-family: Consolas, monospace; font-size:12px; color:#9aa0a6; max-height:220px; overflow-y:auto; line-height:1.6; }
  .clear-btn { background:#512; color:#f0a0a0; border:1px solid #733; border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer; }
  .status-dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
  .status-up { background:#4caf87; }
  .status-down { background:#e05c5c; }
  .redis-banner { background:#3b1414; border:1px solid #6b1f1f; border-radius:8px; padding:10px 12px; margin-bottom:10px; font-size:13px; }
  .circuit-badge { display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px; font-weight:600; margin-left:10px; }
  .circuit-closed { background:#123b24; color:#6fd9a4; border:1px solid #1f6b3e; }
  .circuit-open { background:#3b1414; color:#f0a0a0; border:1px solid #6b1f1f; }
  .circuit-half { background:#3b2c14; color:#f0c884; border:1px solid #6b4f1f; }
</style>
</head>
<body>

<h1>Caching — v3: Circuit breaker</h1>
<div class="sub">Click a product. Watch whether it comes from the slow "database" or the real, separate Redis process. Cache entries expire after 12s (Redis's own TTL).</div>
<div class="sub"><span id="redis-status"></span><span id="circuit-status"></span></div>

<div class="row">
  <div class="panel">
    <div class="buttons" id="buttons"></div>
    <div id="result" class="pending">Click a product to fetch it.</div>
    <div style="margin-top:16px;">
      <strong style="font-size:13px;">Request log</strong>
      <div id="log"></div>
    </div>
  </div>

  <div class="panel">
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <strong style="font-size:13px;">Redis contents (real SETEX / TTL / GET)</strong>
      <button class="clear-btn" onclick="clearCache()">Clear all cache</button>
    </div>
    <div id="cache-list" style="margin-top:10px;"></div>
  </div>
</div>

<script>
const products = ["laptop", "phone", "headphones"];
const buttonsDiv = document.getElementById("buttons");
products.forEach(p => {
  const b = document.createElement("button");
  b.textContent = "Fetch: " + p[0].toUpperCase() + p.slice(1);
  b.onclick = () => fetchProduct(p);
  buttonsDiv.appendChild(b);
});

function log(msg) {
  const el = document.getElementById("log");
  const line = document.createElement("div");
  const t = new Date().toLocaleTimeString();
  line.textContent = `[${t}] ${msg}`;
  el.prepend(line);
}

async function fetchProduct(id) {
  const resultDiv = document.getElementById("result");
  resultDiv.className = "pending";
  resultDiv.textContent = `Checking cache for "${id}"...`;
  log(`Request: fetch "${id}"`);

  const t0 = performance.now();
  let res, data;
  try {
    res = await fetch(`/api/fetch/${id}`);
  } catch (networkErr) {
    // The browser couldn't even complete the HTTP request (e.g. the Flask
    // process itself is gone). Different failure from "Redis is down".
    resultDiv.className = "miss";
    resultDiv.innerHTML = `💥 REQUEST FAILED — the app server itself didn't respond.`;
    log(`"${id}" -> network error: ${networkErr}`);
    return;
  }
  const totalMs = Math.round(performance.now() - t0);

  if (!res.ok) {
    // Flask responded, but with an error status — this is exactly what
    // happens when redis_client.get()/.setex() raises inside fetch()
    // and nothing caught it: Flask turns the unhandled exception into a
    // 500 Internal Server Error.
    resultDiv.className = "miss";
    resultDiv.innerHTML = `🔴 CRASHED — HTTP ${res.status}.<br>
      The app tried to talk to Redis and got no response, and nobody
      caught that error. This is what "the cache going down with no
      graceful fallback" looks like to a real user.`;
    log(`"${id}" -> CRASHED (HTTP ${res.status}) after ${totalMs}ms`);
    refreshCacheList();
    return;
  }

  data = await res.json();

  if (data.source === "cache") {
    resultDiv.className = "hit";
    resultDiv.innerHTML = `⚡ CACHE HIT — <b>${data.product.name}</b> (${data.product.price})<br>
      Served from the separate Redis process. Round-trip: ${totalMs}ms.`;
    log(`"${id}" -> HIT (${totalMs}ms)`);
  } else if (data.circuit_skipped) {
    resultDiv.className = "degraded";
    resultDiv.innerHTML = `⛔ CIRCUIT OPEN — <b>${data.product.name}</b> (${data.product.price})<br>
      The breaker already knows Redis is down from an earlier failure, so
      it skipped trying Redis ENTIRELY this time — straight to the
      database. Round-trip: ${totalMs}ms (no connect-timeout tax paid).`;
    log(`"${id}" -> circuit OPEN, Redis skipped entirely (${totalMs}ms)`);
  } else if (data.redis_ok === false) {
    resultDiv.className = "degraded";
    resultDiv.innerHTML = `🟠 DEGRADED MODE — <b>${data.product.name}</b> (${data.product.price})<br>
      Redis is down, but the app CAUGHT that and fell back to the database
      instead of crashing. Slower (${totalMs}ms), but it still worked.
      Nothing got cached this time.`;
    log(`"${id}" -> degraded (redis down, served from DB) (${totalMs}ms)`);
  } else {
    resultDiv.className = "miss";
    resultDiv.innerHTML = `🐢 CACHE MISS — <b>${data.product.name}</b> (${data.product.price})<br>
      Had to ask the database (simulated 1.5s delay). Round-trip: ${totalMs}ms. Now stored in Redis.`;
    log(`"${id}" -> MISS, hit database (${totalMs}ms)`);
  }
  refreshCacheList();
  refreshCircuitState();
}

async function refreshCircuitState() {
  const el = document.getElementById("circuit-status");
  let data;
  try {
    const res = await fetch("/api/circuit-state");
    data = await res.json();
  } catch (e) {
    return;
  }
  const cls = data.state === "CLOSED" ? "circuit-closed" : (data.state === "OPEN" ? "circuit-open" : "circuit-half");
  let label = `Circuit: ${data.state}`;
  if (data.state === "OPEN") label += ` (retrying in ${data.cooldown_remaining}s)`;
  if (data.state !== "CLOSED") label += ` [${data.failure_count} failures]`;
  el.innerHTML = `<span class="circuit-badge ${cls}">${label}</span>`;
}

async function refreshCacheList() {
  const statusEl = document.getElementById("redis-status");
  const el = document.getElementById("cache-list");
  let data;
  try {
    const res = await fetch("/api/cache-state");
    data = await res.json();
  } catch (e) {
    statusEl.innerHTML = '<span class="status-dot status-down"></span>App server unreachable.';
    return;
  }

  if (!data.redis_up) {
    statusEl.innerHTML = '<span class="status-dot status-down"></span>Redis: DOWN (connection refused)';
    el.innerHTML = '<div class="redis-banner">🔴 The Flask app cannot reach the Redis process on port 6379. Every fetch will now either crash or be forced to hit the database directly, depending on whether error-handling has been added yet.</div>';
    return;
  }
  statusEl.innerHTML = '<span class="status-dot status-up"></span>Redis: UP (127.0.0.1:6379)';

  if (data.items.length === 0) {
    el.innerHTML = '<div style="color:#666; font-size:13px;">Cache is empty.</div>';
    return;
  }
  el.innerHTML = data.items.map(item => {
    const pct = Math.round((item.ttl_remaining / data.ttl_seconds) * 100);
    return `<div class="cache-item">
      <span>${item.name}</span>
      <span class="bar-bg"><span class="bar-fill" style="width:${pct}%"></span></span>
      <span>${item.ttl_remaining}s left</span>
    </div>`;
  }).join("");
}

async function clearCache() {
  await fetch("/api/clear-cache", { method: "POST" });
  log("Cache cleared manually (explicit invalidation).");
  refreshCacheList();
}

// Keep the TTL bars ticking down live, even without clicking anything.
setInterval(refreshCacheList, 1000);
setInterval(refreshCircuitState, 1000);
refreshCacheList();
refreshCircuitState();
</script>

</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
