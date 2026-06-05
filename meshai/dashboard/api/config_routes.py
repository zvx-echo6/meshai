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

# Sections that require restart when changed
RESTART_REQUIRED_SECTIONS = {
    "connection",
    "llm",
    "mesh_sources",
    "meshmonitor",
    "dashboard",
}

# Valid config section names
VALID_SECTIONS = {
    "notifications",
    "environmental",
    "bot",
    "connection",
    "response",
    "history",
    "memory",
    "context",
    "commands",
    "llm",
    "weather",
    "meshmonitor",
    "knowledge",
    "mesh_sources",
    "mesh_intelligence",
    "dashboard",
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
            new_value = _dict_to_dataclass(field_type, body)
            data_to_save = _dataclass_to_dict(new_value)
        else:
            new_value = body
            data_to_save = body

        config_dir = get_config_dir_from_path(config_path)
        save_section(section, data_to_save, config_dir)

        # Determine if restart is required
        restart_required = section in RESTART_REQUIRED_SECTIONS

        # Keep the live config in sync (no disk reload needed) when no restart is required
        if not restart_required and getattr(request.app.state, "config", None) is not None:
            try:
                setattr(request.app.state.config, section, new_value)
            except Exception:
                pass

        logger.info(f"Config section '{section}' updated, restart_required={restart_required}")

        return {"saved": True, "restart_required": restart_required}

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

        # Send test prompt
        response = await backend.generate("Reply with 'OK' if you can read this.", [])
        await backend.close()

        return {"success": True, "response": response}

    except Exception as e:
        logger.error(f"LLM test error: {e}")
        return {"success": False, "error": str(e)}


# v0.6-6 -- live ToggleFilter refresh endpoint.
# Called by the frontend after PUT /api/config/notifications so the
# Inhibitor + Grouper + Dispatcher pick up the new enabled toggle set
# on the next event without a container restart.
@router.post("/notifications/refresh-toggles")
async def refresh_toggles(request: Request):
    """Re-read the live config and refresh the running ToggleFilter."""
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
