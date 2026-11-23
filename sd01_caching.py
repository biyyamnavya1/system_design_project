"""
MODULE 1 of 3 — CACHING
========================

GOAL OF THIS FILE
------------------
Caching means: keep a copy of expensive-to-get data somewhere FAST, so the next
request for the same data doesn't have to pay the full cost again.

"Expensive" can mean: a slow database query, a slow network call to another
service, a heavy computation, or a far-away disk read. "Fast" usually means:
RAM instead of disk, or your own process instead of a network hop.

Every caching decision you'll ever be asked about in an interview reduces to
three questions:
    1. WHERE do I store the cached copy?              (this file: in-memory)
    2. WHEN do I throw old copies away (eviction)?     (LRU / LFU / FIFO / TTL)
    3. HOW do writes/updates stay consistent with the cache?
       (cache-aside / write-through / write-back / write-around)

This file builds those three answers from scratch, with every line commented,
so you can explain the "why" behind each one, not just recite the name.

DESIGN PATTERNS USED IN THIS FILE
-----------------------------------
- STRATEGY PATTERN: the eviction policy (LRU/LFU/FIFO) is a swappable
  "strategy" object. The code that USES a cache (CacheAsideStore etc.) never
  cares which eviction strategy is plugged in — it just calls .get()/.put().
  This is exactly how you'd explain Strategy in an interview: "vary the
  algorithm independently of the client that uses it."
- TEMPLATE METHOD PATTERN: the abstract `Cache` base class defines the
  *shape* every cache must have (get/put/evict), while each subclass fills in
  *how* eviction actually happens. The shared skeleton lives in the parent;
  the varying step lives in the child.
- PROXY PATTERN: `CacheAsideStore`, `WriteThroughStore`, etc. act as a proxy
  in front of the real, slow `SlowDatabase`. Callers talk to the proxy
  (store.get(key)) and don't know or care whether the answer came from cache
  or from the real database. This is the same idea as a lazy-loading proxy.
- DECORATOR-ish behaviour: `TTLCache` wraps any other `Cache` and adds
  "expire after N seconds" behaviour on top, without changing the wrapped
  cache's code. (Not a strict GoF Decorator since it doesn't share the same
  base interface call-for-call, but the *intent* — add behaviour by wrapping
  — is the same, and interviewers accept this framing.)

Run this file directly to see every concept execute with printed output:
    python sd01_caching.py
"""

# --- Standard library imports -----------------------------------------------
import time                     # time.sleep() to *simulate* slow I/O (disk/DB/network)
import threading                # Lock, Thread -> used to demo the cache-stampede problem/fix
from collections import OrderedDict   # dict that remembers insertion/access order -> perfect for LRU/FIFO
from abc import ABC, abstractmethod   # lets us define an "interface" that subclasses must implement


# =============================================================================
# PART A — EVICTION POLICIES: what do we throw away when the cache is full?
# =============================================================================
#
# A cache is just a dict with a SIZE LIMIT. The interesting question is:
# once the limit is reached, which existing entry gets kicked out to make
# room for the new one? That "which one" rule is the eviction POLICY.

