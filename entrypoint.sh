#!/bin/bash
set -e

echo "========================================"
echo "  iflow-bot Docker Entrypoint"
echo "========================================"

# Check if iflow CLI is available
if command -v iflow &> /dev/null; then
    echo "✅ iflow CLI found: $(iflow --version 2>/dev/null || echo 'installed')"
else
    echo "❌ iflow CLI not found!"
    echo "   Please check the Docker image build."
    exit 1
fi

# Check if iflow is logged in by verifying auth files exist
if [ -d /root/.iflow ] && [ "$(ls -A /root/.iflow 2>/dev/null)" ]; then
    echo "✅ iflow auth data found"
else
    echo "⚠️  No iflow auth data detected!"
    echo "   To login, run interactively:"
    echo "   docker run -it -v iflow-auth:/root/.iflow iflow-bot:latest bash"
    echo "   Then run: iflow"
    echo ""
    echo "   Or copy auth from host:"
    echo "   docker run --rm -v iflow-auth:/data -v ~/.iflow:/src alpine sh -c 'cp -r /src/* /data/'"
    echo ""
fi

# Initialize config if not exists
if [ ! -f /root/.iflow-bot/config.json ]; then
    echo "📝 Initializing default config..."
    iflow-bot onboard 2>/dev/null || true
fi

# Show enabled channels
echo ""
echo "Starting iflow-bot..."
echo "========================================"

# Execute the command
exec iflow-bot "$@"
