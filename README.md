# MeshAI

LLM-powered mesh intelligence assistant for Meshtastic networks. MeshAI connects to your mesh as a physical node, monitors network health in real-time, and answers questions about your infrastructure over LoRa.

## What It Does

MeshAI runs on your Meshtastic node and provides:

- **Mesh Intelligence** — 5-pillar health scoring, per-region breakdowns, infrastructure monitoring, coverage gap analysis, and environmental sensing
- **Conversational Queries** — ask "how's the mesh?" or "tell me about MHR" and get data-driven answers over LoRa
- **Node Distance** — GPS-based distance calculations between any two nodes on the mesh
- **Multi-Source Awareness** — aggregates data from multiple Meshview instances and MeshMonitor with staggered polling
- **Feeder Gateway Tracking** — identifies which physical MQTT gateways hear each node and signal quality
- **Subscriptions** — scheduled daily/weekly health reports and instant alerts delivered via DM
- **LLM Chat** — general conversation, knowledge base lookups, and weather queries
- **Multi-Backend** — supports Google Gemini, OpenAI, Anthropic Claude, and local LLMs via LiteLLM

## Quick Start

```bash
# Clone
git clone https://github.com/zvx-echo6/meshai.git
cd meshai

# Install
pip install -e .

# Configure (interactive TUI)
meshai --config

# Run
meshai
```

Or with Docker:

```bash
mkdir -p meshai/data && cd meshai
curl -O https://raw.githubusercontent.com/zvx-echo6/meshai/main/docker-compose.yml
curl -o data/config.yaml https://raw.githubusercontent.com/zvx-echo6/meshai/main/config.example.yaml
# Edit data/config.yaml
docker compose up -d
```

## Commands

| Command | Description |
|---------|-------------|
| `!health` | Mesh health overview with colored status dots |
| `!region` | List all regions with health status |
| `!region [name]` | Detailed region breakdown |
| `!neighbors [node]` | Top infrastructure neighbors with signal quality |
| `!sub daily 6pm` | Subscribe to daily health reports |
| `!sub weekly 8am sun` | Subscribe to weekly digest |
| `!sub alerts` | Subscribe to instant alerts on issues |
| `!unsub [type]` | Remove a subscription |
| `!mysubs` | List your active subscriptions |
| `!clear` | Clear conversation history |
| `!help` | Show available commands |
| `!help [cmd]` | Detailed help for a command |
| `!quakes` | Recent earthquakes in monitored area |
| `!fires` | Active wildfires from NIFC |
| `!hotspots` | NASA FIRMS satellite fire detections |
| `!hotspots --new` | Only hotspots not matching known fires |
| `!traffic` | Traffic incidents from TomTom |
| `!space` | Space weather conditions (solar/geomagnetic) |
| `!water` | USGS stream gauge readings |
| `!air` | Air quality index |

Commands can be disabled in config if another service (like MeshMonitor) handles them.

## Mesh Intelligence

MeshAI continuously polls mesh data sources and computes a 5-pillar health score:

| Pillar | Weight | What It Measures |
|--------|--------|------------------|
| Infrastructure | 30% | Router uptime — how many infra nodes are online |
| Utilization | 25% | Channel busyness — RF congestion across the mesh |
| Coverage | 20% | Gateway reach — how many monitoring sources see each node |
| Behavior | 15% | Traffic patterns — detecting noisy or misconfigured nodes |
| Power | 10% | Battery health — infrastructure nodes only |

### Health Display

`!health` shows a compact overview with personality:

```
📡 Mesh 🟢 healthy
🏗️ 15/16 routers up
❌ Down: TVM Tablerock Relay
📶 152 full coverage, 94 on thin ice
🔥 Hayden Peak Router at 21% util
🔋 All infra powered ✅
🌡️ 29-34°C across 2 sensors
Treasure Valley 🟢 | Magic Valley 🟢
```

Status dots: 🔵 perfect (100) · 🟢 healthy (75+) · 🟠 warning (50+) · 🔴 critical (<50)

### Monitoring Rules

