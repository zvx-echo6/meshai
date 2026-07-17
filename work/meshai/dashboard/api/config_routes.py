"""Configuration API routes."""

import logging

from fastapi import APIRouter, HTTPException, Request

from meshai.config import (
    Config,
    _dataclass_to_dict,
    _dict_to_dataclass,
    load_config,
    save_config,
)
from meshai.config_loader import save_section, get_config_dir_from_path

logger = logging.getLogger(__name__)

router = APIRouter(tags=["config"])

# Sections that require restart when changed.
# v0.16-env-hot-reload: "environmental" and "generic_sources" REMOVED.
# Central is retired (all-native deployments, central.enabled=false,
# CentralConsumer inert), so the transient AND-mode this rule originally
# guarded against (env_store rebuild + CentralConsumer subscribe both
# boot-only, per Central v0.10.2's OR-not-AND / Spokane fix) can no longer
# happen in practice -- there is no live Central subscription to race. A PUT
# to environmental/generic_sources is now hot-applied via
# EnvironmentalStore.apply_config() (see _refresh_environmental below):
# unchanged per-adapter configs are left alone, changed native adapters are
# rebuilt in place, and store-level dedup/seen state survives because it is
# keyed by event source, not by adapter object. The ONE case that still
# needs a restart is a single adapter's feed_source flipping to/from
# "central" -- CentralConsumer's (un)subscribe is still boot-only -- and
# that is now reported per-adapter (see apply_config()'s "restart_required"
# result), not by blanket section membership here.
RESTART_REQUIRED_SECTIONS = {
    "connection",
    "llm",
    "mesh_sources",
    "meshmonitor",
    "dashboard",
    "coverage",
}

# Valid config section names
VALID_SECTIONS = {
    "timezone",
    "notifications",
    "environmental",
    "bot",
    "connection",
    "response",
    "history",
    "memory",
    "context",
    "meshcore_context",
    "commands",
    "llm",
    "weather",
    "meshmonitor",
    "knowledge",
    "mesh_sources",
    "mesh_intelligence",
    "dashboard",
    "danger_zones",
    "coverage",
    "generic_sources",
}


@router.get("/config")
async def get_full_config(request: Request):
    """Get full configuration."""
    config = request.app.state.config
    return _dataclass_to_dict(config)


@router.get("/config/{section}")
async def get_config_section(section: str, request: Request):
    """Get a specific configuration section."""
    if section not in VALID_SECTIONS:
        raise HTTPException(
            status_code=404,
            detail=f"Section '{section}' not found. Valid sections: {', '.join(sorted(VALID_SECTIONS))}"
        )

    config = request.app.state.config

    if not hasattr(config, section):
        raise HTTPException(status_code=404, detail=f"Section '{section}' not found")

    section_data = getattr(config, section)

    # Handle list types (mesh_sources)
    if isinstance(section_data, list):
        return [
            _dataclass_to_dict(item) if hasattr(item, "__dataclass_fields__") else item
            for item in section_data
        ]

    # Handle dataclass types
    if hasattr(section_data, "__dataclass_fields__"):
        return _dataclass_to_dict(section_data)

    return section_data


def _is_dataclass_type(tp) -> bool:
    """True for a nested dataclass FIELD TYPE (config.py uses real classes, not
    string annotations, so __dataclass_fields__ is reachable here)."""
    return hasattr(tp, "__dataclass_fields__")


def _merge_over_current(base: dict, body: dict, cls) -> dict:
    """Merge a PARTIAL `body` over the CURRENT section dict `base`.

    Keys ABSENT from `body` keep their current value; keys PRESENT in `body`
    win -- including falsy ones ('' / False / 0 / []), so intentional clearing
    still works. Only key presence matters, never the value.

    Nested semantics (deliberate, see the module tests):
      * dict -> nested DATACLASS field  : DEEP-MERGE. Fixed, known schema, so a
        partial like {"region_routes": {"mc_enabled": true}} must not drop the
        sibling `cells`/`mt_enabled` fields.
      * dict -> bare `dict` field       : REPLACE-AT-KEY. These are dynamic maps
        (region_routes.cells, notifications.toggles/destinations,
        webhook_headers). Deep-merging them would make key DELETION impossible
        -- a removed cell/toggle would be resurrected from `base`.
      * list                            : REPLACE-AT-KEY. Same deletion argument
        (notifications.rules, mesh_sources, regions).

    Keying the recursion off the dataclass schema -- not off "is it a dict" --
    is what keeps deletion working for maps while still protecting nested
    dataclasses from the partial-payload wipe.
    """
    if not isinstance(base, dict) or not isinstance(body, dict):
        return body

    field_types = {}
    if cls is not None and _is_dataclass_type(cls):
        field_types = {f.name: f.type for f in cls.__dataclass_fields__.values()}

    merged = dict(base)
    for key, value in body.items():
        field_type = field_types.get(key)
        if (isinstance(value, dict)
                and isinstance(base.get(key), dict)
                and _is_dataclass_type(field_type)):
            merged[key] = _merge_over_current(base[key], value, field_type)
        else:
            merged[key] = value
    return merged


