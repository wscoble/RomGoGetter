"""
pipeline.py — Search → filter → select → download.

Owns the indexer's core algorithm. Re-uses the fork's `rgg` module for
all listing parsing and filtering logic; we don't reimplement it.

State machine for each grab:
    REGISTERED  →  DOWNLOADING  →  COMPLETED  |  FAILED  |  USER_STOPPED
        (1)         (3)             (6)            (0)         (0)

CRASH RECOVERY: on startup, any grab left in REGISTERED or DOWNLOADING is
flipped to FAILED with error_string="interrupted by restart". Questarr sees
the error and the user re-grabs.

RETRY: transient HTTP/connection errors are retried with exponential backoff
(up to 3 attempts). Permanent errors (HTTP 404, malformed URL) fail fast.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Wire sys.path to find rgg.py + sibling indexer modules
_APP_ROOT = os.environ.get("RGG_APP_ROOT", "/app")
for _p in (_APP_ROOT, os.path.join(_APP_ROOT, "indexer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rgg  # type: ignore  # noqa: E402
import state  # type: ignore  # noqa: E402

EXECUTOR = ThreadPoolExecutor(max_workers=int(os.environ.get("RGG_WORKERS", "4")))
NAS_ROOT = Path(os.environ.get("RGG_NAS_ROOT", "/mnt/shared/roms"))
NAS_ROOT.mkdir(parents=True, exist_ok=True)

# Search knobs
MIN_SCORE_THRESHOLD = float(os.environ.get("RGG_MIN_SCORE", "0.30"))
MAX_RESULTS_PER_GROUP = int(os.environ.get("RGG_MAX_PER_GROUP", "50"))
MAX_LISTING_RETRIES = 3
LISTING_RETRY_BASE_DELAY = 2.0   # seconds; doubled each attempt
LISTING_FETCH_TIMEOUT = 60      # seconds per attempt

# Categories advertised in Torznab caps (set once at startup)
# Heuristic mapping from group name → category id
CATEGORY_HEURISTIC = [
    # (substring, category_id)  — first match wins
    (r"(?i)playstation\s*3|ps3",        "1020,1022,1023"),   # PS3
    (r"(?i)playstation\s*2|ps2",        "1020,1021"),        # PS2
    (r"(?i)playstation|ps1|psp",        "1020,1040"),        # PS1/PSP
    (r"(?i)xbox\s*360",                 "1050,1051"),
    (r"(?i)xbox\b",                     "1050"),
    (r"(?i)wii[\s_-]*u",                "1031"),
    (r"(?i)wii\b",                      "1030"),
    (r"(?i)3ds",                        "1033"),
    (r"(?i)nintendo\s*ds|^nds",         "1032"),
    (r"(?i)ps[\s_-]*vita",              "1041"),
    (r"(?i)atari",                      "1040,1000"),
    (r"(?i)tekno.?parrot",              "4000"),
    (r"(?i)gamecube|ngc",               "1035"),
]


def _category_for(group_name: str) -> str:
    for pattern, cat in CATEGORY_HEURISTIC:
        if re.search(pattern, group_name):
            return cat
    return "1000,4000,5000"   # generic console + PC + other


# === Group loading ===

def load_groups() -> list[dict]:
    """Load preset groups from RomGoGetter_groups.json at /app/RomGoGetter_groups.json.

    Each group entry can have multiple URLs (sharded A/B/C, or 1G1R archives
    split by letter). We create one group entry PER URL so the search
    handler iterates every shard — otherwise we'd miss games whose title
    starts with a letter that's not in the first shard.
    """
    groups_path = Path(_APP_ROOT) / "RomGoGetter_groups.json"
    if not groups_path.exists():
        print(f"[pipeline] WARN: {groups_path} not found — empty group list")
        return []

    with open(groups_path) as f:
        raw = json.load(f)

    groups = []
    for name, urls_text in raw.items():
        urls = [u.strip() for u in urls_text.strip().splitlines() if u.strip()]
        if not urls:
            continue
        # One group entry per URL. The category is the same for all shards
        # of a logical group (e.g. all PS2 archive.org shards are PS2).
        cat = _category_for(name)
        for shard_idx, url in enumerate(urls):
            # Skip the known-broken TeknoParrot Archive shard (HTTP 404).
            # The other TeknoParrot shards work.
            if 'tp-roms_0/TeknoParrot/' in url:
                continue
            shard_name = name if len(urls) == 1 else f"{name} #{shard_idx + 1}"
            groups.append({
                "name": shard_name,
                "listing_url": url,
                "all_urls": urls,
                "category": cat,
            })
    print(f"[pipeline] loaded {len(groups)} group shards from {groups_path}")
    return groups


GROUPS: list[dict] = []   # populated by startup_recover()


# === Subset torrents (for Minerva groups) ===

SUBSET_TORRENT_DIR = Path(os.environ.get("RGG_DATA_DIR", "/app/data")) / "subtorrents"
SUBSET_TORRENT_DIR.mkdir(parents=True, exist_ok=True)
SUBSET_TORRENT_BASE_URL = os.environ.get("RGG_PUBLIC_URL", "").rstrip("/")


def _subset_cache_key(browse_url: str, rel_path: str) -> str:
    """Stable hash for (browse_url, rel_path). Used as filename and URL slug."""
    import hashlib
    h = hashlib.sha256()
    h.update(browse_url.encode("utf-8"))
    h.update(b"\x00")
    h.update(rel_path.encode("utf-8"))
    return h.hexdigest()[:16]


def get_or_build_subset_torrent(*, browse_url: str, rel_path: str,
                                display_name: str, size_bytes: int) -> str | None:
    """Build (or reuse cached) single-file subset torrent for one Minerva entry.

    Returns the public URL (e.g. "http://host:9696/torrents/<key>.torrent") that
    Questarr/Transmission can fetch. The torrent contains only the single file
    matching `rel_path`, with correct piece hashes — so Transmission will
    download JUST that file via BitTorrent, not the whole collection.

    Args:
        browse_url: The Minerva browse URL (e.g. .../Redump/Sony - PlayStation 3/).
        rel_path:   The relative path inside the parent torrent
                    (e.g. "Redump/Sony - PlayStation 3/Shadow of the Colossus.zip").
        display_name: Filename shown to the user (basename of rel_path).
        size_bytes:  Size of the file in bytes (for info / logging).

    Returns:
        Absolute URL string pointing to the subset torrent, or None on failure.
    """
    key = _subset_cache_key(browse_url, rel_path)
    out_path = SUBSET_TORRENT_DIR / f"{key}.torrent"
    if out_path.exists():
        return f"{SUBSET_TORRENT_BASE_URL}/torrents/{key}.torrent"

    # 1. Find the parent torrent URL
    parent_url = rgg.minerva_torrent_url(browse_url)
    if not parent_url:
        print(f"[pipeline] no parent torrent URL for {browse_url}")
        return None

    # 2. Download the parent torrent (cached locally too — same parent for many files).
    # If prefetch already populated it, this is a fast no-op.
    parent_key = _subset_cache_key(parent_url, "")
    parent_path = SUBSET_TORRENT_DIR / f"_parent_{parent_key}.torrent"
    if not parent_path.exists():
        print(f"[pipeline] downloading parent torrent: {parent_url}", flush=True)
        try:
            req = urllib.request.Request(parent_url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                parent_data = r.read()
            parent_path.write_bytes(parent_data)
        except Exception as e:
            print(f"[pipeline] parent torrent download failed: {e}")
            return None
    else:
        parent_data = parent_path.read_bytes()

    # 3. Build subset torrent
    # rgg.make_subset_torrent expects a set of filenames; try the rel_path first,
    # then the basename (rel_path may use full path or just basename inside the torrent).
    rel_basename = os.path.basename(rel_path)
    subset_data = rgg.make_subset_torrent(parent_data, {rel_path, rel_basename})
    out_path.write_bytes(subset_data)
    print(f"[pipeline] built subset torrent for {display_name}: {len(subset_data):,} bytes", flush=True)

    return f"{SUBSET_TORRENT_BASE_URL}/torrents/{key}.torrent"


# === Listing fetch (with retry) ===

def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return 500 <= exc.code < 600 or exc.code == 429
    if isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError)):
        return True
    return False


def _fetch_listing_sync(url: str) -> tuple[list, str | None]:
    """Fetch + parse a listing URL using the fork's logic. Retries on transient errors.

    rgg.fetch_url_cached is in-process cached (per pod lifetime), so once
    a listing is fetched it's reused for the rest of the pod's life.
    """
    last_err = None
    for attempt in range(1, MAX_LISTING_RETRIES + 1):
        try:
            result = rgg.fetch_url_cached(url)
            # Sanity check: must be (list, str|None)
            if not (isinstance(result, tuple) and len(result) == 2):
                print(f"[pipeline] BAD fetch result for {url[:60]}: type={type(result).__name__}, val={result!r}"[:200])
                return [], None
            entries, title = result
            if not isinstance(entries, list):
                print(f"[pipeline] BAD entries for {url[:60]}: type={type(entries).__name__}, val={entries!r}"[:200])
                return [], None
            return result
        except Exception as e:
            last_err = e
            if not _is_retryable(e):
                print(f"[pipeline] listing fetch non-retryable for {url[:60]}: {e}")
                return [], None
            delay = LISTING_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            print(f"[pipeline] listing fetch attempt {attempt} failed for {url[:60]}: {e} "
                  f"(retrying in {delay}s)")
            time.sleep(delay)
    print(f"[pipeline] listing fetch gave up after {MAX_LISTING_RETRIES} attempts: {url[:60]}: {last_err}")
    return [], None


async def _fetch_listing(url: str) -> tuple[list, str | None]:
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(EXECUTOR, _fetch_listing_sync, url)
    except Exception:
        return [], None


# === Search ===

def _score(bare_a: str, bare_b: str) -> float:
    """Mirror rgg._igdb_score: tokenize + Jaccard + SequenceMatcher."""
    from difflib import SequenceMatcher
    import unicodedata as _ud

    def norm(s: str) -> str:
        s = _ud.normalize("NFKD", s)
        s = "".join(c for c in s if not _ud.combining(c))
        s = re.sub(
            r"\b(limited|collector'?s?|complete|definitive|enhanced|ultimate|platinum|"
            r"directors?|remastered|anniversary|expanded|extended|bundle|"
            r"digital|gam(e|ing) of the year|goty|deluxe)\s*(edition|cut)?\b",
            "", s, flags=re.IGNORECASE,
        )
        s = re.sub(r"[\-:]\s*(the|a|an)\s*$", "", s, flags=re.IGNORECASE)
        s = re.sub(r"[\s\-:]+$", "", s)
        s = re.sub(r"\s{2,}", " ", s)
        return s.lower().strip()

    def tokens(s: str) -> set[str]:
        s = re.sub(r"[:\-\u00b7\u2013\u2014,'\"()!]", " ", norm(s))
        return set(re.findall(r"[a-z0-9]+", s))

    ta, tb = tokens(bare_a), tokens(bare_b)
    jaccard = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    na = re.sub(r"[^a-z0-9]", "", norm(bare_a))
    nb = re.sub(r"[^a-z0-9]", "", norm(bare_b))
    fuzzy = SequenceMatcher(None, na, nb).ratio() if na and nb else 0.0
    return 0.5 * jaccard + 0.5 * fuzzy


def _strip_bare(filename: str) -> str:
    """Strip .zip, then trailing (USA), (Europe), (Rev A), etc."""
    base = re.sub(r"\.zip$", "", filename, flags=re.IGNORECASE)
    base = re.sub(r"\s*\([^)]*\)\s*$", "", base).strip()
    return base


class _StubApp(rgg.App):
    """Minimal stand-in for rgg.App so _apply_filter can mutate state.

    We can't run App.__init__ (it builds a real Tk UI). Instead we extract
    just the attributes that _apply_filter transitively reads:

        raw_file_entries     # the input
        exclude_dir          # tk.StringVar-like with .get() returning str
        _debug               # log hook
        _is_locally_owned    # called from _apply_filter
        _build_exclude_titles # called by _is_locally_owned
        root.after / setup_status.config  # UI progress updates (no-op)

    We bypass __init__ entirely with __new__ + manual attribute setup.
    """
    def __new__(cls, entries):
        # Skip App.__init__ entirely — it requires a live Tk root.
        instance = object.__new__(cls)
        instance.raw_file_entries = entries
        # exclude_dir is read via self.exclude_dir.get().strip() — make it
        # a tiny StringVar stand-in. Empty string = nothing excluded.
        class _EmptyVar:
            def get(self): return ""
        instance.exclude_dir = _EmptyVar()
        instance._excl_titles_cache = None
        instance._excl_cnorms = set()
        return instance

    def __init__(self, entries):
        # __new__ already did the work; suppress App.__init__ from being
        # called automatically by Python's class machinery.
        pass

    def _debug(self, *a, **k):
        pass

    def _is_locally_owned(self, fname: str) -> bool:
        return False

    def _build_exclude_titles(self):
        pass

    @property
    def setup_status(self):
        # self.setup_status.config(text=...) is called in _apply_filter
        # for UI progress. We mock it to a no-op.
        class _NoConfig:
            def config(self, **kw): pass
        return _NoConfig()

    @property
    def root(self):
        class _NoRoot:
            def after(self, *a, **k):
                # The real code uses root.after(0, callable) to schedule UI
                # updates on the Tk thread. We don't have one, so run immediately.
                if len(a) >= 2 and callable(a[1]):
                    try:
                        a[1]()
                    except Exception:
                        pass
        return _NoRoot()


# === Candidate precompute (query-independent) ===
#
# _apply_filter is the expensive CPU-bound step (~0.3-0.4s per group) and
# does NOT depend on the search query. With 190 group shards that's ~70s of
# work on a single uvicorn worker — which blocks the event loop and kills
# health probes. So we run it ONCE per group at prewarm time, cache the
# resulting "candidate rows" (title, filename, url, size), and at search
# time we only score cached rows against the query (pure string matching).
_CANDIDATE_CACHE: dict[str, list[dict]] = {}


def _build_candidates(entries: list) -> list[dict]:
    """Run the query-independent filtering once and return candidate rows.

    Each row: {title, best_filename, direct_url, size_int}
    `title` is the normalized title from _apply_filter (no region/edition markers).
    """
    try:
        app = _StubApp(entries)
        rom_dict, _summary = app._apply_filter(entries, "1G1R English only")
    except Exception as e:
        print(f"[pipeline] _apply_filter failed: {e}")
        return []
    try:
        from rgg import parse_size_bytes  # type: ignore  # noqa: E402
    except Exception:
        parse_size_bytes = lambda s: 0  # noqa: E731
    out: list[dict] = []
    for title, variants_dict in rom_dict.items():
        if not isinstance(variants_dict, dict):
            continue
        instances = variants_dict.get("instances") or []
        if not instances:
            continue
        best = rgg.select_best(instances) or {}
        best_filename = best.get("filename")
        if not best_filename:
            continue
        direct_url = None
        for inst in instances:
            if inst.get("filename") == best_filename:
                direct_url = inst.get("direct_url")
                break
        if not direct_url:
            continue
        size_str = best.get("size", "0") or "0"
        try:
            size_int = parse_size_bytes(size_str)
        except Exception:
            size_int = 0
        out.append({
            "title": title,
            "best_filename": best_filename,
            "direct_url": direct_url,
            "size_int": size_int,
        })
    return out


async def search(query: str) -> list[dict]:
    """Search across all loaded groups for ROMs matching `query`.

    Returns Torznab-compatible dicts:
      {title, guid, link, size, pubDate, category, magnet?, torrent?, indexer, score}
    """
    if not query.strip():
        return []

    results: list[dict] = []
    # Run groups concurrently — bounded by a small semaphore to avoid hammering
    sem = asyncio.Semaphore(4)

    async def _search_group(group: dict) -> list[dict]:
        async with sem:
            listing_url = group["listing_url"]
            # Use the precomputed candidate cache if available (populated at
            # prewarm). This avoids the ~0.4s/group _apply_filter cost on
            # every search. On a cache miss (e.g. a group that failed to
            # prewarm), fall back to fetching + building on demand.
            candidates = _CANDIDATE_CACHE.get(listing_url)
            if candidates is None:
                # Group hasn't been prewarmed yet. Skip it rather than
                # falling back to a synchronous fetch+filter, which would block
                # the search for ~7s/group and blow the 30s Torznab budget.
                # Prewarm runs in the background and will fill the cache; the
                # next search will include this group.
                return []
            if not candidates:
                return []
            try:
                out: list[dict] = []
                for row in candidates:
                    best_filename = row["best_filename"]
                    direct_url = row["direct_url"]
                    size_int = row["size_int"]
                    sc = _score(row["title"], query)
                    if sc < MIN_SCORE_THRESHOLD:
                        continue
                    # Minerva groups are disabled in this fork (parent torrents
                    # too slow). Skip any Minerva-sourced entry entirely.
                    if rgg.is_minerva_url(listing_url):
                        continue
                    out.append({
                        "title": best_filename,
                        "guid": best_filename,
                        "link": direct_url,
                        "size": size_int,
                        "pubDate": "",
                        "category": group["category"],
                        "indexer": group["name"],
                        "score": sc,
                    })
                out.sort(key=lambda x: -x["score"])
                return out[:MAX_RESULTS_PER_GROUP]
            except Exception as e:
                print(f"[pipeline] scoring failed for {group['name']}: {e}")
                return []

    group_results = await asyncio.gather(
        *[_search_group(g) for g in GROUPS],
        return_exceptions=True,
    )
    for r in group_results:
        if isinstance(r, Exception):
            import traceback
            print(f"[pipeline] group search exception: {type(r).__name__}: {r}")
            print(f"  traceback:\n{traceback.format_exc()}")
            continue
        results.extend(r)

    # Sort by score desc, then by title for stability
    results.sort(key=lambda x: (-x["score"], x["title"]))
    return results


# === Download (background, with retry) ===

async def grab(*, indexer_id: str, indexer_name: str, guid: str, url: str,
               title: str, download_dir: str | None = None) -> state.Grab:
    """Register a download and schedule background work.

    Returns the Grab immediately with status=1 (queued). The actual download
    happens in a worker thread that updates the state.json file.
    """
    if download_dir is None:
        download_dir = str(NAS_ROOT / _safe_dirname(indexer_name))
    # Defend against path traversal in title (Questarr sends arbitrary strings)
    safe_title = _safe_filename(title)
    safe_dir = _safe_dirname(download_dir)
    Path(safe_dir).mkdir(parents=True, exist_ok=True)

    grab_obj = state.Grab(
        indexer_id=indexer_id,
        indexer_name=indexer_name,
        guid=guid,
        url=url,
        title=safe_title,
        download_dir=safe_dir,
    )
    grab_obj.status = 1   # queued
    state.upsert(grab_obj)

    # Schedule background work — never block the RPC response
    loop = asyncio.get_event_loop()
    loop.run_in_executor(EXECUTOR, _do_download, grab_obj.id)
    return grab_obj


def _safe_dirname(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._/-]+", "_", s).strip("/_")
    if not s:
        return "unknown"
    # No parent refs
    parts = [p for p in s.split("/") if p and p not in ("..", ".")]
    return "/".join(parts) or "unknown"


def _safe_filename(s: str) -> str:
    """Sanitize filename. Reject anything that smells like path traversal."""
    s = re.sub(r"[/\\]", "_", s).strip()
    s = re.sub(r"\.\.+", ".", s)
    return s or "download.bin"


def _do_download(gid: str) -> None:
    """Sync worker. Updates Grab fields + state.json. Retries transient errors."""
    grab = state.get(gid)
    if not grab:
        return

    try:
        grab.status = 3   # downloading
        state.upsert(grab)

        if grab.url.startswith("magnet:"):
            _download_via_aria2c(grab)
        elif grab.url.endswith(".torrent"):
            _download_via_aria2c(grab)
        else:
            _download_via_urllib(grab)

        # Success
        grab.status = 6        # seeding-done
        grab.percent_done = 1.0
        grab.eta = 0
        grab.rate_download = 0
    except Exception as e:
        grab.status = 0        # stopped/failed
        grab.error_string = str(e)[:512]
    finally:
        state.upsert(grab)


def _download_via_urllib(grab: state.Grab) -> None:
    """Stream URL → file, updating progress."""
    dest = Path(grab.download_dir) / grab.title
    req = urllib.request.Request(grab.url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    })
    with urllib.request.urlopen(req, timeout=600) as r:
        total = int(r.headers.get("Content-Length", "0") or 0)
        grab.total_size = total
        grabbed = 0
        t0 = time.time()
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                grabbed += len(chunk)
                elapsed = time.time() - t0
                grab.downloaded_ever = grabbed
                grab.rate_download = int(grabbed / elapsed) if elapsed > 0 else 0
                grab.eta = int((total - grabbed) / grab.rate_download) if grab.rate_download > 0 and total > 0 else -1
                grab.percent_done = (grabbed / total) if total > 0 else 0
                if grabbed % (1024 * 1024) < 64 * 1024:
                    state.upsert(grab)
        if grab.total_size == 0:
            grab.total_size = grabbed
        grab.percent_done = 1.0
        grab.files = [{
            "name": grab.title,
            "length": grabbed,
            "bytesCompleted": grabbed,
        }]


def _download_via_aria2c(grab: state.Grab) -> None:
    """Use aria2c for .torrent / magnet sources (BitTorrent path)."""
    args = ["aria2c", "--dir", grab.download_dir, "--out", grab.title,
            "--summary-interval=1", "--console-log-level=warn"]
    if grab.url.endswith(".torrent"):
        args.extend(["--select-file=1"])
    args.append(grab.url)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        m = re.search(r"\((\d+)%\).*?DL:(\S+).*?ETA:(\S+)\)", line)
        if m:
            grab.percent_done = int(m.group(1)) / 100.0
            grab.rate_download = _parse_rate(m.group(2))
            grab.eta = _parse_eta(m.group(3))
            state.upsert(grab)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"aria2c exited {proc.returncode}")
    dest = Path(grab.download_dir) / grab.title
    grab.total_size = dest.stat().st_size
    grab.downloaded_ever = grab.total_size
    grab.files = [{
        "name": grab.title,
        "length": grab.total_size,
        "bytesCompleted": grab.total_size,
    }]


def _parse_rate(s: str) -> int:
    m = re.match(r"([\d.]+)(KiB|MiB|GiB|B|KB|MB|GB)", s)
    if not m:
        return 0
    n, unit = float(m.group(1)), m.group(2)
    mult = {"B": 1, "KB": 1000, "KiB": 1024, "MB": 1_000_000, "MiB": 1_048_576,
            "GB": 1_000_000_000, "GiB": 1_073_741_824}[unit]
    return int(n * mult)


def _parse_eta(s: str) -> int:
    m = re.match(r"(\d+)(s|m|h)", s)
    if not m:
        return -1
    n, unit = int(m.group(1)), m.group(2)
    return {"s": n, "m": n * 60, "h": n * 3600}[unit]


# === Startup recovery ===

def startup_recover() -> None:
    """Scan state.json for any grabs left in REGISTERED (1) or DOWNLOADING (3).

    The previous process exited without finishing them. Mark them FAILED so
    Questarr sees the error and the user can re-grab.
    """
    GROUPS.clear()
    GROUPS.extend(load_groups())

    recovered = 0
    for grab in state.all_grabs():
        if grab.status in (1, 3):     # queued or downloading
            grab.status = 0
            grab.error_string = "interrupted by restart"
            state.upsert(grab)
            recovered += 1
    if recovered:
        print(f"[pipeline] startup recovery: marked {recovered} in-flight grab(s) as FAILED")
    else:
        print(f"[pipeline] startup recovery: no in-flight grabs")

    # Pre-warm all listings in parallel. rgg.fetch_url_cached is in-process
    # cached, so once we trigger the first fetch for each group, all subsequent
    # searches hit the cache and return in milliseconds. Without this, the
    # FIRST search takes ~30s to fetch all listings sequentially.
    # Pre-warm all listings IN PARALLEL and precompute candidates.
    # rgg.fetch_url_cached is in-process cached (per pod lifetime). Each
    # archive.org listing takes ~6-7s to fetch from the pod; with 190 group
    # shards that's ~22 min sequential, which makes the first ~20 min of
    # pod life return partial results. Parallelizing with 8 threads cuts
    # warmup to ~3 min.
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _PREWARM_WORKERS = 8

    def _prewarm_one(group: dict) -> tuple[str, str]:
        try:
            t0 = time.time()
            entries, _ = rgg.fetch_url_cached(group["listing_url"])
            cands = _build_candidates(entries)
            _CANDIDATE_CACHE[group["listing_url"]] = cands
            dt = time.time() - t0
            return (f"[pipeline] prewarm {group['name']}: {len(entries)} entries, {len(cands)} candidates in {dt:.2f}s", "")
        except Exception as e:
            return ("", f"[pipeline] prewarm failed for {group['name']}: {e}")

    def _prewarm_listings():
        with ThreadPoolExecutor(max_workers=_PREWARM_WORKERS) as ex:
            futures = [ex.submit(_prewarm_one, g) for g in GROUPS]
            for fut in futures:
                ok, err = fut.result()
                if ok:
                    print(ok, flush=True)
                if err:
                    print(err, flush=True)
    threading.Thread(target=_prewarm_listings, daemon=True).start()