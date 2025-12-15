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
# Configure via http://localhost:7681

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

history:
  database: /data/conversations.db
  max_messages_per_user: 20
  conversation_timeout: 86400

memory:
  enabled: true
  window_size: 4
  summarize_threshold: 8

llm:
  backend: openai
  api_key: ""
  base_url: https://api.openai.com/v1
  model: gpt-4o-mini
  system_prompt: >-
    You are a helpful assistant on a Meshtastic mesh network.
    Keep responses VERY brief - under 250 characters total.
    Be concise but friendly. No markdown formatting.

weather:
  primary: openmeteo
  fallback: llm
  default_location: ""
  openmeteo:
    url: https://api.open-meteo.com/v1
  wttr:
    url: https://wttr.in
EOF
    echo "Default config created. Configure via http://localhost:7682"
fi

# Start ttyd for web-based config access
echo "Starting web config interface on port 7682..."
ttyd -W -p 7682 \
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
