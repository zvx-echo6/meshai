#!/bin/bash
# MeshAI Docker Entrypoint
# Runs ttyd for web config access and the bot

export MESHAI_CONFIG="/data/config.yaml"
export TERM="${TERM:-xterm-256color}"

# First run - no config exists, create defaults
if [ ! -f "$MESHAI_CONFIG" ]; then
    mkdir -p /data
    cat > "$MESHAI_CONFIG" << 'EOF'
# MeshAI Configuration
# Configure via http://localhost:7682

bot:
  name: ai
  owner: ""
  respond_to_mentions: true
  respond_to_dms: true

connection:
  type: tcp
  serial_port: /dev/ttyUSB0
  tcp_host: localhost
  tcp_port: 4403

channels:
  mode: all
  whitelist:
    - 0

response:
  delay_min: 2.2
  delay_max: 3.0
  max_length: 150
  max_messages: 2

rate_limits:
  messages_per_minute: 10
  global_messages_per_minute: 30
  cooldown_seconds: 5.0
  burst_allowance: 3

logging:
  level: INFO
  file: /data/meshai.log
  max_size_mb: 10
  backup_count: 3
  log_messages: true
  log_responses: true
  log_api_calls: false

history:
  database: /data/conversations.db
  max_messages_per_user: 50
  conversation_timeout: 86400
  auto_cleanup: true
  cleanup_interval_hours: 24
  max_age_days: 30

memory:
  enabled: true
  window_size: 4
  summarize_threshold: 8

llm:
  backend: openai
  api_key: ""
  base_url: https://api.openai.com/v1
  model: gpt-4o-mini
  timeout: 30
  system_prompt: >-
    You are a helpful assistant on a Meshtastic mesh network.
    Keep responses VERY brief - under 250 characters total.
    Be concise but friendly. No markdown formatting.
  retry_attempts: 2
  fallback_on_error: true
  fallback_on_timeout: true

safety:
  max_response_length: 250
  filter_profanity: false
  blocked_phrases: []
  require_mention: true
  ignore_self: true
  emergency_keywords:
    - emergency
    - help
    - sos

users:
  blocklist: []
  allowlist_only: false
  allowlist: []
  admin_nodes: []
  vip_nodes: []

commands:
  enabled: true
  prefix: "!"
  disabled_commands: []
  custom_commands: {}

personality:
  system_prompt: ""
  context_injection: ""
  personas: {}

web_status:
  enabled: false
  port: 8080
  show_uptime: true
  show_message_count: true
  show_connected_nodes: true
  show_recent_activity: false
  require_auth: false
  auth_password: ""

announcements:
  enabled: false
  interval_hours: 24
  channel: 0
  messages: []
  random_order: true

weather:
  primary: openmeteo
  fallback: llm
  default_location: ""
  openmeteo:
    url: https://api.open-meteo.com/v1
  wttr:
    url: https://wttr.in

integrations:
  weather:
    primary: openmeteo
    fallback: llm
    default_location: ""
  webhook:
    enabled: false
    url: ""
    events:
      - message_received
      - response_sent
      - error
EOF
    echo "Default config created. Configure via http://localhost:7682"
fi

# Start ttyd for web-based config access
echo "Starting web config interface on port 7682..."
ttyd -W -p 7682 \
    -t enableClipboard=true \
    -t titleFixed="MeshAI Config" \
    -t 'theme={"background":"#0d1117","foreground":"#c9d1d9","cursor":"#58a6ff","selectionBackground":"#388bfd"}' \
    -t fontSize=14 \
    /bin/bash -c 'while true; do python3 -m meshai --config-file "$MESHAI_CONFIG" --config; sleep 1; done' &

# Keep ttyd running even if bot fails
trap "kill %1 2>/dev/null; kill %2 2>/dev/null" EXIT

# Restart watcher - monitors for restart signal from config tool
(
    while true; do
        if [ -f /tmp/meshai_restart ]; then
            rm -f /tmp/meshai_restart
            echo "Restart signal received, restarting bot..."
            pkill -f "python.*meshai.*--config" --signal 0 2>/dev/null || true  # Don't kill config tool
            pkill -f "python -m meshai --config-file" || true
            sleep 1
        fi
        sleep 2
    done
) &

# Start the bot in a loop - retry on failure
echo "Starting MeshAI..."
while true; do
    python -m meshai --config-file "$MESHAI_CONFIG" || true
    echo "Bot exited. Check config at http://localhost:7682. Retrying in 5s..."
    sleep 5
done
