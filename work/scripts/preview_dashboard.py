"""Mesh-free preview launcher for the MeshAI dashboard API.

Stands up ONLY the FastAPI dashboard (NO MeshAI instance, NO mesh connector,
NO broadcast loop) so the region-routing UI can be developed and tested without
a live mesh radio.

Usage:
    python3 scripts/preview_dashboard.py [config_dir]

    config_dir defaults to /tmp/meshai-region-preview/config.
    The directory may be empty — an empty/missing config.yaml yields safe defaults.

The script sets app.state.config and app.state.config_path so all config-backed
endpoints work.  Endpoints that read other app.state attrs (notification_router,
env_store, alert_engine, etc.) guard with getattr(..., None) already, so they
return graceful empty/404 responses rather than crashing.

Run vite separately for the frontend:
    cd dashboard-frontend && npm run dev

Then hit the API at http://localhost:8082/api/...
"""

import sys
from pathlib import Path

# Resolve the config directory from the first CLI arg or use the default preview path.
cfg_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/meshai-region-preview/config")
cfg_dir.mkdir(parents=True, exist_ok=True)

# Ensure there is a minimal config.yaml so load_config doesn't error.
_cfg_yaml = cfg_dir / "config.yaml"
if not _cfg_yaml.exists():
    _cfg_yaml.write_text("# meshai preview config\n")

# Import AFTER path is ready so relative imports resolve correctly.
from meshai.dashboard.server import create_app
from meshai.config_loader import load_config
import uvicorn

app = create_app()
cfg = load_config(cfg_dir)
app.state.config = cfg
app.state.config_path = cfg_dir / "config.yaml"

# Stub out attrs that endpoints guard with getattr(..., None) to avoid AttributeError
# on first access in case FastAPI touches app.state eagerly.
for _attr in ("notification_router", "env_store", "alert_engine", "health_engine",
              "data_store", "connector", "bus", "danger_correlator", "mesh_context",
              "broadcaster"):
    if not hasattr(app.state, _attr):
        setattr(app.state, _attr, None)

if __name__ == "__main__":
    print(f"[preview] config dir : {cfg_dir}")
    print(f"[preview] config path: {app.state.config_path}")
    print(f"[preview] listening  : http://0.0.0.0:8082")
    print(f"[preview] regions API: http://localhost:8082/api/notifications/regions")
    print(f"[preview] matrix API : http://localhost:8082/api/notifications/region-routing")
    uvicorn.run(app, host="0.0.0.0", port=8082)
