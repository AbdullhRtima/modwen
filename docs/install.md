# Install and run Modwen

Modwen runs on **macOS** (Apple Silicon recommended, macOS 14.2+) and **Windows 10/11**. You need about 10–30 GB of free disk space for the speech and AI models, and an internet connection for the first setup only.

## 1. Get it

Download `modwen-<version>.zip` from the project's Releases page and unzip it anywhere, for example in Documents.

## 2. Start it

| macOS | Windows |
|---|---|
| Double-click **`Modwen.command`**. The first time, macOS may block it: open **System Settings → Privacy & Security** and click **Open Anyway** (on macOS 14 and older, right-click → Open also works). | Double-click **`Modwen.bat`**. If SmartScreen appears: **More info → Run anyway**. |

The first start takes a few minutes. The launcher installs `uv`, a small tool that gives Modwen its own private Python, and sets up Modwen's environment. Nothing else on your system changes. Later starts take seconds.

Your browser opens Modwen, and a Modwen icon appears in the menu bar (macOS) or the system tray (Windows). **Keep the launcher window open while you use Modwen; close it to quit.**

## 3. First-run setup (about 10 minutes, mostly downloads)

The setup wizard walks you through four steps:

1. **Permissions.** Press *Test audio*, talk, and play any video. Allow **Microphone** and (macOS) **System Audio Recording** when asked.
2. **Hardware check.** Modwen picks the right models for your computer (tiers A–F) and tells you how long a 2-hour meeting will take to process.
3. **AI model + downloads:**
   - **Speech models (Whisper):** press *Download* (1.6–4.6 GB).
   - **AI model for the notes.** Choose one:
     - **Download Qwen for me (recommended).** Modwen downloads llama.cpp and the right Qwen model (2.5–19 GB) into its own folder and starts and stops it automatically.
     - **Use my own local AI server.** Already running Ollama, LM Studio, llama.cpp or vLLM? Enter its URL (e.g. `http://localhost:11434/v1`) and press *Test and use*. Only `localhost` addresses are allowed.
4. **Test notification.** Make sure "Notes ready" notifications reach you.

You can change any of this later in **Settings**.

## 4. Use it

1. Join your meeting (Google Meet in the browser, the Teams desktop app, or an in-person meeting).
2. Click **● Start meeting** in Modwen or the tray, tick that participants were informed, and press **Start**.
3. Watch the live captions, or just keep the small recording pill open.
4. Press **Stop**. You'll get a **"Notes ready"** notification with the summary, decisions, action items and open questions.

**Tip:** use headphones in online meetings, so other people's voices don't leak into your microphone.

## 5. Import a recording

Press **Import a recording** on Home and choose a file. Modwen transcribes it and writes the notes, the same as a
meeting you recorded.

- **Audio files:** WAV, FLAC, MP3, OGG and AIFF.
- **Video and audio files:** MP4, MOV and M4A. Modwen takes the audio and ignores the video; no video is kept.
  It reads AAC, ALAC, MP3, Opus, FLAC, AC-3, E-AC-3 and common PCM audio. If a file has several audio tracks,
  Modwen uses the one the file marks as default, or else the first.
- **Limits:** up to 8 GB per file and 6 hours of audio. Copy-protected (DRM) files can't be imported.
- **Local only:** the file is read on your computer, with no internet needed, and kept encrypted while it's imported.

**If a file is refused,** Modwen says why (not supported, no audio, damaged, empty, too long or too large) and
imports nothing. Try another copy of the file, or export it as WAV or MP3.

**If Modwen stops during an import,** for example because the computer shut down, the next start removes the partial
import and shows an **Import didn't finish** entry under *Needs your attention*. Press **Import again** and choose
the same file, or open it and choose **Delete this entry**. Your original file is never changed.

**Damaged files.** Modwen refuses files it can tell are damaged or cut off, but MP4 has no checksums, so some damage
can't be detected: a damaged file can occasionally import as a shorter recording. Details:
[system-design.md §10.1](system-design.md#101-imported-recordings-mp4-mov-m4a).

**Security of video files.** MP4, MOV and M4A files are decoded by FFmpeg in a separate process with memory, CPU and
time limits. That contains crashes, hangs and runaway memory use caused by a damaged or hostile file. It is **not a
sandbox**: the process runs as your user account, with the same access to your files, your network and the running
Modwen app as Modwen itself. A file that exploits a security flaw in FFmpeg could read your recordings, transcripts
and Modwen's keys, including from the running Modwen app, and send them over the network. **Import files only from
sources you trust.** WAV, FLAC, MP3, OGG and AIFF are read inside Modwen itself, as before. FFmpeg's version is
fixed in each Modwen release, so FFmpeg security fixes reach you only through a Modwen update.

- **Windows:** the limits are enforced by a job object and have been tested.
- **macOS:** the limits are implemented but **not verified** on a Mac yet.
- Neither platform runs the decoder in an OS sandbox.

## Where your data lives

| | Location |
|---|---|
| macOS | `~/Library/Application Support/Modwen/` |
| Windows | `%LOCALAPPDATA%\Modwen\` |

It contains the database, encrypted recordings and downloaded models. Delete that folder to remove everything. Nothing is ever uploaded: meeting audio and transcripts stay on your computer.

## Troubleshooting

| Problem | Fix |
|---|---|
| macOS: "cannot be opened because the developer cannot be verified" | System Settings → Privacy & Security → **Open Anyway**, right after the blocked attempt (only needed once; on macOS 14 and older, right-click → Open also works) |
| The browser shows "needs the session link" | Use **Open Modwen** in the tray, or the link printed in the launcher window |
| The "Others" meter never moves | macOS: System Settings → Privacy & Security → *Screen & System Audio Recording* → allow the app that runs Modwen (Terminal). Windows: make sure the meeting plays on your default speakers or headset. |
| "AI model offline" | Settings → AI model → *Start* (managed), or start your own server and press *Test* |
| Notes failed | The transcript is saved. Fix the AI model, then press **Retry notes** on the meeting. |
| Port 8080 already in use | Another AI server is running. Use it via "Use my own local AI server", or stop it. |
| macOS 13: the first start fails while setting up, with an error that a package "only has wheels for `macosx_14_0_arm64`" | A known setup problem on Apple Silicon Macs running macOS 13, older than video import: some packages Modwen locks (such as [MLX](https://ml-explore.github.io/mlx/build/html/install.html), PyAV, ONNX Runtime and PyTorch) only publish Mac builds for macOS 14 and later. It is being investigated; there is no workaround in this release. |