class Cache(ABC):
    """
    Abstract base class = the TEMPLATE for any cache.

    Every concrete cache below (LRU / LFU / FIFO) must provide `get` and
    `put`. By coding against THIS class everywhere else in the file, we can
    swap LRUCache for LFUCache without touching a single line of the code
    that uses the cache. That swappability is the whole point of the
    Strategy pattern.
    """

    def __init__(self, capacity: int):
        # capacity = max number of (key, value) pairs this cache will hold.
        # Real caches size this in BYTES (e.g. "allow up to 512MB"), but we
        # use "number of items" here because it's easier to see eviction
        # happen deterministically when you print things.
        if capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self.capacity = capacity

        # Simple hit/miss counters. In a real system these feed a metrics
        # dashboard (e.g. Grafana) showing your "cache hit ratio" — one of
        # the first numbers an interviewer will ask you to reason about.
        self.hits = 0
        self.misses = 0

    @abstractmethod
    def get(self, key):
        """Return the cached value for `key`, or None if not present (a MISS)."""
        raise NotImplementedError   # subclasses MUST override this

    @abstractmethod
    def put(self, key, value):
        """Insert/update `key` -> `value`, evicting something if we're full."""
        raise NotImplementedError

    def hit_ratio(self) -> float:
        # hit_ratio = hits / (hits + misses). This is THE metric that tells
        # you whether a cache is earning its keep. A ratio near 0 means the
        # cache is mostly useless (wrong eviction policy, too small, or data
        # that's never re-requested). A ratio near 1 means huge savings.
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class LRUCache(Cache):
    """
    LRU = Least Recently Used.
    Rule: when full, evict the item that has gone the LONGEST without being
    touched (read OR written). Intuition: "if you haven't needed this in a
    while, you probably won't need it again soon either" — true for most
    real-world access patterns (this is called "temporal locality").

    Implementation trick: Python's OrderedDict keeps keys in the order they
    were inserted, AND lets us cheaply move a key to the "most recently
    used" end with move_to_end(). That gives us O(1) get and O(1) put,
    which is the Big-O every interviewer expects for LRU.
    """

    def __init__(self, capacity: int):
        super().__init__(capacity)          # set up capacity + hit/miss counters
        self._store: OrderedDict = OrderedDict()
        # Order convention we choose: LEFT (front) = least recently used,
        # RIGHT (back) = most recently used. We enforce this convention
        # ourselves on every get/put below.

    def get(self, key):
        if key not in self._store:
            self.misses += 1              # not found -> count a miss
            return None                   # caller treats None as "not cached"

        self.hits += 1                    # found -> count a hit
        self._store.move_to_end(key)      # touching it makes it "most recently used" -> move to right end
        return self._store[key]

    def put(self, key, value):
        if key in self._store:
            # Key already exists: update its value AND refresh its recency.
            self._store.move_to_end(key)
        self._store[key] = value

        if len(self._store) > self.capacity:
            # We just went over budget -> evict the LEAST recently used
            # item, which by our convention sits at the LEFT (front).
            # last=False tells OrderedDict "pop from the front, not the back".
            evicted_key, _ = self._store.popitem(last=False)
            # (In production you'd log/metric this eviction; we just drop it.)


class FIFOCache(Cache):
    """
    FIFO = First In, First Out.
    Rule: when full, evict whichever item was INSERTED first, regardless of
    how often or recently it's been read. Simpler than LRU (no reordering on
    read), but worse in practice: a key that's read constantly can still get
    evicted just because it happened to arrive early. Interviewers like
    asking "when would FIFO beat LRU?" — answer: when access patterns are
    closer to random/sequential than "recently used = will be used again"
    (e.g. a scan over a huge dataset once, never revisited).
    """

    def __init__(self, capacity: int):
        super().__init__(capacity)
        self._store: OrderedDict = OrderedDict()
        # Here the order ONLY ever reflects insertion time — we deliberately
        # do NOT call move_to_end() on get(), which is what makes this FIFO
        # instead of LRU. That one missing line is the entire difference.

    def get(self, key):
        if key not in self._store:
            self.misses += 1
            return None
        self.hits += 1
        return self._store[key]           # <-- no move_to_end() here (vs LRU)

    def put(self, key, value):
        if key not in self._store:
            self._store[key] = value
            if len(self._store) > self.capacity:
                # Evict the oldest-inserted key (front of the dict).
                self._store.popitem(last=False)
        else:
            # Updating an existing key's value does NOT count as a fresh
            # insertion in true FIFO, so we don't touch ordering.
            self._store[key] = value


