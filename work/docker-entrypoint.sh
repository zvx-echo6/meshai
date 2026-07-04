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
  respond_to_dms: true
  filter_bbs_protocols: true

connection:
  type: tcp
  serial_port: /dev/ttyUSB0
  tcp_host: localhost
  tcp_port: 4403

response:
  delay_min: 2.2
  delay_max: 3.0
  max_length: 150
  max_messages: 2

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

context:
  enabled: true
  observe_channels: []
  ignore_nodes: []
  max_age: 1209600
  max_context_items: 20

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
  google_grounding: false

meshmonitor:
  enabled: false
  inject_into_prompt: true
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
trap "kill %1 2>/dev/null" EXIT

# Kill bot gracefully with SIGKILL fallback
kill_bot() {
    local pid=$1
    if ! kill -0 "$pid" 2>/dev/null; then
        return
    fi
    kill "$pid" 2>/dev/null || true
    echo "Sent SIGTERM to bot (PID $pid)"
    # Wait up to 5 seconds for graceful shutdown
    for i in 1 2 3 4 5; do
        kill -0 "$pid" 2>/dev/null || return
        sleep 1
    done
    # Force kill if still alive
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
        echo "Sent SIGKILL to bot (PID $pid)"
    fi
}

# Start the bot in a loop with integrated restart watcher
echo "Starting MeshAI..."
rm -f /tmp/meshai_restart
while true; do
    python -m meshai -v --config-file "$MESHAI_CONFIG" &
    BOT_PID=$!
    echo "$BOT_PID" > /tmp/meshai.pid
    echo "Bot started (PID $BOT_PID)"

    # Poll: wait for bot to exit OR restart signal
    while kill -0 $BOT_PID 2>/dev/null; do
        if [ -f /tmp/meshai_restart ]; then
            rm -f /tmp/meshai_restart
            echo "Restart signal received, restarting bot..."
            kill_bot $BOT_PID
            break
        fi
        sleep 1
    done

    wait $BOT_PID 2>/dev/null || true
    rm -f /tmp/meshai.pid
    echo "Bot exited. Restarting in 3s..."
    sleep 3
done
