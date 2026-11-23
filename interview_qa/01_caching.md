# Caching — Interview Q&A

Companion to [`sd01_caching.py`](../sd01_caching.py). Each answer is written the
way you'd actually say it out loud, then backed with the "why".

---

### 1. What is caching, and why does it help?

Caching stores a copy of expensive-to-fetch data somewhere fast (RAM, same
process) so repeat requests skip the expensive path (disk, network, a slow
downstream service, a heavy computation). It trades a small amount of extra
memory for a large reduction in latency and load on the system behind it.

### 2. What's the difference between cache-aside, write-through, write-back, and write-around?

| Strategy | Read path | Write path | Best for | Risk |
|---|---|---|---|---|
| Cache-aside (lazy loading) | App checks cache, falls back to DB on miss, then populates cache | App writes DB, then **invalidates** (deletes) cache key | General-purpose, most common in practice | Brief staleness between write and next read |
| Write-through | Same as cache-aside | App writes cache **and** DB in the same call | Read-heavy, can't tolerate stale reads | Every write pays full DB latency |
| Write-back (write-behind) | Same as cache-aside | App writes cache only; DB updated later in a batch/async flush | Write-heavy, latency-sensitive writes | Data loss if process crashes before flush (mitigated with a write-ahead log) |
| Write-around | Same as cache-aside | App writes DB only, cache untouched | Write-once/read-rarely data (logs, analytics) | Guaranteed cache miss right after a write |

I implemented all four in `sd01_caching.py` against the same `SlowDatabase` so
the latency difference is directly visible when you run it.

### 3. Explain LRU, LFU, and FIFO eviction. When would you pick each?

- **LRU (Least Recently Used):** evict whoever hasn't been *touched* in the
  longest time. Works well because most real traffic has temporal locality
  (recently used data tends to be used again soon). Implemented with an
  `OrderedDict` + `move_to_end()` for O(1) get/put.
- **FIFO:** evict whoever was *inserted* first, full stop — reads don't
  protect an item. Simpler, but a frequently-read key can still get evicted
  just because it arrived early. Better when access is closer to one-pass/
  sequential than "reused" (e.g. scanning a dataset once).
- **LFU (Least Frequently Used):** evict whoever has the *lowest access
  count*. Resists being fooled by a short burst of one-off reads the way LRU
  can be. Costs more to maintain (need a count per key); naive implementation
  is O(n) per eviction (scan for the min), production-grade uses a
  frequency-bucket structure for O(1).

### 4. How do you implement an O(1) LRU cache? Walk through it.

Use a hash map for O(1) key lookup plus a doubly linked list (or Python's
`OrderedDict`, which is a hash map + doubly linked list internally) to track
order. On `get`: look up in O(1), then move the node to the "most recently
used" end in O(1) (pointer relinking, no scanning). On `put` past capacity:
remove the node at the "least recently used" end in O(1). That's exactly what
`LRUCache` in `sd01_caching.py` does via `OrderedDict.move_to_end()` and
`popitem(last=False)`.

### 5. What is cache invalidation, and why is it "one of the two hard problems in computer science"?

Invalidation = deciding when a cached value is no longer trustworthy and must
be removed or refreshed. It's hard because the cache and the source of truth
can be updated independently, by different code paths or even different
machines, with no single moment where both are guaranteed to agree. Common
strategies: TTL (expire after N seconds, simple but can serve stale data
until expiry), explicit invalidation on write (delete-on-write, what
cache-aside does), and write-through (keep both in sync on every write, at a
latency cost).

### 6. What is a cache stampede (thundering herd), and how do you prevent it?

When a hot key expires or misses, many concurrent requests can all see a miss
at once and all hammer the database simultaneously to recompute the same
value — the exact overload the cache was supposed to prevent. Fix: a per-key
lock. The first request to miss acquires the lock and does the real fetch;
every other concurrent request for the *same* key blocks on that lock instead
of also hitting the DB, then re-checks the cache after acquiring the lock
(now populated) and returns the cached value. `StampedeSafeStore` in the code
demonstrates this collapsing 8 concurrent misses into exactly 1 DB call.
Other mitigations: jittered TTLs (don't expire a million keys at the same
timestamp), and "early recomputation" (refresh slightly before expiry in the
background).

### 7. TTL vs explicit invalidation — which do you use?

TTL is simpler (set-and-forget, self-healing if you forget to invalidate
something) but can serve stale data for up to the TTL window. Explicit
invalidation (delete-on-write) is more precise — zero staleness window — but
requires every write path to remember to invalidate, and gets complicated
once multiple keys depend on the same underlying data. Real systems usually
combine both: invalidate on write as the primary mechanism, with a TTL as a
safety net in case an invalidation is ever missed.

### 8. How would a CDN fit into this picture?

A CDN (Content Delivery Network) is caching applied at the network edge,
geographically close to users, usually for static or semi-static content
(images, JS bundles, API responses that are the same for everyone). Same
core ideas apply — TTL-based expiry, cache-aside-like "pull" from origin on
miss — just moved from "same machine's RAM" out to "a server near the user".

### 9. Redis vs Memcached — what's the practical difference?

Memcached is a simple, multi-threaded, pure key-value cache — great when you
need the absolute simplest LRU cache over the network. Redis supports richer
data structures (lists, sets, sorted sets, hashes), persistence options,
pub/sub, and built-in replication/clustering — so it's usually the default
choice when the cache needs to do more than "get/set a blob," or needs to
survive a restart.

### 10. How would you design the caching layer for a high-traffic, read-heavy API?

1. Identify the hot keys/queries (e.g. via logging/metrics) worth caching.
2. Use cache-aside as the default read/write pattern — simplest, most
   forgiving of partial failures.
3. Pick TTL per data type based on staleness tolerance (user profile: minutes;
   stock price: seconds; static config: hours).
4. Add stampede protection (per-key locks or request coalescing) for your
   hottest keys specifically — that's where a stampede actually hurts.
5. Once a single cache node can't hold the working set or handle the QPS,
   shard the cache across multiple nodes using consistent hashing (covered in
   the load-balancing module) so you can scale the cache horizontally without
   a full rehash on every node addition/removal.
6. Monitor hit ratio continuously — a low hit ratio means wrong TTL, wrong
   eviction policy, or cache too small, and tells you exactly which lever to
   pull.