class LFUCache(Cache):
    """
    LFU = Least Frequently Used.
    Rule: when full, evict the item with the SMALLEST access count. Intuition:
    "popularity over time" matters more than "recency". Classic use case:
    a cache of product pages where a few items are perennially hot and most
    are rarely viewed — LRU can be tricked by a short burst of one-off scans,
    LFU resists that because one-off items never build up a high count.

    NOTE ON BIG-O (common interview follow-up): a naive LFU, like the one
    below, scans all keys to find the minimum count on eviction -> O(n).
    A production-grade O(1) LFU keeps a "frequency -> OrderedDict of keys
    at that frequency" structure (a dict of buckets) so the minimum bucket
    is always known. We use the simple O(n) version here because the GOAL
    of this file is to make the *concept* obvious to read; mention the O(1)
    bucket trick out loud in an interview to show depth.
    """

    def __init__(self, capacity: int):
        super().__init__(capacity)
        self._values: dict = {}          # key -> value
        self._freq: dict = {}            # key -> access count

    def get(self, key):
        if key not in self._values:
            self.misses += 1
            return None
        self.hits += 1
        self._freq[key] += 1             # every successful read bumps popularity
        return self._values[key]

    def put(self, key, value):
        if key in self._values:
            self._values[key] = value
            self._freq[key] += 1
            return

        if len(self._values) >= self.capacity:
            # Find the key with the SMALLEST frequency count.
            # min(..., key=...) scans every (k, count) pair -> O(n).
            least_used_key = min(self._freq, key=self._freq.get)
            del self._values[least_used_key]
            del self._freq[least_used_key]

        self._values[key] = value
        self._freq[key] = 1               # brand-new key starts at frequency 1


# =============================================================================
# PART B — TTL (Time To Live): eviction based on TIME, not capacity
# =============================================================================
#
# LRU/LFU/FIFO all answer "what do I evict when I'm FULL?". TTL answers a
# different question: "how do I evict something that's gone STALE, even if
# there's plenty of room left?" Think: a weather API response is fine to
# cache for 60 seconds, but dead wrong if served for an hour.

class TTLCache:
    """
    Wraps *any* Cache (LRU, LFU, FIFO — doesn't matter which) and adds an
    expiry timestamp per key. This is the "wrap to add behaviour" idea
    mentioned in the Decorator note at the top of the file.

    Expiry here is LAZY: we don't run a background timer constantly scanning
    for expired keys (that costs CPU for keys nobody is even asking about).
    Instead, we check "has this key's time run out?" only at the moment
    someone calls get() — this is called "lazy expiration" and is exactly
    what Redis does by default for most keys.
    """

    def __init__(self, inner_cache: Cache, default_ttl_seconds: float):
        self._inner = inner_cache                  # the wrapped LRU/LFU/FIFO cache
        self._default_ttl = default_ttl_seconds     # how long an entry lives, in seconds
        self._expires_at: dict = {}                 # key -> absolute unix timestamp when it dies

    def put(self, key, value, ttl_seconds: float = None):
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        self._inner.put(key, value)
        # time.time() = current wall-clock time in seconds since epoch.
        # "expires_at" = now + however long this entry is allowed to live.
        self._expires_at[key] = time.time() + ttl

    def get(self, key):
        # First check: do we even have an expiry recorded for this key?
        expiry = self._expires_at.get(key)
        if expiry is None:
            # Never put, or already purged -> treat exactly like the inner
            # cache would (this also lets the inner cache's own miss count
            # increment correctly).
            return self._inner.get(key)

        if time.time() >= expiry:
            # STALE: time's up. We actively remove it now ("lazy" deletion —
            # we only clean up when someone actually asks for this key).
            self._expires_at.pop(key, None)
            if isinstance(self._inner, LRUCache) or isinstance(self._inner, FIFOCache):
                self._inner._store.pop(key, None)
            elif isinstance(self._inner, LFUCache):
                self._inner._values.pop(key, None)
                self._inner._freq.pop(key, None)
            self._inner.misses += 1      # an expired key is, from the caller's point of view, a miss
            return None

        # Not expired yet -> delegate to the inner cache as normal.
        return self._inner.get(key)


