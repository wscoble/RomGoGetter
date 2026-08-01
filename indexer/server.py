"""
server.py — FastAPI app combining Torznab + Transmission-RPC emulator.

Routes:
  GET  /                                     → health banner
  GET  /api?t=caps&apikey=...                → Torznab caps XML
  GET  /api?t=search&q=...&cat=...&apikey=...→ Torznab search RSS XML
  POST /transmission/rpc                      → Transmission JSON-RPC

Auth: API key check (Torznab requires &apikey=...; Questarr sends one by default).
The same key is required in the Transmission RPC header too — keep it simple.
"""
from __future__ import annotations

import os
import secrets
import sys
import urllib.parse

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse

# Make the fork's modules importable. The flake.nix wires /app to be the
# fork root, so rgg.py sits at /app/rgg.py and RomGoGetter_groups.json at /app/.
import os as _os, sys as _sys
_APP_ROOT = _os.environ.get("RGG_APP_ROOT", "/app")
_INDEXER_DIR = _os.path.join(_APP_ROOT, "indexer")
for _p in (_APP_ROOT, _INDEXER_DIR):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
import pipeline, state, torznab, transmission  # type: ignore  # noqa: E402

app = FastAPI(title="RomGoGetter Indexer")

# API key: env-set or auto-generated
_API_KEY = os.environ.get("RGG_API_KEY") or secrets.token_urlsafe(16)


@app.on_event("startup")
async def _banner():
    pipeline.startup_recover()
    print(f"======================================================================", flush=True)
    print(f"RomGoGetter Indexer", flush=True)
    print(f"  Torznab    :  GET  /api?t=...", flush=True)
    print(f"  Transmission: POST /transmission/rpc", flush=True)
    print(f"  API key    :  {_API_KEY}", flush=True)
    print(f"  NAS root   :  {pipeline.NAS_ROOT}", flush=True)
    print(f"  State path :  {state.STATE_PATH}", flush=True)
    print(f"  Groups     :  {len(pipeline.GROUPS)}", flush=True)
    print(f"======================================================================", flush=True)


@app.get("/")
async def root():
    return PlainTextResponse(
        f"RomGoGetter Indexer\n"
        f"  Torznab:     /api?t=caps\n"
        f"  Transmission: /transmission/rpc\n"
    )


@app.get("/api")
async def torznab_endpoint(request: Request):
    """Torznab endpoint. Handles t=caps and t=search."""
    params = dict(request.query_params)

    # API key check
    apikey = params.get("apikey", "")
    if apikey != _API_KEY:
        return PlainTextResponse(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<error code=\"100\" description=\"Incorrect user credentials\" />",
            status_code=401,
            media_type="application/xml",
        )

    t = params.get("t", "caps")

    if t == "caps":
        return Response(
            content=torznab.caps_xml(),
            media_type="application/xml",
        )

    if t == "search" or t == "movie" or t == "tvsearch" or t == "music":
        query = params.get("q", "").strip()
        limit = int(params.get("limit", "50"))
        offset = int(params.get("offset", "0"))
        cat = params.get("cat", "")

        if not query:
            return Response(
                content="<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
                        "<error code=\"200\" description=\"Missing q parameter\" />",
                media_type="application/xml",
                status_code=400,
            )

        items = await pipeline.search(query)
        # Apply cat filter if requested
        if cat:
            wanted_cats = set(c for c in cat.split(",") if c)
            items = [
                it for it in items
                if any(c.split(",")[0] == cat.split(",")[0] for c in it.get("category", "").split(","))
            ]
        items = items[offset:offset + limit]

        return Response(
            content=torznab.items_xml(items),
            media_type="application/xml",
        )

    # unknown t=
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?>\n<error code="201" description="Unknown t={t!r}" />',
        media_type="application/xml",
        status_code=400,
    )


@app.post("/transmission/rpc")
async def transmission_endpoint(request: Request):
    """Emulate Transmission's RPC endpoint."""
    headers = {k.lower(): v for k, v in request.headers.items()}
    body = await request.body()
    status, payload, extra = await transmission.handle(body, headers)
    headers_out = {"X-Transmission-Session-Id": transmission.SESSION_ID}
    headers_out.update(extra)
    return JSONResponse(content=payload, status_code=status, headers=headers_out)


@app.get("/transmission/rpc")
async def transmission_session_check(request: Request):
    """Real Transmission returns 409 + session id on GET to the RPC endpoint."""
    return JSONResponse(
        content={},
        status_code=409,
        headers={"X-Transmission-Session-Id": transmission.SESSION_ID},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("RGG_PORT", "9696")))