Infrastructure nodes (routers, repeaters) are monitored individually with full detail — battery, offline alerts, coverage, neighbors, hardware. Client nodes dying is normal and not tracked. Channel utilization and environmental sensors are monitored for all nodes.

### Conversational Queries

Ask questions naturally over LoRa:

- "how's the mesh?" → health overview with top issues
- "tell me about MHR" → full node detail with neighbors, coverage, feeders
- "where do we need more coverage?" → named gaps with specific nodes
- "how far is MHR from AIDA?" → GPS distance calculation
- "which nodes only reach one gateway?" → named nodes with their gateway
- "which gateway has the best signal?" → feeder comparison

### Geographic Regions

Regions are configurable with local names, descriptions, aliases, and cities — all manageable through the TUI. No hardcoded geography in the code.

```yaml
mesh_intelligence:
  regions:
    - name: "South Central ID"
      local_name: "Magic Valley"
      description: "Twin Falls area"
      aliases: ["southern Idaho", "magic valley"]
      cities: ["Twin Falls", "Burley", "Jerome"]
      lat: 42.5
      lon: -114.5
      radius_km: 80
```

## Environmental Feeds

MeshAI integrates real-time environmental data for situational awareness beyond mesh network health.

### USGS Earthquake Monitoring

```yaml
env:
  usgs:
    enabled: true
    min_magnitude: 2.5
    radius_km: 500
    center_lat: 43.6150
    center_lon: -116.2023
```