# =============================================================================
# PART C — A slow "database" to cache in front of
# =============================================================================

class SlowDatabase:
    """
    Stands in for a real database / downstream service that is SLOW.
    We use time.sleep() to simulate real network/disk latency so that, when
    you run this file, you can literally watch cached calls return instantly
    while un-cached calls visibly pause.
    """

    def __init__(self, latency_seconds: float = 0.2):
        self._data = {}
        self._latency = latency_seconds

    def read(self, key):
        time.sleep(self._latency)         # pretend this took real I/O time
        return self._data.get(key)

    def write(self, key, value):
        time.sleep(self._latency)         # writes are "slow" too, same as reads
        self._data[key] = value


# =============================================================================
# PART D — THE FOUR WRITE/READ STRATEGIES
# =============================================================================
#
# This is the part interviewers care about most, because it's where real
# outages happen: a cache and a database can DISAGREE about the current
# value of a key. Each strategy is a different trade-off between
# consistency, write latency, and data-loss risk.

class CacheAsideStore:
    """
    CACHE-ASIDE (a.k.a. "Lazy Loading"). The MOST common pattern in the
    real world (how most apps use Redis/Memcached in front of Postgres/MySQL).

    READ path:
        1. Ask the cache first.
        2. Cache hit  -> return immediately, database never touched.
        3. Cache miss -> read the database, THEN populate the cache for
           next time, THEN return the value.

    WRITE path:
        - Write straight to the database.
        - INVALIDATE (delete) the cache entry rather than updating it.
          Why delete instead of update-in-place? Because recomputing the
          "correct" cached value on write can itself be wrong under
          concurrent writes; deleting is simpler and safe — the NEXT read
          will repopulate it correctly via the read path above.

    Trade-off: first read after a miss (or after any write) is slow
    (hits the DB); every read after that is fast. Database and cache can
    be briefly inconsistent between "someone wrote" and "someone next reads".
    """

    def __init__(self, cache: Cache, db: SlowDatabase):
        self.cache = cache
        self.db = db

    def read(self, key):
        cached = self.cache.get(key)
        if cached is not None:
            return cached                 # cache HIT: short-circuit, DB never touched

        value = self.db.read(key)         # cache MISS: pay the slow DB cost
        if value is not None:
            self.cache.put(key, value)    # populate cache so NEXT read is fast
        return value

    def write(self, key, value):
        self.db.write(key, value)         # source of truth updated first
        # Delete rather than update: forces the next read to go fetch a
        # fresh, guaranteed-correct copy instead of trusting a value we
        # computed locally (which could race with another writer).
        if isinstance(self.cache, LRUCache) or isinstance(self.cache, FIFOCache):
            self.cache._store.pop(key, None)
        elif isinstance(self.cache, LFUCache):
            self.cache._values.pop(key, None)
            self.cache._freq.pop(key, None)


class WriteThroughStore:
    """
    WRITE-THROUGH. Every write goes to the cache AND the database, in the
    SAME request, before returning to the caller. Cache and DB are always
    in sync the instant the write call returns.

    Trade-off: write latency = cache latency + database latency (you pay
    the slow DB cost on every single write, not just misses). You get
    strong consistency in exchange for slower writes. Good when reads vastly
    outnumber writes and you cannot tolerate stale reads.
    """

    def __init__(self, cache: Cache, db: SlowDatabase):
        self.cache = cache
        self.db = db

    def read(self, key):
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        value = self.db.read(key)
        if value is not None:
            self.cache.put(key, value)
        return value

    def write(self, key, value):
        self.db.write(key, value)         # 1) persist to the real database
        self.cache.put(key, value)        # 2) THEN mirror it into the cache, same call
        # Notice: unlike cache-aside, we UPDATE the cache with the new value
        # directly instead of deleting it — safe here because write-through
        # guarantees this call is the last word on `key` before returning.


