"""Synthetic Studio Mode & Normal Mode rough-cut recording generator for instant demo & verification."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import soundfile as sf


def _synthesize_voiced_word(
    duration_sec: float,
    sample_rate: int,
    f0_hz: float = 135.0,
    amplitude: float = 0.35,
) -> np.ndarray:
    """Synthesize a rich harmonic human-like voiced syllable contour."""
    n = max(1, int(round(duration_sec * sample_rate)))
    t = np.arange(n, dtype=np.float64) / sample_rate
    # Pitch vibrato & natural declination
    freq = f0_hz * (1.0 - 0.04 * (t / max(0.05, duration_sec)) + 0.015 * np.sin(2.0 * math.pi * 5.5 * t))
    phase = 2.0 * math.pi * np.cumsum(freq) / sample_rate
    sig = (
        0.55 * np.sin(phase)
        + 0.28 * np.sin(2.0 * phase + 0.3)
        + 0.12 * np.sin(3.0 * phase + 0.7)
        + 0.05 * np.sin(4.0 * phase)
    )
    # Smooth attack/release envelope
    attack = min(n // 3, max(4, int(0.025 * sample_rate)))
    release = min(n // 3, max(4, int(0.035 * sample_rate)))
    env = np.ones(n, dtype=np.float64)
    if attack > 1:
        env[:attack] = 0.5 * (1.0 - np.cos(np.linspace(0.0, math.pi, attack)))
    if release > 1:
        env[-release:] = 0.5 * (1.0 + np.cos(np.linspace(0.0, math.pi, release)))
    return sig * env * amplitude


def _synthesize_breath(
    duration_sec: float,
    sample_rate: int,
    amplitude: float = 0.038,
    seed: int = 101,
) -> np.ndarray:
    """Synthesize a high-frequency unvoiced inhalation breath sound."""
    n = max(1, int(round(duration_sec * sample_rate)))
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 1.0, size=n)
    # Differentiate to emphasize high-frequency breath friction (> 700 Hz)
    hf = np.diff(noise, prepend=noise[0])
    hf = hf / (np.max(np.abs(hf)) + 1e-9)
    env = np.sin(np.linspace(0.0, math.pi, n)) ** 1.4
    return hf * env * amplitude


def _synthesize_pure_tone(
    duration_sec: float,
    sample_rate: int,
    freq_hz: float = 1000.0,
    amplitude: float = 0.45,
) -> np.ndarray:
    """Synthesize a pure studio talkback start/end beep tone."""
    n = max(1, int(round(duration_sec * sample_rate)))
    t = np.arange(n, dtype=np.float64) / sample_rate
    tone = np.sin(2.0 * math.pi * freq_hz * t)
    fade = min(n // 4, max(4, int(0.010 * sample_rate)))
    env = np.ones(n, dtype=np.float64)
    if fade > 1:
        env[:fade] = np.linspace(0.0, 1.0, fade)
        env[-fade:] = np.linspace(1.0, 0.0, fade)
    return tone * env * amplitude


def generate_studio_mode_sample(output_dir: str, sample_rate: int = 44100) -> str:
    """
    Create a realistic stereo Studio Mode voiceover recording (`Alpine_Studio_Mode_Demo.wav`):
      - Left Channel (Ch 0): Voiceover mic with:
        * Group 1 Take 1 (False start): "Welcome to the Alpine—"
        * [Talkback 1 on Right Ch: 1kHz tone + "Hold on, let's redo that opening from the top." + 1kHz tone]
        * Group 1 Take 2 (Clean keeper with comma & breath): "Welcome to the Alpine Recording Station, [breath] where every story comes alive."
        * Group 2 Take 1 (Good take): "Our acoustic booths are engineered for crystal clear vocals, warm presence, and effortless mastering."
        * Group 2 Take 2 (Duplicate take with stumble/clipping): "Our acoustic booths are um engineered for crystal clear vocals, warm presence, and effortless mastering."
        * [Talkback 2 on Right Ch: 1kHz tone + "Keep the first take on that second line, the first one was best." + 1kHz tone]
        * Group 3 Take 1 (Final sentence): "Book your next session today, and hear the difference."
      - Right Channel (Ch 1): Talkback track with start/end 1000 Hz audio tones and spoken director notes in the middle.
    Also writes a `.transcript.json` sidecar so offline environments without downloaded Whisper weights
    can still inspect the exact word tokens and talkback transcripts immediately.
    """
    os.makedirs(output_dir, exist_ok=True)
    wav_path = os.path.join(output_dir, "Alpine_Studio_Mode_Demo.wav")

    rng = np.random.default_rng(7)
    total_sec = 25.0
    n_total = int(total_sec * sample_rate)

    # Low-level studio room tone (-66 dBFS) on VO channel
    left_vo = rng.normal(0.0, 10.0 ** (-66.0 / 20.0), size=n_total)
    right_tb = np.zeros(n_total, dtype=np.float64)

    words_meta: List[Dict] = []

    def place_utterance(
        start_t: float,
        tokens: List[Tuple[str, str, float, float]],
        f0_base: float = 130.0,
        amp: float = 0.36,
        inter_word_gap: float = 0.095,
        breath_after_idx: int = -1,
    ) -> float:
        cursor = start_t
        for i, (w_text, punct, dur, prob) in enumerate(tokens):
            s_i = int(round(cursor * sample_rate))
            w_sig = _synthesize_voiced_word(dur, sample_rate, f0_hz=f0_base + (i % 3) * 6.0, amplitude=amp)
            e_i = min(n_total, s_i + len(w_sig))
            left_vo[s_i:e_i] = w_sig[: e_i - s_i]
            w_end = round(cursor + dur, 4)
            words_meta.append(
                {
                    "word": w_text,
                    "start": round(cursor, 4),
                    "end": w_end,
                    "probability": prob,
                    "punctuation_after": punct,
                }
            )
            cursor = w_end
            if i == breath_after_idx:
                # Insert a 0.26s audible inhalation breath between words
                cursor += 0.04
                br_dur = 0.26
                bs = int(round(cursor * sample_rate))
                br_sig = _synthesize_breath(br_dur, sample_rate, amplitude=0.042, seed=200 + i)
                be = min(n_total, bs + len(br_sig))
                left_vo[bs:be] = br_sig[: be - bs]
                cursor += br_dur + 0.05
            elif i < len(tokens) - 1:
                if "," in punct:
                    cursor += 0.33  # natural uncut comma pause
                else:
                    cursor += inter_word_gap
        return cursor

    def place_talkback(
        start_t: float,
        note_duration: float,
        tone_freq: float = 1000.0,
        bleed_on_vo: bool = True,
    ) -> float:
        tone_dur = 0.22
        # Start tone
        t1_s = int(round(start_t * sample_rate))
        t1_sig = _synthesize_pure_tone(tone_dur, sample_rate, freq_hz=tone_freq, amplitude=0.48)
        right_tb[t1_s : t1_s + len(t1_sig)] = t1_sig

        # Spoken director note in middle on right channel
        note_s_t = start_t + tone_dur + 0.06
        note_s = int(round(note_s_t * sample_rate))
        note_sig = _synthesize_voiced_word(note_duration, sample_rate, f0_hz=115.0, amplitude=0.32)
        right_tb[note_s : note_s + len(note_sig)] = note_sig

        # Optional faint bleed on left channel during talkback to prove Studio Mode zeroes it out!
        if bleed_on_vo:
            left_vo[note_s : note_s + len(note_sig)] += 0.02 * note_sig

        # End tone
        t2_s_t = note_s_t + note_duration + 0.06
        t2_s = int(round(t2_s_t * sample_rate))
        t2_sig = _synthesize_pure_tone(tone_dur, sample_rate, freq_hz=tone_freq, amplitude=0.48)
        right_tb[t2_s : t2_s + len(t2_sig)] = t2_sig

        return t2_s_t + tone_dur

    # --- Group 1 Take 1 (False start at 0.40s) ---
    c = place_utterance(
        0.40,
        [
            ("Welcome", "", 0.28, 0.94),
            ("to", "", 0.14, 0.95),
            ("the", "", 0.14, 0.92),
            ("Alpine", "—", 0.30, 0.78),
        ],
    )

    # --- Talkback 1 (1.60s .. 3.40s): "Hold on, let's redo that opening from the top." ---
    c = place_talkback(1.60, note_duration=1.15, tone_freq=1000.0)

    # --- Group 1 Take 2 (Clean complete take at 3.80s, with comma and a breath after comma) ---
    c = place_utterance(
        3.80,
        [
            ("Welcome", "", 0.27, 0.98),
            ("to", "", 0.13, 0.99),
            ("the", "", 0.13, 0.98),
            ("Alpine", "", 0.28, 0.97),
            ("Recording", "", 0.32, 0.98),
            ("Station", ",", 0.34, 0.98),
            ("where", "", 0.20, 0.97),
            ("every", "", 0.22, 0.98),
            ("story", "", 0.28, 0.98),
            ("comes", "", 0.24, 0.97),
            ("alive", ".", 0.34, 0.99),
        ],
        breath_after_idx=5,
    )

    # --- Group 2 Take 1 (Clean complete take at 8.40s) ---
    c = place_utterance(
        8.40,
        [
            ("Our", "", 0.18, 0.97),
            ("acoustic", "", 0.30, 0.98),
            ("booths", "", 0.26, 0.97),
            ("are", "", 0.15, 0.98),
            ("engineered", "", 0.34, 0.98),
            ("for", "", 0.15, 0.98),
            ("crystal", "", 0.26, 0.97),
            ("clear", "", 0.24, 0.98),
            ("vocals", ",", 0.32, 0.98),
            ("warm", "", 0.24, 0.97),
            ("presence", ",", 0.32, 0.98),
            ("and", "", 0.16, 0.98),
            ("effortless", "", 0.32, 0.97),
            ("mastering", ".", 0.36, 0.99),
        ],
    )

    # --- Group 2 Take 2 (Duplicate take with disfluency 'um' at 14.20s) ---
    c = place_utterance(
        14.20,
        [
            ("Our", "", 0.18, 0.92),
            ("acoustic", "", 0.30, 0.91),
            ("booths", "", 0.26, 0.90),
            ("are", "", 0.15, 0.88),
            ("um", "", 0.22, 0.65),
            ("engineered", "", 0.34, 0.90),
            ("for", "", 0.15, 0.92),
            ("crystal", "", 0.26, 0.91),
            ("clear", "", 0.24, 0.92),
            ("vocals", ",", 0.32, 0.91),
            ("warm", "", 0.24, 0.90),
            ("presence", ",", 0.32, 0.91),
            ("and", "", 0.16, 0.92),
            ("effortless", "", 0.32, 0.91),
            ("mastering", ".", 0.36, 0.92),
        ],
    )

    # --- Talkback 2 (19.60s .. 21.40s): "Keep the first take on that line, take one was best." ---
    c = place_talkback(19.60, note_duration=1.15, tone_freq=1000.0)

    # --- Group 3 Take 1 (Final sentence at 21.80s) ---
    c = place_utterance(
        21.80,
        [
            ("Book", "", 0.22, 0.98),
            ("your", "", 0.16, 0.98),
            ("next", "", 0.22, 0.98),
            ("session", "", 0.28, 0.99),
            ("today", ",", 0.30, 0.98),
            ("and", "", 0.15, 0.98),
            ("hear", "", 0.20, 0.98),
            ("the", "", 0.13, 0.99),
            ("difference", ".", 0.35, 0.99),
        ],
    )

    stereo_data = np.column_stack([left_vo, right_tb])
    sf.write(wav_path, stereo_data, sample_rate, subtype="PCM_24")

    sidecar_path = f"{wav_path}.transcript.json"
    sidecar_payload = {
        "words": words_meta,
        "talkback_notes_ordered": [
            "Hold on, let's redo that opening line from the top.",
            "Keep the first take on that second line, take one was best.",
        ],
    }
    Path(sidecar_path).write_text(json.dumps(sidecar_payload, indent=2), encoding="utf-8")

    return wav_path


def generate_normal_mode_sample(output_dir: str, sample_rate: int = 44100) -> str:
    """
    Create a mono Normal Mode voiceover recording (`Alpine_Normal_Mode_Demo.wav`)
    containing false starts, duplicate takes, audible breaths, commas, and periods.
    """
    os.makedirs(output_dir, exist_ok=True)
    wav_path = os.path.join(output_dir, "Alpine_Normal_Mode_Demo.wav")

    rng = np.random.default_rng(19)
    total_sec = 14.5
    n_total = int(total_sec * sample_rate)
    mono_vo = rng.normal(0.0, 10.0 ** (-66.0 / 20.0), size=n_total)

    words_meta: List[Dict] = []

    def place_utterance(
        start_t: float,
        tokens: List[Tuple[str, str, float, float]],
        breath_after_idx: int = -1,
    ) -> float:
        cursor = start_t
        for i, (w_text, punct, dur, prob) in enumerate(tokens):
            s_i = int(round(cursor * sample_rate))
            w_sig = _synthesize_voiced_word(dur, sample_rate, f0_hz=142.0 + (i % 3) * 5.0, amplitude=0.35)
            e_i = min(n_total, s_i + len(w_sig))
            mono_vo[s_i:e_i] = w_sig[: e_i - s_i]
            w_end = round(cursor + dur, 4)
            words_meta.append(
                {
                    "word": w_text,
                    "start": round(cursor, 4),
                    "end": w_end,
                    "probability": prob,
                    "punctuation_after": punct,
                }
            )
            cursor = w_end
            if i == breath_after_idx:
                cursor += 0.04
                br_dur = 0.24
                bs = int(round(cursor * sample_rate))
                br_sig = _synthesize_breath(br_dur, sample_rate, amplitude=0.040, seed=300 + i)
                be = min(n_total, bs + len(br_sig))
                mono_vo[bs:be] = br_sig[: be - bs]
                cursor += br_dur + 0.04
            elif i < len(tokens) - 1:
                cursor += 0.32 if "," in punct else 0.090
        return cursor

    # Group 1 Take 1 (Aborted take with stumble)
    place_utterance(
        0.35,
        [
            ("Precision", "", 0.28, 0.93),
            ("editing", "", 0.26, 0.91),
            ("saves", "", 0.24, 0.85),
            ("oops", "—", 0.22, 0.60),
        ],
    )

    # Group 1 Take 2 (Clean keeper with comma and breath)
    place_utterance(
        2.30,
        [
            ("Precision", "", 0.28, 0.98),
            ("editing", "", 0.26, 0.98),
            ("saves", "", 0.24, 0.99),
            ("hours", "", 0.25, 0.98),
            ("in", "", 0.14, 0.99),
            ("the", "", 0.13, 0.99),
            ("studio", ",", 0.32, 0.98),
            ("while", "", 0.20, 0.98),
            ("preserving", "", 0.32, 0.98),
            ("natural", "", 0.26, 0.99),
            ("human", "", 0.24, 0.98),
            ("pacing", ".", 0.34, 0.99),
        ],
        breath_after_idx=6,
    )

    # Group 2 Take 1 (Clean second sentence)
    place_utterance(
        7.80,
        [
            ("Every", "", 0.22, 0.98),
            ("comma", "", 0.26, 0.99),
            ("breathes", "", 0.28, 0.98),
            ("naturally", ";", 0.34, 0.98),
            ("every", "", 0.22, 0.98),
            ("sentence", "", 0.28, 0.99),
            ("lands", "", 0.24, 0.98),
            ("with", "", 0.16, 0.99),
            ("clarity", ".", 0.36, 0.99),
        ],
    )

    sf.write(wav_path, mono_vo[:, np.newaxis], sample_rate, subtype="PCM_24")

    sidecar_path = f"{wav_path}.transcript.json"
    Path(sidecar_path).write_text(
        json.dumps({"words": words_meta, "talkback_notes_ordered": []}, indent=2),
        encoding="utf-8",
    )

    return wav_path
