# Testing guide: macOS and Windows

Date: 2026-10-20. Build: Modwen 0.4.0 (new UI). Earlier levels are unchanged; the app checklist (A6) covers the new screens.

Go through the levels in order. Each level needs only what the level before it set up:

| Level | What it proves | Needs |
|---|---|---|
| **L0** Automated tests | Core logic: language rule, deadlines, encryption, map-reduce, API security | Nothing |
| **L1** Transcribe a file | Whisper on your machine, Arabic + English | Whisper model |
| **L2** Notes from a file | The whole pipeline with the local LLM | L1 + local Qwen ([local-llm-setup.md](local-llm-setup.md)) |
| **L3** Simulated live meeting | Recorder, live captions, notification | L2 |
| **L4** The app UI | Every screen, English and Arabic, light and dark: recording, meeting page, speakers, People, Settings | L2 |
| **L5** Real meetings | Mic + system audio: Google Meet and the Teams desktop app | L4 + permissions |
| **L6** Long meeting and recovery | 2-hour handling, crash and retry behaviour | L5 |

Levels L0–L4 have been verified on macOS (Apple Silicon). **Windows has not been tested yet.** L5 needs you, because macOS asks for permission on screen.

---

## Part A: macOS

### A1. One-time setup (about 15 minutes, plus model downloads)

```bash
xcode-select --install              # Command Line Tools (Swift compiler); skip if already installed
brew install uv node                # if you don't have them (you already have uv and node)
cd modwen

uv sync --extra stt --extra mac --extra desktop     # Python 3.12 environment in .venv
(cd ui && npm install && npm run build)             # builds the web UI into ui/dist
bash modwen/native/macos/build.sh                 # builds the system-audio helper
uv run modwen doctor                              # shows your tier, e.g. "Tier D: Apple M2 Pro, 16 GB"
```

Download the speech models. For Tier D these are MLX `large-v3` (about 3 GB) and `large-v3-turbo` (about 1.6 GB):
```bash
uv run modwen models download
```
For a quicker first test, use only the small model (about 460 MB) and point the config at it:
```bash
uv run modwen models download --model small
cp config.example.yaml config.yaml
# then set  stt.final.model: small  and  stt.live.model: small  in config.yaml
```

### A2. L0: automated tests (1 minute)
```bash
uv run pytest
```
Expect `112 passed` or more. No network or models are needed.

### A3. L1: transcribe a file (no AI model needed yet)
```bash
bash samples/make_samples.sh        # 33 s bilingual sample made with the built-in macOS voices
uv run modwen process samples/generated/bilingual_meeting.wav --title "File test"
```
**Expect:**
- A transcript with English lines tagged `(en)` and Arabic lines tagged `(ar)`.
- Then `FAILED at notes: ... Connection error`. That's correct when no LLM is running. The transcript is saved.

You can also try your own recording (WAV, MP3, FLAC, OGG, AIFF):
```bash
uv run modwen process ~/Downloads/my-meeting.m4a     # this command can't read .m4a: export as WAV or MP3 first (the app's Import a recording can)
uv run modwen process my-meeting.wav --language ar   # force Arabic only (or en)
```

### A4. L2: notes from a file (with the local LLM)
1. Start Qwen in a second terminal: `./scripts/start-llm.sh` (see [local-llm-setup.md](local-llm-setup.md)).
2. Check the connection with `uv run modwen llm-test`. Expect `"ok": true` and a short reply in English and Arabic.
3. Run the pipeline:
   ```bash
   uv run modwen process samples/generated/bilingual_meeting.wav --title "Notes test" \
          --summary-language both --out notes-test.md
   ```

**Expect:**
- A Markdown report with a summary in English and Arabic, decisions, an action-items table, key points and open questions.
- In the action items, "Ahmed / QA testing" is due on the coming Wednesday.
- Times shown in the report, like "(at 00:00:25)", point back to the transcript.

### A5. L3: simulated live meeting
```bash
uv run modwen simulate --system samples/generated/bilingual_meeting.wav \
                         --mic samples/generated/mic_track.wav --speed 1
```
**Expect:**
- `LIVE [..] Others: ...` lines appear a few seconds behind the audio, then `LIVE [..] You: Thanks everyone...`.
- After the audio ends, the processing steps run, then `EVENT notes_ready` and the notes.
- "You" owns "update the client".

### A6. L4: the app
```bash
uv run modwen serve
```
- A tray icon appears in the menu bar, and the browser opens `http://127.0.0.1:8765`.
- If you open the address by hand without the `?token=` link, you'll see "needs the session link". Use the link printed in the terminal.

