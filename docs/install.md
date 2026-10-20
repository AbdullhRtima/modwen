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
