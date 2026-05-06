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

MeshAI answers questions using a local knowledge base built from Meshtastic documentation. The system uses hybrid search combining FTS5 keyword search, vector embeddings via `bge-small-en-v1.5`, and Reciprocal Rank Fusion for best relevance.

```bash
# Build from Meshtastic ZIM file
python scripts/zim_to_knowledge.py meshtastic.zim --output knowledge.db

# Or from markdown files
python scripts/md_to_knowledge.py docs/ --output knowledge.db
```

```yaml
knowledge:
  enabled: true
  db_path: /data/meshai_knowledge.db
  top_k: 5
  fts_weight: 0.5
  vector_weight: 0.5
```

Requires `sqlite-vec` and `fastembed` (installed with requirements.txt).

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
│  ┌─────────────┐  ┌──────┴──┐ │              │               │          │
│  │ MeshMonitor │─▶│  Data   │─┘              │         ┌─────▼──────┐   │
│  │ (staggered) │  │  Store  │                │         │   Router   │   │
│  └─────────────┘  │ SQLite  │                │         │ scope/dist │   │
│                   └─────────┘                │         └─────┬──────┘   │
│                        │                     │               │          │
│                   ┌────▼────┐          ┌─────▼──────┐  ┌────▼────┐    │
│                   │ Feeder  │          │    LLM     │  │ Chunker │    │
│                   │ Sampling│          │  Backend   │  │LoRa-fit │    │
│                   └─────────┘          └────────────┘  └────┬────┘    │
│                                                             │         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   ┌────▼────┐   │
│  │  Knowledge  │  │ Conversation│  │ Subscription │   │Responder│   │
│  │  Base (RAG) │  │   History   │  │   Manager    │   │  DM/CH  │   │
│  └─────────────┘  └─────────────┘  └─────────────┘   └─────────┘   │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

## Message Chunking

Long responses are split into mesh-friendly chunks with sentence-aware splitting, configurable limits, and continuation prompts. Command output (like `!health`) packs multiple lines into 2-3 messages using newlines within each message to minimize airtime usage.

```yaml
response:
  max_length: 200       # Max chars per message
  max_messages: 3       # Messages before continuation prompt
```

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
