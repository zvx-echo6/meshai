# MeshAI - Meshtastic LLM Bridge

## Project Overview

A Python application that connects to a Meshtastic node and provides LLM-powered responses to mesh network users. Responds to direct mentions (@nodename) or direct messages. Includes bang commands (`!command`) for utility functions.

## Design Decisions

### 1. Trigger Mechanism
- **@mentions**: Respond when message contains `@<nodename>` (configurable node name)
- **Direct Messages**: Respond to all DMs automatically
- **Bang commands**: `!command` syntax for utility functions (handled before LLM)
- Ignore general channel chatter that doesn't mention the bot

### 2. Conversation History
- Maintain per-user conversation history
- Storage: SQLite database for persistence across restarts
- Context window: Last N messages per user (configurable, default ~20 exchanges)
- With 300 char limit per exchange, context stays small - can maintain long conversations
- Include timestamp tracking for potential "conversation timeout" (e.g., reset after 24h inactivity)

### 3. Rate Limiting & Response Behavior
- **Response delay**: Configurable 2.2-3.0 second random delay before sending
- **Message chunking**: Split responses at 150 characters max per message
- **Max chunks**: 2 messages maximum per response (300 chars total)
- **Brevity prompt**: System prompt instructs LLM to keep responses concise
- **Cooldown**: Optional per-user cooldown to prevent spam

### 4. Identity & Configuration
- Node name/ID determined by the physical node configuration
- Application config includes:
  - `bot_name`: The @mention trigger name (e.g., "meshbot", "ai")
  - `owner`: Owner identification for logging/admin purposes
  - Connection settings (serial port or TCP host:port)

### 5. Channel Filtering
- Configurable list of channels to respond on
- Option to respond on all channels or specific ones only
- DMs always processed regardless of channel settings

## Technical Architecture

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
│  │  Response   │  - 2.2-3s delay                           │
│  │  Handler    │  - Chunk to 150 chars                     │
│  │             │  - Max 2 messages                         │
│  └─────────────┘                                           │
└─────────────────────────────────────────────────────────────┘
```

## LLM Backend Support

### Pluggable Backend Interface
```python
class LLMBackend(ABC):
    @abstractmethod
    async def generate(self, messages: list[dict], system_prompt: str) -> str:
        pass
```

### Supported Backends (Priority Order)
1. **OpenAI-compatible** (covers most bases)
   - OpenAI (GPT-4, GPT-4o, etc.)
   - Local LiteLLM/Open WebUI (ai.echo6.co)
   - Any OpenAI-compatible API

2. **Anthropic** (Claude)
   - Direct Anthropic API

3. **Google** (Gemini)
   - Google AI Studio / Vertex AI

### Configuration Example
```yaml
llm:
  backend: "openai"  # openai, anthropic, google
  api_key: "${OPENAI_API_KEY}"
  base_url: "https://api.openai.com/v1"  # or http://ai.echo6.co/api for local
  model: "gpt-4o-mini"

  # For local LiteLLM:
  # backend: "openai"
  # base_url: "http://192.168.1.239:4000/v1"
  # model: "llama3"
```

## Configuration File Structure

```yaml
# config.yaml
bot:
  name: "ai"                    # @mention trigger
  owner: "K7ZVX"               # Owner callsign/name
  respond_to_mentions: true
  respond_to_dms: true

connection:
  type: "serial"               # serial or tcp
  serial_port: "/dev/ttyUSB0"  # if serial
  tcp_host: "192.168.1.100"    # if tcp
  tcp_port: 4403               # if tcp

channels:
  mode: "all"                  # "all" or "whitelist"
  whitelist: [0, 1]            # Only if mode is "whitelist"

response:
  delay_min: 2.2               # seconds
  delay_max: 3.0               # seconds
  max_length: 150              # chars per message
  max_messages: 2              # messages per response

history:
  database: "conversations.db"
  max_messages_per_user: 20
  conversation_timeout: 86400  # seconds (24h)

llm:
  backend: "openai"
  api_key: "${LLM_API_KEY}"
  base_url: "https://api.openai.com/v1"
  model: "gpt-4o-mini"
  system_prompt: |
    You are a helpful assistant on a Meshtastic mesh network.
    Keep responses VERY brief - under 250 characters total.
    Be concise but friendly. No markdown formatting.

weather:
  primary: "openmeteo"         # openmeteo, wttr, or llm
  fallback: "llm"              # openmeteo, wttr, llm, or none
  default_location: ""         # Fallback if node has no GPS (e.g., "Seattle, WA")

  openmeteo:
    url: "https://api.open-meteo.com/v1"  # or self-hosted URL

  wttr:
    url: "https://wttr.in"     # or self-hosted
```

## Bang Commands

Commands use `!` prefix (like fq51bbs). Processed before LLM routing.

| Command | Description | Example |
|---------|-------------|---------|
| `!help` | List available commands | `!help` |
| `!ping` | Connectivity test, responds "pong" | `!ping` |
| `!reset` | Clear your conversation history | `!reset` |
| `!status` | Bot uptime, message count, version | `!status` |
| `!weather` | Weather for your node's GPS location (or default) | `!weather` |
| `!weather <loc>` | Weather for specified location | `!weather Seattle` |

### Weather Command Details

Location resolution order:
1. If `!weather <location>` - geocode the provided location
2. If `!weather` (no args) - use sender's node GPS position if available
3. Fall back to `weather.default_location` from config
4. If no location found: "No location available. Use !weather <city> or enable GPS on your node."

**Providers:**
- `openmeteo` - Open-Meteo API (free, no key, self-hostable)
- `wttr` - wttr.in (free, simple, self-hostable)
- `llm` - Pass to LLM with websearch (flexible, slower)

Primary/fallback configurable. If primary fails, tries fallback.

### Command Processing Flow

```
Message received
      │
      ▼
