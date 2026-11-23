"""
VERSION 2 — Load balancing: retry-on-failure (a failed request gets
redirected, not lost).

=====================================================================
TOPICS COVERED IN THIS VERSION (on top of v1)
=====================================================================
  1. The exact gap in v1, found by deliberately crashing a backend:
     round robin has zero concept of health — it blindly cycled
     A, B, C, A, B, C... even after B was killed, sending roughly
     1-in-3 requests into a 2-second timeout followed by a failure
     (`requests.exceptions.ConnectTimeoutError`). Confirmed live:
     Server B's counter froze while A and C kept climbing.
  2. Retry-on-failure, the fix: round robin still decides WHERE TO
     START (so long-run fairness is unchanged), but if that pick
     fails, the request is NOT dropped — the load balancer walks
     forward through the REST of the backend list, in order, trying
     each one, until one succeeds or every single backend has failed.
  3. Answers the question "does a failed request get lost, or
     redirected?":
       - At least one backend still healthy -> REDIRECTED (the user
         gets a correct, successful response, just slower)
       - ALL backends down -> genuinely lost (HTTP 502), there's
         nowhere left to send it
  4. Verified live, end to end: killed Server B, sent a request that
     round robin picked for B -> watched it time out, skip B (UI
     flashes the tile red), and automatically redirect to C, which
     handled it successfully in ~2080ms total (the "NOT lost" case).
     Then restarted B and confirmed it rejoined rotation normally on
     its very next turn (fresh PID, counter reset to 1 — a brand new
     process, not a revived old one).

WHAT THIS VERSION DOES **NOT** YET HAVE:
  - No PROACTIVE health checks — a dead backend is still only
    discovered reactively, by actually trying it and timing out
    (costing up to ~2s per request until that backend's next turn).
    `backend_server.py` already exposes `/api/health` for this.
  - Still only round robin for picking the STARTING point — no
    least-connections, weighted, or consistent-hashing algorithms yet.
  - If ALL backends are down, the request is still genuinely lost
    (correctly — there's nothing left to redirect to without a queue
    or circuit-breaker-style client backoff, which isn't built here).

DEPENDENCY: needs all three backend_server.py copies running first:
    python ../backend_server.py A 6101
    python ../backend_server.py B 6102
    python ../backend_server.py C 6103

This file is a frozen SNAPSHOT at this point in the lesson. It won't
be edited further — new concepts get their own vN file from here.
See ../VERSIONS.md for the full index and ../load_balancer.py for
the current, actively-edited version (always equal to the newest vN).

Run it with:
    python versions/v2_retry_on_failure.py
Then open:    http://127.0.0.1:6100
=====================================================================
"""

import time
import threading
import requests
from flask import Flask, jsonify

app = Flask(__name__)

# The load balancer's "address book" of backends it can send traffic to.
# In a real system this list might come from service discovery (backends
# register themselves) rather than being hardcoded — but the CONCEPT is
# identical: a list of "where are the real workers."
BACKENDS = [
    {"id": "A", "url": "http://127.0.0.1:6101"},
    {"id": "B", "url": "http://127.0.0.1:6102"},
    {"id": "C", "url": "http://127.0.0.1:6103"},
]

# The entire round-robin algorithm is this one counter plus a modulo.
_rr_lock = threading.Lock()
_rr_counter = 0


def pick_backend_round_robin():
    global _rr_counter
    with _rr_lock:
        backend = BACKENDS[_rr_counter % len(BACKENDS)]
        _rr_counter += 1
    return backend


@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/request")
def handle_request():
    # RETRY-ON-FAILURE: round robin still picks WHERE TO START (so fairness
    # over time is unchanged — the next click still begins at the next
    # backend in line). But if that pick fails, we don't give up on this
    # user's request — we walk forward through the REST of the backend
    # list, in order, and try each one, until either one succeeds or
    # every single backend has failed.
    start = time.time()
    first_choice = pick_backend_round_robin()
    start_index = BACKENDS.index(first_choice)
    candidates = [BACKENDS[(start_index + i) % len(BACKENDS)] for i in range(len(BACKENDS))]

    failed_attempts = []
    for backend in candidates:
        try:
            # This is the actual PROXYING: the load balancer makes its OWN
            # HTTP request to the chosen backend, over the network (even
            # though it's just 127.0.0.1), and waits for that answer.
            resp = requests.get(f"{backend['url']}/api/handle", timeout=2)
            backend_data = resp.json()
            elapsed_ms = round((time.time() - start) * 1000, 1)
            return jsonify(
                ok=True,
                chosen_backend=backend["id"],
                original_pick=first_choice["id"],
                retried=backend["id"] != first_choice["id"],
                failed_attempts=failed_attempts,
                backend_response=backend_data,
                elapsed_ms=elapsed_ms,
            )
        except requests.exceptions.RequestException as e:
            # This candidate failed — record it and move on to the NEXT
            # one in `candidates` instead of giving up immediately. This
            # loop iteration is the entire fix: v1 would have `return`ed
            # right here instead of `continue`ing.
            failed_attempts.append({"backend": backend["id"], "error": str(e)})
            continue

    # Every single backend failed — now, and only now, do we give up.
    elapsed_ms = round((time.time() - start) * 1000, 1)
    return jsonify(
        ok=False,
        original_pick=first_choice["id"],
        failed_attempts=failed_attempts,
        error="All backends failed",
        elapsed_ms=elapsed_ms,
    ), 502