@router.put("/config/{section}")
async def update_config_section(section: str, request: Request):
    """Update a configuration section."""
    if section not in VALID_SECTIONS:
        raise HTTPException(
            status_code=404,
            detail=f"Section '{section}' not found. Valid sections: {', '.join(sorted(VALID_SECTIONS))}"
        )

    config_path = request.app.state.config_path
    if not config_path:
        raise HTTPException(status_code=500, detail="Config path not set")

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {e}")

    try:
        # Get the section's dataclass type
        field_info = Config.__dataclass_fields__.get(section)
        if not field_info:
            raise HTTPException(status_code=404, detail=f"Section '{section}' not found")

        field_type = field_info.type

        # Validate by coercing to the dataclass (runs __post_init__ validators),
        # then persist via the multi-file / !include-aware save_section. The
        # monolithic save_config cannot parse the !include orchestrator and blew
        # up on every save in the prod layout (v0.4 C.2.1 fix).
        if section == "mesh_sources":
            from meshai.config import MeshSourceConfig
            new_value = [
                _dict_to_dataclass(MeshSourceConfig, item) if isinstance(item, dict) else item
                for item in body
            ]
            data_to_save = [
                _dataclass_to_dict(v) if hasattr(v, "__dataclass_fields__") else v
                for v in new_value
            ]
        elif hasattr(field_type, "__dataclass_fields__"):
            # MERGE the (possibly partial) body over the CURRENT live section
            # before coercing. _dict_to_dataclass() builds kwargs only from the
            # keys it is handed and lets `cls(**kwargs)` default the rest, so
            # coercing a partial body directly resets every omitted field to its
            # dataclass default. That is what took both radios offline on
            # 2026-07-17: a one-key PUT of meshcore_advert_interval_seconds
            # rewrote type/tcp_host/meshcore_host/meshcore_conn_type/
            # meshcore_serial_port to defaults (see tests/
            # test_config_partial_save_merge.py).
            #
            # The merge base is the LIVE config -- the same values GET
            # /api/config/{section} serves and the UI edits on top of -- so a
            # partial PUT now lands exactly where a full-object PUT from that
            # same GET would have. Full-object callers are unaffected: every key
            # they send simply wins.
            current = getattr(request.app.state, "config", None)
            base = None
            if current is not None:
                base = _section_to_plain(getattr(current, section, None))
            if isinstance(base, dict) and isinstance(body, dict):
                merged_body = _merge_over_current(base, body, field_type)
            else:
                # No live base to merge over (should not happen in prod, where
                # app.state.config is always set) -- fall back to the historical
                # coerce-the-body-as-given path rather than inventing a base.
                logger.warning(
                    "Config PUT %r: no live section to merge over; "
                    "applying body as-is", section)
                merged_body = body
            new_value = _dict_to_dataclass(field_type, merged_body)
            data_to_save = _dataclass_to_dict(new_value)
        else:
            new_value = body
            data_to_save = body

        config_dir = get_config_dir_from_path(config_path)
        save_section(section, data_to_save, config_dir)

        # v0.6-tail-3: compute the dotted-key diff so the UI banner can
        # show *which* fields require a restart, not just "something
        # restart-y changed". This is purely advisory -- the static OR
        # enforcement at boot remains the runtime guard.
        try:
            before_section = _section_to_plain(getattr(
                request.app.state.config, section, None))
        except Exception:
            before_section = None
        after_section = data_to_save
        changed_keys = _diff_keys(before_section, after_section,
                                    prefix=section)

        restart_required = (section in RESTART_REQUIRED_SECTIONS
                              and len(changed_keys) > 0)

        # Keep the live config in sync (no disk reload needed) when no
        # restart is required. When a restart IS required, the live
        # config object intentionally diverges from disk until the user
        # actually restarts -- otherwise the runtime would silently
        # switch into the transient AND-mode this commit exists to
        # prevent.
        adapter_results = None
        if not restart_required and getattr(request.app.state, "config", None) is not None:
            try:
                setattr(request.app.state.config, section, new_value)
            except Exception:
                pass
            if section == "context":
                _refresh_mesh_context(request.app, new_value)
            elif section == "environmental":
                adapter_results = _refresh_environmental(request.app, new_value)
            elif section == "generic_sources":
                current_env_cfg = getattr(request.app.state.config, "environmental", None)
                adapter_results = _refresh_environmental(
                    request.app, current_env_cfg, generic_sources=new_value)

            # A specific adapter's feed_source flip to/from "central" still
            # needs a restart (CentralConsumer (un)subscribe is boot-only) --
            # apply_config() reports it per-adapter rather than by blanket
            # section membership, so surface it here.
            if adapter_results and any(
                    v == "restart_required" for v in adapter_results.values()):
                restart_required = True

        logger.info(
            "Config section %r updated, restart_required=%s changed_keys=%s%s",
            section, restart_required, changed_keys,
            f" adapter_results={adapter_results}" if adapter_results is not None else "",
        )

        response = {
            "saved": True,
            "restart_required": restart_required,
            "changed_keys": changed_keys,
        }
        if adapter_results is not None:
            response["adapter_results"] = adapter_results
        return response

    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"Config update error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config/test-llm")
