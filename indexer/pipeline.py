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

    Each group has multiple URLs (sometimes sharded A/B/C). We take the first
    URL of each group as the listing URL.
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
        groups.append({
            "name": name,
            "listing_url": urls[0],   # first shard only for now
            "all_urls": urls,
            "category": _category_for(name),
        })
    print(f"[pipeline] loaded {len(groups)} groups from {groups_path}")
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
            entries, _title = await _fetch_listing(group["listing_url"])
            if not entries:
                return []
            # Sanity check: entries should be a list of 3-tuples
            if not isinstance(entries, list):
                return []
            if entries and not isinstance(entries[0], tuple):
                return []
            try:
                app = _StubApp(entries)
                rom_dict, _summary = app._apply_filter(entries, "1G1R English only")
            except Exception as e:
                print(f"[pipeline] _apply_filter failed for {group['name']}: {e}")
                return []
            try:
                from rgg import parse_size_bytes  # type: ignore  # noqa: E402
                out: list[dict] = []
                for title, variants_dict in rom_dict.items():
                    # rom_dict structure from _apply_filter is:
                    #   {title: {'selected': ..., 'instances': [list of dicts]}}
                    # `title` here is the already-normalized title from
                    # _apply_filter (clean: no (USA), no edition markers).
                    if not isinstance(variants_dict, dict):
                        continue
                    instances = variants_dict.get("instances") or []
                    if not instances:
                        continue
                    best = rgg.select_best(instances) or {}
                    # rgg.select_best returns {'filename', 'size'} only — direct_url
                    # is NOT included. Look it up from the original instance list.
                    direct_url = None
                    best_filename = best.get("filename")
                    if best_filename:
                        for inst in instances:
                            if inst.get("filename") == best_filename:
                                direct_url = inst.get("direct_url")
                                break
                    if not direct_url:
                        continue
                    sc = _score(title, query)
                    if sc < MIN_SCORE_THRESHOLD:
                        continue
                    # best["size"] from select_best is a human-readable string like '244.1M'.
                    # Parse to bytes; fall back to 0 if unparseable.
                    size_str = best.get("size", "0") or "0"
                    try:
                        size_int = parse_size_bytes(size_str)
                    except Exception:
                        size_int = 0
                    # For Minerva groups, fetch_minerva_filenames stores the
                    # relative path *inside* the BitTorrent (e.g.
                    # "Redump/Sony - PlayStation 3/file.zip"). That path is NOT
                    # a URL — Questarr/Transmission can't fetch it directly.
                    #
                    # Approach: pre-build a SINGLE-FILE subset torrent that
                    # contains only the matching file, served at
                    # /torrents/<sha>.torrent. The downloader (Transmission)
                    # will then download just that one file via BitTorrent.
                    #
                    # This is lazy + cached: the first search that needs a
                    # given (group, file) pair builds the subset torrent; later
                    # searches reuse it.
                    if rgg.is_minerva_url(group["listing_url"]):
                        # Look up the relative path for this match
                        rel_path = None
                        for inst in instances:
                            if inst.get("filename") == best_filename:
                                rel_path = inst.get("direct_url")
                                break
                        if not rel_path:
                            continue
                        # Build the subset torrent synchronously. Parent torrents are
                        # pre-fetched at startup (see startup_recover), so the only
                        # blocking work is the in-memory subset creation (fast for
                        # a single file).
                        try:
                            subset_url = get_or_build_subset_torrent(
                                browse_url=group["listing_url"],
                                rel_path=rel_path,
                                display_name=best_filename,
                                size_bytes=size_int,
                            )
                        except Exception as e:
                            print(f"[pipeline] subset torrent build failed for {best_filename}: {e}")
                            continue
                        if not subset_url:
                            continue
                        direct_url = subset_url
                        entry_title = best_filename
                    else:
                        entry_title = best_filename
                    out.append({
                        "title": entry_title,
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
                print(f"[pipeline] post-filter failed for {group['name']}: {e}")
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

    # Pre-fetch parent torrents for Minerva groups in background.
    # Each parent is 12–14 MB; downloading them in the search path causes
    # 60s+ timeouts. Doing it eagerly at startup means search responses
    # are fast on the first user query.
    import threading
    def _prefetch_one(group):
        url = group["listing_url"]
        if not rgg.is_minerva_url(url):
            return
        parent_url = rgg.minerva_torrent_url(url)
        if not parent_url:
            return
        parent_key = _subset_cache_key(parent_url, "")
        parent_path = SUBSET_TORRENT_DIR / f"_parent_{parent_key}.torrent"
        if parent_path.exists():
            return
        try:
            print(f"[pipeline] prefetch parent torrent: {parent_url}", flush=True)
            req = urllib.request.Request(parent_url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
            })
            with urllib.request.urlopen(req, timeout=120) as r:
                parent_path.write_bytes(r.read())
            print(f"[pipeline] prefetch done: {parent_path}", flush=True)
        except Exception as e:
            print(f"[pipeline] prefetch failed for {parent_url}: {e}", flush=True)
    def _prefetch():
        threads = []
        for group in GROUPS:
            t = threading.Thread(target=_prefetch_one, args=(group,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=180)

    # Also pre-warm all listings in parallel. rgg.fetch_url_cached is in-process
    # cached, so once we trigger the first fetch for each group, all subsequent
    # searches hit the cache and return in milliseconds. Without this, the
    # FIRST search takes ~30s to fetch all listings sequentially.
    def _prewarm_listings():
        for group in GROUPS:
            try:
                t0 = time.time()
                entries, _ = rgg.fetch_url_cached(group["listing_url"])
                dt = time.time() - t0
                print(f"[pipeline] prewarm {group['name']}: {len(entries)} entries in {dt:.2f}s", flush=True)
            except Exception as e:
                print(f"[pipeline] prewarm failed for {group['name']}: {e}", flush=True)

    # Run parent torrent prefetch and listing prewarm in parallel
    t1 = threading.Thread(target=_prefetch, daemon=True)
    t2 = threading.Thread(target=_prewarm_listings, daemon=True)
    t1.start()
    t2.start()