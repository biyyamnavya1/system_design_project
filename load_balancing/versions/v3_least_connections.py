"""
VERSION 3 — Load balancing: least connections (route by real-time load,
not blind rotation), plus a genuine tie-breaking bug found and fixed live.

=====================================================================
TOPICS COVERED IN THIS VERSION (on top of v1 + v2)
=====================================================================
  1. Least connections, the algorithm: track how many requests are
     CURRENTLY in-flight on each backend (active_connections, a live
     dict) and always send the next request to whichever backend has
     the FEWEST right now — not a fixed rotation like round robin.
  2. Why this requires real infrastructure changes to even test:
     requests must be able to genuinely overlap in time, so BOTH the
     load balancer and backend_server.py now run with threaded=True,
     and backend_server.py accepts an artificial delay argument to
     simulate one backend being "overloaded."
  3. BUG #1 found live: fired 6 concurrent requests via the browser's
     Promise.all — ALL 6 landed on Server A, none on B or C. Diagnosed
     by firing the identical 6 requests from a genuine multi-threaded
     Python client (concurrent.futures.ThreadPoolExecutor) against the
     SAME running server: that test correctly spread A/B/C/B/A/C,
     proving the algorithm itself was correct. Root cause isolated to
     this specific browser environment serializing same-origin
     requests over a single connection (confirmed by comparing total
     batch duration, ~3s, against 6x the per-request time, ~0.4-0.65s
     each — consistent with sequential delivery, not parallel).
  4. BUG #2 found live (the more important one): even with BUG #1
     explained, single sequential clicks ("Send Request" one at a
     time) STILL always routed to Server A. Root cause: plain
     min(remaining, key=...) breaks ties by returning the FIRST
     matching list item — and under light/sequential traffic, EVERY
     click sees a perfect 3-way tie (0 active everywhere by the time
     the next click happens), so Server A won literally every tie,
     forever. B and C would never be used at all under normal,
     non-concurrent load. This is a real, known flaw in naive
     least-connections implementations, not just a demo artifact.
  5. The fix: round-robin tie-breaking among EQUALLY-least-busy
     backends (pick_least_connections()), same approach real load
     balancers use (e.g. nginx's least_conn module). Verified live:
     4 sequential single clicks now correctly cycled A -> B -> C -> A.
  6. Re-verified genuine concurrency still works correctly after the
     tie-break fix, via the same ThreadPoolExecutor test: B/C/A/A/B/C,
     2 each, B properly taking the ~2s hit for its artificial delay.

WHAT THIS VERSION DOES **NOT** YET HAVE:
  - The in-browser "Send 6 Concurrent" button is NOT a reliable way to
    witness true concurrency in this specific tool's browser pane (see
    bug #1) — the Python ThreadPoolExecutor test is the trustworthy
    proof of correct behavior, kept here as the real verification.
  - No weighting (e.g. "this backend is a bigger machine, send it
    more") — every backend is still treated as equally capable.
  - No IP/client hashing (sticky sessions) yet.

DEPENDENCY: needs all three backend_server.py copies running first,
with an artificial delay on at least one to see least-connections do
anything meaningful:
    python ../backend_server.py A 6101
    python ../backend_server.py B 6102 2
    python ../backend_server.py C 6103

This file is a frozen SNAPSHOT at this point in the lesson. It won't
be edited further — new concepts get their own vN file from here.
See ../VERSIONS.md for the full index and ../load_balancer.py for
the current, actively-edited version (always equal to the newest vN).

Run it with:
    python versions/v3_least_connections.py
Then open:    http://127.0.0.1:6100
=====================================================================
"""

import time
import threading
import requests
from flask import Flask, jsonify

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
<title>Load Balancing — v3: Least Connections</title>
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
  #result { margin-top:14px; padding:14px; border-radius:8px; font-size:14px; min-height:20px; background:#2a2e37; border:1px solid #3a4258; }
  #result.ok { background:#123b24; border:1px solid #1f6b3e; }
  #result.err { background:#3b1414; border:1px solid #6b1f1f; }
  #result.retried { background:#3b2c14; border:1px solid #6b4f1f; }
  .tile-skipped { border-color:#6b1f1f !important; background:#2a1414 !important; }
  #log { font-family: Consolas, monospace; font-size:12px; color:#9aa0a6; max-height:220px; overflow-y:auto; line-height:1.6; margin-top:14px; }
</style>
</head>
<body>

<h1>Load Balancing — v3: Least Connections</h1>
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
    <button class="send" onclick="sendRequest()">Send Request</button>
    <button class="send batch" onclick="sendBatch(6)">Send 6 Concurrent</button>
    <div id="result">Click to send a request through the load balancer.</div>
    <div id="log"></div>
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