Checklist (do it once in English, then switch to Arabic in **Settings → General → Interface language** and skim
every screen again; also try **Dark**):
- [ ] **Sidebar:** meetings grouped Today / Yesterday / This week / Older; search filters by title. A new meeting
  shows bold with a dot until you open it.
- [ ] **Import a recording** (Home) opens the meeting page with progress steps, then the notes.
  - An MP4, MOV or M4A file with sound imports the same way. A video with no sound says it has no audio; a text
    file renamed `.mp4` says it isn't supported. Nothing is added to the sidebar for either.
- [ ] **Import didn't finish** (the failed-import entry): start a long video import, stop `modwen serve` with
  Ctrl+C before it finishes, and start it again. Home shows the file under *Needs your attention* with
  **Import didn't finish** and **Import again**.
  - **Import again** with a good file opens the new meeting; the entry is gone from Home and the sidebar.
  - **Import again** with a refused file (a text file renamed `.mp4`) shows the error; the entry stays.
  - Open the entry: **Delete this entry** asks first; **Cancel** puts focus back on **Delete this entry**;
    **Delete** removes it and opens Home.
  - Repeat in Arabic (لم يكتمل الاستيراد، تحتاج انتباهك، استورد من جديد، احذف هذا العنصر، إلغاء، حذف) and in a
    window under 560 px wide.
- [ ] **Meeting page:**
  - Clicking a timestamp chip next to a note highlights the transcript line and plays from there.
  - Search the transcript; matches are highlighted.
  - Arabic lines read right-to-left; mixed lines keep English words in order.
  - Summary language **EN / AR / Both**: the first switch writes the other language (a few seconds), then it's instant.
- [ ] **Edit in place:** change an action item's task, owner or date; edit the summary; double-click a transcript
  line and fix a word. Reload: everything is kept. Edited lines show a pencil icon.
- [ ] **Copy → Copy as Markdown** pastes into a note; **Export .md file** downloads a file.
- [ ] **Speakers panel** (the people icon next to the speaker chips):
  - Type a name for a speaker and **Save**. The chips, transcript and notes use it.
  - "Same person as…" merges two speakers.
  - **Re-detect speakers** with a people count re-runs detection and keeps your names.
  - Click a speaker name on one transcript line to move just that line to someone else.
- [ ] **People page:**
  - The person you named is there. **Edit** opens a dialog; renaming updates every meeting.
  - **Delete** and **Delete voice** ask first and say what will happen. Esc or Cancel changes nothing.
- [ ] **Voices** (with consent): tick "Remember this voice" when naming someone. In the next meeting with them they
  are named automatically ("recognised"). **Confirm** / **Not <name>** in the Speakers panel.
- [ ] **Delete a meeting** (bottom of the meeting page): it disappears from the sidebar at once.
- [ ] **Failed notes:** stop the AI model, import a file. The meeting ends as Failed with the transcript kept.
  Start the model and press **Retry notes**; it ends as Notes ready.
- [ ] **Settings:**
  - Audio → **Test audio (3 seconds)**: both meters move while you talk and play a video.
  - AI model → **Test** shows the reply and the time.
  - About shows your tier and the llama-server command.
- [ ] **Phone:** Settings → General → Other devices, open the link on your phone. The layout fits the screen
  (menu button top-left).

### A7. L5: real meetings on macOS

Permissions: macOS asks for them the first time, and it attributes them to the app you started Modwen from (Terminal, iTerm or VS Code). Allow:
- **Microphone**.
- **System Audio Recording**. It's under Privacy & Security → *Screen & System Audio Recording* on macOS 15; on macOS 14 the prompt may refer to audio capture.

If system audio stays silent, open System Settings → Privacy & Security, enable your terminal app, then restart it.

**Test 1: system audio without a meeting (2 minutes)**
1. Press **Record a meeting**. Choose **Online call**, keep both sources ticked, then **Start recording**.
2. Play an Arabic or English video in Chrome.
3. Expect:
   - The **Call** meter in the recording pill moves; open **Live captions** to see lines appear.
   - Speaking makes the **You** meter move.
   - The pill stays on every page; **Pop out** opens it as a small window.
4. Press **Stop**. The processing screen appears, then a **"Notes ready"** notification arrives. Click it to open the notes.

**Test 2: Google Meet (in Chrome)**
1. Start a meeting at meet.google.com and join from your phone as a second participant (headphones on the Mac avoid echo).
2. Start Modwen, talk on the phone and on the Mac for 3–5 minutes, mixing Arabic and English, then Stop.
3. Expect:
   - "You" for the Mac mic; the phone speaker becomes "Person 1" (name them in the Speakers panel).