class WriteBackStore:
    """
    WRITE-BACK (a.k.a. "Write-Behind"). Writes land in the cache
    IMMEDIATELY and return to the caller right away. The database is
    updated later, asynchronously, in a batch ("flush").

    Trade-off: writes are extremely fast (you only pay cache latency, never
    DB latency, on the hot path). The cost: if the process crashes before a
    flush happens, those writes are LOST — this is the classic "write-back
    cache data-loss risk" every interviewer will push on. Real systems
    mitigate this with a durable write-ahead log so a crash can replay the
    pending writes instead of losing them.
    """

    def __init__(self, cache: Cache, db: SlowDatabase):
        self.cache = cache
        self.db = db
        self._dirty: dict = {}            # key -> value, written to cache but NOT yet flushed to DB

    def read(self, key):
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        value = self.db.read(key)
        if value is not None:
            self.cache.put(key, value)
        return value

    def write(self, key, value):
        self.cache.put(key, value)        # cache updated instantly -> caller unblocked fast
        self._dirty[key] = value          # remember we owe the DB this write
        # Note: we return here WITHOUT touching self.db at all. That's the
        # entire trick of write-back — the slow part is deferred.

    def flush(self):
        """
        Explicitly simulate the background flush job that a real write-back
        cache runs on a timer (e.g. every few seconds) or when the dirty
        buffer gets large. Call this manually in the demo to make the
        "writes happen later" behaviour visible.
        """
        for key, value in self._dirty.items():
            self.db.write(key, value)     # NOW we pay the slow DB cost, in a batch
        self._dirty.clear()


class WriteAroundStore:
    """
    WRITE-AROUND. Writes go STRAIGHT to the database, completely bypassing
    the cache. The cache only ever gets populated by READS (same lazy-load
    read path as cache-aside).

    Trade-off: good when you write data that's rarely re-read immediately
    after writing (e.g. bulk log ingestion, analytics events) — you avoid
    filling the cache with "write-once, read-never" data that would just
    evict genuinely hot keys. Bad when an app writes a value and then
    immediately re-reads it expecting it to be fast: that read will be a
    guaranteed MISS right after the write.
    """

    def __init__(self, cache: Cache, db: SlowDatabase):
        self.cache = cache
        self.db = db

    def read(self, key):
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        value = self.db.read(key)
        if value is not None:
            self.cache.put(key, value)    # only reads ever warm the cache
        return value

    def write(self, key, value):
        self.db.write(key, value)         # cache is never touched on write
        # Deliberately NOT calling self.cache.put() or even invalidating —
        # the cache simply doesn't know this write happened until some
        # later read pulls the fresh value from the DB.


# =============================================================================
# PART E — CACHE STAMPEDE ("thundering herd") AND HOW TO PREVENT IT
# =============================================================================
#
# THE PROBLEM: imagine a popular cache key expires. In the same instant, 100
# concurrent requests all check the cache, all get a MISS, and all 100 race
# to the database AT ONCE to recompute the same value. The database — which
# the cache was supposed to be protecting — gets hammered by a "stampede" of
# duplicate work, sometimes hard enough to fall over (this has caused real
# production outages at large companies).
#
# THE FIX: a per-key LOCK. The first request to miss acquires the lock and
# goes to the database. Every OTHER concurrent request for the SAME key
# blocks on that lock instead of also hitting the database — then, once the
# first request finishes and populates the cache, the waiters wake up and
# simply read the now-populated cache. Net result: the database is hit
# EXACTLY ONCE per stampede, no matter how many requests piled up.

