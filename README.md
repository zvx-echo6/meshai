# MeshAI

LLM-powered assistant for Meshtastic mesh networks.

## Features

- **LLM Chat**: Responds to @mentions and DMs with AI-generated responses
- **Multi-backend**: Supports OpenAI, Anthropic Claude, Google Gemini, and local LLMs via LiteLLM
- **Bang Commands**: `!help`, `!ping`, `!reset`, `!status`, `!weather`
- **Conversation History**: Per-user context maintained in SQLite
- **Smart Chunking**: Automatically splits long responses for mesh transmission
- **Rate Limiting**: Configurable delays to avoid flooding the mesh
- **advBBS Compatible**: Runs alongside [advBBS](https://github.com/NovaNexusMesh/advBBS) on the same node — protocol sync messages and mail notifications are automatically filtered
- **Rich Configurator**: Interactive TUI for easy setup

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
┌─────────────────────────────────────────────────────────────┐
│                        MeshAI                                │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────┐ │
│  │  Meshtastic │    │   Message   │    │   LLM Backend   │ │
│  │  Connector  │───▶│   Router    │───▶│   (pluggable)   │ │
│  │ Serial/TCP  │    │             │    │                 │ │
│  └─────────────┘    └─────────────┘    └─────────────────┘ │
│         │                 │                    │            │
│         │           ┌─────▼─────┐              │            │
│         │           │ Conversation│             │            │
│         │           │  History   │◀────────────┘            │
│         │           │  (SQLite)  │                          │
│         │           └───────────┘                           │
│         │                                                   │
│         ▼                                                   │
│  ┌─────────────┐                                           │
│  │  Responder  │  - 2.2-3s delay                           │
│  │             │  - Chunk to 150 chars                     │
│  │             │  - Max 2 messages                         │
│  └─────────────┘                                           │
└─────────────────────────────────────────────────────────────┘
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

## License

MIT License

## Author

K7ZVX - matt@echo6.co
