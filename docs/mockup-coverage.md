# Mockup coverage (v0.2)

This compares [mockups/index.html](mockups/index.html) with the app as built.

| Screen | Status | Notes |
|---|---|---|
| Tray / menu-bar menu | ✅ | Status header, Start meeting…, Start in-person meeting…, Notes ready (n) →, Open Modwen, AI model online/offline, Settings…, Quit. While recording: Stop (timer), Pause/Resume, Mark moment, Show live transcript, Local only. Icon colours for idle / recording / processing / notes ready. **Later (Tauri):** global hotkeys ⌘⇧B / ⌘⇧M and a timer next to the menu-bar icon. |
| Start dialog + consent | ✅ | Title, online/in-person, audio sources, language (Auto/EN/AR), local-only label, copy notice EN/AR, consent tick, Bluetooth warning, floating-pill option |
| Recording pill | ✅ | A small separate browser window with timer, You/Others meters, warnings, Mark, Pause, Stop and Expand. **Later (Tauri):** always on top. |
| Live transcript | ✅ | You/Others labels, auto-scroll, "live paused" banner, Mark moment |
| Processing | ✅ | Steps, transcription %, notes part N of M, ETA, Retry / Retry notes on failure |
| "Notes ready" notification | ✅ | Native notification on macOS and Windows; failure notification too |
| Meetings home | ✅ | Search, Import recording, "Needs attention" first, status chips, Retry |
| Meeting review | ✅ | Notes ↔ transcript evidence links, summary EN/AR/Both, inline edits, Speakers drawer (rename, merge, People suggestions), Regenerate (summary only / whole), Copy / Export .md, warnings bar, **audio player** (play, seek, plays from evidence), **transcript search**, delete. Not yet: per-speaker 5-second sample ▶. |
| Settings | ✅ | Tabs General, Audio (device, meeting audio, 3-second test), Transcription (language, models, live on/off, glossary, downloads), **AI model** (managed Qwen download/start/stop, profiles: Edit / Test / Use / Delete / **Add local profile**, generation settings), Notifications (test), Privacy, About (tier, own llama.cpp command, machine). |
| People & voices | 🟡 | Add/edit/delete people (name, Arabic name, email); names are suggested when naming speakers. **Voiceprint enrollment is not built**: it needs the optional diarization extra (a speaker-ID model). |
| First-run onboarding | ✅ | 4 steps: permissions + audio test, hardware tier, speech-model download + AI model (download Qwen **or** connect your own localhost server), test notification. Re-runnable from Settings → General. |
| Send / approval (email) | ⏳ | Phase 3, as planned |
