"""
state.py — In-memory + on-disk registry for active "downloads" (ROM grabs).

Each grab gets a hash-stable id derived from (indexer_id, item_guid).
This survives restarts: the indexer/repo is mounted at /app/data/state.json
inside the container, so a pod restart doesn't lose track of in-flight downloads.

Shape mirrors what Questarr's TransmissionClient expects:
  - id (hashString)
  - name
  - status: 0=stopped, 1=queued, 2=verifying, 3=downloading, 4=seeding, 5=seeding-wait, 6=seeding-done
  - percentDone (0.0-1.0)
  - totalSize
  - downloadedEver
  - rateDownload (bytes/sec, 0 when idle)
  - eta (seconds, -1 when unknown)
  - errorString
  - downloadDir
  - files[]
"""
from __future__ import annotations

import json
import os
import time
import hashlib
import threading
from pathlib import Path

STATE_PATH = Path(os.environ.get("RGG_STATE_PATH", "/app/data/state.json"))
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()


def _stable_id(indexer_id: str, guid: str) -> str:
    """Stable torrent id. Real Transmission uses info-hash, but we don't
    have one (HTTP downloads have no infohash), so we hash (indexer, guid)."""
    h = hashlib.sha256()
    h.update(indexer_id.encode("utf-8"))
    h.update(b"\0")
    h.update(guid.encode("utf-8"))
    return h.hexdigest()[:40]   # 40 hex chars = SHA-1 length, matches Transmission convention


class Grab:
    __slots__ = (
        "id", "name", "status", "percent_done", "total_size",
        "downloaded_ever", "rate_download", "eta", "error_string",
        "download_dir", "files", "indexer_id", "indexer_name",
        "guid", "url", "title", "created_at", "updated_at",
    )

    def __init__(self, *, indexer_id: str, indexer_name: str, guid: str,
                 url: str, title: str, download_dir: str):
        self.id = _stable_id(indexer_id, guid)
        self.indexer_id = indexer_id
        self.indexer_name = indexer_name
        self.guid = guid
        self.url = url
        self.title = title
        self.download_dir = download_dir

        # Transmission-compatible fields
        self.name = title
        self.status = 1               # queued
        self.percent_done = 0.0
        self.total_size = 0
        self.downloaded_ever = 0
        self.rate_download = 0
        self.eta = -1
        self.error_string = ""
        self.files = []               # [{name, length, bytesCompleted}]
        self.created_at = time.time()
        self.updated_at = self.created_at

    def to_transmission(self) -> dict:
        """Serialize in the exact field set Questarr's TransmissionClient requests."""
        return {
            "id": int(self.id[:8], 16) & 0x7fffffff,  # numeric id, capped
            "hashString": self.id,
            "name": self.name,
            "status": self.status,
            "percentDone": self.percent_done,
            "totalSize": self.total_size,
            "downloadedEver": self.downloaded_ever,
            "rateDownload": self.rate_download,
            "rateUpload": 0,
            "eta": self.eta,
            "uploadRatio": 0.0,
            "errorString": self.error_string,
            "peersSendingToUs": 0,
            "peersGettingFromUs": 0,
            "isFinished": self.status == 6,
            "downloadDir": self.download_dir,
            "files": self.files,
            "labels": ["romgogetter"],
        }


def _read() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(STATE_PATH)


def get(gid: str) -> Grab | None:
    with _lock:
        data = _read()
        raw = data.get(gid)
        if not raw:
            return None
        g = Grab.__new__(Grab)
        for slot in Grab.__slots__:
            setattr(g, slot, raw.get(slot))
        return g


def upsert(grab: Grab) -> None:
    with _lock:
        data = _read()
        grab.updated_at = time.time()
        data[grab.id] = {slot: getattr(grab, slot) for slot in Grab.__slots__}
        _write(data)


def remove(gid: str) -> bool:
    with _lock:
        data = _read()
        if gid in data:
            del data[gid]
            _write(data)
            return True
        return False


def all_grabs() -> list[Grab]:
    with _lock:
        data = _read()
        grabs = []
        for raw in data.values():
            g = Grab.__new__(Grab)
            for slot in Grab.__slots__:
                setattr(g, slot, raw.get(slot))
            grabs.append(g)
        return grabs