"""
VERSION 2 — Caching: a REAL separate Redis process, plus resilience.

=====================================================================
TOPICS COVERED IN THIS VERSION (on top of everything in v1)
=====================================================================
  1. The cache is now a REAL, separate OS process (redis_server.py,
     a few folders up, via `fakeredis.TcpFakeServer` — a pure-Python
     server that speaks the actual Redis wire protocol on the real
     Redis port, 6379), not a dict living inside this same process.
  2. Process independence: redis_server.py can be killed/restarted
     completely independently of this Flask app — proved live by
     stopping ONLY Redis and watching Flask stay up on its own port.
  3. Native Redis TTL (SETEX / TTL commands) replaces the manual
     "store a timestamp and do the math" approach from v1 — Redis
     expires the key itself, we just ask it "how long left?".
  4. FAILURE MODE 1 — crash: with no error handling around the Redis
     calls, killing Redis turns every /api/fetch request into a raw,
     unhandled exception -> Flask 500 error. Confirmed live via the
     server log (redis.exceptions.TimeoutError traceback).
  5. FAILURE MODE 2 — graceful degradation (the fix): wrap the Redis
     calls in try/except redis.exceptions.RedisError. If Redis is
     unreachable, skip it and read straight from the database instead
     of crashing — the user still gets a correct answer, just slower.
  6. Hidden retry/timeout layers in client libraries: measured LIVE
     that a "1 second" socket_connect_timeout actually took 15-25
     real seconds to fail, because redis-py has its OWN internal
     connection-retry logic sitting on top of that timeout. Fixed by
     passing retry=Retry(NoBackoff(), 0) explicitly — confirmed this
     brings failure time down to the configured ~1s. Lesson: a
     configured timeout value does not bound total wait time unless
     you also account for (and usually disable) the library's own
     retry policy.
  7. Absolute TTL (discussed, not yet built as a toggle): this
     version's TTL only resets on WRITE (cache miss), never on READ
     (cache hit) — repeated hits do NOT push the expiry back out.
     Sliding/rolling TTL (refresh-on-read, e.g. via Redis's GETEX
     command) is a different, equally valid pattern for different
     data (e.g. session expiry) — not implemented here yet.

WHAT THIS VERSION DOES **NOT** YET HAVE:
  - Sliding/rolling TTL (see point 7 above).
  - A circuit breaker — right now EVERY request while Redis is down
    pays the full ~1s connect-timeout tax again, even though the
    previous request already proved Redis is down. A circuit breaker
    would "trip" after the first failure and skip straight to the
    database for a cooldown window, at zero extra cost per request.
  - A real (non-fake) database — PRODUCTS is still a plain dict with
    a time.sleep() standing in for real disk/network latency.
  - Anything about load balancing or scaling (separate topics/files).

DEPENDENCY: this file needs redis_server.py (one folder up) running
first, on port 6379, or every fetch will hit the "Redis is down" path.

This file is a frozen SNAPSHOT at this point in the lesson. It won't
be edited further — new concepts get their own vN file from here.
See ../VERSIONS.md for the full index and ../app.py for the current,
actively-edited version (always equal to the newest vN).

Run it with (from the live_demo/ folder, redis_server.py first):
    python redis_server.py
    python versions/v2_real_redis_resilience.py
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
# The cache — now a REAL Redis client, talking over the network (even
# though that network is just 127.0.0.1) to a SEPARATE process
# (redis_server.py) on port 6379. This app no longer holds the cached
# data itself at all — it just asks Redis for it. That's the whole point:
# this process and the cache process can now be started, stopped, and
# killed completely independently of each other.
#
# decode_responses=True -> get back normal Python strings instead of raw
# bytes, so we don't have to .decode() everything by hand.
#
# The short timeouts matter for the demo: without them, if Redis is down,
# this app would hang for a long time trying to connect before giving up.
#
# retry=Retry(NoBackoff(), 0) matters JUST AS MUCH as the timeouts, and is
# easy to miss: by default, redis-py retries a failed connection attempt
# internally a few times before raising, and those retries don't cleanly
# respect socket_connect_timeout either. Measured live on this machine:
# WITHOUT this line, a dead Redis took 15-25 SECONDS to fail. WITH it,
# exactly ~1 second (matching socket_connect_timeout below). A timeout
# value alone does not bound total wait time unless retries are also
# accounted for.
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


@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/fetch/<product_id>")
def fetch(product_id):
    # GRACEFUL DEGRADATION: Redis is an OPTIMIZATION, not the source of
    # truth. If it's unreachable, the correct behaviour is "fall back to
    # the database and keep serving users" — NOT "crash the whole
    # request". So we wrap every Redis call in its own try/except and
    # track whether Redis is usable at all this request.
    start = time.time()
    redis_ok = True
    cached_json = None

    try:
        cached_json = redis_client.get(product_id)   # real Redis GET command, over the network
    except redis.exceptions.RedisError:
        redis_ok = False   # Redis is down/unreachable — note it, don't crash

    if cached_json is not None:
        # CACHE HIT: Redis still had it (and Redis itself enforces the
        # TTL we set below — it deletes expired keys on its own, we don't
        # track expiry ourselves at all anymore).
        value = json.loads(cached_json)
        elapsed_ms = round((time.time() - start) * 1000, 1)
        return jsonify(source="cache", product=value, elapsed_ms=elapsed_ms)

    # CACHE MISS (or Redis is down, which looks the same from here: "no
    # value available") — pay the slow database cost either way. This is
    # what makes it "graceful": the user still gets a correct answer, just
    # without the speed boost, instead of an error page.
    value = read_from_database(product_id)
    elapsed_ms = round((time.time() - start) * 1000, 1)

    if redis_ok:
        try:
            redis_client.setex(product_id, TTL_SECONDS, json.dumps(value))
        except redis.exceptions.RedisError:
            redis_ok = False   # it died between the GET and here — still fine, just skip caching it

    source = "database" if redis_ok else "database (redis is down — degraded mode)"
    return jsonify(source=source, product=value, elapsed_ms=elapsed_ms, redis_ok=redis_ok)


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
        ttl_remaining = redis_client.ttl(key)     # Redis tells us directly, no math needed
        raw = redis_client.get(key)
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
<title>Caching — v2: Real Redis + resilience</title>
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
</style>
</head>
<body>

<h1>Caching — v2: Real Redis + resilience</h1>
<div class="sub">Click a product. Watch whether it comes from the slow "database" or the real, separate Redis process. Cache entries expire after 12s (Redis's own TTL).</div>
<div class="sub"><span id="redis-status"></span></div>

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
refreshCacheList();
</script>

</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
