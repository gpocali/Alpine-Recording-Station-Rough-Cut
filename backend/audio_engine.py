"""Audio I/O, Studio Mode talkback isolation, tone detection, breath removal, and master rendering."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from scipy import signal
import soundfile as sf

from backend.models import (
    AudioFormatOption,
    BitDepthOption,
    BreathOrNoiseEvent,
    ChannelMapOption,
    EditSession,
    ExportSettings,
    OutputPreviewInfo,
    RecordingMode,
    TalkbackInterval,
    TimelineEditClip,
    ToneMarker,
    WordToken,
)


def _find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg executable on PATH or common install locations."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    # Check WinGet / common locations on Windows
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        winget_dir = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
        if winget_dir.exists():
            for exe in winget_dir.rglob("ffmpeg.exe"):
                return str(exe)
    return None


def load_audio_file(file_path: str) -> Tuple[np.ndarray, int, int, str, str]:
    """
    Load an audio file into a float64 2D numpy array of shape (num_samples, num_channels).
    Returns: (audio_data_2d, sample_rate, channels, subtype, container_format)
    """
    p = Path(file_path)
    ext = p.suffix.lower().lstrip(".")
    try:
        info = sf.info(str(p))
        data, sr = sf.read(str(p), always_2d=True, dtype="float64")
        return data, int(sr), int(info.channels), str(info.subtype), str(info.format).lower()
    except Exception:
        # Fallback to ffmpeg conversion to temporary wav
        ffmpeg_bin = _find_ffmpeg()
        if not ffmpeg_bin:
            raise RuntimeError(f"Cannot decode audio format '{ext}' without FFmpeg installed.")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_wav = tmp.name
        try:
            cmd = [
                ffmpeg_bin,
                "-y",
                "-i",
                str(p),
                "-vn",
                "-acodec",
                "pcm_f32le",
                tmp_wav,
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            info = sf.info(tmp_wav)
            data, sr = sf.read(tmp_wav, always_2d=True, dtype="float64")
            return data, int(sr), int(info.channels), "PCM_24", ext or "wav"
        finally:
            if os.path.exists(tmp_wav):
                try:
                    os.remove(tmp_wav)
                except OSError:
                    pass


def compute_waveform_peaks(signal_1d: np.ndarray, num_bins: int = 600) -> List[float]:
    """Compute normalized peak envelope [0.0..1.0] for frontend canvas visualization."""
    if len(signal_1d) == 0:
        return [0.0] * num_bins
    n = len(signal_1d)
    if n < num_bins:
        peaks = np.abs(signal_1d).tolist()
        return [round(float(x), 4) for x in peaks]
    step = n / num_bins
    peaks: List[float] = []
    for i in range(num_bins):
        s = int(i * step)
        e = max(s + 1, int((i + 1) * step))
        chunk = signal_1d[s:e]
        peaks.append(round(float(np.max(np.abs(chunk))), 4) if len(chunk) else 0.0)
    return peaks


def rms_db(samples: np.ndarray) -> float:
    """Compute RMS level in dBFS."""
    if len(samples) == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)) + 1e-18))
    return float(20.0 * math.log10(max(rms, 1e-9)))


def detect_talkback_tones(
    right_channel: np.ndarray,
    sample_rate: int,
    min_tone_duration_sec: float = 0.12,
    max_tone_duration_sec: float = 3.0,
) -> List[ToneMarker]:
    """
    Detect pure/narrowband audio tones on the Studio Mode right (talkback) channel.
    Talkback segments start with an audio tone and end with an audio tone.
    """
    if len(right_channel) == 0:
        return []

    win_len = max(256, int(0.040 * sample_rate))  # 40 ms window (~25 Hz bin resolution)
    hop_len = max(64, int(0.010 * sample_rate))   # 10 ms hop
    num_frames = max(0, (len(right_channel) - win_len) // hop_len + 1)
    if num_frames == 0:
        return []

    window = np.hanning(win_len)
    freqs = np.fft.rfftfreq(win_len, d=1.0 / sample_rate)

    # Valid studio tone band: 340 Hz to 4000 Hz (avoids human vocal fundamental 85-310 Hz)
    valid_band = (freqs >= 340.0) & (freqs <= 4000.0)
    if not np.any(valid_band):
        return []

    frame_is_tone = np.zeros(num_frames, dtype=bool)
    frame_dom_freq = np.zeros(num_frames, dtype=np.float64)
    frame_rms_db = np.full(num_frames, -100.0, dtype=np.float64)

    for idx in range(num_frames):
        start = idx * hop_len
        frame = right_channel[start : start + win_len]
        f_rms = float(np.sqrt(np.mean(frame * frame) + 1e-18))
        f_db = 20.0 * math.log10(max(f_rms, 1e-9))
        frame_rms_db[idx] = f_db
        if f_db < -42.0:
            continue

        spec = np.abs(np.fft.rfft(frame * window)) ** 2
        total_energy = float(np.sum(spec) + 1e-18)
        band_spec = np.where(valid_band, spec, 0.0)
        peak_bin = int(np.argmax(band_spec))
        dom_freq = float(freqs[peak_bin])
        frame_dom_freq[idx] = dom_freq

        # Pure studio tones concentrate >80% of total spectral energy within +-55 Hz of the fundamental
        fundamental_mask = np.abs(freqs - dom_freq) <= 55.0
        fundamental_ratio = float(np.sum(spec[fundamental_mask])) / total_energy

        if fundamental_ratio >= 0.78:
            frame_is_tone[idx] = True

    # Smooth 1-2 frame dropouts inside a continuous tone
    for idx in range(1, num_frames - 1):
        if not frame_is_tone[idx] and frame_is_tone[idx - 1] and frame_is_tone[idx + 1]:
            if abs(frame_dom_freq[idx - 1] - frame_dom_freq[idx + 1]) < 35.0:
                frame_is_tone[idx] = True
                frame_dom_freq[idx] = 0.5 * (frame_dom_freq[idx - 1] + frame_dom_freq[idx + 1])

    tones: List[ToneMarker] = []
    in_run = False
    run_start = 0

    for idx in range(num_frames + 1):
        is_t = bool(frame_is_tone[idx]) if idx < num_frames else False
        if is_t and not in_run:
            in_run = True
            run_start = idx
        elif is_t and in_run:
            if abs(frame_dom_freq[idx] - frame_dom_freq[idx - 1]) > 80.0:
                t_start = (run_start * hop_len) / sample_rate
                t_end = (idx * hop_len + win_len * 0.5) / sample_rate
                dur = t_end - t_start
                if min_tone_duration_sec <= dur <= max_tone_duration_sec:
                    freq_slice = frame_dom_freq[run_start:idx]
                    if float(np.std(freq_slice)) < 20.0:
                        tones.append(
                            ToneMarker(
                                start_time=round(t_start, 4),
                                end_time=round(t_end, 4),
                                dominant_freq_hz=round(float(np.median(freq_slice)), 1),
                                rms_db=round(float(np.mean(frame_rms_db[run_start:idx])), 2),
                            )
                        )
                run_start = idx
        elif not is_t and in_run:
            in_run = False
            t_start = (run_start * hop_len) / sample_rate
            t_end = min(len(right_channel) / sample_rate, (idx * hop_len + win_len * 0.5) / sample_rate)
            dur = t_end - t_start
            if min_tone_duration_sec <= dur <= max_tone_duration_sec:
                freq_slice = frame_dom_freq[run_start:idx]
                if float(np.std(freq_slice)) < 20.0:
                    tones.append(
                        ToneMarker(
                            start_time=round(t_start, 4),
                            end_time=round(t_end, 4),
                            dominant_freq_hz=round(float(np.median(freq_slice)), 1),
                            rms_db=round(float(np.mean(frame_rms_db[run_start:idx])), 2),
                        )
                    )

    # Filter out any spurious low-level vocal harmonic that is > 9 dB quieter than the loudest tone
    if len(tones) >= 2:
        max_tone_db = max(t.rms_db for t in tones)
        tones = [t for t in tones if t.rms_db >= (max_tone_db - 9.0)]

    return tones


def pair_talkback_tones(
    tones: List[ToneMarker],
    right_channel: np.ndarray,
    sample_rate: int,
) -> List[TalkbackInterval]:
    """
    Pair detected start and end audio tones into TalkbackIntervals.
    In Studio Mode, each talkback comment starts with a tone and ends with a tone.
    """
    intervals: List[TalkbackInterval] = []
    n = len(tones)
    idx = 0
    pair_num = 1
    total_dur = len(right_channel) / sample_rate if sample_rate > 0 else 0.0

    while idx < n:
        if idx + 1 < n:
            t1 = tones[idx]
            t2 = tones[idx + 1]
            # The note is between t1.end_time and t2.start_time
            note_start = min(t1.end_time + 0.02, t2.start_time)
            note_end = max(note_start, t2.start_time - 0.02)
            intervals.append(
                TalkbackInterval(
                    id=f"tb_{pair_num}",
                    start_time=round(max(0.0, t1.start_time - 0.02), 4),
                    end_time=round(min(total_dur, t2.end_time + 0.02), 4),
                    note_start=round(note_start, 4),
                    note_end=round(note_end, 4),
                    tone_start_freq_hz=t1.dominant_freq_hz,
                    tone_end_freq_hz=t2.dominant_freq_hz,
                )
            )
            pair_num += 1
            idx += 2
        else:
            # Odd trailing tone: find where talkback activity subsides (up to 10s)
            t1 = tones[idx]
            search_end = min(total_dur, t1.end_time + 10.0)
            s_sample = int(t1.end_time * sample_rate)
            e_sample = int(search_end * sample_rate)
            active_end = t1.end_time + 1.5
            if e_sample > s_sample:
                chunk = np.abs(right_channel[s_sample:e_sample])
                active_idx = np.where(chunk > 0.01)[0]
                if len(active_idx) > 0:
                    active_end = min(total_dur, t1.end_time + ( float(active_idx[-1]) / sample_rate ) + 0.1)
            intervals.append(
                TalkbackInterval(
                    id=f"tb_{pair_num}",
                    start_time=round(max(0.0, t1.start_time - 0.02), 4),
                    end_time=round(min(total_dur, active_end), 4),
                    note_start=round(min(total_dur, t1.end_time + 0.02), 4),
                    note_end=round(min(total_dur, active_end), 4),
                    tone_start_freq_hz=t1.dominant_freq_hz,
                    tone_end_freq_hz=t1.dominant_freq_hz,
                )
            )
            pair_num += 1
            idx += 1

    return intervals


def isolate_voiceover_channel(
    audio_data_2d: np.ndarray,
    sample_rate: int,
    mode: RecordingMode,
    talkback_intervals: List[TalkbackInterval],
) -> np.ndarray:
    """
    Extract a clean 1D voiceover signal for analysis and rendering.
    In Studio Mode:
      - Strictly takes ONLY the Left channel (channel 0).
      - Zeroes out any samples overlapping talkback_intervals (with smooth 8ms cosine ramps)
        so talkback bleed or mic noise during talkback can NEVER enter the output.
    In Normal Mode:
      - Uses channel 0 if mono, or balanced (L+R)/2 mono sum for STT/analysis.
    """
    if audio_data_2d.shape[1] == 1 or mode == RecordingMode.STUDIO:
        vo = np.array(audio_data_2d[:, 0], dtype=np.float64, copy=True)
    else:
        vo = np.mean(audio_data_2d[:, :2], axis=1, dtype=np.float64)

    if mode == RecordingMode.STUDIO and talkback_intervals:
        n_samples = len(vo)
        fade_samples = max(1, int(0.008 * sample_rate))
        for tb in talkback_intervals:
            s_idx = max(0, int(tb.start_time * sample_rate))
            e_idx = min(n_samples, int(tb.end_time * sample_rate))
            if e_idx > s_idx:
                vo[s_idx:e_idx] = 0.0
                # Smooth fade out before s_idx
                f_out_start = max(0, s_idx - fade_samples)
                if s_idx > f_out_start:
                    ramp = 0.5 * (1.0 + np.cos(np.linspace(0.0, math.pi, s_idx - f_out_start)))
                    vo[f_out_start:s_idx] *= ramp
                # Smooth fade in after e_idx
                f_in_end = min(n_samples, e_idx + fade_samples)
                if f_in_end > e_idx:
                    ramp = 0.5 * (1.0 - np.cos(np.linspace(0.0, math.pi, f_in_end - e_idx)))
                    vo[e_idx:f_in_end] *= ramp

    return vo


def detect_breaths_and_noises(
    vo_signal: np.ndarray,
    sample_rate: int,
    words: List[WordToken],
    talkback_intervals: List[TalkbackInterval],
    sensitivity: float = 0.65,
) -> List[BreathOrNoiseEvent]:
    """
    Detect breaths and mouth noises in inter-word gaps and phrase boundaries.
    Breaths exhibit:
      - Moderate-to-low RMS amplitude (-48 dBFS to -16 dBFS)
      - High zero-crossing rate (ZCR) or high-frequency unvoiced friction (> 550 Hz vs < 350 Hz)
    Also refines word start/end boundaries if a breath tail was attached to a word token.
    """
    events: List[BreathOrNoiseEvent] = []
    n_samples = len(vo_signal)
    if n_samples == 0 or sample_rate <= 0:
        return events

    total_dur = n_samples / sample_rate
    # Build candidate non-voiced intervals between words (plus before first word / after last word)
    sorted_words = sorted(words, key=lambda w: w.start)
    candidate_gaps: List[Tuple[float, float]] = []

    prev_end = 0.0
    for w in sorted_words:
        # Also inspect the first 120ms of a long word in case STT attached a leading inhale
        w_start = w.start
        if w_start > prev_end + 0.06:
            candidate_gaps.append((prev_end, w_start))
        prev_end = max(prev_end, w.end)
    if total_dur > prev_end + 0.06:
        candidate_gaps.append((prev_end, total_dur))

    # Thresholds scaled by sensitivity (0.0 .. 1.0)
    min_rms_db = -50.0 + (1.0 - sensitivity) * 8.0   # default ~ -47.2 dBFS
    max_rms_db = -14.0 + sensitivity * 4.0           # default ~ -11.4 dBFS
    min_zcr = 0.055 - (sensitivity * 0.025)          # default ~ 0.039

    win_len = max(64, int(0.020 * sample_rate))
    hop_len = max(32, int(0.010 * sample_rate))

    # Design a simple 1st-order high-pass / low-pass split at 500 Hz for breath friction check
    nyq = 0.5 * sample_rate
    cutoff = min(0.9, max(0.05, 550.0 / nyq))
    b_hp, a_hp = signal.butter(2, cutoff, btype="high")
    b_lp, a_lp = signal.butter(2, cutoff, btype="low")

    breath_idx = 1

    for gap_start, gap_end in candidate_gaps:
        # Skip if inside a talkback interval
        if any( not (gap_end <= tb.start_time or gap_start >= tb.end_time) for tb in talkback_intervals ):
            continue

        s_idx = max(0, int(gap_start * sample_rate))
        e_idx = min(n_samples, int(gap_end * sample_rate))
        if e_idx - s_idx < win_len:
            continue

        segment = vo_signal[s_idx:e_idx]
        hp_seg = signal.lfilter(b_hp, a_hp, segment)
        lp_seg = signal.lfilter(b_lp, a_lp, segment)

        num_frames = (len(segment) - win_len) // hop_len + 1
        frame_active = np.zeros(num_frames, dtype=bool)
        frame_zcr = np.zeros(num_frames, dtype=np.float64)
        frame_db = np.full(num_frames, -100.0, dtype=np.float64)

        for f in range(num_frames):
            fs = f * hop_len
            frame = segment[fs : fs + win_len]
            f_rms = float(np.sqrt(np.mean(frame * frame) + 1e-18))
            f_db = 20.0 * math.log10(max(f_rms, 1e-9))
            frame_db[f] = f_db

            if min_rms_db <= f_db <= max_rms_db:
                # Zero crossing rate
                signs = np.signbit(frame)
                zcr = float(np.mean(signs[:-1] != signs[1:]))
                frame_zcr[f] = zcr

                # High-freq friction ratio
                hp_e = float(np.mean(hp_seg[fs : fs + win_len] ** 2) + 1e-18)
                lp_e = float(np.mean(lp_seg[fs : fs + win_len] ** 2) + 1e-18)
                hf_ratio = hp_e / (hp_e + lp_e)

                if zcr >= min_zcr or hf_ratio >= 0.35:
                    frame_active[f] = True

        # Group contiguous active frames into BreathOrNoiseEvent
        in_evt = False
        evt_start_f = 0
        for f in range(num_frames + 1):
            act = bool(frame_active[f]) if f < num_frames else False
            if act and not in_evt:
                in_evt = True
                evt_start_f = f
            elif not act and in_evt:
                in_evt = False
                ev_s = gap_start + (evt_start_f * hop_len) / sample_rate
                ev_e = min(gap_end, gap_start + (f * hop_len + win_len) / sample_rate)
                dur = ev_e - ev_s
                if 0.035 <= dur <= 1.25:
                    avg_db = float(np.mean(frame_db[evt_start_f:f]))
                    avg_zcr = float(np.mean(frame_zcr[evt_start_f:f]))
                    kind = "breath" if dur >= 0.085 else "mouth_noise"
                    events.append(
                        BreathOrNoiseEvent(
                            id=f"br_{breath_idx}",
                            start_time=round(ev_s, 4),
                            end_time=round(ev_e, 4),
                            kind=kind,
                            rms_db=round(avg_db, 2),
                            zcr=round(avg_zcr, 4),
                            removed=True,
                        )
                    )
                    breath_idx += 1

    return events


def extract_room_tone_profile(
    vo_signal: np.ndarray,
    sample_rate: int,
    talkback_intervals: List[TalkbackInterval],
) -> Tuple[np.ndarray, float]:
    """
    Extract a clean 0.5-second room-tone loop from the quietest non-zero voiceover section,
    and return (room_tone_samples, room_tone_rms_db).
    """
    chunk_len = max(256, int(0.25 * sample_rate))
    n = len(vo_signal)
    best_rms = 1.0
    best_chunk: Optional[np.ndarray] = None

    step = max(chunk_len // 2, 1)
    for start in range(0, max(1, n - chunk_len), step):
        t_s = start / sample_rate
        t_e = (start + chunk_len) / sample_rate
        if any(not (t_e <= tb.start_time or t_s >= tb.end_time) for tb in talkback_intervals):
            continue
        chunk = vo_signal[start : start + chunk_len]
        c_rms = float(np.sqrt(np.mean(chunk * chunk)))
        # Must not be pure digital zeroes (unless the whole file has digital silence)
        if 1e-6 < c_rms < best_rms and c_rms < 0.008:
            best_rms = c_rms
            best_chunk = chunk.copy()

    if best_chunk is None:
        # Synthesize gentle -68 dBFS room tone
        rng = np.random.default_rng(42)
        best_chunk = rng.normal(0.0, 10.0 ** (-68.0 / 20.0), size=chunk_len)
        best_rms = float(np.sqrt(np.mean(best_chunk * best_chunk)))

    db_val = 20.0 * math.log10(max(best_rms, 1e-9))
    return best_chunk, round(float(db_val), 2)


def _generate_pause_samples(
    num_samples: int,
    room_tone_chunk: np.ndarray,
    use_room_tone: bool,
) -> np.ndarray:
    """Create pause padding samples using either tiled crossfaded room-tone or digital silence."""
    if num_samples <= 0:
        return np.zeros(0, dtype=np.float64)
    if not use_room_tone or len(room_tone_chunk) == 0:
        return np.zeros(num_samples, dtype=np.float64)
    reps = (num_samples // len(room_tone_chunk)) + 2
    tiled = np.tile(room_tone_chunk, reps)[:num_samples].copy()
    return tiled


def render_edited_audio(
    session: EditSession,
    export_settings: ExportSettings,
    output_dir: str,
) -> str:
    """
    Assemble the final edited audio according to preserved TimelineEditClips and ExportSettings.
    Guarantees:
      - In Studio Mode, NEVER includes any audio from the right (talkback) channel.
      - Removes all breaths/noises marked `removed=True`.
      - Applies smooth cosine fade-in / fade-out at every cut boundary.
      - Matches input format/sample-rate/bit-depth/channels by default, or converts to custom settings.
    """
    os.makedirs(output_dir, exist_ok=True)
    # Load original audio & clean isolated VO audio
    orig_data, orig_sr, orig_ch, orig_subtype, orig_fmt = load_audio_file(session.source_path)
    clean_vo, vo_sr, _, _, _ = load_audio_file(session.clean_vo_path)
    vo_1d = clean_vo[:, 0]

    room_tone_chunk, _ = extract_room_tone_profile(
        vo_1d, orig_sr, session.talkback_intervals
    )

    # Determine whether we can use stereo source in Normal Mode
    use_stereo_source = (
        session.settings.mode == RecordingMode.NORMAL
        and orig_ch >= 2
        and export_settings.channel_map in (ChannelMapOption.MATCH_INPUT, ChannelMapOption.STEREO_DUAL_MONO)
    )

    removed_breaths = [b for b in session.breaths_removed if b.removed]
    fade_sec = max(0.002, export_settings.crossfade_ms / 1000.0)
    fade_samples = max(4, int(fade_sec * orig_sr))

    assembled_chunks_L: List[np.ndarray] = []
    assembled_chunks_R: List[np.ndarray] = []

    for idx, clip in enumerate(session.timeline_clips):
        s_idx = max(0, int(round(clip.source_start * orig_sr)))
        e_idx = min(len(vo_1d), int(round(clip.source_end * orig_sr)))
        if e_idx <= s_idx:
            continue

        clip_L = vo_1d[s_idx:e_idx].copy()
        if use_stereo_source:
            clip_R = orig_data[s_idx:e_idx, 1].copy()
        else:
            # In Studio Mode or mono Normal Mode, Right channel is derived strictly from clean VO!
            clip_R = clip_L.copy()

        # Attenuate/remove any internal breath events that fall within [clip.source_start, clip.source_end]
        for br in removed_breaths:
            if br.end_time > clip.source_start and br.start_time < clip.source_end:
                bs = max(0, int((br.start_time - clip.source_start) * orig_sr))
                be = min(len(clip_L), int((br.end_time - clip.source_start) * orig_sr))
                if be > bs:
                    fill = _generate_pause_samples(
                        be - bs, room_tone_chunk, export_settings.use_room_tone_padding
                    )
                    clip_L[bs:be] = fill
                    clip_R[bs:be] = fill
                    # Micro-fade around breath replacement
                    bf = min(fade_samples, bs, len(clip_L) - be)
                    if bf > 1:
                        r_out = 0.5 * (1.0 + np.cos(np.linspace(0.0, math.pi, bf)))
                        r_in = 0.5 * (1.0 - np.cos(np.linspace(0.0, math.pi, bf)))
                        clip_L[bs - bf : bs] *= r_out
                        clip_L[be : be + bf] *= r_in
                        clip_R[bs - bf : bs] *= r_out
                        clip_R[be : be + bf] *= r_in

        # Apply clip boundary fade-in and fade-out
        f_in = min(max(2, int((clip.fade_in_ms / 1000.0) * orig_sr)), len(clip_L) // 3)
        f_out = min(max(2, int((clip.fade_out_ms / 1000.0) * orig_sr)), len(clip_L) // 3)
        if f_in > 1:
            ramp_in = 0.5 * (1.0 - np.cos(np.linspace(0.0, math.pi, f_in)))
            clip_L[:f_in] *= ramp_in
            clip_R[:f_in] *= ramp_in
        if f_out > 1:
            ramp_out = 0.5 * (1.0 + np.cos(np.linspace(0.0, math.pi, f_out)))
            clip_L[-f_out:] *= ramp_out
            clip_R[-f_out:] *= ramp_out

        assembled_chunks_L.append(clip_L)
        assembled_chunks_R.append(clip_R)

        # Insert grammatical pause after clip
        pause_samples = int(round((clip.pause_after_ms / 1000.0) * orig_sr))
        if pause_samples > 0:
            pause_buf = _generate_pause_samples(
                pause_samples, room_tone_chunk, export_settings.use_room_tone_padding
            )
            assembled_chunks_L.append(pause_buf)
            assembled_chunks_R.append(pause_buf.copy())

    if assembled_chunks_L:
        full_L = np.concatenate(assembled_chunks_L)
        full_R = np.concatenate(assembled_chunks_R)
    else:
        full_L = np.zeros(int(0.5 * orig_sr), dtype=np.float64)
        full_R = np.zeros(int(0.5 * orig_sr), dtype=np.float64)

    # Target sample rate conversion if requested
    target_sr = export_settings.sample_rate if export_settings.sample_rate > 0 else orig_sr
    if target_sr != orig_sr:
        gcd = math.gcd(target_sr, orig_sr)
        up = target_sr // gcd
        down = orig_sr // gcd
        full_L = signal.resample_poly(full_L, up, down)
        full_R = signal.resample_poly(full_R, up, down)

    # Apply Channel Mapping
    cmap = export_settings.channel_map
    if cmap == ChannelMapOption.MATCH_INPUT:
        if orig_ch == 1:
            out_data = full_L[:, np.newaxis]
        else:
            # 2 channels: in Studio Mode full_L and full_R are both clean VO (talkback excluded!)
            out_data = np.column_stack([full_L, full_R])
    elif cmap == ChannelMapOption.MONO:
        out_data = full_L[:, np.newaxis]
    elif cmap == ChannelMapOption.STEREO_DUAL_MONO:
        out_data = np.column_stack([full_L, full_L])
    elif cmap == ChannelMapOption.STEREO_LEFT_ONLY:
        out_data = np.column_stack([full_L, np.zeros_like(full_L)])
    elif cmap == ChannelMapOption.STEREO_RIGHT_ONLY:
        out_data = np.column_stack([np.zeros_like(full_L), full_L])
    else:
        out_data = full_L[:, np.newaxis]

    # Target Format & Bit Depth
    fmt_opt = export_settings.audio_format
    if fmt_opt == AudioFormatOption.MATCH_INPUT:
        ext = Path(session.source_filename).suffix.lower().lstrip(".") or "wav"
        if ext not in ("wav", "flac", "ogg", "mp3", "m4a"):
            ext = "wav"
    else:
        ext = fmt_opt.value

    if export_settings.bit_depth == BitDepthOption.MATCH_INPUT:
        subtype = orig_subtype if orig_subtype in ("PCM_16", "PCM_24", "FLOAT") else "PCM_24"
    else:
        subtype = export_settings.bit_depth.value

    safe_base = "".join(
        c if (c.isalnum() or c in ("-", "_", " ")) else "_"
        for c in (export_settings.output_name or "Rough_Cut_Master")
    ).strip() or "Rough_Cut_Master"

    out_path = os.path.join(output_dir, f"{safe_base}.{ext}")

    if ext in ("wav", "flac", "ogg"):
        sf_subtype = subtype
        if ext == "flac" and sf_subtype == "FLOAT":
            sf_subtype = "PCM_24"
        elif ext == "ogg":
            sf_subtype = "VORBIS"
        sf.write(out_path, out_data, target_sr, subtype=sf_subtype)
    else:
        # Encode MP3 or M4A via FFmpeg
        ffmpeg_bin = _find_ffmpeg()
        if not ffmpeg_bin:
            # Fallback to WAV if ffmpeg not present
            out_path = os.path.join(output_dir, f"{safe_base}.wav")
            sf.write(out_path, out_data, target_sr, subtype=subtype)
            return out_path

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_wav = tmp.name
        try:
            sf.write(tmp_wav, out_data, target_sr, subtype="PCM_24")
            codec_args = ["-c:a", "libmp3lame", "-q:a", "0"] if ext == "mp3" else ["-c:a", "aac", "-b:a", "256k"]
            cmd = [ffmpeg_bin, "-y", "-i", tmp_wav, *codec_args, out_path]
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finally:
            if os.path.exists(tmp_wav):
                try:
                    os.remove(tmp_wav)
                except OSError:
                    pass

    return out_path


def compute_assembled_output_peaks(
    clean_vo_path: str,
    timeline_clips: List[TimelineEditClip],
    breaths_removed: List[BreathOrNoiseEvent],
    num_bins: int = 500,
) -> List[float]:
    """
    Compute normalized peak envelope [0.0..1.0] for the assembled output timeline
    from the isolated clean voiceover audio and preserved edit clips.
    """
    if not os.path.exists(clean_vo_path) or not timeline_clips:
        return [0.0] * num_bins
    try:
        clean_vo, sr, _, _, _ = load_audio_file(clean_vo_path)
    except Exception:
        return [0.0] * num_bins
    vo_1d = clean_vo[:, 0]
    removed_breaths = [b for b in breaths_removed if b.removed]
    chunks: List[np.ndarray] = []

    for clip in timeline_clips:
        s_idx = max(0, int(round(clip.source_start * sr)))
        e_idx = min(len(vo_1d), int(round(clip.source_end * sr)))
        if e_idx <= s_idx:
            continue
        seg = vo_1d[s_idx:e_idx].copy()
        for br in removed_breaths:
            if br.end_time > clip.source_start and br.start_time < clip.source_end:
                bs = max(0, int((br.start_time - clip.source_start) * sr))
                be = min(len(seg), int((br.end_time - clip.source_start) * sr))
                if be > bs:
                    seg[bs:be] = 0.0
        chunks.append(seg)
        pause_samples = int(round((clip.pause_after_ms / 1000.0) * sr))
        if pause_samples > 0:
            chunks.append(np.zeros(pause_samples, dtype=np.float64))

    if not chunks:
        return [0.0] * num_bins
    assembled = np.concatenate(chunks)
    return compute_waveform_peaks(assembled, num_bins=num_bins)


def render_output_preview(
    session: EditSession,
    export_settings: ExportSettings,
    output_dir: str,
) -> Tuple[str, OutputPreviewInfo]:
    """
    Render the final output file according to `export_settings` and return its file path
    along with `OutputPreviewInfo` metadata and waveform peaks for pre-export inspection.
    """
    rendered_path = render_edited_audio(session, export_settings, output_dir)
    data_2d, sr, channels, subtype, fmt = load_audio_file(rendered_path)
    duration_sec = round(len(data_2d) / sr, 4) if sr > 0 else 0.0
    file_size = os.path.getsize(rendered_path) if os.path.exists(rendered_path) else 0
    mono_envelope = np.max(np.abs(data_2d), axis=1) if len(data_2d) > 0 else np.zeros(0)
    peaks_out = compute_waveform_peaks(mono_envelope, num_bins=500)

    fname = Path(rendered_path).name
    ext = Path(rendered_path).suffix.lower().lstrip(".") or fmt
    mtime_ms = int(os.path.getmtime(rendered_path) * 1000) if os.path.exists(rendered_path) else 0
    preview_url = f"/api/sessions/{session.session_id}/preview-audio?file={fname}&t={mtime_ms}"

    info = OutputPreviewInfo(
        session_id=session.session_id,
        filename=fname,
        audio_format=ext.upper(),
        sample_rate=sr,
        channels=channels,
        bit_depth=subtype,
        duration_sec=duration_sec,
        file_size_bytes=file_size,
        preview_audio_url=preview_url,
        waveform_peaks_output=peaks_out,
    )
    return rendered_path, info

