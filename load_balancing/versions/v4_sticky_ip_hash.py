"""
VERSION 4 — Load balancing: IP hashing / sticky sessions, side by side
with least connections.

=====================================================================
TOPICS COVERED IN THIS VERSION (on top of v1 + v2 + v3)
=====================================================================
  1. IP hashing / sticky sessions: hash a CLIENT identifier into a
     number, use it to pick a backend — same client ID always produces
     the same hash, so the same client always lands on the same
     backend. No counters, no shared mutable state needed at all
     (compare to least-connections' active_connections dict) — it's a
     pure function of the input.
  2. Why we hash a simulated "client ID" instead of the real IP: every
     request in this demo originates from the same machine (127.0.0.1),
     so hashing the real IP would always pick ONE backend for everyone.
     The UI lets you pick between 3 simulated clients instead — same
     mechanism real IP hashing uses, just a stand-in for "several real
     users" that this local setup can't produce on its own.
  3. A real, easy-to-miss correctness bug AVOIDED on purpose: Python's
     built-in hash() for strings is RANDOMIZED per process run (a
     security feature against hash-flooding attacks) — the same
     string can hash to a different number every time the process
     restarts, which would silently break stickiness across restarts.
     Used hashlib.md5 instead: stable, same input -> same output,
     always, regardless of process restarts or which machine runs it.
  4. Verified live, with the sticky-history panel proving it directly
     in the UI (not just logs): client-1 always -> Server A, client-2
     always -> Server C, client-3 always -> Server C, across multiple
     separate requests each, zero deviation.
  5. A real limitation observed, not a bug: client-2 and client-3
     hashed to the SAME backend (C). With only 3 backends (3 "buckets"),
     collisions like this are expected by the pigeonhole principle, not
     a flaw — this is one of the standard, real motivations for
     CONSISTENT hashing in larger/more dynamic clusters (not built
     here), which spreads collision risk more evenly and, more
     importantly, minimizes how many clients get reassigned when the
     backend list itself changes.
  6. Deliberately NO retry-on-failure for this route (unlike
     /api/request's least-connections path) — named explicitly as a
     real design tension: if a client's assigned backend goes down, do
     you break stickiness (route elsewhere, losing the guarantee) or
     fail outright? This version fails outright, to keep the concept
     uncluttered; real systems often reach for consistent hashing
     specifically to soften this tradeoff.

WHAT THIS VERSION DOES **NOT** YET HAVE:
  - Consistent hashing (the standard refinement over plain modulo
    hashing, for both collision spread and minimal reassignment on
    backend changes) — named above, not built.
  - No fallback when a client's sticky backend is down (see point 6).
  - Weighted algorithms (treating backends as unequal capacity).

DEPENDENCY: needs all three backend_server.py copies running first:
    python ../backend_server.py A 6101
    python ../backend_server.py B 6102 2
    python ../backend_server.py C 6103

This file is a frozen SNAPSHOT at this point in the lesson. It won't
be edited further — new concepts get their own vN file from here.
See ../VERSIONS.md for the full index and ../load_balancer.py for
the current, actively-edited version (always equal to the newest vN).

Run it with:
    python versions/v4_sticky_ip_hash.py
Then open:    http://127.0.0.1:6100
=====================================================================
"""

import time
import hashlib
import threading
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

BACKENDS = [
    {"id": "A", "url": "http://127.0.0.1:6101"},
    {"id": "B", "url": "http://127.0.0.1:6102"},
    {"id": "C", "url": "http://127.0.0.1:6103"},
]

# The entire "state" least-connections needs: a live count of IN-FLIGHT
# (started, not yet finished) requests per backend. This is NOT the same
# as each backend's own request_count (that's a lifetime total); this is
# "how many am I waiting on RIGHT NOW", and it changes every time a
# request starts or finishes.
active_connections = {b["id"]: 0 for b in BACKENDS}
active_lock = threading.Lock()

