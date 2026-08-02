"""
transmission.py — Transmission RPC emulator.

Implements just enough of the Transmission RPC spec
(https://github.com/transmission/transmission/blob/master/docs/rpc-spec.md)
for Questarr's TransmissionClient (server/downloaders/transmission.ts) to work:

  session-get
  torrent-add
  torrent-get
  torrent-stop
  torrent-start
  torrent-remove
  session-stats

Session-id handshake: 409 + X-Transmission-Session-Id header (real Transmission behavior)
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.parse

# Avoid circular import when imported by server.py
import state  # type: ignore  # noqa: E402


def _recover_real_url(raw: str) -> str | None:
    """Recover the real archive.org URL from a torrent-add filename/metainfo.

    Handles three forms Questarr can hand us:
      1. A direct archive.org URL (rare - Questarr usually rewrites the host):
         https://archive.org/download/<id>/<file>
      2. A self-referential URL pointing at our /dl/<key> endpoint (after the
         /dl/ fix): http://<us>/dl/<key> -> look up _DL_MAP.
      3. A legacy self-referential URL pointing at /download/<path> (pre-fix,
         when Questarr rewrote archive.org host to our host):
         http://<us>/download/<id>/<file> -> reconstruct archive.org.
    """
    if not raw:
        return None
    raw = raw.strip()
    # Form 1: already a real external URL
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host and host not in ("", ) and not _is_self_host(host):
        return raw
    # Form 2: /dl/<key>
    m = _re.search(r"/dl/([0-9a-f]{16})", raw)
    if m:
        import pipeline  # type: ignore  # noqa: E402
        entry = pipeline.dl_lookup(m.group(1))
        return entry["url"] if entry else None
    # Form 3: /download/<path> -> https://archive.org/download/<path>
    m = _re.search(r"/download/(.+)$", raw)
    if m:
        path = m.group(1)
        # Strip any query string the client may have appended
        path = path.split("?")[0]
        return f"https://archive.org/download/{path}"
    return None


def _is_self_host(host: str) -> bool:
    """True if <host> is our own advertised public host."""
    import os
    pub = os.environ.get("RGG_PUBLIC_URL", "")
    pub_host = urllib.parse.urlparse(pub).hostname or ""
    return bool(pub_host) and host.lower() == pub_host.lower()


import re as _re  # noqa: E402

# rgg provides bdecode/bencode for parsing metainfo .torrent blobs.
import sys as _sys, os as _os  # noqa: E402
_app_root = _os.environ.get("RGG_APP_ROOT", "/app")
if _app_root not in _sys.path:
    _sys.path.insert(0, _app_root)
try:
    import rgg  # type: ignore  # noqa: E402
except Exception:
    rgg = None  # type: ignore


SESSION_ID = secrets.token_hex(6)
SESSIONS: dict[str, float] = {}   # session_id → last_seen (for auth timeout)


def _check_session(headers: dict) -> tuple[bool, str | None]:
    """Validate X-Transmission-Session-Id. Refresh on each call."""
    sid = headers.get("x-transmission-session-id") or headers.get("X-Transmission-Session-Id")
    if not sid:
        return False, None
    if sid != SESSION_ID:
        return False, SESSION_ID   # 409 with our session id
    SESSIONS[sid] = time.time()
    return True, None


def _json_rpc_ok(result: dict) -> dict:
    return {"result": "success", "arguments": result}


def _json_rpc_err(message: str) -> dict:
    return {"result": message}


async def handle(request_body: bytes, headers: dict) -> tuple[int, dict, dict]:
    """Dispatch a JSON-RPC request. Returns (http_status, body_dict, extra_headers)."""
    try:
        req = json.loads(request_body)
    except json.JSONDecodeError as e:
        return 400, {"result": "success", "arguments": {}, "error": str(e)}, {}

    # Batch requests: just take the first
    if isinstance(req, list):
        if not req:
            return 400, {"result": "success", "arguments": {}}, {}
        req = req[0]

    method = req.get("method", "")
    args = req.get("arguments", {}) or {}
    req_id = req.get("tag") or req.get("id")

    # Auth check
    ok, session_id = _check_session(headers)
    if not ok:
        # 409 Conflict + X-Transmission-Session-Id (real Transmission behavior)
        return 409, {}, {"X-Transmission-Session-Id": session_id or SESSION_ID}

    handlers = {
        "session-get":     _session_get,
        "torrent-add":     _torrent_add,
        "torrent-get":     _torrent_get,
        "torrent-stop":    _torrent_stop,
        "torrent-start":   _torrent_start,
        "torrent-remove":  _torrent_remove,
        "session-stats":   _session_stats,
    }
    fn = handlers.get(method)
    if not fn:
        return 200, {
            "result": "Method not supported",
            "arguments": {},
            "id": req_id,
        }, {}

    try:
        result = await fn(args)
    except Exception as e:
        return 200, _json_rpc_err(str(e)) | {"id": req_id}, {}

    return 200, _json_rpc_ok(result) | {"id": req_id}, {}


# === handlers ===

async def _session_get(args: dict) -> dict:
    """Real Transmission returns version, rpc-version, download-dir, etc."""
    return {
        "version": "4.0.5-romgogget",
        "rpc-version": 17,
        "rpc-version-minimum": 14,
        "download-dir": "/var/lib/transmission-daemon/downloads",
        "download-dir-free-space": 1_000_000_000_000,   # 1 TB, plenty
        "encryption": "tolerated",
        "default-trackers": [],
    }


async def _torrent_add(args: dict) -> dict:
    """Add a download. We don't have real torrent info, so we kick off
    a background download via pipeline.grab and return the assigned id."""
    import pipeline  # type: ignore  # avoid circular import  # noqa: E402

    # Questarr fetches our /dl/<key> .torrent, parse-torrents it, and POSTs
    # it here as base64 `metainfo`. The .torrent's `comment` field carries the
    # real archive.org URL. Fallback: `filename` may be our own /dl/<key> URL
    # (if Questarr's fetch failed and it passed the URL through directly) or a
    # raw archive.org URL (if Questarr didn't rewrite the host). Recover the
    # real URL in all three cases.
    import base64 as _b64, re as _re  # noqa: E402
    real_url = None
    title = (args.get("labels") or [""])[0] or "download"

    if args.get("metainfo"):
        try:
            tdata = _b64.b64decode(args["metainfo"])
            decoded = rgg.bdecode(tdata)
            if isinstance(decoded, tuple):
                decoded = decoded[0]
            if decoded and b"comment" in decoded:
                real_url = decoded[b"comment"].decode("utf-8", "replace")
            # filename inside the torrent is the best display title
            info = decoded.get(b"info", {}) if decoded else {}
            if isinstance(info, dict) and b"name" in info:
                title = info[b"name"].decode("utf-8", "replace")
        except Exception as e:
            print(f"[transmission] metainfo decode failed: {e}")
    if not real_url:
        raw = args.get("filename") or args.get("metainfo")
        if not raw:
            return {}
        real_url = _recover_real_url(raw)
    if not real_url:
        print(f"[transmission] could not recover real URL from torrent-add args: {args}")
        return {}

    dest_dir = args.get("download-dir", "/mnt/shared/roms")

    grab = await pipeline.grab(
        indexer_id="transmission-rpc",
        indexer_name="RomGoGetter",
        guid=real_url,
        url=real_url,
        title=title,
        download_dir=dest_dir,
    )

    # Return shape that transmission.ts's addDownload expects:
    #   response.arguments["torrent-added"].hashString  OR
    #   response.arguments["torrent-duplicate"].hashString
    return {
        "torrent-added": {
            "id": int(grab.id[:8], 16) & 0x7fffffff,
            "hashString": grab.id,
            "name": grab.name,
            "torrentFile": "",
            "magnets": {},
            "metadataPercentComplete": 100,
            "files": [],
            "totalSize": grab.total_size,
        }
    }


async def _torrent_get(args: dict) -> dict:
    """Return torrents matching args.ids or all if not specified."""
    requested_ids = args.get("ids")
    fields = args.get("fields")   # what fields Questarr wants

    all_grabs = state.all_grabs()
    if requested_ids and requested_ids != "recently-active":
        wanted = set(str(i) for i in requested_ids) if isinstance(requested_ids, list) else None
        # Match by hashString OR by numeric id
        matched = []
        for g in all_grabs:
            if wanted is None:
                matched.append(g)
                continue
            if g.id in wanted or str(int(g.id[:8], 16) & 0x7fffffff) in wanted:
                matched.append(g)
        all_grabs = matched

    torrents = [g.to_transmission() for g in all_grabs]
    return {"torrents": torrents}


async def _torrent_stop(args: dict) -> dict:
    for g in state.all_grabs():
        if str(int(g.id[:8], 16) & 0x7fffffff) in [str(i) for i in args.get("ids", [])]:
            g.status = 0   # stopped
            state.upsert(g)
    return {}


async def _torrent_start(args: dict) -> dict:
    for g in state.all_grabs():
        if str(int(g.id[:8], 16) & 0x7fffffff) in [str(i) for i in args.get("ids", [])]:
            g.status = 3   # downloading
            state.upsert(g)
    return {}


async def _torrent_remove(args: dict) -> dict:
    ids = args.get("ids", [])
    delete_files = args.get("delete-local-data", False)
    removed = []
    for g in list(state.all_grabs()):
        if str(int(g.id[:8], 16) & 0x7fffffff) in [str(i) for i in ids]:
            if state.remove(g.id):
                removed.append(int(g.id[:8], 16) & 0x7fffffff)
    return {}


async def _session_stats(args: dict) -> dict:
    """Provide enough fields for Questarr's storage calculations."""
    grabs = state.all_grabs()
    return {
        "activeTorrentCount": sum(1 for g in grabs if g.status == 3),
        "torrentCount": len(grabs),
        "downloadSpeedBytesPerSecond": sum(g.rate_download for g in grabs),
        "uploadSpeedBytesPerSecond": 0,
        "cumulative-stats": {
            "downloadedBytes": sum(g.downloaded_ever for g in grabs),
            "filesAdded": len(grabs),
        },
        "current-stats": {
            "downloadedBytes": sum(g.downloaded_ever for g in grabs),
            "filesAdded": len(grabs),
        },
    }