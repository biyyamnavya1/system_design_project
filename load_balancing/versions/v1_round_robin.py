"""
VERSION 1 — Load balancing: round robin across real separate processes.

=====================================================================
TOPICS COVERED IN THIS VERSION
=====================================================================
  1. The physical shape of load balancing: ONE process (this file,
     port 6100) that the outside world talks to, sitting in front of
     SEVERAL identical backend processes (../backend_server.py,
     ports 6101/6102/6103) that do the actual work. The client never
     talks to the backends directly and doesn't know how many exist.
  2. Proxying: the load balancer makes its OWN HTTP request to the
     chosen backend (via the `requests` library) and relays that
     backend's response back to the original caller — it does none
     of the real work itself.
  3. Round robin, the simplest routing algorithm: a counter, taken
     modulo the number of backends. Request 1 -> backend 0, request 2
     -> backend 1, ... wrapping back to backend 0 after the last one.
     No awareness of backend load, health, or request content.
  4. Verified live: clicked "Send Request" four times, watched strict
     A -> B -> C -> A rotation, and confirmed each backend's own
     request_count and PID stayed independent and consistent (e.g.
     Server A's PID was identical across its 1st and 2nd request,
     proving it's the same long-lived process both times).
  5. Per-backend independent state: each backend process tracks its
     own request_count in its own memory — proof that these are truly
     separate, independent copies, not one program pretending to be
     three.

WHAT THIS VERSION DOES **NOT** YET HAVE:
  - No fault tolerance: if the chosen backend is down, this request
    simply fails (HTTP 502) — no retry, no skip-to-a-healthy-backend.
  - No health checks (though backend_server.py already exposes
    /api/health for a future version to poll).
  - No weighting, no "least connections," no consistent hashing —
    only the simplest possible algorithm so far.
  - Not yet combined with the caching demo — these are still two
    separate systems.

DEPENDENCY: needs all three backend_server.py copies running first:
    python ../backend_server.py A 6101
    python ../backend_server.py B 6102
    python ../backend_server.py C 6103

This file is a frozen SNAPSHOT at this point in the lesson. It won't
be edited further — new concepts get their own vN file from here.
See ../VERSIONS.md for the full index and ../load_balancer.py for
the current, actively-edited version (always equal to the newest vN).

Run it with:
    python versions/v1_round_robin.py
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
    backend = pick_backend_round_robin()
    start = time.time()
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
            backend_response=backend_data,
            elapsed_ms=elapsed_ms,
        )
    except requests.exceptions.RequestException as e:
        # v1 has no fault tolerance yet — if the chosen backend is down,
        # this request simply fails. Handling that (skip to a healthy
        # backend instead) is a deliberate next step, not an oversight.
        elapsed_ms = round((time.time() - start) * 1000, 1)
        return jsonify(
            ok=False,
            chosen_backend=backend["id"],
            error=str(e),
            elapsed_ms=elapsed_ms,
        ), 502


INDEX_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Load Balancing — v1: Round Robin</title>
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
  #log { font-family: Consolas, monospace; font-size:12px; color:#9aa0a6; max-height:220px; overflow-y:auto; line-height:1.6; margin-top:14px; }
</style>
</head>
<body>

<h1>Load Balancing — v1: Round Robin</h1>
<div class="sub">Click "Send Request" repeatedly. Watch which backend server lights up — A, B, C, A, B, C... in strict rotation.</div>

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

  // Light up whichever tile handled this one, briefly.
  document.querySelectorAll(".server-tile").forEach(t => t.classList.remove("active"));

  if (data.ok) {
    const tile = document.getElementById("tile-" + data.chosen_backend);
    tile.classList.add("active");
    counts[data.chosen_backend] += 1;
    document.getElementById("count-" + data.chosen_backend).textContent = counts[data.chosen_backend] + " requests";

    resultDiv.className = "ok";
    resultDiv.innerHTML = `Routed to <b>Server ${data.chosen_backend}</b> (PID ${data.backend_response.pid}).
      That server has now personally handled ${data.backend_response.request_count} request(s).
      Round-trip: ${data.elapsed_ms}ms.`;
    log(`Request -> Server ${data.chosen_backend} (its own count: ${data.backend_response.request_count})`);
  } else {
    resultDiv.className = "err";
    resultDiv.innerHTML = `Load balancer picked <b>Server ${data.chosen_backend}</b>, but it did not respond: ${data.error}`;
    log(`Request -> Server ${data.chosen_backend} FAILED: ${data.error}`);
  }
}
</script>

</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=6100, debug=False)
