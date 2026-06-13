#!/bin/bash

# HL Terminal Bot Runner
# Usage: ./run.sh

# Load environment variables
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# Check token
if [ -z "$TOKEN" ]; then
    echo "❌ TOKEN not set! Please set TOKEN in .env file"
    exit 1
fi

# Create directories
mkdir -p data logs

# Run bot
echo "🚀 Starting HL Terminal Bot..."
python3 bot.py