async def test_llm_connection(request: Request):
    """Test LLM backend connection."""
    config = request.app.state.config

    try:
        # Create LLM backend based on config
        api_key = config.resolve_api_key()
        if not api_key:
            return {"success": False, "error": "No API key configured"}

        backend_name = config.llm.backend.lower()

        if backend_name == "openai":
            from meshai.backends import OpenAIBackend
            backend = OpenAIBackend(config.llm, api_key, 0, 0)
        elif backend_name == "anthropic":
            from meshai.backends import AnthropicBackend
            backend = AnthropicBackend(config.llm, api_key, 0, 0)
        elif backend_name == "google":
            from meshai.backends import GoogleBackend
            backend = GoogleBackend(config.llm, api_key, 0, 0)
        else:
            return {"success": False, "error": f"Unknown backend: {backend_name}"}

        # Send test prompt — generate(messages: list[dict], system_prompt: str)
        response = await backend.generate(
            [{"role": "user", "content": "Reply with 'OK' if you can read this."}],
            "",
        )
        await backend.close()

        return {"success": True, "response": response}

    except Exception as e:
        logger.error(f"LLM test error: {e}")
        return {"success": False, "error": str(e)}


# v0.6-6 -- live ToggleFilter refresh endpoint.
# Called by the frontend after PUT /api/config/notifications so the
# Inhibitor + Grouper + Dispatcher pick up the new enabled toggle set
# on the next event without a container restart.
def _refresh_toggle_filter(app) -> bool:
    """Best-effort live refresh of the running ToggleFilter. Returns True
    when the refresh actually fired, False if the pipeline isn t up yet
    (typical during tests / early startup). Never raises."""
    try:
        bus = getattr(app.state, "bus", None)
        config = getattr(app.state, "config", None)
        if bus is None or config is None:
            return False
        components = getattr(bus, "_pipeline_components", {}) or {}
        tf = components.get("toggle_filter")
        if tf is None:
            return False
        tf.refresh(config)
        return True
    except Exception:
        logger.exception("toggle_filter refresh failed")
        return False


