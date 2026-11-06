"""Natural Cadence & English Grammatical Pause Engine for Voiceover Assembly."""

from __future__ import annotations

from typing import List, Tuple
import numpy as np

from backend.models import (
    BreathOrNoiseEvent,
    CadenceProfile,
    TakeCandidate,
    TakeGroup,
    TimelineEditClip,
)


def analyze_uncut_cadence(
    take_groups: List[TakeGroup],
    breaths_removed: List[BreathOrNoiseEvent],
    comma_multiplier: float = 3.2,
    period_multiplier: float = 6.8,
    min_word_gap_ms: float = 85.0,
    room_tone_rms_db: float = -62.0,
) -> CadenceProfile:
    """
    Analyze uncut continuous sections of speech within the selected takes to establish
    the speaker's natural inter-word timing, speaking rate (WPM), and grammatical pause durations.
    """
    word_gaps_ms: List[float] = []
    uncut_comma_gaps_ms: List[float] = []
    total_words = 0
    total_speech_sec = 0.0

    active_breaths = [b for b in breaths_removed if b.removed]

    for group in take_groups:
        selected = next((t for t in group.takes if t.selected), None)
        if not selected or not selected.words:
            continue

        w_list = selected.words
        total_words += len(w_list)
        dur = max(0.1, w_list[-1].end - w_list[0].start)
        total_speech_sec += dur

        for i in range(len(w_list) - 1):
            w_curr = w_list[i]
            w_next = w_list[i + 1]
            raw_gap_sec = max(0.0, w_next.start - w_curr.end)

            # Subtract any removed breath that fell in this inter-word gap
            breath_overlap_sec = 0.0
            for br in active_breaths:
                overlap_s = max(w_curr.end, br.start_time)
                overlap_e = min(w_next.start, br.end_time)
                if overlap_e > overlap_s:
                    breath_overlap_sec += overlap_e - overlap_s

            net_gap_ms = max(15.0, (raw_gap_sec - breath_overlap_sec) * 1000.0)
            punct = w_curr.punctuation_after.strip()

            if not punct:
                if 20.0 <= net_gap_ms <= 380.0:
                    word_gaps_ms.append(net_gap_ms)
            elif "," in punct:
                if 150.0 <= net_gap_ms <= 800.0:
                    uncut_comma_gaps_ms.append(net_gap_ms)

    if word_gaps_ms:
        median_gap = float(np.median(word_gaps_ms))
        mean_gap = float(np.mean(word_gaps_ms))
    else:
        median_gap = max(min_word_gap_ms, 95.0)
        mean_gap = max(min_word_gap_ms, 105.0)

    # Clamp baseline word gap to natural human cadence bounds [65ms .. 185ms]
    base_word_gap = max(min_word_gap_ms, min(185.0, median_gap))

    wpm = (total_words / total_speech_sec) * 60.0 if total_speech_sec > 0.2 else 145.0
    wpm = max(80.0, min(240.0, wpm))

    # Calculate English grammatical pause targets from speaker's uncut cadence
    computed_comma = base_word_gap * comma_multiplier
    if uncut_comma_gaps_ms:
        # Blend 50% measured uncut comma pause + 50% multiplier rule for natural consistency
        measured_comma = float(np.median(uncut_comma_gaps_ms))
        computed_comma = 0.5 * measured_comma + 0.5 * computed_comma

    comma_pause = max(220.0, min(650.0, computed_comma))
    semicolon_pause = max(280.0, min(780.0, comma_pause * 1.24))
    period_pause = max(440.0, min(1250.0, base_word_gap * period_multiplier))
    paragraph_pause = max(750.0, min(1800.0, period_pause * 1.55))

    return CadenceProfile(
        median_word_gap_ms=round(base_word_gap, 1),
        mean_word_gap_ms=round(mean_gap, 1),
        words_per_minute=round(wpm, 1),
        comma_pause_ms=round(comma_pause, 1),
        semicolon_pause_ms=round(semicolon_pause, 1),
        period_pause_ms=round(period_pause, 1),
        paragraph_pause_ms=round(paragraph_pause, 1),
        room_tone_rms_db=round(room_tone_rms_db, 2),
    )