No API key required. Data from [USGS Earthquake Hazards Program](https://earthquake.usgs.gov/fdsnws/event/1/).

### NWS Weather Alerts

```yaml
env:
  nws:
    enabled: true
    zone: IDZ025         # NWS zone ID
    point: "43.6150,-116.2023"
```

No API key required. Find your zone at [NWS Zone Lookup](https://alerts.weather.gov/).

### NOAA Space Weather

```yaml
env:
  noaa_space:
    enabled: true
```

No API key required. Data from [NOAA SWPC](https://services.swpc.noaa.gov/).

### NIFC Wildfire Perimeters

```yaml
env:
  nifc:
    enabled: true
    radius_km: 200
    center_lat: 43.6150
    center_lon: -116.2023
```

No API key required. Data from [NIFC Open Data](https://data-nifc.opendata.arcgis.com/).

### NASA FIRMS Satellite Fire Detection

```yaml
env:
  firms:
    enabled: true
    map_key: "your-map-key"    # Required
    radius_km: 200
    center_lat: 43.6150
    center_lon: -116.2023
    source: VIIRS_SNPP         # VIIRS_SNPP, VIIRS_NOAA20, MODIS_NRT
    day_range: 1               # 1, 2, or 10 days
```

**API Key Required**: Register at [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/api/area/). Free MAP_KEY provides access to near real-time satellite fire detections. Hotspots are cross-referenced against NIFC perimeters to identify potential new ignitions.

### TomTom Traffic

```yaml
env:
  tomtom:
    enabled: true
    api_key: "your-api-key"    # Required
    bbox: "-117.5,42.5,-115.0,44.5"  # lon1,lat1,lon2,lat2
```

**API Key Required**: Register at [TomTom Developer Portal](https://developer.tomtom.com/). Free tier includes 2,500 requests/day.

### 511 Road Conditions

```yaml
env:
  fiveonone:
    enabled: true
    state: ID                  # State code
    api_key: "your-api-key"    # If required by state
    bbox: [-117.5, 42.5, -115.0, 44.5]
```

API key requirements vary by state. Check your state's 511 developer portal.

### USGS Water Services

```yaml
env:
  usgs_water:
    enabled: true
    sites: ["13206000", "13202000"]  # USGS site numbers
```

No API key required. Find sites at [USGS Water Services](https://waterservices.usgs.gov/).

### AirNow Air Quality

```yaml
env:
  airnow:
    enabled: true
    api_key: "your-api-key"    # Required
    zipcode: "83702"
```

**API Key Required**: Register at [AirNow API](https://docs.airnowapi.org/).

### Dashboard Configuration

```yaml
dashboard:
  enabled: true
  host: 0.0.0.0
  port: 8080
```

The web dashboard provides real-time visualization of mesh nodes, environmental conditions, and alerts with WebSocket push notifications.

---

## Data Sources

MeshAI aggregates from multiple sources using staggered tick-based polling (one API call per 30-second tick):

### Meshview

Unauthenticated REST API. Supports multiple instances.

| Endpoint | Interval | Data |
|----------|----------|------|
| `/api/packets` | 30s | Near real-time packet feed |
| `/api/nodes` | 2 min | Node list with metadata |
| `/api/stats` | 3 min | Traffic statistics |
| `/api/edges` | 3 min | Node-to-node connections |
| `/api/traceroutes` | 5 min | Route data |
| `/api/packets_seen` | 10 min | Per-gateway RSSI/SNR (sampled) |

### MeshMonitor

Authenticated (Bearer token). Single instance.

| Endpoint | Interval | Data |
|----------|----------|------|
| `/api/v1/packets` | 60s | Packet feed |
| `/api/v1/nodes` | 2 min | Nodes with battery, utilization, hardware |
| `/api/v1/telemetry` | 2 min | Environmental sensors, device metrics |
| `/api/v1/traceroutes` | 5 min | Route data |
| `/api/v1/channels` | 5 min | Channel configuration |
| `/api/v1/network` | 5 min | Network statistics |
| `/api/v1/solar` | 10 min | Solar estimates |

### Rate Limiting

Built-in protection for all sources: HTTP 429 backoff with Retry-After, exponential backoff on consecutive errors, slow response warnings, and optional polite mode for shared instances.

### Source Configuration

```yaml
mesh_sources:
  - name: "local-meshview"
    type: meshview
    url: "http://192.168.1.100:8080"
    enabled: true

  - name: "meshmonitor"
    type: meshmonitor
    url: "http://192.168.1.100:3333"
    api_token: "your-bearer-token"
    enabled: true
```

## Knowledge Base (RAG)

MeshAI uses a hybrid knowledge retrieval system with two backends:

### Primary: RECON Qdrant Backend

Queries [RECON](https://forge.echo6.co/matt/recon)'s knowledge extraction pipeline — 2.8M+ vectors covering survival skills, communications, medical, technical documentation, Meshtastic docs, and more. Uses the same embedding infrastructure as RECON:

- **Dense embeddings**: TEI service with BAAI/bge-m3 (1024-dim)
- **Sparse embeddings**: bge-m3-sparse with IDF modifier
- **Search**: Qdrant hybrid with Reciprocal Rank Fusion (dense + sparse)

No data is copied — MeshAI queries RECON's Qdrant and TEI services over the network.

```yaml
knowledge:
  enabled: true
  backend: auto            # "qdrant", "sqlite", or "auto" (try qdrant, fall back)
  qdrant_host: "192.168.1.150"
  qdrant_port: 6333
  qdrant_collection: "recon_knowledge_hybrid"
  tei_host: "192.168.1.150"
  tei_port: 8090
  sparse_host: "192.168.1.150"
  sparse_port: 8091
  use_sparse: true
  top_k: 5
```

### Fallback: Local SQLite

If the Qdrant backend is unreachable, MeshAI falls back to a local SQLite knowledge base using FTS5 keyword search and `bge-small-en-v1.5` vector embeddings (384-dim).

```bash
# Build from Meshtastic ZIM file
python scripts/zim_to_knowledge.py meshtastic.zim --output knowledge.db
```

```yaml
knowledge:
  enabled: true
  backend: sqlite
  db_path: /data/meshai_knowledge.db
  top_k: 5
```

Requires `sqlite-vec` and `fastembed` for the SQLite backend.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                              MeshAI                                  │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  DATA SOURCES              INTELLIGENCE              DELIVERY        │
│  ┌─────────────┐          ┌──────────────┐         ┌────────────┐   │
│  │ Meshview ×N  │─────┐   │ Health Engine │────────▶│  Reporter  │   │
│  │ (staggered)  │     │   │ 5-pillar     │         │ Tier 1/2   │   │
│  └─────────────┘     ▼   │ scoring      │         └─────┬──────┘   │
│  ┌─────────────┐  ┌──────┴──┐            │               │          │
│  │ MeshMonitor │─▶│  Data   │─┘          │         ┌─────▼──────┐   │
│  │ (staggered) │  │  Store  │            │         │   Router   │   │
│  └─────────────┘  │ SQLite  │            │         │ scope/dist │   │
│                   └─────────┘            │         └─────┬──────┘   │
│                        │                 │               │          │
│                   ┌────▼────┐      ┌─────▼──────┐  ┌────▼────┐    │
│                   │ Feeder  │      │    LLM     │  │ Chunker │    │
│                   │ Sampling│      │  Backend   │  │LoRa-fit │    │
│                   └─────────┘      └────────────┘  └────┬────┘    │
│                                                         │         │
│  KNOWLEDGE             ALERTS             DELIVERY      │         │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐    │         │
│  │ RECON/Qdrant│  │   Alert     │  │ Subscription │    │         │
│  │ 2.8M vectors│  │   Engine    │  │   Manager    │    │         │
│  │ (network)   │  │ 17 triggers │  │ daily/weekly │    │         │
│  ├─────────────┤  │  scaling    │  │   alerts     │    │         │
│  │ SQLite FTS5 │  │  cooldown   │  └──────┬───────┘    │         │
│  │ (fallback)  │  └──────┬──────┘         │            │         │
│  └─────────────┘         │          ┌─────▼────────┐   │         │
│                          └─────────▶│  Responder   │◀──┘         │
│  ┌─────────────┐                    │ ACK-paced DM │             │
│  │ Conversation│                    │ Channel alert│             │
│  │   History   │                    └──────────────┘             │
│  └─────────────┘                                                 │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
         │                    │
    ┌────▼────┐          ┌────▼────┐
    │  TEI    │          │ Qdrant  │
    │ bge-m3  │          │ hybrid  │
    │ cortex  │          │ cortex  │
    └─────────┘          └─────────┘
```

## Dashboard API Reference

The dashboard exposes a REST API (default port 8080):

### Core Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | System health check |
| `/api/status` | GET | Full system status with health scores |
| `/api/nodes` | GET | Connected mesh nodes |
| `/api/messages` | GET | Recent mesh messages |

### Environmental Data

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/env/earthquakes` | GET | Recent earthquakes |
| `/api/env/weather` | GET | Weather conditions and alerts |
| `/api/env/fires` | GET | Active wildfires from NIFC |
| `/api/env/hotspots` | GET | NASA FIRMS satellite detections |
| `/api/env/traffic` | GET | Traffic incidents |
| `/api/env/water` | GET | Stream gauge readings |
| `/api/env/space` | GET | Space weather data |
| `/api/env/air` | GET | Air quality readings |

### Alerts

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/alerts/active` | GET | Currently active alerts |
| `/api/alerts/history` | GET | Historical alerts (`?severity=`, `?source=`, `?limit=`, `?offset=`) |
| `/api/alerts/{id}/ack` | POST | Acknowledge an alert |
| `/api/subscriptions` | GET | Alert subscriptions |

### WebSocket

Connect to `/ws` for real-time updates:

```javascript
const ws = new WebSocket('ws://localhost:8080/ws');
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  // data.type: 'message', 'alert', 'node_update', 'health_update'
};
```

## Message Chunking

Long responses are split into mesh-friendly chunks with sentence-aware splitting, configurable limits, and continuation prompts. Command output (like `!health`) packs multiple lines into 2-3 messages using newlines within each message to minimize airtime usage.

```yaml
response:
  max_length: 200       # Max chars per message
  max_messages: 3       # Messages before continuation prompt
```

## Alerting

Real-time alerts when mesh conditions change, with scaling cooldowns to prevent spam.

### Alert Conditions (17 total, each toggleable)

| Pillar | Condition | Default Threshold |
|--------|-----------|-------------------|
| Infrastructure | Router goes offline | — |
| Infrastructure | Router recovery | — |
| Infrastructure | New router appears | — |
| Power | Battery warning | <50% |
| Power | Battery critical | <25% |
| Power | Battery emergency | <10% |
| Power | 7-day declining trend | >10% drop with rate |
| Power | USB → battery (power outage) | — |
| Power | Solar not charging during day | — |
| Utilization | Sustained high utilization | >20% for 6h |
| Utilization | Packet flood | >500 pkts/24h |
| Coverage | Infra drops to single gateway | — |
| Coverage | Feeder gateway stops responding | — |
| Coverage | Region total blackout | All infra offline |
| Scores | Mesh health score drop | <70/100 |
| Scores | Region health score drop | <60/100 |

### Scaling Cooldown

Alerts don't spam. When a condition triggers:
1. **Alert 1**: fires immediately
2. **Alert 2**: 12 hours later (if still in condition)
3. **Alert 3**: 24 hours after that
4. **Alert 4**: 48 hours after that
5. **Stops** until condition resolves

When the condition clears, one recovery notification fires and the tracker resets.

### Delivery

Alerts are delivered two ways:
- **Channel broadcast**: configurable channel index for mesh-wide visibility
- **DM to subscribers**: users who ran `!sub alerts` receive DMs matching their scope

### Critical Nodes

Designate important infrastructure (e.g., MHR, HPR) as critical. When a critical node goes offline, alerts use priority formatting.

```yaml
mesh_intelligence:
  critical_nodes: ["MHR", "HPR"]
  alert_channel: 0        # Channel for broadcast alerts (-1 = disabled)
```

All conditions and thresholds are configurable via the TUI under Mesh Intelligence → Alert Rules.

## LLM Configuration

```yaml
llm:
  backend: "google"            # openai, anthropic, google
  api_key: "your-api-key"
  model: "gemini-2.0-flash"
```

### Local LLMs

MeshAI works with any OpenAI-compatible API:

- **LiteLLM**: `base_url: "http://localhost:4000/v1"`
- **Open WebUI**: `base_url: "http://localhost:3000/api"`
- **Ollama**: `base_url: "http://localhost:11434/v1"`

## Docker

### TCP Connection (recommended)

```yaml
connection:
  type: "tcp"
  tcp_host: "192.168.1.100"
  tcp_port: 4403
```

### Serial Connection

```yaml
connection:
  type: "serial"
  serial_port: "/dev/ttyUSB0"
```

Edit `docker-compose.serial.yml` to match your device path.

### Environment Variables

```bash
LLM_API_KEY=your-key-here docker compose up -d
```

## Running Alongside Other Services

### advBBS

MeshAI coexists with [advBBS](https://github.com/zvx-echo6/advbbs) on the same node. BBS protocol messages (sync, RAP, mail notifications) are automatically filtered. No configuration needed.

```yaml
bot:
  filter_bbs_protocols: true
```

### MeshMonitor

MeshAI integrates with [MeshMonitor](https://github.com/Yeraze/meshmonitor) at two levels: it fetches MeshMonitor's auto-responder patterns to avoid duplicate responses, and it uses MeshMonitor's API as a data source for mesh intelligence (battery, telemetry, traceroutes, solar).

```yaml
meshmonitor:
  enabled: true
  url: "http://192.168.1.100:8080"
  inject_into_prompt: true
  refresh_interval: 300
```

## Running as a Service

```ini
# /etc/systemd/system/meshai.service
[Unit]
Description=MeshAI - Meshtastic Mesh Intelligence
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/meshai
ExecStart=/usr/bin/python3 -m meshai
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable meshai
sudo systemctl start meshai
```

## Acknowledgments

- [Meshtastic](https://meshtastic.org/) — the mesh networking platform
- [MeshMonitor](https://github.com/Yeraze/meshmonitor) by Yeraze — monitoring integration and data source
- [advBBS](https://github.com/zvx-echo6/advbbs) — BBS coexistence design
- [sqlite-vec](https://github.com/asg017/sqlite-vec) by Alex Garcia — vector search in SQLite
- [fastembed](https://github.com/qdrant/fastembed) by Qdrant — fast local embeddings

## License

MIT License

## Author

K7ZVX - matt@echo6.co
