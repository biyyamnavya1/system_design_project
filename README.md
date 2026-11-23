# System Design Project — Scaling, Load Balancing, Caching

A hands-on project for learning (and interviewing on) three core system
design topics, built as **live, clickable web apps** (not just code to
read) — one concept at a time, with real processes you can see started,
killed, and restarted.

## Where things live

- **[`live_demo/`](live_demo/)** — caching. A Flask app (`app.py`) plus a
  real separate Redis-protocol server (`redis_server.py`).
  **[`live_demo/VERSIONS.md`](live_demo/VERSIONS.md)** is the index — every
  concept milestone is frozen as its own `vN_*.py` in `live_demo/versions/`.
- **[`load_balancing/`](load_balancing/)** — load balancing. A load
  balancer (`load_balancer.py`) proxying across several real backend
  processes (`backend_server.py A/B/C`, one per port).
  **[`load_balancing/VERSIONS.md`](load_balancing/VERSIONS.md)** is its
  index, same convention.
- **`sd01_caching.py`** + **`interview_qa/`** — an earlier, terminal-based
  pass at caching concepts. Kept as reference/interview material; the live
  demos are the primary teaching path now.

Each topic's own `VERSIONS.md` is the real source of truth for what's been
covered and what each version proved live — this top-level file is just a map.

## Plan

| # | Topic | Status |
|---|-------|--------|
| 1 | Caching | 🟩 in progress — see `live_demo/VERSIONS.md` (v1, v2, v3 done) |
| 2 | Load balancing | 🟩 in progress — see `load_balancing/VERSIONS.md` (v1-v4 done: round robin, retry, least connections, sticky/IP hash) |
| 3 | Scaling | ⬜ pending |
| — | Docker | ⬜ pending — deferred until after load balancing/scaling; will also unlock real-Redis-only caching topics (eviction, persistence, pub/sub) |

## How to run

**Caching:**
```powershell
cd "system_design_project/live_demo"
python redis_server.py
```
(in a second terminal)
```powershell
cd "system_design_project/live_demo"
python app.py
```
Open http://127.0.0.1:5050.

**Load balancing:**
```powershell
cd "system_design_project/load_balancing"
python backend_server.py A 6101
```
(and, each in its own terminal)
```powershell
python backend_server.py B 6102
python backend_server.py C 6103
python load_balancer.py
```
Open http://127.0.0.1:6100.

To revisit an earlier version instead of the current one, run its file from
that topic's `versions/` folder directly — see that topic's `VERSIONS.md`
for what each one covers and exact run instructions.
