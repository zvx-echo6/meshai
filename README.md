# MeshAI

LLM-powered assistant for Meshtastic mesh networks.

## Features

- **LLM Chat**: Responds to @mentions and DMs with AI-generated responses
- **Multi-backend**: Supports OpenAI, Anthropic Claude, Google Gemini, and local LLMs via LiteLLM
- **Knowledge Base (RAG)**: Hybrid FTS5 + vector search over Meshtastic documentation
- **Message Chunking**: Sentence-aware splitting with continuation prompts for long responses
- **Bang Commands**: `!help`, `!ping`, `!reset`, `!status`, `!weather`
- **Conversation History**: Per-user context maintained in SQLite
- **Rate Limiting**: Configurable delays to avoid flooding the mesh
- **advBBS Compatible**: Runs alongside [advBBS](https://github.com/NovaNexusMesh/advBBS) on the same node — protocol sync messages and mail notifications are automatically filtered
- **Rich Configurator**: Interactive TUI for easy setup
- **MeshMonitor Integration**: Syncs with [MeshMonitor](https://github.com/Yeraze/meshmonitor) by Yeraze to avoid duplicate responses

## Installation

```bash
# Clone the repository
git clone https://github.com/zvx-echo6/meshai.git
cd meshai

# Install with pip
pip install -e .

# Or install dependencies manually
pip install -r requirements.txt
```

## Quick Start

```bash
# Run the configurator
meshai --config

# Or copy and edit the example config
cp config.example.yaml config.yaml
# Edit config.yaml with your settings

# Run the bot
meshai
```

## Configuration

Run `meshai --config` to launch the interactive configurator, or edit `config.yaml` directly.

### Key Settings

```yaml
bot:
  name: "ai"                    # @mention trigger
  respond_to_mentions: true
  respond_to_dms: true

connection:
  type: "serial"               # serial or tcp
  serial_port: "/dev/ttyUSB0"

llm:
  backend: "openai"            # openai, anthropic, google
  api_key: "your-api-key"
  model: "gpt-4o-mini"
```

### Using Local LLMs

MeshAI works with any OpenAI-compatible API, including:

- **LiteLLM**: `base_url: "http://localhost:4000/v1"`
- **Open WebUI**: `base_url: "http://localhost:3000/api"`
- **Ollama**: `base_url: "http://localhost:11434/v1"`

## Commands

| Command | Description |
|---------|-------------|
| `!help` | Show available commands |
| `!ping` | Test connectivity |
| `!reset` | Clear your conversation history |
| `!status` | Show bot status and stats |
| `!weather [location]` | Get weather (uses GPS if no location given) |

## Usage Examples

**Chat via @mention:**
```
@ai What's the weather like today?
> Seattle: 52F, Partly Cloudy, Wind 8mph
```

**Direct message:**
```
DM: Tell me a short joke
> Why don't scientists trust atoms? They make up everything!
```

**Weather command:**
```
!weather Portland
> Portland: 48F, Rain, Wind 12mph
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                           MeshAI                                  │
├──────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌──────────────────────┐ │
│  │  Meshtastic │    │   Message   │    │    LLM Backend       │ │
│  │  Connector  │───▶│   Router    │───▶│    (pluggable)       │ │
│  │ Serial/TCP  │    │             │    │                      │ │
│  └─────────────┘    └──────┬──────┘    └──────────────────────┘ │
│         │                  │                     │               │
│         │           ┌──────▼──────┐              │               │
│         │           │ Conversation│              │               │
│         │           │   History   │◀─────────────┘               │
│         │           │  (SQLite)   │                              │
│         │           └─────────────┘                              │
│         │                  │                                     │
│         │           ┌──────▼──────┐    ┌──────────────────────┐ │
│         │           │  Knowledge  │───▶│  Hybrid FTS5+Vector  │ │
│         │           │    Base     │    │  (sqlite-vec + BGE)  │ │
│         │           └─────────────┘    └──────────────────────┘ │
│         │                                                        │
│         ▼                                                        │
│  ┌─────────────┐    ┌─────────────┐                             │
│  │  Responder  │───▶│   Chunker   │  Sentence-aware splitting   │
│  │             │    │             │  + continuation prompts     │
│  └─────────────┘    └─────────────┘                             │
└──────────────────────────────────────────────────────────────────┘
```

## Knowledge Base (RAG)

MeshAI can answer questions using a local knowledge base built from Meshtastic documentation. The system uses hybrid search combining:

- **FTS5 keyword search** — fast exact term matching with domain-aware query construction
- **Vector embeddings** — semantic similarity using `bge-small-en-v1.5` (384 dimensions)
- **Reciprocal Rank Fusion** — merges results from both methods for best relevance

**Building the knowledge base:**

```bash
# Extract from Meshtastic ZIM file
python scripts/zim_to_knowledge.py meshtastic.zim --output knowledge.db

# Or from markdown files
python scripts/md_to_knowledge.py docs/ --output knowledge.db
```

**Configuration:**

```yaml
knowledge:
  enabled: true
  db_path: /data/meshai_knowledge.db
  top_k: 5              # Number of chunks to retrieve
  fts_weight: 0.5       # Weight for keyword matches (0-1)
  vector_weight: 0.5    # Weight for semantic matches (0-1)
```

The knowledge base requires `sqlite-vec` and `fastembed` (installed automatically with requirements.txt).

## Message Chunking

Long LLM responses are automatically split into mesh-friendly chunks:

- **Sentence-aware** — never splits a sentence across messages
- **Configurable limits** — max characters per message, max messages per response
- **Continuation prompts** — if content remains, asks "Want me to keep going?"
- **Natural follow-ups** — responds to "yes", "ok", "continue", "more", etc.

**Configuration:**

```yaml
response:
  max_length: 200       # Max chars per message
  max_messages: 3       # Messages before continuation prompt
```

## Docker

### Quick Start with Docker

```bash
# Create working directory
mkdir -p meshai/data && cd meshai

# Download docker-compose file
curl -O https://raw.githubusercontent.com/zvx-echo6/meshai/main/docker-compose.yml

# Copy and edit config
curl -o data/config.yaml https://raw.githubusercontent.com/zvx-echo6/meshai/main/config.example.yaml
# Edit data/config.yaml with your settings

# Start
docker compose up -d

# View logs
docker compose logs -f
```

### Docker Configuration

**TCP Connection** (recommended for Docker):
```yaml
# data/config.yaml
connection:
  type: "tcp"
  tcp_host: "192.168.1.100"  # Your Meshtastic node IP
  tcp_port: 4403
```

**Serial Connection**:
```yaml
# data/config.yaml
connection:
  type: "serial"
  serial_port: "/dev/ttyUSB0"
```

Then edit `docker-compose.serial.yml` to match your device path.

### Environment Variables

You can pass the API key via environment variable instead of config file:

```bash
LLM_API_KEY=your-key-here docker compose up -d
```

Or create a `.env` file:
```bash
LLM_API_KEY=your-key-here
```

### View Logs

```bash
docker compose logs -f meshai
```

## Running Alongside advBBS

MeshAI is designed to coexist with [advBBS](https://github.com/NovaNexusMesh/advBBS) on the same Meshtastic node. Both connect via TCP to meshtasticd and share the radio, but MeshAI automatically ignores advBBS traffic:

- **Sync protocol** — `MAILREQ|`, `MAILACK|`, `MAILDAT|`, `BOARDREQ|`, etc.
- **RAP protocol** — `advBBS|` pings, pongs, and route advertisements
- **Mail notifications** — `[MAIL]` new message alerts
- **Bang commands in DMs** — `!mail`, `!board`, etc. are left for advBBS to handle

No special configuration is needed. The filter is enabled by default and can be toggled in `config.yaml`:

```yaml
bot:
  filter_bbs_protocols: true   # set false to disable
```

Plain-text BBS responses (e.g. "Welcome back, matt!") are indistinguishable from normal user messages and will be processed normally — this is a known and accepted limitation.

## MeshMonitor Integration

MeshAI integrates with [MeshMonitor](https://github.com/Yeraze/meshmonitor), a comprehensive Meshtastic monitoring platform by Yeraze. When enabled, MeshAI automatically fetches MeshMonitor's auto-responder trigger patterns and ignores messages that MeshMonitor handles, preventing duplicate responses on the mesh.

**Features:**
- Automatic trigger discovery via MeshMonitor's HTTP API
- Dynamic ignore list — no manual sync needed
- Trigger list injected into the LLM prompt so MeshAI can discuss MeshMonitor commands conversationally
- Configurable via TUI (option 9) or config.yaml

**Configuration:**

```yaml
meshmonitor:
  enabled: true
  url: "http://192.168.1.100:8080"
  inject_into_prompt: true
  refresh_interval: 300
```

MeshMonitor is a separate project — get it at https://github.com/Yeraze/meshmonitor

## Running as a Service

Create `/etc/systemd/system/meshai.service`:

```ini
[Unit]
Description=MeshAI - Meshtastic LLM Assistant
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

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable meshai
sudo systemctl start meshai
```

## Acknowledgments

- [Meshtastic](https://meshtastic.org/) — the mesh networking platform
- [MeshMonitor](https://github.com/Yeraze/meshmonitor) by Yeraze — monitoring integration
- [advBBS](https://github.com/NovaNexusMesh/advBBS) by NovaNexusMesh — BBS coexistence design
- [sqlite-vec](https://github.com/asg017/sqlite-vec) by Alex Garcia — vector search in SQLite
- [fastembed](https://github.com/qdrant/fastembed) by Qdrant — fast local embeddings

## License

MIT License

## Author

K7ZVX - matt@echo6.co
