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
        # Load fresh config from file to avoid conflicts
        config = load_config(config_path)

        # Get the section's dataclass type
        field_info = Config.__dataclass_fields__.get(section)
        if not field_info:
            raise HTTPException(status_code=404, detail=f"Section '{section}' not found")

        field_type = field_info.type

        # Handle list types (mesh_sources)
        if section == "mesh_sources":
            from meshai.config import MeshSourceConfig
            new_value = [
                _dict_to_dataclass(MeshSourceConfig, item) if isinstance(item, dict) else item
                for item in body
            ]
        # Handle dataclass types
        elif hasattr(field_type, "__dataclass_fields__"):
            new_value = _dict_to_dataclass(field_type, body)
        else:
            new_value = body

        # Set the section on config
        setattr(config, section, new_value)

        # Save config to file
        save_config(config, config_path)

        # Determine if restart is required
        restart_required = section in RESTART_REQUIRED_SECTIONS

        # Update live config if restart not required
        if not restart_required:
            request.app.state.config = config

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
