"""
VERSION 1 — Caching: cache-aside pattern, in-memory, single process.

=====================================================================
TOPICS COVERED IN THIS VERSION
=====================================================================
  1. Cache-aside (lazy-loading) read pattern: check cache -> miss ->
     read "database" -> populate cache -> return.
  2. TTL (time-to-live) expiry, tracked MANUALLY in Python
     (store a timestamp, compare against time.time() on every read).
  3. Explicit invalidation (a "Clear cache" button -> delete from dict).
  4. "Stopping a process is not pausing it" — restarting this script
     wipes the cache, because it's just a dict in this process's RAM.
  5. Simulated slow database via time.sleep(), to make the hit/miss
     latency difference actually felt, not just claimed.

WHAT THIS VERSION DOES **NOT** YET HAVE (see v2):
  - No real separate cache process — "the cache" is just a Python
    dict living inside this same Flask process. Nothing here can be
    killed independently of anything else.
  - No handling at all for "what if the cache disappears" — moot in
    this version, since the cache can't disappear without the whole
    app disappearing with it.
  - No native TTL support — we're reimplementing by hand what Redis
    would give us for free (see v2's SETEX/TTL commands).

This file is a frozen SNAPSHOT — a complete, runnable record of what
"caching, step 1" looked like. It won't be edited going forward; new
concepts get their own vN file. See ../VERSIONS.md for the full index.

Run it with:  python v1_cache_aside_dict.py
Then open:    http://127.0.0.1:5050
=====================================================================
"""

import time
import threading
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
# The "cache" — just a dict living in this process's RAM. Keys expire
# after TTL_SECONDS so you can watch an entry go stale on its own, without
# clicking anything.
# -----------------------------------------------------------------------
TTL_SECONDS = 12
cache: dict = {}            # product_id -> {"value": ..., "stored_at": epoch_seconds}
cache_lock = threading.Lock()


def read_from_database(product_id: str):
    time.sleep(DB_LATENCY_SECONDS)       # <-- this is the "slow" part, felt for real
    return PRODUCTS.get(product_id)


@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/fetch/<product_id>")
def fetch(product_id):
    now = time.time()

    with cache_lock:
        entry = cache.get(product_id)
        is_fresh = entry is not None and (now - entry["stored_at"]) < TTL_SECONDS

    start = time.time()

    if is_fresh:
        # CACHE HIT: return immediately, database never touched.
        value = entry["value"]
        elapsed_ms = round((time.time() - start) * 1000, 1)
        return jsonify(source="cache", product=value, elapsed_ms=elapsed_ms)

    # CACHE MISS (never cached, or expired): pay the slow database cost,
    # then store the fresh result for next time ("cache-aside" pattern).
    value = read_from_database(product_id)
    elapsed_ms = round((time.time() - start) * 1000, 1)

    with cache_lock:
        cache[product_id] = {"value": value, "stored_at": time.time()}

    return jsonify(source="database", product=value, elapsed_ms=elapsed_ms)


@app.route("/api/cache-state")
def cache_state():
    now = time.time()
    with cache_lock:
        items = []
        for product_id, entry in cache.items():
            age = now - entry["stored_at"]
            remaining = max(0, TTL_SECONDS - age)
            items.append({
                "product_id": product_id,
                "name": entry["value"]["name"],
                "age_seconds": round(age, 1),
                "ttl_remaining": round(remaining, 1),
                "expired": remaining <= 0,
            })
    return jsonify(items=items, ttl_seconds=TTL_SECONDS)


@app.route("/api/clear-cache", methods=["POST"])
def clear_cache():
    product_id = request.json.get("product_id") if request.is_json else None
    with cache_lock:
        if product_id:
            cache.pop(product_id, None)
        else:
            cache.clear()
    return jsonify(ok=True)


INDEX_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Caching — v1: Cache-aside (in-memory dict)</title>
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
  .pending { background:#2a2e37; border:1px solid #3a4258; }
  .cache-item { display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px solid #2a2e37; font-size:13px; }
  .bar-bg { background:#2a2e37; border-radius:4px; height:6px; width:100px; overflow:hidden; }
  .bar-fill { background:#4caf87; height:100%; }
  #log { font-family: Consolas, monospace; font-size:12px; color:#9aa0a6; max-height:220px; overflow-y:auto; line-height:1.6; }
  .clear-btn { background:#512; color:#f0a0a0; border:1px solid #733; border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer; }
</style>
</head>
<body>

<h1>Caching — v1: Cache-aside (in-memory dict)</h1>
<div class="sub">Click a product. Watch whether it comes from the slow "database" or the fast "cache". Cache entries expire after 12s on their own.</div>

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
      <strong style="font-size:13px;">Cache contents (TTL = 12s)</strong>
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
  const res = await fetch(`/api/fetch/${id}`);
  const data = await res.json();
  const totalMs = Math.round(performance.now() - t0);

  if (data.source === "cache") {
    resultDiv.className = "hit";
    resultDiv.innerHTML = `⚡ CACHE HIT — <b>${data.product.name}</b> (${data.product.price})<br>
      Served from memory. Round-trip: ${totalMs}ms.`;
    log(`"${id}" -> HIT (${totalMs}ms)`);
  } else {
    resultDiv.className = "miss";
    resultDiv.innerHTML = `🐢 CACHE MISS — <b>${data.product.name}</b> (${data.product.price})<br>
      Had to ask the database (simulated 1.5s delay). Round-trip: ${totalMs}ms. Now stored in cache.`;
    log(`"${id}" -> MISS, hit database (${totalMs}ms)`);
  }
  refreshCacheList();
}

async function refreshCacheList() {
  const res = await fetch("/api/cache-state");
  const data = await res.json();
  const el = document.getElementById("cache-list");
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
