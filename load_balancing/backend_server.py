"""
A single BACKEND APP SERVER — one of several identical copies that sit
behind a load balancer.

This is the exact same idea as app.py in the caching demo (a Flask process
listening on a port), just made generic enough to run as "server A", "server
B", "server C" — multiple real, separate OS processes, each capable of
answering the same request on its own. The load balancer (a different file,
different process) decides which one of these actually gets each request.

Run it with a server ID and a port, e.g.:
    python backend_server.py A 6101
    python backend_server.py B 6102
    python backend_server.py C 6103
(three separate terminals / processes — that's the whole point)

An optional 4th argument adds an artificial processing delay in seconds,
e.g. `python backend_server.py B 6102 3` makes Server B deliberately slow
(as if it were overloaded) — used to make "least connections" routing
actually visible: requests pile up on a slow backend while fast ones
drain quickly, which only matters if more than one request can be
in flight on this server at once (see threaded=True below).
"""

import sys
import time
import os
from flask import Flask, jsonify

app = Flask(__name__)

# Passed on the command line so the SAME script can become "server A" or
# "server B" etc. — proving these are genuinely interchangeable copies,
# not three different programs.
SERVER_ID = sys.argv[1] if len(sys.argv) > 1 else "A"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 6101
ARTIFICIAL_DELAY_SECONDS = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

# Counts how many requests THIS process has personally handled. Since this
# is a separate OS process with its own memory, this counter is private to
# this one server — server B has no idea what server A's count is. That
# separation is exactly what makes load balancing possible: each copy is
# independent and can be swapped in/out without affecting the others.
request_count = 0


@app.route("/api/handle")
def handle():
    global request_count
    if ARTIFICIAL_DELAY_SECONDS > 0:
        time.sleep(ARTIFICIAL_DELAY_SECONDS)   # simulate this server being slow/overloaded
    request_count += 1
    return jsonify(
        server_id=SERVER_ID,
        pid=os.getpid(),              # proves this is a genuinely separate process
        port=PORT,
        request_count=request_count,  # proves each server tracks its own load independently
        handled_at=time.strftime("%H:%M:%S"),
    )


@app.route("/api/health")
def health():
    # A real load balancer polls something like this to know if a backend
    # is alive before sending it traffic (used in a later version).
    return jsonify(status="healthy", server_id=SERVER_ID)


if __name__ == "__main__":
    delay_note = f", artificial delay {ARTIFICIAL_DELAY_SECONDS}s" if ARTIFICIAL_DELAY_SECONDS else ""
    print(f"Backend server '{SERVER_ID}' starting on port {PORT} (PID {os.getpid()}){delay_note}")
    # threaded=True: without this, Flask's dev server handles ONE request at
    # a time, even from different clients — a 2nd incoming request would
    # just queue up and wait, making it impossible for this server to ever
    # have more than 1 "request in progress" at once. We need real
    # concurrency here so "active connections" can genuinely be > 1,
    # otherwise least-connections routing has nothing meaningful to measure.
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