def _local_prose_tempo_ratio(
    selected_takes: List[TakeCandidate],
    center_idx: int,
    global_wpm: float,
) -> float:
    """
    Compute the local speaking tempo ratio for a prose neighborhood (center_idx +- 1 take)
    relative to the global speaking rate.
    If the speaker is speaking slower in a dramatic passage, local_ratio > 1.0 (longer pauses);
    if speaking briskly, local_ratio < 1.0 (tighter pauses).
    """
    if not selected_takes or global_wpm <= 0:
        return 1.0
    lo = max(0, center_idx - 1)
    hi = min(len(selected_takes), center_idx + 2)
    window = selected_takes[lo:hi]
    words_count = sum(len(t.words) for t in window)
    dur_sec = sum(max(0.15, t.end_time - t.start_time) for t in window)
    if words_count < 3 or dur_sec <= 0.15:
        return 1.0
    local_wpm = (words_count / dur_sec) * 60.0
    # Ratio of global_wpm / local_wpm (slower local WPM => larger pause multiplier)
    ratio = global_wpm / max(60.0, min(260.0, local_wpm))
    return max(0.78, min(1.30, ratio))


def classify_grammatical_pause(
    punctuation: str,
    cadence: CadenceProfile,
    local_tempo_ratio: float = 1.0,
) -> Tuple[str, float]:
    """
    Determine pause type and duration (in milliseconds) after a segment based on
    English grammatical punctuation rules and the local uncut prose cadence.
    """
    p = (punctuation or "").strip()
    if not p:
        return "word", round(cadence.median_word_gap_ms * local_tempo_ratio, 1)
    if any(ch in p for ch in (";", ":", "—", "-")):
        return "semicolon", round(cadence.semicolon_pause_ms * local_tempo_ratio, 1)
    if "," in p:
        return "comma", round(cadence.comma_pause_ms * local_tempo_ratio, 1)
    if any(ch in p for ch in (".", "!", "?")):
        return "period", round(cadence.period_pause_ms * local_tempo_ratio, 1)
    return "period", round(cadence.period_pause_ms * local_tempo_ratio, 1)


def _split_take_on_removed_breaths_or_long_hesitations(
    take: TakeCandidate,
    removed_breaths: List[BreathOrNoiseEvent],
    cadence: CadenceProfile,
    local_tempo_ratio: float,
) -> List[Tuple[float, float, str, str, float]]:
    """
    Within a single selected take, if two words are separated by a removed breath OR
    an unnatural hesitation gap (> 380ms), split the take into clean sub-clips at that
    word boundary and replace the gap with the exact grammatical pause dictated by
    English punctuation rules (e.g., comma pause if after a comma, or natural word gap
    if no punctuation).
    Returns list of (sub_start, sub_end, sub_text, pause_type, pause_after_ms).
    """
    words = take.words
    if not words:
        p_type, p_ms = classify_grammatical_pause(
            take.terminal_punctuation, cadence, local_tempo_ratio
        )
        if take.custom_pause_after_ms is not None:
            p_ms = take.custom_pause_after_ms
        return [(take.trimmed_start, take.trimmed_end, take.text, p_type, p_ms)]

    sub_clips: List[Tuple[float, float, str, str, float]] = []
    chunk_words = [words[0]]

    for i in range(len(words) - 1):
        w_curr = words[i]
        w_next = words[i + 1]
        gap_sec = w_next.start - w_curr.end

        # Check if a removed breath falls between w_curr and w_next
        has_breath = any(
            br.removed and br.end_time > (w_curr.end - 0.02) and br.start_time < (w_next.start + 0.02)
            for br in removed_breaths
        )

        # Also split if there is a comma/semicolon followed by a removed breath or an overly long gap (> 0.45s),
        # or an unpunctuated mid-sentence gap > 0.35s so pacing stays tight and grammatical
        has_punct = bool(w_curr.punctuation_after.strip())
        should_cut_internal = (
            has_breath
            or (not has_punct and gap_sec > 0.32)
            or (has_punct and gap_sec > 0.55)
        )

        if should_cut_internal and w_next.start > w_curr.end:
            c_start = max(take.trimmed_start, chunk_words[0].start - 0.012)
            c_end = min(take.trimmed_end, w_curr.end + 0.015)
            c_text = " ".join(f"{w.word}{w.punctuation_after}" for w in chunk_words).strip()
            p_type, p_ms = classify_grammatical_pause(
                w_curr.punctuation_after, cadence, local_tempo_ratio
            )
            sub_clips.append((round(c_start, 4), round(c_end, 4), c_text, p_type, p_ms))
            chunk_words = [w_next]
        else:
            chunk_words.append(w_next)

    # Final sub-clip of the take
    c_start = max(take.trimmed_start, chunk_words[0].start - 0.012)
    c_end = take.trimmed_end
    c_text = " ".join(f"{w.word}{w.punctuation_after}" for w in chunk_words).strip()
    term_punct = chunk_words[-1].punctuation_after or take.terminal_punctuation or "."
    p_type, p_ms = classify_grammatical_pause(term_punct, cadence, local_tempo_ratio)
    if take.custom_pause_after_ms is not None:
        p_ms = take.custom_pause_after_ms

    sub_clips.append((round(c_start, 4), round(c_end, 4), c_text, p_type, p_ms))
    return sub_clips