# TIE-BREAKER counter. Found live: under light/sequential traffic, EVERY
# click sees a 3-way tie (0 active everywhere), and plain min() always
# resolves a tie to the first list item — meaning Server A would win
# every single tie, forever, and B/C would never be picked at all except
# under genuine heavy concurrency. Real load balancers (e.g. nginx's
# least_conn) avoid this by breaking ties with round robin instead of
# always favoring the same backend. Separate from active_connections —
# this only ever decides WHICH of several EQUALLY-idle backends goes next.
_tiebreak_counter = 0


def pick_least_connections(remaining):
    global _tiebreak_counter
    min_active = min(active_connections[b["id"]] for b in remaining)
    tied = [b for b in remaining if active_connections[b["id"]] == min_active]
    if len(tied) == 1:
        return tied[0]
    backend = tied[_tiebreak_counter % len(tied)]
    _tiebreak_counter += 1
    return backend


# =============================================================================
# IP HASHING / STICKY SESSIONS — the SAME client always lands on the SAME
# backend, as long as the backend list doesn't change. Useful when a
# backend holds something locally for that client (a local in-memory
# session or cache — exactly what our ORIGINAL v1 caching demo was,
# before we moved to shared Redis). No active_connections, no counters,
# no memory needed on the load balancer at all — the hash alone decides.
#
# In real life you'd hash the client's IP address. Every request in THIS
# demo comes from the same machine (127.0.0.1), so hashing the real IP
# would always pick ONE backend for everyone — nothing to demonstrate.
# Instead we hash a CLIENT ID the UI lets you choose (simulating several
# different real users), using the exact same mechanism real IP hashing
# would use.
# =============================================================================
def pick_backend_by_hash(client_id: str):
    # IMPORTANT: Python's built-in hash() is RANDOMIZED per process run for
    # strings (a security feature against hash-flooding attacks) — the
    # SAME string can hash differently every time you restart the process,
    # which would silently break stickiness across restarts. hashlib.md5
    # is stable: same input -> same output, always, on any machine, any
    # run. That stability is the entire point of this algorithm.
    digest = hashlib.md5(client_id.encode()).hexdigest()
    index = int(digest, 16) % len(BACKENDS)
    return BACKENDS[index]


@app.route("/api/sticky-request")
def sticky_request():
    client_id = request.args.get("client", "anonymous")
    backend = pick_backend_by_hash(client_id)
    start = time.time()
    # NOTE: deliberately no retry-on-failure here, to keep this version
    # focused purely on the hashing concept. The real tension it would
    # introduce: if the assigned backend is down, do you break stickiness
    # for this one request (route elsewhere, losing the "same backend"
    # guarantee) or fail outright? Real systems often reach for CONSISTENT
    # hashing specifically to minimize how many clients get reassigned
    # when the backend list changes — a refinement on top of this, not
    # built here.
    try:
        resp = requests.get(f"{backend['url']}/api/handle", timeout=5)
        backend_data = resp.json()
        elapsed_ms = round((time.time() - start) * 1000, 1)
        return jsonify(
            ok=True,
            client_id=client_id,
            chosen_backend=backend["id"],
            backend_response=backend_data,
            elapsed_ms=elapsed_ms,
        )
    except requests.exceptions.RequestException as e:
        elapsed_ms = round((time.time() - start) * 1000, 1)
        return jsonify(
            ok=False,
            client_id=client_id,
            chosen_backend=backend["id"],
            error=str(e),
            elapsed_ms=elapsed_ms,
        ), 502


@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/active-connections")
def active_connections_endpoint():
    with active_lock:
        return jsonify(dict(active_connections))