**Test 3: Microsoft Teams desktop app**
1. In Teams, go to Settings → Devices → **Make a test call**. The echo bot talks and replays you.
2. Run Modwen during the call.
3. Expect the bot's voice as a separate person from you.

Things to watch:

| Watch | Expected |
|---|---|
| Bluetooth headset as the mic | A warning on the Start page and in the recording pill. Use the built-in or a wired mic. |
| In the room (one mic, several people) | Choose **In the room**; speakers become Person 1, 2, 3… and saved voices are recognised |
| Meeting muted for 60 s | An amber "No system audio for 60 s" warning |
| Speakers instead of headphones | Remote voices leak into your mic; Modwen removes the echo duplicates |

### A8. L6: long meeting and recovery
- **Long meeting:** play a 2-hour Arabic/English podcast while recording, or replay a long file with `uv run modwen simulate --system long.wav --speed 4`.
  - The processing steps show transcription, speakers, then "Writing notes … part N".
  - Measured on an M1 16 GB (Tier D, large-v3, 2 h 8 min, 4 speakers): see the deep-test notes in the release.
- **Crash during recording:** while recording, quit the terminal (Ctrl+C twice) and restart `uv run modwen serve`. The meeting is picked up from its saved 30-second chunks and processed automatically (at most the last 30 seconds are lost). Closed in the first 30 seconds, it shows "Modwen closed before any audio was saved".
- **Disk space:** the recording stops itself, with a warning, if free space drops under 2 GB.

