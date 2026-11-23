"""
A REAL, standalone Redis-protocol server, running as its own OS process on
the standard Redis port (6379) — on purpose separate from app.py.

Honest disclosure: this uses `fakeredis`'s TcpFakeServer, an open-source
pure-Python implementation of the actual Redis wire protocol. It is NOT the
real C-based Redis binary (installing that cleanly needs Docker or WSL,
neither of which is available on this machine right now). But crucially,
it is a genuinely SEPARATE PROCESS that speaks the exact same protocol real
Redis does, over the exact same port (6379), and the real `redis` Python
client (the one app.py now uses) can't tell the difference. For the lesson
we care about here — "the cache is its own process, independent of your
app, and can be killed independently" — this behaves identically to real
Redis.

Run it with:  python redis_server.py
Stop it with: Ctrl+C (or however this session's preview tool stops it)
"""

from fakeredis import TcpFakeServer

if __name__ == "__main__":
    server = TcpFakeServer(("127.0.0.1", 6379))
    print("Redis-protocol server listening on 127.0.0.1:6379 ...")
    server.serve_forever()
