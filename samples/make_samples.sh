#!/usr/bin/env bash
# macOS: generate a short bilingual (English + Arabic) test meeting with the built-in voices.
# Output: samples/generated/bilingual_meeting.wav (system channel) and mic_track.wav ("You").
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p generated && cd generated
say -v Samantha -o en1.aiff "Good morning everyone. Let's review the release plan for version two point three."
say -v Majed    -o ar1.aiff "تمام، القائمة جاهزة، فاضل بس موافقة فريق الأمن."
say -v Samantha -o en2.aiff "So the release is on Thursday. Ahmed, can you finish the QA testing before Wednesday?"
say -v Majed    -o ar2.aiff "أيوه، أنا هخلص اختبارات الجودة قبل يوم الأربعاء إن شاء الله."
say -v Samantha -o en3.aiff "Great. Sara will send the security sign off request tomorrow. Who will update the client? Let's decide that next time."
say -v Daniel   -o me.aiff  "Thanks everyone. I will update the client myself on Sunday."
cd ../..
uv run python - <<'EOF'
import numpy as np, soundfile as sf
from modwen.audio.chunks import load_audio_file
gap = np.zeros(int(0.9 * 16000), np.float32)
parts = []
for n in ["en1", "ar1", "en2", "ar2", "en3"]:
    parts += [load_audio_file(f"samples/generated/{n}.aiff"), gap]
system = np.concatenate(parts)
sf.write("samples/generated/bilingual_meeting.wav", system, 16000)
me = load_audio_file("samples/generated/me.aiff")
mic = np.concatenate([np.zeros(int(34.5 * 16000), np.float32), me, np.zeros(16000, np.float32)])
sf.write("samples/generated/mic_track.wav", mic, 16000)
print("wrote samples/generated/bilingual_meeting.wav and mic_track.wav")
EOF