class StampedeSafeStore:
    """
    Cache-aside, PLUS a per-key threading.Lock so concurrent misses on the
    same key collapse into a single database read instead of N of them.
    """

    def __init__(self, cache: Cache, db: SlowDatabase):
        self.cache = cache
        self.db = db
        self._locks: dict = {}                 # key -> threading.Lock, created lazily
        self._locks_guard = threading.Lock()    # protects creation of entries in self._locks itself

    def _lock_for(self, key) -> threading.Lock:
        # We need a lock PER KEY (not one global lock for the whole cache,
        # which would serialize unrelated keys and defeat the purpose of
        # caching). Creating that per-key lock is itself a tiny critical
        # section, so we guard it with one small global lock just for the
        # bookkeeping dict — this lock is held for microseconds, not for
        # the slow DB call.
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def read(self, key):
        cached = self.cache.get(key)
        if cached is not None:
            return cached                       # fast path: unchanged from cache-aside

        key_lock = self._lock_for(key)
        with key_lock:
            # IMPORTANT: re-check the cache AFTER acquiring the lock.
            # Why? Because while we were waiting for the lock, the request
            # that got here FIRST may have already finished and populated
            # the cache. Without this re-check, every waiter would still
            # redundantly call the database once the lock frees up — this
            # second check is what actually collapses N calls into 1.
            cached = self.cache.get(key)
            if cached is not None:
                return cached

            value = self.db.read(key)           # only the FIRST thread ever reaches here
            if value is not None:
                self.cache.put(key, value)
            return value


# =============================================================================
# PART F — A NOTE ON DISTRIBUTED CACHING (preview of the next module)
# =============================================================================
# Everything above lives in ONE process's memory. That's fine until your
# traffic outgrows one machine. Real systems then run a cache CLUSTER
# (e.g. multiple Redis nodes) and need a rule for "which node holds key X?".
# That rule is almost always CONSISTENT HASHING — the exact same technique
# used to route traffic to servers in load balancing. We build consistent
# hashing in the next file (sd02_load_balancing.py) so you see it solve BOTH
# problems with one idea, which is a great thing to point out in an
# interview: "load balancing and distributed caching both reduce to the same
# partitioning problem."