def _refresh_environmental(app, new_env_cfg, generic_sources=None):
    """Best-effort live hot-reload of the running EnvironmentalStore after an
    "environmental" or "generic_sources" config PUT. Delegates the actual
    diff-and-swap to ``EnvironmentalStore.apply_config()`` (env/store.py):
    only adapters whose OWN config changed are rebuilt in place; everything
    else -- including store-level dedup/seen state -- is left untouched.

    ``generic_sources``, when omitted, defaults to the live config's current
    list (an "environmental" PUT doesn't touch generic_sources); a
    "generic_sources" PUT passes its own new value explicitly.

    Returns the ``{adapter_name: "reloaded"|"unchanged"|"restart_required"}``
    map from ``apply_config()``, or ``None`` when the store isn't up yet
    (environmental feeds disabled, or early startup/tests). Never raises.
    """
    try:
        store = getattr(app.state, "env_store", None)
        if store is None or new_env_cfg is None:
            return None
        if generic_sources is None:
            config = getattr(app.state, "config", None)
            generic_sources = getattr(config, "generic_sources", None) if config else None
        return store.apply_config(new_env_cfg, generic_sources=generic_sources)
    except Exception:
        logger.exception("environmental store refresh failed")
        return None


def _refresh_mesh_context(app, new_ctx_cfg) -> bool:
    """Best-effort live refresh of the running MeshContext after a context
    config PUT. Returns True when the refresh actually fired, False if the
    context instance is absent/None (passive context disabled, or early
    startup). Never raises."""
    try:
        ctx = getattr(app.state, "mesh_context", None)
        if ctx is None:
            return False
        ctx.update_settings(
            max_age=new_ctx_cfg.max_age,
            observe_channels=new_ctx_cfg.observe_channels,
            ignore_nodes=new_ctx_cfg.ignore_nodes,
        )
        return True
    except Exception:
        logger.exception("mesh_context refresh failed")
        return False


@router.post("/notifications/refresh-toggles")
async def refresh_toggles(request: Request):
    """Explicit refresh endpoint. Not called by the dashboard frontend --
    the _auto_refresh_toggle_filter middleware below already refreshes the
    ToggleFilter automatically on every successful notifications config PUT.
    Kept for ops/debug use (manually forcing a refresh outside that path)."""
    bus = getattr(request.app.state, "bus", None)
    config = getattr(request.app.state, "config", None)
    if bus is None or config is None:
        raise HTTPException(503, "pipeline bus not yet initialized")
    components = getattr(bus, "_pipeline_components", {}) or {}
    tf = components.get("toggle_filter")
    if tf is None:
        raise HTTPException(503, "toggle_filter not on pipeline bus")
    tf.refresh(config)
    return {"ok": True}



# v0.6-tail item 1: auto-refresh the ToggleFilter after any successful
# config PUT that touches notifications. Registered from server.py at
# startup via register_config_routes_hooks(app).
def register_config_routes_hooks(app):
    @app.middleware("http")
    async def _auto_refresh_toggle_filter(request, call_next):
        response = await call_next(request)
        try:
            method = request.method.upper()
            path = request.url.path
            if (method == "PUT"
                    and 200 <= response.status_code < 300
                    and ("/api/config/notifications" in path
                         or path.rstrip("/").endswith("/api/config"))):
                _refresh_toggle_filter(request.app)
        except Exception:
            logger.exception("auto-refresh middleware failed")
        return response



# ---- v0.6-tail-3 diff helpers ------------------------------------------


def _section_to_plain(section_value):
    """Dataclass / list / scalar -> JSON-serializable shape."""
    if section_value is None:
        return None
    if isinstance(section_value, list):
        return [
            _dataclass_to_dict(item) if hasattr(item, "__dataclass_fields__") else item
            for item in section_value
        ]
    if hasattr(section_value, "__dataclass_fields__"):
        return _dataclass_to_dict(section_value)
    return section_value


def _diff_keys(before, after, *, prefix: str) -> list[str]:
    """Recursively collect dotted-path keys where `before` and `after` differ.

    Lists are compared element-wise -- structural mismatch yields a single
    bracketless path. The function is deliberately tolerant of None /
    missing keys so a section being added or removed produces a meaningful
    diff instead of crashing.
    """
    out: list[str] = []

    def walk(b, a, p: str):
        if b == a:
            return
        if isinstance(b, dict) and isinstance(a, dict):
            for k in set(b.keys()) | set(a.keys()):
                walk(b.get(k), a.get(k), f"{p}.{k}" if p else k)
            return
        if isinstance(b, list) and isinstance(a, list):
            if len(b) != len(a):
                out.append(p)
                return
            for i, (bi, ai) in enumerate(zip(b, a)):
                walk(bi, ai, f"{p}[{i}]")
            return
        out.append(p)

    walk(before, after, prefix)
    return sorted(out)
