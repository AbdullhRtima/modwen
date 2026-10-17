#!/usr/bin/env bash
# Start the local Qwen server for Modwen on macOS/Linux (llama.cpp).
# Default = Tier D (Apple Silicon 16 GB): Qwen3-8B. Override with MODEL / ALIAS / CTX env vars, e.g.
#   MODEL=unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF:Q4_K_M ALIAS=qwen3-30b-a3b CTX=32768 ./scripts/start-llm.sh
set -euo pipefail
MODEL="${MODEL:-Qwen/Qwen3-8B-GGUF:Q4_K_M}"
ALIAS="${ALIAS:-qwen3-8b}"
CTX="${CTX:-16384}"
command -v llama-server >/dev/null || { echo "llama-server not found: brew install llama.cpp (see docs/local-llm-setup.md)"; exit 1; }
exec llama-server -hf "$MODEL" --alias "$ALIAS" --host 127.0.0.1 --port 8080 -c "$CTX" -ngl 99 --jinja \
  --cache-type-k q8_0 --cache-type-v q8_0