@app.route("/api/request")
def handle_request():
    start = time.time()
    tried_ids = set()
    failed_attempts = []
    original_pick = None

    # Same retry-on-failure shape as before, but the CHOICE at each step
    # is "whichever untried backend has the fewest active connections
    # right now" instead of a fixed rotation.
    while len(tried_ids) < len(BACKENDS):
        with active_lock:
            remaining = [b for b in BACKENDS if b["id"] not in tried_ids]
            backend = pick_least_connections(remaining)
            active_connections[backend["id"]] += 1   # claim a slot BEFORE calling it
        if original_pick is None:
            original_pick = backend["id"]
        tried_ids.add(backend["id"])

        try:
            resp = requests.get(f"{backend['url']}/api/handle", timeout=5)
            backend_data = resp.json()
            elapsed_ms = round((time.time() - start) * 1000, 1)
            return jsonify(
                ok=True,
                chosen_backend=backend["id"],
                original_pick=original_pick,
                retried=backend["id"] != original_pick,
                failed_attempts=failed_attempts,
                backend_response=backend_data,
                elapsed_ms=elapsed_ms,
            )
        except requests.exceptions.RequestException as e:
            failed_attempts.append({"backend": backend["id"], "error": str(e)})
            continue
        finally:
            # Release the slot the instant this backend is done with us —
            # success OR failure — so the active-connections count always
            # reflects reality for the NEXT request's decision.
            with active_lock:
                active_connections[backend["id"]] = max(0, active_connections[backend["id"]] - 1)

    elapsed_ms = round((time.time() - start) * 1000, 1)
    return jsonify(
        ok=False,
        original_pick=original_pick,
        failed_attempts=failed_attempts,
        error="All backends failed",
        elapsed_ms=elapsed_ms,
    ), 502


