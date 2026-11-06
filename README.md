# Alpine Recording Station — Rough Cut

An AI-powered Voiceover Rough Cut Audio Editor that takes raw studio voiceover recordings, isolates studio talkback channels, removes bad or duplicate takes to select the best performance, eliminates breaths and mouth noises, enforces consistent natural pacing with English grammatical pause rules, and exports both master audio files and non-destructive **Adobe Audition Multitrack Sessions (`.sesx`)**.

---

## Key Capabilities

### 1. Dual Recording Modes (`Normal Mode` & `Studio Mode`)
- **Normal Mode (Mono or Stereo Mic)**:
  - Processes standard mono (`1.0`) or stereo (`2.0`) microphone recordings.
- **Studio Mode (Left Channel: Voiceover | Right Channel: Tone-Bounded Talkback)**:
  - **Left Channel (`Ch 0`)**: Dedicated to the voiceover microphone.
  - **Right Channel (`Ch 1`)**: Dedicated to the studio talkback track containing producer/director notes bounded by an **audio tone at the start** and an **audio tone at the end** of each comment.
  - **Strict Zero-Bleed Isolation Guarantee**:
    - Narrowband STFT tone detection automatically locates all start/end talkback tones (`250 Hz – 4000 Hz`) and pairs them into talkback intervals.
    - At **no time** is any audio from the Right (talkback) channel ever included in the output audio or referenced in exported multitrack sessions.
    - In addition, the Left (voiceover) channel is automatically muted/zeroed (with smooth 8ms cosine ramps) during active talkback intervals so headphone bleed or room noise during talkback notes can never leak into the edit.
  - **AI Talkback Directive Parsing**:
    - The spoken note between the start and end tones on the talkback track is transcribed via Speech-to-Text and analyzed for director instructions:
      - **Retake cues** (*"redo that"*, *"one more time"*, *"from the top"*, *"stumbled"*) automatically mark the preceding take as superseded.
      - **Specific take preferences** (*"keep take 1"*, *"the second take was best"*, *"use the first one"*) boost the specified take in that phrase group.

### 2. Speech-to-Text (STT) & Breath / Mouth-Noise Removal
- Uses **local/offline `faster-whisper`** with word-level timestamps (`word_timestamps=True`) and VAD filtering, plus an optional Reference Script input and optional OpenAI GPT-4o-mini verification.
- Scans inter-word gaps and phrase boundaries using RMS amplitude, Zero-Crossing Rate (ZCR), and high-frequency friction ratios (`> 550 Hz` vs `< 350 Hz`) to detect and excise **inhalation/exhalation breaths** and **mouth clicks** without clipping word tails.
- Applies cosine micro-crossfades (`8 ms` default) and optional natural studio **room-tone padding** extracted from the recording's quietest non-talkback floor so edits never click or suffer from "dead-air" gating dropouts before mastering.

### 3. Consistent Prose Pacing & English Grammatical Pause Engine
- Analyzes **uncut continuous prose sections** within the selected takes to measure the speaker's natural inter-word gap baseline ($\Delta_{\text{word}}$) and local speaking tempo (WPM).
- Applies **English grammatical pause rules** across cuts and breath removals so the performance retains its natural rhythm and feel:
  - **Mid-clause word transition (no punctuation)**: Matched to the speaker's uncut inter-word cadence ($\Delta_{\text{word}}$, typically `80–120 ms`).
  - **Comma (`,`)**: Scaled by the speaker's cadence ($\Delta_{\text{word}} \times \text{comma\_multiplier}$, default `3.2x`, blended with uncut comma pauses in the recording).
  - **Semicolon (`;`), Colon (`:`), Em-Dash (`—`)**: `1.24x` the comma pause duration.
  - **Sentence Terminals (`.`, `!`, `?`, `...`)**: Scaled by the speaker's cadence ($\Delta_{\text{word}} \times \text{period\_multiplier}$, default `6.8x`).
  - **Local Prose Tempo Adaptation**: Rolling window tempo calculation ensures slower dramatic passages and faster energetic passages maintain their local pacing.

### 4. Non-Destructive Edit Preservation & Adobe Audition Multitrack (`.sesx`) Export
- Once processed, the edit session is preserved (`workspace_data/sessions/<session_id>.ars.json`) with all word timestamps, candidate takes, breath events, and timeline edit points intact.
- **Instant Re-Export Without Re-Processing**:
  - Swap any take selection, adjust comma/period pause multipliers, or change output formats and re-export in under a second without re-running Speech-to-Text.