┌─────────────┐
│ Starts with │──No──▶ Check @mention / DM ──▶ LLM
│    "!"?     │
└─────────────┘
      │Yes
      ▼
┌─────────────┐
│ Parse cmd   │
│ & args      │
└─────────────┘
      │
      ▼
┌─────────────┐
│ Lookup in   │──Not found──▶ "Unknown command. Try !help"
│ registry    │
└─────────────┘
      │Found
      ▼
┌─────────────┐
│ Execute     │
│ handler     │
└─────────────┘
```

### Command Handler Interface

```python
class CommandHandler(ABC):
    @abstractmethod
    async def execute(self, sender_id: str, args: str, context: MessageContext) -> str:
        """Execute command and return response string."""
        pass
```

## CLI Configurator

Interactive TUI configurator using Rich library (same style as fq51bbs).

**Features:**
- Hierarchical menu system with numeric selection
- `0` always = back/save & exit
- Tables showing current values
- Status icons (✓/✗) with color coding
- Setup wizard for first-time configuration
- Unsaved changes tracking
- Inline help for complex options

**Menu Structure:**
```
Main Menu
├── 1. Bot Settings (name, owner, triggers)
├── 2. Connection (serial/TCP config)
├── 3. LLM Backend (provider, API keys, model)
├── 4. Commands & Weather (providers, fallbacks)
├── 5. Response Settings (delays, chunking)
├── 6. Channel Filtering
├── 7. History Settings
├── 8. Run Setup Wizard
└── 0. Save & Exit
```

**Invocation:**
```bash
meshai --config          # Launch configurator
meshai                   # Run bot (uses config.yaml)
meshai --config-file /path/to/config.yaml  # Use alternate config
```

**Config Reload/Restart:**
- On save, prompt: "Restart bot with new config? [Y/n]"
- If bot is running as systemd service: `systemctl restart meshai`
- If running in foreground: signal reload (SIGHUP) or full restart
- Store PID file at runtime for service management

## File Structure

```
meshai/
├── meshai/
│   ├── __init__.py
│   ├── main.py              # Entry point
│   ├── config.py            # Configuration loading/saving
│   ├── connector.py         # Meshtastic serial/TCP connection
│   ├── router.py            # Message routing logic
│   ├── history.py           # Conversation history (SQLite)
│   ├── responder.py         # Response handling (delay, chunking)
│   ├── cli/
│   │   ├── __init__.py
│   │   └── configurator.py  # Rich-based TUI configurator
│   ├── commands/
│   │   ├── __init__.py
│   │   ├── base.py          # Command handler interface
│   │   ├── dispatcher.py    # Command registry & routing
│   │   ├── help.py          # !help
│   │   ├── ping.py          # !ping
│   │   ├── reset.py         # !reset
│   │   ├── status.py        # !status
│   │   └── weather.py       # !weather
│   └── backends/
│       ├── __init__.py
│       ├── base.py          # Abstract backend interface
│       ├── openai.py        # OpenAI-compatible backend
│       ├── anthropic.py     # Anthropic backend
│       └── google.py        # Google Gemini backend
├── config.yaml              # User configuration
├── requirements.txt
├── pyproject.toml
└── README.md
```

## Dependencies

```
meshtastic>=2.3.0
pyyaml>=6.0
aiosqlite>=0.19.0
openai>=1.0.0
anthropic>=0.18.0
google-generativeai>=0.4.0
```

## Implementation Phases

### Phase 1: Core Foundation
- [ ] Project structure setup
- [ ] Configuration loading
- [ ] Meshtastic connector (serial first, then TCP)
- [ ] Basic message receiving and logging

### Phase 2: Message Processing
- [ ] Message router (detect @mentions and DMs)
- [ ] Conversation history database
- [ ] User context management

### Phase 3: LLM Integration
- [ ] Backend interface definition
- [ ] OpenAI-compatible backend (covers local + OpenAI)
- [ ] Response generation with history

### Phase 4: Response Handling
- [ ] Delay implementation (2.2-3s random)
- [ ] Message chunking (150 char limit)
- [ ] Send responses back to mesh

### Phase 5: Additional Backends
- [ ] Anthropic backend
- [ ] Google Gemini backend

### Phase 6: Polish
- [ ] Error handling and resilience
- [ ] Logging and monitoring
- [ ] Documentation
- [ ] Packaging for easy installation

## Future Considerations

- **Multi-node support**: One instance managing multiple nodes (different presets/locations)
- **Store-and-forward**: Queue messages for offline users
- **Games**: Simple text games (trivia, 8-ball, etc.)
- **Scheduled broadcasts**: Periodic announcements

## Notes

- Meshtastic Python API: https://meshtastic.org/docs/software/python/cli/
- Message size limit is 237 bytes, but we're targeting 150 chars for safety and readability
- The meshtastic library handles serial/TCP abstraction well