INDEX_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Load Balancing — v4: Sticky Sessions (IP Hash)</title>
<style>
  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; background:#0f1115; color:#e8e8e8; margin:0; padding:24px; }
  h1 { font-size:20px; margin-bottom:4px; }
  .sub { color:#9aa0a6; margin-bottom:20px; font-size:14px; }
  .row { display:flex; gap:16px; flex-wrap:wrap; }
  .panel { background:#181b21; border:1px solid #2a2e37; border-radius:10px; padding:16px; flex:1; min-width:280px; }
  button.send { background:#2a3142; color:#e8e8e8; border:1px solid #3a4258; border-radius:8px; padding:10px 18px; cursor:pointer; font-size:14px; margin-right:8px; }
  button.send:hover { background:#3a4258; }
  button.batch { background:#2a3a2e; border-color:#3a5a42; }
  button.batch:hover { background:#3a5a42; }
  .servers { display:flex; gap:14px; margin:20px 0; }
  .server-tile {
      flex:1; background:#181b21; border:2px solid #2a2e37; border-radius:10px;
      padding:18px; text-align:center; transition: all 0.15s;
  }
  .server-tile.active { border-color:#4caf87; background:#123b24; transform: scale(1.04); }
  .server-tile .id { font-size:28px; font-weight:700; }
  .server-tile .count { color:#9aa0a6; font-size:13px; margin-top:6px; }
  .server-tile .active-count { font-size:13px; margin-top:8px; font-weight:600; }
  .bar-bg { background:#2a2e37; border-radius:4px; height:8px; width:100%; overflow:hidden; margin-top:6px; }
  .bar-fill { background:#4caf87; height:100%; transition: width 0.2s; }
  #result, #sticky-result { margin-top:14px; padding:14px; border-radius:8px; font-size:14px; min-height:20px; background:#2a2e37; border:1px solid #3a4258; }
  #result.ok, #sticky-result.ok { background:#123b24; border:1px solid #1f6b3e; }
  #result.err, #sticky-result.err { background:#3b1414; border:1px solid #6b1f1f; }
  #result.retried { background:#3b2c14; border:1px solid #6b4f1f; }
  .tile-skipped { border-color:#6b1f1f !important; background:#2a1414 !important; }
  #log, #sticky-log { font-family: Consolas, monospace; font-size:12px; color:#9aa0a6; max-height:220px; overflow-y:auto; line-height:1.6; margin-top:14px; }
  .sticky-row { display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid #2a2e37; }
  .sticky-match { color:#4caf87; }
  .sticky-mismatch { color:#e05c5c; }
</style>
</head>
<body>

<h1>Load Balancing — v4: Sticky Sessions (IP Hash)</h1>
<div class="sub">"Send Request" sends one at a time — watch it rotate fairly through A/B/C even though every click ties (0 active). "Send 6 Concurrent" fires 6 at once, but note: some browsers serialize same-origin requests over one connection, which can make this button look sequential rather than truly parallel — if so, trust the Python-side concurrency proof in VERSIONS.md instead.</div>

<div class="servers" id="servers">
  <div class="server-tile" id="tile-A">
    <div class="id">A</div>
    <div class="count" id="count-A">0 total</div>
    <div class="active-count" id="active-A">0 active now</div>
    <div class="bar-bg"><div class="bar-fill" id="bar-A" style="width:0%"></div></div>
  </div>
  <div class="server-tile" id="tile-B">
    <div class="id">B</div>
    <div class="count" id="count-B">0 total</div>
    <div class="active-count" id="active-B">0 active now</div>
    <div class="bar-bg"><div class="bar-fill" id="bar-B" style="width:0%"></div></div>
  </div>
  <div class="server-tile" id="tile-C">
    <div class="id">C</div>
    <div class="count" id="count-C">0 total</div>
    <div class="active-count" id="active-C">0 active now</div>
    <div class="bar-bg"><div class="bar-fill" id="bar-C" style="width:0%"></div></div>
  </div>
</div>

<div class="row">
  <div class="panel">
    <strong style="font-size:13px;">Least connections</strong>
    <div style="margin-top:8px;">
      <button class="send" onclick="sendRequest()">Send Request</button>
      <button class="send batch" onclick="sendBatch(6)">Send 6 Concurrent</button>
    </div>
    <div id="result">Click to send a request through the load balancer.</div>
    <div id="log"></div>
  </div>

  <div class="panel">
    <strong style="font-size:13px;">Sticky sessions (hash-based)</strong>
    <div class="sub" style="margin:6px 0 10px;">Same client ID always lands on the same backend. Click one client repeatedly to prove it never changes.</div>
    <div>
      <button class="send" onclick="sendSticky('client-1')">Client 1</button>
      <button class="send" onclick="sendSticky('client-2')">Client 2</button>
      <button class="send" onclick="sendSticky('client-3')">Client 3</button>
    </div>
    <div id="sticky-result">Click a client to send their request.</div>
    <div id="sticky-history" style="margin-top:10px; font-size:13px;"></div>
    <div id="sticky-log"></div>
  </div>
</div>

<script>
const counts = { A: 0, B: 0, C: 0 };

function log(msg) {
  const el = document.getElementById("log");
  const line = document.createElement("div");
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  el.prepend(line);
}

function showResult(data) {
  const resultDiv = document.getElementById("result");
  document.querySelectorAll(".server-tile").forEach(t => {
    t.classList.remove("active");
    t.classList.remove("tile-skipped");
  });
  (data.failed_attempts || []).forEach(f => {
    document.getElementById("tile-" + f.backend).classList.add("tile-skipped");
  });

  if (data.ok) {
    const tile = document.getElementById("tile-" + data.chosen_backend);
    tile.classList.add("active");
    counts[data.chosen_backend] += 1;
    document.getElementById("count-" + data.chosen_backend).textContent = counts[data.chosen_backend] + " total";

    if (data.retried) {
      resultDiv.className = "retried";
      const skipped = data.failed_attempts.map(f => f.backend).join(", ");
      resultDiv.innerHTML = `↪️ RETRIED — picked <b>Server ${data.original_pick}</b>, which was DOWN (skipped: ${skipped}).
        Redirected to <b>Server ${data.chosen_backend}</b>. Round-trip: ${data.elapsed_ms}ms.`;
      log(`-> Server ${data.original_pick} DOWN, retried -> Server ${data.chosen_backend} (${data.elapsed_ms}ms)`);
    } else {
      resultDiv.className = "ok";
      resultDiv.innerHTML = `Routed to <b>Server ${data.chosen_backend}</b> (least busy at the time).
        Round-trip: ${data.elapsed_ms}ms.`;
      log(`-> Server ${data.chosen_backend} (${data.elapsed_ms}ms)`);
    }
  } else {
    resultDiv.className = "err";
    resultDiv.innerHTML = `💀 ALL BACKENDS DOWN.`;
    log(`-> ALL backends failed`);
  }
}

async function sendRequest() {
  document.getElementById("result").textContent = "Sending...";
  const res = await fetch("/api/request");
  const data = await res.json();
  showResult(data);
}

async function sendBatch(n) {
  log(`--- firing ${n} concurrent requests ---`);
  document.getElementById("result").textContent = `Sending ${n} requests at once...`;
  // Promise.all fires all N fetch() calls essentially simultaneously,
  // instead of waiting for each one to finish before starting the next —
  // THIS is what creates genuine overlap for least-connections to react to.
  const promises = Array.from({length: n}, () => fetch("/api/request").then(r => r.json()));
  const results = await Promise.all(promises);
  results.forEach(showResult);   // shows the LAST one on screen, logs all of them
  log(`--- batch of ${n} complete ---`);
}

// Remembers, in the BROWSER only, which backend each client was assigned
// LAST time — purely so the UI can show you "yes, still the same one" on
// every repeat click. The load balancer itself remembers NOTHING between
// requests for this route; the hash alone reproduces the same answer
// every time, which is the whole point.
const stickyHistory = {};

function stickyLog(msg) {
  const el = document.getElementById("sticky-log");
  const line = document.createElement("div");
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  el.prepend(line);
}

function renderStickyHistory() {
  const el = document.getElementById("sticky-history");
  const rows = Object.keys(stickyHistory).sort().map(client => {
    return `<div class="sticky-row"><span>${client}</span><span>always -> Server ${stickyHistory[client]}</span></div>`;
  });
  el.innerHTML = rows.length ? rows.join("") : "";
}

async function sendSticky(clientId) {
  const resultDiv = document.getElementById("sticky-result");
  resultDiv.className = "";
  resultDiv.textContent = `Sending request for "${clientId}"...`;

  const res = await fetch(`/api/sticky-request?client=${encodeURIComponent(clientId)}`);
  const data = await res.json();

  if (!data.ok) {
    resultDiv.className = "err";
    resultDiv.innerHTML = `💀 ${clientId}'s assigned backend (Server ${data.chosen_backend}) is DOWN. No fallback in this version.`;
    stickyLog(`${clientId} -> Server ${data.chosen_backend} FAILED`);
    return;
  }

  const previous = stickyHistory[clientId];
  const matched = previous === undefined || previous === data.chosen_backend;
  stickyHistory[clientId] = data.chosen_backend;
  renderStickyHistory();

  resultDiv.className = "ok";
  if (previous === undefined) {
    resultDiv.innerHTML = `First request from <b>${clientId}</b> -> assigned <b>Server ${data.chosen_backend}</b> (via hash, not chosen randomly).
      Round-trip: ${data.elapsed_ms}ms.`;
  } else {
    resultDiv.innerHTML = `<b>${clientId}</b> -> <b>Server ${data.chosen_backend}</b> again.
      ${matched ? '✅ Same backend as every previous request from this client.' : '⚠️ Different from last time — unexpected!'}
      Round-trip: ${data.elapsed_ms}ms.`;
  }
  stickyLog(`${clientId} -> Server ${data.chosen_backend} (${data.elapsed_ms}ms)${previous !== undefined ? ', matches history: ' + matched : ' (first request)'}`);
}

async function refreshActiveConnections() {
  try {
    const res = await fetch("/api/active-connections");
    const data = await res.json();
    for (const id of ["A", "B", "C"]) {
      document.getElementById("active-" + id).textContent = data[id] + " active now";
      // Scale the bar against a max of 6 (our batch size) just for visibility.
      const pct = Math.min(100, Math.round((data[id] / 6) * 100));
      document.getElementById("bar-" + id).style.width = pct + "%";
    }
  } catch (e) { /* ignore transient errors between process restarts */ }
}

setInterval(refreshActiveConnections, 200);
refreshActiveConnections();
</script>

</body>
</html>
"""


if __name__ == "__main__":
    # threaded=True: the load balancer itself must handle multiple
    # incoming requests concurrently too — otherwise the 6 "concurrent"
    # fetch() calls from the browser would just queue up and be processed
    # one at a time by THIS process, and active_connections would never
    # exceed 1 no matter what the backends are doing.
    app.run(host="127.0.0.1", port=6100, debug=False, threaded=True)
