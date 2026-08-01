# indexer — Torznab + Transmission-RPC emulator for Questarr

A single FastAPI service that lets Questarr talk to the RomGoGetter fork
as if it were a Prowlarr-style indexer and a Transmission download client.

## What this exposes

| Endpoint | Protocol | Consumer | Purpose |
|----------|----------|----------|---------|
| `GET /api?t=caps` | Torznab XML | Questarr/Prowlarr | Advertise categories |
| `GET /api?t=search&q=...&cat=...&limit=...` | Torznab XML | Questarr/Prowlarr | Search across all 13 archive.org groups + IGDB-enriched Top-N |
| `POST /transmission/rpc` | Transmission JSON-RPC | Questarr | Add/get/list/remove downloads |

Both protocols call into the same `pipeline.py`:
1. `rgg.fetch_url_cached(listing_url)` — archive.org HTML → `(filename, size, url)` tuples
2. `rgg._apply_filter(entries, "1G1R English only")` — group by title
3. `rgg.select_best(group)` — pick the canonical variant (USA > Europe > Japan, non-revised)
4. `urllib.request.urlopen(entry.direct_url)` — download to `/mnt/shared/roms/<group>/<title>.zip`

## Configuration (env vars)

| Var | Default | Purpose |
|-----|---------|---------|
| `RGG_API_KEY` | random | Torznab apikey (also gates Transmission RPC) |
| `RGG_PORT` | 9696 | Combined HTTP port for Torznab + Transmission |
| `RGG_NAS_ROOT` | /mnt/shared/roms | Where downloaded ROMs land |
| `RGG_STATE_PATH` | /app/data/state.json | Where active downloads are tracked |
| `RGG_WORKERS` | 4 | Concurrent download workers |
| `RGG_PUBLIC_URL` | http://localhost:9696 | What to advertise in Torznab caps |
| `IGDB_CLIENT_ID` | (none) | Required for IGDB Top-N filtering |
| `IGDB_TWITCH_SECRET` | (none) | Required for IGDB Top-N filtering |

## Smoke test

```bash
# 1. Caps
curl "http://localhost:9696/api?t=caps&apikey=$RGG_API_KEY"

# 2. Search for Phantasy Star IV
curl "http://localhost:9696/api?t=search&q=Phantasy+Star+IV&apikey=$RGG_API_KEY"

# 3. Grab via Transmission RPC
SID=$(curl -s -o /dev/null -D - "http://localhost:9091/transmission/rpc" \
  | awk -F': ' '/X-Transmission-Session-Id/ {print $2}' | tr -d '\r\n')
curl -X POST "http://localhost:9091/transmission/rpc" \
  -H "X-Transmission-Session-Id: $SID" \
  -d '{"method":"torrent-add","arguments":{"filename":"<link-from-search>","download-dir":"/tmp/dl_test"}}'
```

## Architecture

```
                 Questarr (LAN)
                       │
                       ▼
        ┌──────────────────────────────────┐
        │   romgogetter-indexer (Pod)      │
        │                                  │
        │   /api       →  torznab.py       │ ←  same code path as
        │   /transmission/rpc → transmission.py │   fork's GUI button
        │                                  │
        │   pipeline.py                    │
        │     ├─ fetch_url_cached()        │
        │     ├─ _apply_filter()           │
        │     ├─ select_best()             │
        │     └─ urllib download           │
        │                                  │
        │   state.py (in-mem + JSON disk)  │
        └──────────────────────────────────┘
                       │
                       ▼  (hostPath mount)
                /mnt/shared/roms/
```

The fork's `RomGoGetter_v0.18.pyw` and `RomGoGetter_groups.json` are baked
into the container image. The `rgg` module imports them at startup.