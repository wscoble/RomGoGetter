"""
pipeline.py — The full search → filter → select → download pipeline.

Calls the fork's own `rgg` module so we don't duplicate logic:
  - fetch_url_cached(url)       → archive.org HTML → list of (filename, size, url)
  - rgg._apply_filter(entries, mode) → 1G1R grouped rom_dict
  - rgg.select_best(group)      → single best variant
  - urllib download to NAS path

For the indexer we wrap these in async-friendly functions (the fork is sync;
we run sync work in a thread executor to keep FastAPI non-blocking).
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Re-use the fork's actual code (we run inside the same venv)
import rgg  # type: ignore

from . import state

EXECUTOR = ThreadPoolExecutor(max_workers=int(os.environ.get("RGG_WORKERS", "4")))
NAS_ROOT = Path(os.environ.get("RGG_NAS_ROOT", "/mnt/shared/roms"))
NAS_ROOT.mkdir(parents=True, exist_ok=True)

# 13 archive.org preset groups, sourced from upstream RomGoGetter_groups.json.
# Format: (display_name, archive.org listing URL, category)
# Categories map to Torznab category ids:
#   1000 = Console (general)
#   4000 = PC (general)
#   sub-categories are added for specific platforms when known.
GROUPS = [
    ("Atari 2600 (No-Intro)", "https://archive.org/download/no-intro-atari-2600-20170123/", "1000,1040"),
    ("Sony PlayStation (Redump)", "https://archive.org/download/RedumpSonyPlayStation/", "1000,1020"),
    ("Sony PlayStation 2 (Redump)", "https://archive.org/download/RedumpSonyPlayStation2/", "1000,1021"),
    ("Sony PlayStation 3 (Redump)", "https://archive.org/download/RedumpSonyPlayStation3/", "1000,1022"),
    ("Sony PSP (No-Intro)", "https://archive.org/download/no-intro-psp-20170601/", "1000,1040"),
    ("Sony PSP Minis (No-Intro)", "https://archive.org/download/no-intro-psp-minis-20170601/", "1000,1040"),
    ("Nintendo Wii (No-Intro)", "https://archive.org/download/no-intro-nintendo-wii-20170320/", "1000,1030"),
    ("Nintendo Wii U (No-Intro)", "https://archive.org/download/no-intro-nintendo-wii-u-20170320/", "1000,1031"),
    ("Nintendo DS Decrypted (No-Intro)", "https://archive.org/download/no-intro-nintendo-ds-decrypted-20170320/", "1000,1032"),
    ("Nintendo 3DS Encrypted (No-Intro)", "https://archive.org/download/no-intro-nintendo-3ds-encrypted-20170320/", "1000,1033"),
    ("Microsoft Xbox (Redump)", "https://archive.org/download/RedumpMicrosoftXbox/", "1000,1050"),
    ("Microsoft Xbox 360 (Redump)", "https://archive.org/download/RedumpMicrosoftXbox360/", "1000,1051"),
    ("TeknoParrot Archive", "https://archive.org/download/techparrot/", "4000"),
]


def _list_to_entries(html: str, base_url: str) -> list[tuple[str, str, str]]:
    """Parse archive.org's standard directory listing HTML into (filename, size, url) tuples.

    Same shape as rgg.fetch_url_cached, so downstream rgg._apply_filter just works.
    """
    entries = []
    for m in re.finditer(
        r'<a href="([^"]*?\.zip)">([^<]+)\.zip</a>.*?id="size">(\d+)',
        html, re.DOTALL,
    ):
        raw_url = m.group(1).replace("//archive.org/", "https://archive.org/")
        if raw_url.startswith("/"):
            url = "https://archive.org" + raw_url
        elif not raw_url.startswith("http"):
            url = "https://archive.org/" + raw_url.lstrip("/")
        else:
            url = raw_url
        fname = m.group(2) + ".zip"
        size = m.group(3)
        entries.append((fname, size, url))
    return entries


async def _fetch_listing(listing_url: str) -> tuple[list, str | None]:
    """Fetch archive.org listing HTML → entries (using rgg's fetch_url_cached as a baseline)."""
    def _sync():
        # rgg.fetch_url_cached returns (entries, title) but only handles the
        # `view_archive.php?archive=...` pattern. For the standard directory
        # listing pattern, we fall back to fetching + parsing directly.
        try:
            return rgg.fetch_url_cached(listing_url)
        except Exception:
            pass
        req = urllib.request.Request(listing_url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            html = r.read().decode("utf-8", errors="replace")
        # Determine page title from listing page metadata
        title = None
        m = re.search(r'<title>([^<]+)</title>', html)
        if m:
            title = m.group(1).strip()
        return _list_to_entries(html, listing_url), title

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(EXECUTOR, _sync)


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


async def search(query: str, *, max_results_per_group: int = 50,
                 groups: list[str] | None = None) -> list[dict]:
    """Search across all preset groups for ROMs matching `query`.

    Returns Torznab-compatible result dicts:
      {title, guid, link, size, pubDate, category, magnet?, torrent?}
    """
    chosen = [(name, url, cat) for name, url, cat in GROUPS
              if groups is None or name in groups]
    results: list[dict] = []

    for name, listing_url, category in chosen:
        try:
            entries, _title = await _fetch_listing(listing_url)
        except Exception as e:
            print(f"[pipeline] listing fetch failed for {name}: {e}")
            continue

        if not entries:
            continue

        # Apply the fork's own 1G1R filter
        app = _make_stub_app(entries)
        try:
            rom_dict, _summary = rgg._apply_filter(entries, "1G1R English only")
        except Exception as e:
            print(f"[pipeline] _apply_filter failed for {name}: {e}")
            rom_dict = {}

        # Score each grouped title against the query
        scored: list[tuple[float, dict]] = []
        for title, variants in rom_dict.items():
            best = rgg.select_best(variants) or {}
            if not best.get("direct_url"):
                continue
            sc = _score(title, query)
            if sc < 0.30:        # pre-filter to cut noise before deeper work
                continue
            scored.append((sc, {
                "title": _format_title(title, best),
                "guid": best.get("filename", title),
                "link": best.get("direct_url"),
                "size": int(best.get("size", 0) or 0),
                "pubDate": "",
                "category": category,
                "indexer": name,
                "score": sc,
            }))

        scored.sort(key=lambda x: -x[0])
        for _sc, item in scored[:max_results_per_group]:
            results.append(item)

    # Sort by score descending (Questarr also sorts by seeders, but HTTP results have none)
    results.sort(key=lambda x: -x["score"])
    return results


def _format_title(title: str, best: dict) -> str:
    """Build a clean human-readable title like 'Phantasy Star IV (USA).zip'."""
    fname = best.get("filename") or f"{title}.zip"
    return fname


def _make_stub_app(entries: list) -> object:
    """Construct a minimal App-like object so _apply_filter can mutate state.

    _apply_filter touches self.raw_file_entries and self._debug — nothing else.
    """
    class StubApp:
        pass
    app = StubApp()
    app.raw_file_entries = entries
    app._debug = lambda *a, **k: None
    return app


async def grab(*, indexer_id: str, indexer_name: str, guid: str, url: str,
               title: str) -> state.Grab:
    """Add a 'download' to the registry, kick off background download.

    For HTTP results the pipeline is:
      1. fetch_url_cached on the listing URL we got from the result
      2. _apply_filter 1G1R English only
      3. select_best on the group matching the title
      4. urllib download of select_best's direct_url → NAS_ROOT / title

    For .torrent / magnet results, we'd shell out to aria2c.
    """
    download_dir = str(NAS_ROOT / _safe_dirname(indexer_name))
    Path(download_dir).mkdir(parents=True, exist_ok=True)

    grab_obj = state.Grab(
        indexer_id=indexer_id,
        indexer_name=indexer_name,
        guid=guid,
        url=url,
        title=title,
        download_dir=download_dir,
    )
    grab_obj.status = 1   # queued
    grab_obj.total_size = 0
    state.upsert(grab_obj)

    # Kick off background download — do not await
    asyncio.get_event_loop().run_in_executor(
        EXECUTOR, _do_download, grab_obj.id,
    )
    return grab_obj


def _safe_dirname(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return s or "unknown"


def _do_download(gid: str) -> None:
    """Sync download worker. Updates state.Grab progress fields in-place."""
    grab = state.get(gid)
    if not grab:
        return

    try:
        grab.status = 3   # downloading
        state.upsert(grab)

        # If the URL ends in .torrent or is a magnet, defer to aria2c
        if grab.url.startswith("magnet:"):
            _download_via_aria2c(grab, grab.url)
        elif grab.url.endswith(".torrent"):
            _download_via_aria2c(grab, grab.url)
        else:
            _download_via_urllib(grab)

        grab.status = 6   # seeding-done (we don't seed)
        grab.percent_done = 1.0
        grab.eta = 0
        grab.rate_download = 0
        state.upsert(grab)
    except Exception as e:
        grab.status = 0   # stopped
        grab.error_string = str(e)
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
                # Persist periodically; don't hammer the disk
                if grabbed % (1024 * 1024) < 64 * 1024:
                    state.upsert(grab)
        grab.files = [{
            "name": grab.title,
            "length": grabbed,
            "bytesCompleted": grabbed,
        }]


def _download_via_aria2c(grab: state.Grab, source: str) -> None:
    """Use aria2c for .torrent / magnet sources (BitTorrent path)."""
    import subprocess
    dest = Path(grab.download_dir) / grab.title
    args = [
        "aria2c",
        "--select-file=1" if not source.startswith("magnet:") and source.endswith(".torrent") else None,
        "--dir", grab.download_dir,
        "--out", grab.title,
        "--summary-interval=1",
        "--console-log-level=warn",
        source,
    ]
    args = [a for a in args if a is not None]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        # aria2c prints " [#0 B/123 B (0%) CN:1 DL:0B ETA:30s]" lines we can parse
        m = re.search(r"\((\d+)%\).*?DL:(\S+).*?ETA:(\S+)\)", line)
        if m:
            grab.percent_done = int(m.group(1)) / 100.0
            grab.rate_download = _parse_rate(m.group(2))
            grab.eta = _parse_eta(m.group(3))
            state.upsert(grab)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"aria2c exited {proc.returncode}")
    grab.total_size = dest.stat().st_size
    grab.downloaded_ever = grab.total_size
    grab.files = [{
        "name": grab.title,
        "length": grab.total_size,
        "bytesCompleted": grab.total_size,
    }]


def _parse_rate(s: str) -> int:
    """Parse '1.2KiB', '500B', '2MiB' → bytes/sec."""
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