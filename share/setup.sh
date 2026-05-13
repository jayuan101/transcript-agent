#!/bin/bash
# Transcript Agent — First Time Setup (Mac / Linux)
echo ""
echo "============================================"
echo " Transcript Agent - Setup"
echo "============================================"
echo ""

if ! command -v docker &>/dev/null; then
    echo "ERROR: Docker is not installed."
    echo "Mac:   https://www.docker.com/products/docker-desktop"
    echo "Linux: sudo apt install docker.io docker-compose-plugin"
    exit 1
fi

if ! docker info &>/dev/null; then
    echo "ERROR: Docker is not running. Start Docker Desktop and try again."
    exit 1
fi

if [ ! -f .env ]; then
    echo "You need an Anthropic API key — get one free at https://console.anthropic.com"
    echo ""
    read -p "Paste your Anthropic API key: " API_KEY
    echo "ANTHROPIC_API_KEY=$API_KEY" > .env
    echo "API key saved."
else
    echo "API key file found."
fi

echo ""
echo "[1/2] Pulling image from Docker Hub..."
docker compose pull

echo ""
echo "[2/2] Starting Transcript Agent..."
docker compose up -d

echo ""
echo "Waiting for app to start..."
sleep 20

open "http://localhost:7860" 2>/dev/null || xdg-open "http://localhost:7860" 2>/dev/null || true

echo ""
echo "============================================"
echo " App is running!"
echo ""
echo " UI:  http://localhost:7860"
echo " API: http://localhost:8000/docs"
echo ""
echo " To stop:    docker compose down"
echo " To restart: docker compose up -d"
echo "============================================"
