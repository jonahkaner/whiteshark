#!/usr/bin/env bash
set -euo pipefail

# Quicksand Trading Bot — One-command deploy script
# Usage: bash deploy.sh

echo "=== Quicksand Deploy ==="

# 1. Check Docker is installed
if ! command -v docker &>/dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    echo "Docker installed. You may need to log out and back in, then re-run this script."
    exit 0
fi

# 2. Check config.yaml exists
if [ ! -f config.yaml ]; then
    echo ""
    echo "ERROR: config.yaml not found!"
    echo ""
    echo "Quick setup:"
    echo "  cp config.example.yaml config.yaml"
    echo "  nano config.yaml   # Fill in your Kalshi API key ID and private key path"
    echo ""
    exit 1
fi

# 3. Check private key exists
KEY_PATH=$(grep 'private_key_path' config.yaml | head -1 | sed 's/.*: *//' | tr -d '"' | tr -d "'")
if [ -n "$KEY_PATH" ] && [ ! -f "$KEY_PATH" ]; then
    echo ""
    echo "ERROR: Private key not found at: $KEY_PATH"
    echo "Copy your .pem file there, e.g.:"
    echo "  mkdir -p keys"
    echo "  cp ~/Downloads/quicksand.pem keys/kalshi.pem"
    echo ""
    exit 1
fi

# 4. Create logs dir
mkdir -p logs

# 5. Build and run
echo "Building and starting Quicksand..."
if command -v docker-compose &>/dev/null; then
    docker-compose up -d --build
elif docker compose version &>/dev/null 2>&1; then
    docker compose up -d --build
else
    echo "docker-compose not found, using plain docker..."
    docker build -t quicksand .
    docker run -d \
        --name quicksand \
        -p 8000:8000 \
        -v "$(pwd)/config.yaml:/app/config.yaml:ro" \
        -v "$(pwd)/keys:/app/keys:ro" \
        -v "$(pwd)/logs:/app/logs" \
        --restart unless-stopped \
        quicksand
fi

echo ""
echo "=== Quicksand is running! ==="
echo ""
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):8000"
echo "  or:      http://localhost:8000"
echo ""
echo "Commands:"
echo "  docker-compose logs -f    # View logs"
echo "  docker-compose down       # Stop"
echo "  docker-compose restart    # Restart"
echo ""