- **Adobe Audition Multitrack Export (`.sesx` Bundle)**:
  - Generates a native Adobe Audition `<sesx version="1.9">` XML multitrack session containing:
    - **Track 1 (`VO Rough Cut - Selected Takes`)**: Unmuted primary track with non-destructive `<audioClip>` entries, exact sample frame `sourceInPoint`/`sourceOutPoint` and `startPoint`/`endPoint` positions, and cosine `<fadeIn>`/`<fadeOut>` envelopes.
    - **Track 2 (`VO Rejected & Alternate Takes - Muted`)**: Muted reference track containing rejected takes so an engineer in Adobe Audition can audition or swap takes directly on the timeline.
    - **Track 3 (`Excised Breaths & Mouth Noises - Muted`)**: Muted track containing removed breaths in case a specific breath is desired for dramatic effect.
    - **Session Markers (`<markers>`)**: Timeline cue markers for every edit point, grammatical pause duration, and transcribed Studio Mode talkback comment.
    - **Companion Clean VO Source (`_Clean_VO_Source.wav`)**, **CMX 3600 EDL (`.edl`)**, and **Audition Cue Markers CSV (`.csv`)**.
- **Flexible Master Audio Format & Channel Mapping**:
  - **Default (`Match Input`)**: Preserves input container format, sample rate, bit depth, and channel count with zero conversion required (while guaranteeing zero talkback audio in Studio Mode).
  - **Custom Conversion Options**: Export to `WAV`, `FLAC`, `MP3`, `OGG`, or `M4A/AAC`; sample rates `44.1 kHz`, `48 kHz`, or `96 kHz`; bit depths `16-bit PCM`, `24-bit PCM`, or `32-bit Float`; and channel maps (`Match Input`, `Mono 1.0`, `Stereo Dual-Mono L+R`, `Stereo Left Only`, `Stereo Right Only`).

---

## Zero-Intervention Installation

### Windows (PowerShell or Command Prompt)
Run either the PowerShell installer or batch script from the repository root:
```powershell
.\install.ps1
```
or double-click / run:
```cmd
install.bat
```
*(Optional: pass `--preload-model` to pre-download the `faster-whisper` `base.en` model weights during setup: `.\install.ps1 --preload-model`)*

### macOS & Linux
```bash
chmod +x install.sh run.sh
./install.sh
```

### Cross-Platform Python Bootstrapper
You can also invoke the cross-platform installer directly with Python 3.10+:
```bash
python setup_env.py
```
`setup_env.py` automatically creates the `.venv` virtual environment, installs all dependencies from `requirements.txt`, checks/configures `ffmpeg`, and initializes workspace directories.

---

## Launching the Application

### Windows
```powershell
.\run.ps1
```
or double-click `run.bat`.

### macOS / Linux
```bash
./run.sh
```
The server starts at **`http://127.0.0.1:8000`** and automatically opens in your default browser.

---

## How to Use the Web UI

1. **Select Input Recording or Load a Built-in Demo**:
   - Click **"Load Studio Mode Demo"** or **"Load Normal Mode Demo"** in the top-right header to immediately test a synthesized multi-take recording (complete with Studio Mode 1 kHz talkback tones, director notes, false starts, breaths, and duplicate takes).
   - Or choose your own audio file (`WAV`, `FLAC`, `MP3`, `M4A`, `OGG`) and set your desired **Output Base Filename**.
2. **Choose Recording Channel Mode**:
   - **Normal Mode**: For single-channel mono or standard stereo voiceover recordings.
   - **Studio Mode**: For stereo recordings where **Left = Voiceover** and **Right = Talkback** (bounded by start/end audio tones).
3. **Configure STT, Breath Removal & Grammatical Pacing**:
   - Select the local Whisper model size (`tiny.en`, `base.en`, `small.en`, `medium.en`, `large-v3`).
   - Toggle breath & mouth-noise removal and natural studio room-tone padding.
   - Set **Comma Pause Multiplier** (default `3.2x`) and **Period Pause Multiplier** (default `6.8x`), and optionally paste a **Reference Script**.
4. **Analyze & Monitor Progress**:
   - Click **"Analyze & Assemble Rough Cut"** and watch the real-time progress bar track each pipeline stage.
5. **Inspect, Audition, Swap Takes & Export Instantly**:
   - View the dual-channel waveform timeline highlighting **Kept Takes (Green)**, **Rejected Takes (Orange)**, **Excised Breaths (Purple)**, and **Excluded Studio Talkback Intervals (Red)**.
   - Click **"Audition Take"** on any take to hear its isolated voiceover audio, or click **"Select This Take"** to non-destructively swap takes and recalculate grammatical pacing on the fly.
   - Click **"Export Master Audio File"**, **"Export Adobe Audition Multitrack (.sesx Bundle)"**, **"Download .sesx XML Only"**, or **"Save Preserved Edit Session (.ars.json)"** at any time without re-running the AI analysis.

---

## Running Automated Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```
(On macOS/Linux: `./.venv/bin/python -m pytest -v`)
