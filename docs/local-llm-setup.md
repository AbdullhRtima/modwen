# Local LLM setup (Qwen on localhost:8080)

For: macOS (Apple Silicon) and Windows. Run `uv run modwen doctor` first; it prints your hardware tier and the exact command for it.

Modwen needs one thing from the AI model: an OpenAI-compatible server at `http://localhost:8080/v1`. This guide sets that up with **llama.cpp** (recommended) or **Ollama**. Both run 100% locally.


---

## Step 0: Pick the model for each machine

Run `uv run modwen doctor` on each machine. It prints the tier and the exact command to start the server.

| Machine | Model | Why |
|---|---|---|
| **Apple Silicon, 16 GB (Tier D)** | **Qwen3-8B, Q4_K_M** (about 5 GB), 16k context | Fits next to Whisper in 16 GB. 14B is possible but tight. |
| Windows + NVIDIA 12 GB+ VRAM (Tier A) | Qwen3-14B, Q4_K_M, 32k context | Best quality that fits |
| Windows + NVIDIA 8 GB (Tier B) | Qwen3-8B, Q4_K_M, 16k context | Modwen runs Whisper first, then the LLM |
| Windows, no NVIDIA GPU, 32 GB RAM (Tier E) | Qwen3-30B-A3B-Instruct-2507, Q4_K_M | Fast on CPU because only 3B parameters are active per token |
| Windows, no NVIDIA GPU, 16 GB RAM (Tier F) | Qwen3-4B-Instruct-2507, Q4_K_M | English summaries are OK; Arabic is weak |

**Find your Windows specs:**
- Task Manager → **Performance**: Memory (GB), and GPU → "Dedicated GPU memory".
- Or run `nvidia-smi` in PowerShell, if you have an NVIDIA card.

---

## Option A: llama.cpp `llama-server` (recommended)

### A1. Install

**macOS:**
```bash
brew install llama.cpp
llama-server --version
```
No Homebrew? Download `llama-<build>-bin-macos-arm64.tar.gz` from https://github.com/ggml-org/llama.cpp/releases, unpack it, and run `xattr -dr com.apple.quarantine <folder>` once. Then use `./llama-server` from that folder.

**Windows:**
- **NVIDIA GPU:** from https://github.com/ggml-org/llama.cpp/releases download both `llama-<build>-bin-win-cuda-12.4-x64.zip` and `cudart-llama-bin-win-cuda-12.4-x64.zip`. Unzip both into one folder, e.g. `C:\llama`.
- **No NVIDIA GPU:** run `winget install llama.cpp`, or download `llama-<build>-bin-win-cpu-x64.zip` (or the `vulkan` build for AMD/Intel GPUs).

Check it works:
```powershell
C:\llama\llama-server.exe --version
```

### A2. Start the server on port 8080

**macOS, Apple Silicon 16 GB (Tier D):**
```bash
llama-server -hf Qwen/Qwen3-8B-GGUF:Q4_K_M --alias qwen3-8b \
  --host 127.0.0.1 --port 8080 -c 16384 -ngl 99 --jinja \
  --cache-type-k q8_0 --cache-type-v q8_0
```
- The first start downloads the model, about 5 GB.
- It's ready when the log says `server is listening on http://127.0.0.1:8080`.
- Leave this terminal open while you use Modwen.

**Windows (one line in PowerShell; pick the line for your tier):**
```powershell
# Tier A (NVIDIA 12 GB+)
C:\llama\llama-server.exe -hf Qwen/Qwen3-14B-GGUF:Q4_K_M --alias qwen3-14b --host 127.0.0.1 --port 8080 -c 32768 -ngl 99 --jinja --cache-type-k q8_0 --cache-type-v q8_0
# Tier B (NVIDIA 8 GB)
C:\llama\llama-server.exe -hf Qwen/Qwen3-8B-GGUF:Q4_K_M --alias qwen3-8b --host 127.0.0.1 --port 8080 -c 16384 -ngl 99 --jinja --cache-type-k q8_0 --cache-type-v q8_0
# Tier E (CPU, 32 GB RAM)
C:\llama\llama-server.exe -hf unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF:Q4_K_M --alias qwen3-30b-a3b --host 127.0.0.1 --port 8080 -c 32768 -ngl 0 --jinja
```

What the flags mean:

| Flag | Meaning |
|---|---|
| `-hf repo:quant` | Download the model from Hugging Face on first run. If a tag isn't found, open the repo page and use the exact file name with `-m path\to\file.gguf`. |
| `--alias` | The model name Modwen sends. It must equal `model:` in `config.yaml`. |
| `-c` | Context size. It must equal `context_window:` in `config.yaml`. |
| `-ngl 99` | Put the whole model on the GPU (Metal or CUDA). Use `-ngl 0` for CPU only. |
| `--jinja` | Use Qwen's chat template, so Modwen can switch "thinking" off. |
| `--cache-type-k/v q8_0` | Halve the memory used by the context. |

### A3. Test it