# =============================================================================
# DEMO — run this file directly to see every concept execute
# =============================================================================
if __name__ == "__main__":

    def section(title):
        print("\n" + "=" * 70)
        print(title)
        print("=" * 70)

    # --- 1. Eviction policies ------------------------------------------------
    section("1. LRU eviction: capacity=3")
    lru = LRUCache(capacity=3)
    for k in ["a", "b", "c"]:
        lru.put(k, k.upper())
    lru.get("a")                      # touching "a" makes it recently-used again
    lru.put("d", "D")                 # cache is full -> must evict someone
    # "b" is now the least-recently-used (a was refreshed, c and d are newer)
    # so "b" should be the one evicted:
    print("b present after eviction? ->", lru.get("b") is not None)  # expect False
    print("a present after eviction? ->", lru.get("a") is not None)  # expect True
    print(f"hit ratio so far: {lru.hit_ratio():.2f}")

    section("2. FIFO eviction: capacity=3")
    fifo = FIFOCache(capacity=3)
    for k in ["a", "b", "c"]:
        fifo.put(k, k.upper())
    fifo.get("a")                     # in FIFO, reading does NOT protect "a"
    fifo.put("d", "D")                # "a" was inserted first -> "a" gets evicted, not "b"
    print("a present after eviction? ->", fifo.get("a") is not None)  # expect False

    section("3. LFU eviction: capacity=2")
    lfu = LFUCache(capacity=2)
    lfu.put("hot", 1)
    lfu.put("cold", 1)
    lfu.get("hot"); lfu.get("hot"); lfu.get("hot")   # "hot" read 3 extra times -> frequency builds up
    lfu.put("new", 1)                 # cache full -> evict the LOWEST-frequency key ("cold")
    print("cold present after eviction? ->", lfu.get("cold") is not None)  # expect False
    print("hot present after eviction?  ->", lfu.get("hot") is not None)   # expect True

    # --- 2. TTL ---------------------------------------------------------------
    section("4. TTL expiry (0.3s lifetime)")
    ttl_cache = TTLCache(LRUCache(capacity=10), default_ttl_seconds=0.3)
    ttl_cache.put("session_token", "abc123")
    print("immediately after put:", ttl_cache.get("session_token"))
    time.sleep(0.4)                   # let it go stale
    print("after 0.4s (expired):", ttl_cache.get("session_token"))  # expect None

    # --- 3. The four read/write strategies, timed to make latency visible ----
    section("5. Cache-aside: first read slow, second read fast")
    db = SlowDatabase(latency_seconds=0.2)
    db.write("user:1", "Navya")
    store = CacheAsideStore(LRUCache(capacity=100), db)

    t0 = time.time()
    store.read("user:1")              # MISS -> hits the slow DB
    print(f"first read (miss):  {time.time() - t0:.3f}s")

    t0 = time.time()
    store.read("user:1")              # HIT -> served from memory, no sleep()
    print(f"second read (hit):  {time.time() - t0:.3f}s")

    section("6. Write-through: write pays DB cost immediately")
    wt_store = WriteThroughStore(LRUCache(capacity=100), SlowDatabase(0.2))
    t0 = time.time()
    wt_store.write("price:sku42", 19.99)
    print(f"write-through write: {time.time() - t0:.3f}s (pays DB latency now)")
    t0 = time.time()
    wt_store.read("price:sku42")      # already in cache from the write above -> instant
    print(f"read right after:    {time.time() - t0:.3f}s (already cached)")

    section("7. Write-back: write is instant, DB pays later via flush()")
    wb_db = SlowDatabase(0.2)
    wb_store = WriteBackStore(LRUCache(capacity=100), wb_db)
    t0 = time.time()
    wb_store.write("counter:views", 1)
    print(f"write-back write:    {time.time() - t0:.3f}s (DB untouched so far)")
    print("value in DB before flush:", wb_db.read("counter:views"))  # expect None
    wb_store.flush()                  # simulate the background flush job running
    print("value in DB after flush: ", wb_db.read("counter:views"))  # expect 1

    section("8. Write-around: write bypasses cache, next read is a guaranteed miss")
    wa_store = WriteAroundStore(LRUCache(capacity=100), SlowDatabase(0.2))
    wa_store.write("event:click", {"x": 10, "y": 20})
    t0 = time.time()
    wa_store.read("event:click")      # MISS even though we JUST wrote it (cache was bypassed)
    print(f"read right after write-around: {time.time() - t0:.3f}s (still slow -> cache was skipped on write)")

    # --- 4. Cache stampede protection -----------------------------------------
    section("9. Cache stampede: WITHOUT protection, N threads all hit the DB")
    shared_db = SlowDatabase(latency_seconds=0.3)
    shared_db.write("trending_post", "...")
    naive_store = CacheAsideStore(LRUCache(capacity=10), shared_db)

    call_count = {"n": 0}
    real_read = shared_db.read
    def counting_read(key):          # wrap SlowDatabase.read just to COUNT calls for this demo
        call_count["n"] += 1
        return real_read(key)
    shared_db.read = counting_read

    threads = [threading.Thread(target=naive_store.read, args=("trending_post",)) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    print(f"database reads triggered by 8 concurrent misses (NO lock): {call_count['n']}")
    # Expect close to 8 — all threads raced past the cache check before any
    # of them finished populating the cache.

    section("10. Cache stampede: WITH per-key lock protection")
    shared_db2 = SlowDatabase(latency_seconds=0.3)
    shared_db2.write("trending_post", "...")
    call_count2 = {"n": 0}
    real_read2 = shared_db2.read
    def counting_read2(key):
        call_count2["n"] += 1
        return real_read2(key)
    shared_db2.read = counting_read2

    safe_store = StampedeSafeStore(LRUCache(capacity=10), shared_db2)
    threads = [threading.Thread(target=safe_store.read, args=("trending_post",)) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    print(f"database reads triggered by 8 concurrent misses (WITH lock): {call_count2['n']}")
    # Expect exactly 1 — the lock collapsed 8 concurrent misses into 1 DB call.
