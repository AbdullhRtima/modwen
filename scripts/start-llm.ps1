# Start the local Qwen server for Modwen on Windows (llama.cpp).
# Edit the three values for your tier (run `uv run modwen doctor` to see it), then:
#   powershell -ExecutionPolicy Bypass -File scripts\start-llm.ps1
$LlamaDir = "C:\llama"                       # folder where you unzipped llama.cpp
$Model    = "Qwen/Qwen3-8B-GGUF:Q4_K_M"      # Tier A: Qwen/Qwen3-14B-GGUF:Q4_K_M  | Tier E: unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF:Q4_K_M
$Alias    = "qwen3-8b"                       # must match llm.profiles.local_qwen.model in config.yaml
$Ctx      = 16384                            # must match context_window (Tier A/E: 32768)
$Gpu      = 99                               # 0 = CPU only (no NVIDIA GPU)

& "$LlamaDir\llama-server.exe" -hf $Model --alias $Alias --host 127.0.0.1 --port 8080 -c $Ctx -ngl $Gpu --jinja `
  --cache-type-k q8_0 --cache-type-v q8_0
