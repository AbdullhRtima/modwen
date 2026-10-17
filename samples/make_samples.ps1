# Windows: generate a short English test recording with the built-in speech engine (System.Speech).
# Windows rarely ships an Arabic voice; for Arabic, record yourself (Voice Recorder app) or use any
# Arabic audio file (WAV/MP3) with `uv run modwen process <file>`.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force -Path generated | Out-Null
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SetOutputToWaveFile("$PSScriptRoot\generated\english_meeting.wav")
$s.Speak("Good morning everyone. Let's review the release plan for version two point three.")
$s.Speak("So the release is on Thursday. Ahmed, can you finish the QA testing before Wednesday?")
$s.Speak("Great. Sara will send the security sign off request tomorrow. Who will update the client? Let's decide that next time.")
$s.Dispose()
Write-Host "wrote samples\generated\english_meeting.wav"