def build_timeline_clips(
    take_groups: List[TakeGroup],
    breaths_removed: List[BreathOrNoiseEvent],
    cadence: CadenceProfile,
    crossfade_ms: float = 8.0,
) -> Tuple[List[TimelineEditClip], float]:
    """
    Assemble all selected takes into a continuous TimelineEditClip list with preserved
    source edit points, timeline positions, and English grammatical pauses.
    Returns (timeline_clips, total_output_duration_sec).
    """
    selected_takes: List[TakeCandidate] = []
    for g in take_groups:
        sel = next((t for t in g.takes if t.selected), None)
        if sel is not None:
            selected_takes.append(sel)

    timeline_clips: List[TimelineEditClip] = []
    cursor_sec = 0.0
    clip_counter = 1

    for idx, take in enumerate(selected_takes):
        local_ratio = _local_prose_tempo_ratio(
            selected_takes, idx, cadence.words_per_minute
        )
        sub_Editable = _split_take_on_removed_breaths_or_long_hesitations(
            take, breaths_removed, cadence, local_ratio
        )

        for s_idx, (src_s, src_e, txt, p_type, p_ms) in enumerate(sub_Editable):
            dur = max(0.01, src_e - src_s)
            # For the very last clip in the entire recording, use a gentle 300ms tail
            is_very_last = (idx == len(selected_takes) - 1) and (s_idx == len(sub_Editable) - 1)
            if is_very_last and take.custom_pause_after_ms is None:
                p_ms = 300.0
                p_type = "tail"

            tl_start = cursor_sec
            tl_end = cursor_sec + dur
            timeline_clips.append(
                TimelineEditClip(
                    clip_id=f"clip_{clip_counter}",
                    take_id=take.id,
                    take_group_id=take.take_group_id,
                    take_number=take.take_number,
                    source_start=round(src_s, 4),
                    source_end=round(src_e, 4),
                    timeline_start=round(tl_start, 4),
                    timeline_end=round(tl_end, 4),
                    pause_after_ms=round(p_ms, 1),
                    pause_type=p_type,
                    fade_in_ms=round(crossfade_ms, 1),
                    fade_out_ms=round(crossfade_ms, 1),
                    text=txt,
                )
            )
            clip_counter += 1
            cursor_sec = tl_end + (p_ms / 1000.0)

    return timeline_clips, round(cursor_sec, 4)