macOS / Linux:
```bash
curl http://localhost:8080/v1/models
curl http://localhost:8080/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"qwen3-8b","temperature":0.3,"chat_template_kwargs":{"enable_thinking":false},
       "messages":[{"role":"user","content":"Summarize in English and Arabic: اتفقنا إن الـ release هيكون يوم الخميس، وأحمد هيعمل الـ QA قبلها."}]}'
```
Windows PowerShell:
```powershell
Invoke-RestMethod http://localhost:8080/v1/models
$body = @{ model="qwen3-8b"; temperature=0.3; chat_template_kwargs=@{enable_thinking=$false};
  messages=@(@{role="user"; content="Summarize in English and Arabic: اتفقنا إن الـ release يوم الخميس"}) } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri http://localhost:8080/v1/chat/completions -Method Post `
  -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
```
A reply with `choices[0].message.content` means the server works.

### A4. Point Modwen at it

The defaults already match the Mac command above: profile `local_qwen`, `http://localhost:8080/v1`, model `qwen3-8b`, context 16384. On Windows with a different `--alias` or `-c`, create a config:

```bash
uv run modwen init-config          # writes config.yaml to the OS config folder and prints the path
```
Then edit `llm.profiles.local_qwen.model` and `context_window` to match your command. Confirm with:
```bash
uv run modwen llm-test             # sends a short Arabic/English prompt; should print "ok": true
uv run modwen doctor               # shows "local_qwen ... is online"
```
In the app, **Settings → AI model → Test** does the same check.

---

## Option B: Ollama

Ollama is easier to install, and it unloads the model when idle, which helps a 16 GB Mac. Its default context is small, though, and it **cuts long prompts without warning**, so always set the context length.

### B1. Install and pull
```bash
brew install ollama                      # macOS (or the app from ollama.com)
winget install Ollama.Ollama             # Windows
ollama pull qwen3:8b                     # or qwen3:14b, qwen3:30b, qwen3:4b
```

### B2. Serve with a large context

You have two choices.

**(a) Keep Ollama's default port 11434 and use Modwen's `ollama_qwen` profile.** This is simplest.

macOS (quit the Ollama menu-bar app first):
```bash
OLLAMA_CONTEXT_LENGTH=16384 ollama serve
```
Windows: quit Ollama from the tray, then
```powershell
setx OLLAMA_CONTEXT_LENGTH "16384"
```
and start Ollama again from the Start menu.

Then in `config.yaml`:
```yaml
llm:
  active: ollama_qwen
```

**(b) Run Ollama on 8080 instead.**
```bash
OLLAMA_HOST=127.0.0.1:8080 OLLAMA_CONTEXT_LENGTH=16384 ollama serve        # macOS/Linux
```
```powershell
setx OLLAMA_HOST "127.0.0.1:8080"; setx OLLAMA_CONTEXT_LENGTH "16384"      # Windows, then restart Ollama
```
Then set `llm.profiles.local_qwen.model: qwen3:8b`, and also set `no_think_suffix: true` on that profile.

Test with `uv run modwen llm-test --profile ollama_qwen`.

---

## Common problems

| Problem | Fix |
|---|---|
| `address already in use` on 8080 | Find the process: `lsof -i :8080` (macOS) or `netstat -ano \| findstr :8080` (Windows). Stop it, or use `--port 8081` and change `base_url`. |
| Out of memory, or the Mac gets slow | Use a smaller quant or model (8B → 4B-Instruct-2507), lower `-c` to 8192, keep the `--cache-type-k/v q8_0` flags, close big apps. Or use Ollama, which unloads after 5 minutes. |
| Very slow (under 5 tokens/s) | Check the log shows layers offloaded to the GPU (`-ngl 99`), and plug the laptop in. On CPU-only Windows, use 30B-A3B rather than a dense 14B. |
| `Notes failed ... Connection error` in Modwen | The server isn't running, or it's on another port. The transcript is safe: start the server, then press **Retry notes** on the meeting. |
| Notes miss the end of a long meeting | The context is too small. Modwen already splits 2-hour meetings into parts; make sure `context_window` equals `-c` / `OLLAMA_CONTEXT_LENGTH`. |
| Long "thinking" text, or slow replies with Qwen3-8B/14B | Keep `--jinja`. The profile sends `enable_thinking: false` (llama.cpp) or `/no_think` (Ollama). |
| Poor Arabic summary | Use 8B or larger (not 4B or 1.7B), keep the English summary default, or switch to Qwen3-14B / 30B-A3B if memory allows. Modwen retries automatically if Chinese characters or the wrong language appear. |
| Arabic shows as `????` in PowerShell tests | Send the body as UTF-8 bytes, as in the example above. |

## Starting it every day

The project includes ready scripts with the commands above:
- macOS: `./scripts/start-llm.sh`. The default is Qwen3-8B (Tier D, Apple Silicon 16 GB). Override with the `MODEL`, `ALIAS` and `CTX` environment variables.
- Windows: edit the values at the top of `scripts\start-llm.ps1`, then run `powershell -ExecutionPolicy Bypass -File scripts\start-llm.ps1`.

If you forget to start it, nothing is lost:
- The recording and transcript are saved.
- The notes step fails with a "Notes failed" notification.
- Start the server and press **Retry notes** on the meeting.

Auto-start at login (launchd / Task Scheduler) is a Phase 3 item.