### A9. Verify MP4, MOV and M4A import on a Mac
MP4, MOV and M4A import has not been verified on macOS yet (see
[system-design.md §10.1](system-design.md#101-imported-recordings-mp4-mov-m4a)). One script checks it. In Terminal,
from a checkout of the branch under test:

```bash
bash scripts/verify-macos-import.sh
```

- **Time:** plan on an hour or more, mostly unattended, plus package downloads. Keep the Mac awake and plugged in,
  and close other heavy apps.
- **What it does,** for Python 3.11 and then 3.12: records the macOS version, chip, uv, Python, PyAV and FFmpeg
  versions; sets up with `uv sync --frozen` and the same extras as `Modwen.command`; runs the import test files with
  pytest; then runs a real engine on `127.0.0.1` with fake speech-to-text and a fake local AI, and checks generated
  hostile files, a real memory-limit breach (its peak footprint and overshoot), the CPU limit, frozen and spinning
  decoders, every core busy, an engine kill and restart, several imports at once, and valid fragmented and
  edit-list files.
- **If setup fails,** for example with the macOS 13 setup problem in [install.md](install.md#troubleshooting), that
  failure is recorded as the result for that Python. Nothing is worked around; send the report anyway.
- **What it never does:** download a model, use a recording, or use the network for anything but dependency setup.
  Everything it writes stays in `.tmp/macos-verify/` in the checkout (if `uv` is missing, the desktop app's pinned
  uv goes to `.cache/desktop-uv/`). Your own `.venv` isn't touched.
- **What to send back:** the one file `.tmp/macos-verify/report.txt`. Read it first: it holds versions and PASS /
  FAIL / NOT RUN lines only, with paths shortened to `<checkout>` and `~`, and no user name, host name, serial number
  or recording. Logs and generated media stay on the Mac.
- **Clean up afterwards:** `rm -rf .tmp/macos-verify`
- Four tests only run on Windows and are skipped on a Mac; the report lists each as NOT RUN with the engine check that
  replaces it. `limits.descendants` is always NOT RUN on macOS, because Modwen sets no process-count limit there.
- An Apple Silicon run is the one that matters most, ideally on macOS 13 and on 14 or later. The report is evidence
  for the maintainers, not an approval.

---

## Part B: Windows 10/11

> Not run yet. The code paths are written for Windows (WASAPI loopback, toast notifications, CUDA DLL loading), but nothing has been executed there. Please report anything that fails at the step where it fails.

### B1. One-time setup

PowerShell:
```powershell
winget install --id Git.Git -e
winget install --id OpenJS.NodeJS.LTS -e
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# open a NEW PowerShell window so uv and node are on PATH
```
Copy the `modwen` folder to the PC (or `git clone` it), then:
```powershell
cd $HOME\Desktop\modwen
uv python install 3.12
uv sync --extra stt --extra desktop --extra windows
cd ui; npm install; npm run build; cd ..
uv run modwen doctor
```
**NVIDIA GPU only:** Whisper needs cuBLAS and cuDNN. Modwen finds these pip packages automatically:
```powershell
uv pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
uv run modwen doctor          # "CUDA: True" on the Whisper line
```
Then download the models with `uv run modwen models download`. Tier A/B fetch large-v3 and turbo; CPU tiers fetch turbo and small.

Windows privacy settings:
- Settings → Privacy & security → **Microphone** → turn on "Let desktop apps access your microphone".
- Turn off **Focus / Do not disturb**, or notifications won't show.

### B2. L0–L4 on Windows
Same commands as Part A, run from PowerShell:
```powershell
uv run pytest
powershell -ExecutionPolicy Bypass -File samples\make_samples.ps1     # English sample (Windows has no Arabic voice)
uv run modwen process samples\generated\english_meeting.wav
uv run modwen process C:\path\to\arabic-recording.wav                 # use any Arabic recording you have
powershell -ExecutionPolicy Bypass -File scripts\start-llm.ps1          # in a second window (edit tier values first)
uv run modwen llm-test
uv run modwen serve
```
Then follow the A6 checklist.

### B3. L5 on Windows
- **System audio** uses WASAPI loopback on the **default output device**. Whatever plays there is captured: Chrome with Meet, or Teams.
  - If you switch headsets mid-call, stop and start the recording.
  - In `uv run modwen doctor`, the System audio line must say "WASAPI loopback available".
- **Test 1:** play a YouTube video and record 2 minutes. The Call meter moves and live lines appear (Live captions).
- **Test 2:** Google Meet in Chrome, with your phone as the second participant.
- **Test 3:** Teams desktop → Settings → Devices → **Make a test call**.
- **Notification:** a Windows toast "Notes ready: …". Clicking it opens the review screen.

---

## Results checklist (fill this in)

| # | Test | macOS | Windows | Notes |
|---|---|---|---|---|
| 1 | `uv run pytest` all passed | | | |
| 2 | `doctor` tier correct | | | |
| 3 | L1 file transcript: Arabic + English lines | | | |
| 4 | L2 notes from file (`llm-test` ok) | | | |
| 5 | L3 simulate: live lines + notes_ready | | | |
| 6 | L4 UI checklist (English) | | | |
| 6b | L4 UI checklist (Arabic, dark) | | | |
| 6c | Speakers panel, People, voices recognised in a 2nd meeting | | | |
| 7 | L5 system audio (YouTube) | | | |
| 8 | L5 Google Meet | | | |
| 9 | L5 Teams desktop test call | | | |
| 10 | "Notes ready" notification appears and opens the meeting | | | |
| 11 | L6 2-hour meeting: notes complete, time taken | | | |
| 12 | L6 crash recovery | | | |
| 13 | A9 `verify-macos-import.sh` report sent (macOS only) | | n/a | |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Symbol not found: _X509_STORE_get1_objects ... MATLAB.../libcrypto.3.dylib` | Your shell's `DYLD_LIBRARY_PATH` points at MATLAB. Modwen now ignores it automatically. For other tools, run `unset DYLD_LIBRARY_PATH`, or remove the MATLAB line from `~/.zshrc` |
| `Missing or invalid session token` / "needs the session link" | Open the `?token=` link printed by `modwen serve`, or use the tray's **Open Modwen** |
| Call meter flat on macOS | System Audio Recording permission for your terminal app; rebuild the helper with `bash modwen/native/macos/build.sh`. Workaround: install BlackHole 2ch, then set `capture.system_backend: device` and `capture.system_device: "BlackHole 2ch"` |
| Call meter flat on Windows | Check the meeting's audio plays on the **default** output device; run `uv sync --extra windows` |
| "Live transcript paused" | The machine is busy; recording is unaffected. Lower the live model (`stt.live.model: small`) or disable live captions (`stt.live.provider: none`) |
| Very slow transcription on the Mac | Install the mac extra: `uv sync --extra stt --extra mac --extra desktop`. `doctor` must show `backend=mlx` |
| Everyone is one person, or one person is split in two | Speakers panel → **Re-detect speakers** with the number of people, or "Same person as…" to merge |
| Notes failed | Start the LLM, then **Retry notes**. Error details are on the meeting page. |

## Where your data lives / reset
- **macOS:** `~/Library/Application Support/Modwen/`
- **Windows:** `%LOCALAPPDATA%\Modwen\`

Each contains `modwen.db` (meetings, transcripts, notes), `audio/` (encrypted chunks) and `models/`. The encryption master key is in the macOS Keychain or Windows Credential Manager, under the service "modwen".

To start fresh, quit Modwen and delete that folder.
