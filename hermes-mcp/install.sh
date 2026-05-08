#!/usr/bin/env bash
#
# Install the hermes-mcp server as a systemd service.
#
# Usage:
#   ./install.sh             # full install: venv + deps + systemd unit
#   ./install.sh test        # smoke-run from venv (foreground)
#   ./install.sh uninstall   # remove the systemd unit (keeps venv + code)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/venv"
SERVICE_NAME="hermes-mcp"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TRADING_BOT_ROOT="$(cd "$HERE/.." && pwd)"

cmd="${1:-install}"

# ---------------------------------------------------------------------------

ensure_venv() {
    if [[ ! -d "$VENV" ]]; then
        echo "[install] creating venv at $VENV"
        python3 -m venv "$VENV"
    fi
    "$VENV/bin/pip" install --quiet --upgrade pip
    echo "[install] installing requirements"
    "$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
}

write_service() {
    local env_file="$TRADING_BOT_ROOT/.env"
    if [[ ! -f "$env_file" ]]; then
        echo "[install] WARNING: $env_file not found — service may fail to start"
    fi

    local key
    key="${HERMES_MCP_KEY:-}"
    if [[ -z "$key" ]]; then
        key="$(openssl rand -hex 24)"
        echo "[install] generated HERMES_MCP_KEY=$key"
        echo "          add this to $env_file"
    fi

    sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Hermes MCP server (trading-bot)
After=network.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
Group=$(id -gn)
WorkingDirectory=$HERE
EnvironmentFile=-${env_file}
Environment=TRADING_BOT_ROOT=${TRADING_BOT_ROOT}
Environment=HERMES_MCP_PORT=8089
Environment=HERMES_MCP_TRANSPORT=sse
Environment=FREQTRADE_API_URL=http://localhost:8080
Environment=POSTGRES_HOST=localhost
Environment=POSTGRES_PORT=5434
ExecStart=${VENV}/bin/python ${HERE}/server.py
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    echo "[install] systemd unit installed at $SERVICE_FILE"
}

case "$cmd" in
    install)
        ensure_venv
        write_service
        sudo systemctl enable "$SERVICE_NAME"
        sudo systemctl restart "$SERVICE_NAME"
        sleep 2
        sudo systemctl --no-pager status "$SERVICE_NAME" || true
        ;;
    test)
        ensure_venv
        echo "[test] running server in foreground — Ctrl-C to stop"
        TRADING_BOT_ROOT="$TRADING_BOT_ROOT" \
        HERMES_MCP_PORT=8089 \
        HERMES_MCP_TRANSPORT=sse \
        FREQTRADE_API_URL=http://localhost:8080 \
        POSTGRES_HOST=localhost POSTGRES_PORT=5434 \
        "$VENV/bin/python" "$HERE/server.py"
        ;;
    uninstall)
        sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
        sudo rm -f "$SERVICE_FILE"
        sudo systemctl daemon-reload
        echo "[uninstall] removed $SERVICE_FILE"
        ;;
    *)
        echo "usage: $0 {install|test|uninstall}" >&2
        exit 2
        ;;
esac