INDEX_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Load Balancing — v2: Retry-on-failure</title>
<style>
  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; background:#0f1115; color:#e8e8e8; margin:0; padding:24px; }
  h1 { font-size:20px; margin-bottom:4px; }
  .sub { color:#9aa0a6; margin-bottom:20px; font-size:14px; }
  .row { display:flex; gap:16px; flex-wrap:wrap; }
  .panel { background:#181b21; border:1px solid #2a2e37; border-radius:10px; padding:16px; flex:1; min-width:280px; }
  button.send { background:#2a3142; color:#e8e8e8; border:1px solid #3a4258; border-radius:8px; padding:10px 18px; cursor:pointer; font-size:14px; }
  button.send:hover { background:#3a4258; }
  .servers { display:flex; gap:14px; margin:20px 0; }
  .server-tile {
      flex:1; background:#181b21; border:2px solid #2a2e37; border-radius:10px;
      padding:18px; text-align:center; transition: all 0.15s;
  }
  .server-tile.active { border-color:#4caf87; background:#123b24; transform: scale(1.04); }
  .server-tile .id { font-size:28px; font-weight:700; }
  .server-tile .count { color:#9aa0a6; font-size:13px; margin-top:6px; }
  #result { margin-top:14px; padding:14px; border-radius:8px; font-size:14px; min-height:20px; background:#2a2e37; border:1px solid #3a4258; }
  #result.ok { background:#123b24; border:1px solid #1f6b3e; }
  #result.err { background:#3b1414; border:1px solid #6b1f1f; }
  #result.retried { background:#3b2c14; border:1px solid #6b4f1f; }
  .tile-skipped { border-color:#6b1f1f !important; background:#2a1414 !important; }
  #log { font-family: Consolas, monospace; font-size:12px; color:#9aa0a6; max-height:220px; overflow-y:auto; line-height:1.6; margin-top:14px; }
</style>
</head>
<body>

<h1>Load Balancing — v2: Retry-on-failure</h1>
<div class="sub">Click "Send Request" repeatedly. Watch which backend lights up. If round robin's pick is down, watch it skip (flash red) and redirect to the next one instead of failing.</div>

<div class="servers" id="servers">
  <div class="server-tile" id="tile-A"><div class="id">A</div><div class="count" id="count-A">0 requests</div></div>
  <div class="server-tile" id="tile-B"><div class="id">B</div><div class="count" id="count-B">0 requests</div></div>
  <div class="server-tile" id="tile-C"><div class="id">C</div><div class="count" id="count-C">0 requests</div></div>
</div>

<div class="row">
  <div class="panel">
    <button class="send" onclick="sendRequest()">Send Request</button>
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

async function sendRequest() {
  const resultDiv = document.getElementById("result");
  resultDiv.className = "";
  resultDiv.textContent = "Sending request to the load balancer...";

  let res, data;
  try {
    res = await fetch("/api/request");
    data = await res.json();
  } catch (e) {
    resultDiv.className = "err";
    resultDiv.textContent = "Load balancer itself did not respond: " + e;
    return;
  }

  // Reset tile styling, then briefly flash any SKIPPED (failed) backends
  // red before showing which one actually handled the request.
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
    document.getElementById("count-" + data.chosen_backend).textContent = counts[data.chosen_backend] + " requests";

    if (data.retried) {
      resultDiv.className = "retried";
      const skipped = data.failed_attempts.map(f => f.backend).join(", ");
      resultDiv.innerHTML = `↪️ RETRIED — round robin picked <b>Server ${data.original_pick}</b>, which was DOWN
        (skipped: ${skipped}). Automatically redirected to <b>Server ${data.chosen_backend}</b> instead,
        which handled it successfully (its own count: ${data.backend_response.request_count}).
        Round-trip: ${data.elapsed_ms}ms. The request was NOT lost.`;
      log(`Request -> Server ${data.original_pick} DOWN, retried -> Server ${data.chosen_backend} succeeded (${data.elapsed_ms}ms)`);
    } else {
      resultDiv.className = "ok";
      resultDiv.innerHTML = `Routed to <b>Server ${data.chosen_backend}</b> (PID ${data.backend_response.pid}).
        That server has now personally handled ${data.backend_response.request_count} request(s).
        Round-trip: ${data.elapsed_ms}ms.`;
      log(`Request -> Server ${data.chosen_backend} (its own count: ${data.backend_response.request_count})`);
    }
  } else {
    resultDiv.className = "err";
    const skipped = (data.failed_attempts || []).map(f => f.backend).join(", ");
    resultDiv.innerHTML = `💀 ALL BACKENDS DOWN — tried every server (${skipped}), none responded. Request lost.`;
    log(`Request -> ALL backends failed (tried: ${skipped})`);
  }
}
</script>

</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=6100, debug=False)
