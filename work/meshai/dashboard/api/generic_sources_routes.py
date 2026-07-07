"""Generic-source URL preview endpoint.

Powers the no-code Data Sources editor: the operator pastes a public REST /
GeoJSON URL, hits Preview, and the SERVER (not the browser — avoids CORS) fetches
it, so they can SEE the JSON structure and figure out the dotted paths
(``items_path`` / ``id_path`` / ``lat_path`` / ``field_mappings`` …) without
knowing the schema ahead of time.

The single route never raises: on any error it returns
``{"ok": false, "error": ...}`` so the UI can show the problem inline.
"""

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

# Reuse the adapter's dotted-path walker so Preview resolves items_path exactly
# the way the running GenericHttpAdapter will.
from meshai.env.generic_http import _dig

logger = logging.getLogger(__name__)

router = APIRouter(tags=["generic-sources"])

# Cap the pretty-printed JSON we ship back so a huge feed can't bloat the
# response / freeze the browser. ~8 KB is plenty to read the structure.
_SAMPLE_MAX = 8192
_ITEM_MAX = 2048
_FETCH_TIMEOUT = 30
_USER_AGENT = "MeshAI/1.0"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (truncated, {len(text)} chars total)"


def _fetch_preview(url: str, items_path: str) -> dict:
    """Blocking fetch + parse. NEVER raises — always returns a result dict."""
    if not url or not isinstance(url, str):
        return {"ok": False, "error": "A url is required."}

    try:
        req = UrlRequest(url, headers={"User-Agent": _USER_AGENT})
        with urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read()
    except HTTPError as e:
        return {"ok": False, "status": e.code, "error": f"HTTP {e.code}: {e.reason}"}
    except URLError as e:
        return {"ok": False, "error": f"Could not reach URL: {e.reason}"}
    except Exception as e:  # timeout, DNS, connection reset, …
        return {"ok": False, "error": f"Fetch failed: {e}"}

    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "status": status, "error": f"Decode failed: {e}"}

    try:
        data = json.loads(text)
    except Exception as e:
        # Not JSON — still show a raw sample so the operator sees what came back.
        return {
            "ok": False,
            "status": status,
            "error": f"Response is not valid JSON: {e}",
            "sample": _truncate(text, _SAMPLE_MAX),
        }

    result: dict = {
        "ok": True,
        "status": status,
        "sample": _truncate(json.dumps(data, indent=2, ensure_ascii=False), _SAMPLE_MAX),
    }

    # If the operator gave an items_path, resolve it so they can confirm the
    # array + see a single item's shape (the fields they'll map).
    if items_path:
        items = _dig(data, items_path)
        if isinstance(items, list):
            result["item_count"] = len(items)
            if items:
                result["first_item"] = _truncate(
                    json.dumps(items[0], indent=2, ensure_ascii=False), _ITEM_MAX
                )
        else:
            result["item_count"] = None
            result["items_path_note"] = (
                f"items_path '{items_path}' did not resolve to a list "
                f"(got {type(items).__name__})."
            )

    return result


@router.post("/generic-sources/preview")
async def preview_generic_source(request: Request):
    """Server-side fetch of a candidate feed URL for the no-code editor.

    Body: ``{"url": str, "items_path"?: str}``.
    Returns ``{ok, status?, error?, sample?, item_count?, first_item?}`` — never
    raises; failures come back as ``{ok: false, error: ...}``.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    url = (body or {}).get("url")
    items_path = (body or {}).get("items_path") or ""

    return await run_in_threadpool(_fetch_preview, url, items_path)
