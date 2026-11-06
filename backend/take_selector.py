"""AI Take Grouping, Disfluency Detection, Talkback Directive Resolution, and Best-Take Selection."""

from __future__ import annotations

import difflib
import json
import math
import re
from typing import List, Optional, Tuple
import urllib.request

import numpy as np

from backend.models import (
    ProcessingSettings,
    TakeCandidate,
    TakeGroup,
    TalkbackInterval,
    WordToken,
)

_DISFLUENCY_WORDS = {
    "um",
    "uh",
    "er",
    "ah",
    "hmm",
    "oops",
    "sorry",
    "wait",
    "whoops",
    "dang",
    "shoot",
}


def _normalize_word(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower()).strip()


def _normalize_phrase(words: List[WordToken]) -> List[str]:
    out = []
    for w in words:
        nw = _normalize_word(w.word)
        if nw and nw not in _DISFLUENCY_WORDS:
            out.append(nw)
    return out


def _utterance_similarity(words_a: List[WordToken], words_b: List[WordToken]) -> float:
    """
    Compute semantic/lexical similarity between two utterances, accounting for:
      - Full-sentence retakes (high SequenceMatcher & Jaccard similarity)
      - Aborted false starts (where one utterance is a prefix of the other)
      - Pick-up takes (where one utterance repeats the second half of the sentence)
    """
    norm_a = _normalize_phrase(words_a)
    norm_b = _normalize_phrase(words_b)
    if not norm_a or not norm_b:
        return 0.0

    seq_ratio = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()

    set_a, set_b = set(norm_a), set(norm_b)
    jaccard = len(set_a & set_b) / max(1, len(set_a | set_b))

    # Prefix match (false start detection): e.g. ["welcome", "to", "alpine"] vs ["welcome", "to", "alpine", "recording", "station"]
    shorter, longer = (norm_a, norm_b) if len(norm_a) <= len(norm_b) else (norm_b, norm_a)
    prefix_score = 0.0
    if len(shorter) >= 2:
        prefix_len = min(len(shorter), len(longer))
        matches = sum(1 for i in range(prefix_len) if shorter[i] == longer[i])
        if matches / len(shorter) >= 0.70:
            prefix_score = 0.78

    # Suffix match (pick-up take detection)
    suffix_score = 0.0
    if len(shorter) >= 3:
        matches = sum(1 for i in range(1, len(shorter) + 1) if shorter[-i] == longer[-i])
        if matches / len(shorter) >= 0.75:
            suffix_score = 0.72

    return max(seq_ratio, jaccard * 0.95, prefix_score, suffix_score)


def _has_talkback_between(
    t_end: float,
    t_next_start: float,
    talkback_intervals: List[TalkbackInterval],
) -> List[TalkbackInterval]:
    """Return any talkback intervals occurring between two timestamps."""
    found = []
    for tb in talkback_intervals:
        # Check if talkback midpoint falls between t_end - 0.15 and t_next_start + 0.15
        tb_mid = 0.5 * (tb.start_time + tb.end_time)
        if (t_end - 0.15) <= tb_mid <= (t_next_start + 0.15):
            found.append(tb)
    return found


def segment_words_into_utterances(
    words: List[WordToken],
    talkback_intervals: List[TalkbackInterval],
) -> List[List[WordToken]]:
    """
    Partition word tokens into discrete spoken utterances (candidate takes).
    Splits occur when:
      1. A Studio Mode talkback interval occurs between two words.
      2. A sentence terminal (`.`, `!`, `?`) is followed by a pause >= 0.22s.
      3. An inter-word pause >= 0.65s occurs (typical retake reset pause).
      4. The speaker restarts the same opening 2+ words mid-stream after a pause >= 0.15s.
    """
    if not words:
        return []

    sorted_words = sorted(words, key=lambda w: w.start)
    utterances: List[List[WordToken]] = []
    current: List[WordToken] = [sorted_words[0]]

    for idx in range(1, len(sorted_words)):
        prev_w = sorted_words[idx - 1]
        curr_w = sorted_words[idx]
        gap = max(0.0, curr_w.start - prev_w.end)

        tbs = _has_talkback_between(prev_w.end, curr_w.start, talkback_intervals)
        is_terminal_punct = any(p in prev_w.punctuation_after for p in (".", "!", "?"))
        is_dash_abort = any(p in prev_w.punctuation_after for p in ("—", "-"))

        # Check if speaker abruptly restarted the opening words of `current`
        restarted_prefix = False
        if len(current) >= 2 and idx + 1 < len(sorted_words) and gap >= 0.12:
            first_two = [_normalize_word(current[0].word), _normalize_word(current[1].word)]
            next_two = [_normalize_word(curr_w.word), _normalize_word(sorted_words[idx + 1].word)]
            if first_two[0] and first_two == next_two:
                restarted_prefix = True

        should_split = (
            bool(tbs)
            or gap >= 0.65
            or (is_terminal_punct and gap >= 0.20)
            or (is_dash_abort and gap >= 0.15)
            or restarted_prefix
        )

        if should_split:
            utterances.append(current)
            current = [curr_w]
        else:
            current.append(curr_w)

    if current:
        utterances.append(current)

    return utterances


def _score_acoustic_slice(
    vo_signal: np.ndarray,
    sample_rate: int,
    start_sec: float,
    end_sec: float,
) -> Tuple[float, List[str]]:
    """
    Evaluate acoustic quality of a take on a 0..25 scale.
    Checks peak clipping, RMS level consistency, and signal-to-noise quality.
    """
    issues: List[str] = []
    s_idx = max(0, int(start_sec * sample_rate))
    e_idx = min(len(vo_signal), int(end_sec * sample_rate))
    if e_idx <= s_idx or sample_rate <= 0:
        return 15.0, issues

    clip = vo_signal[s_idx:e_idx]
    peak = float(np.max(np.abs(clip)))
    rms = float(np.sqrt(np.mean(clip * clip) + 1e-18))
    rms_db = 20.0 * math.log10(max(rms, 1e-9))

    score = 23.5

    # Check clipping
    clipped_samples = int(np.sum(np.abs(clip) >= 0.985))
    if clipped_samples > 3 or peak >= 0.995:
        score -= 9.5
        issues.append("audio clipping detected")
    elif peak >= 0.95:
        score -= 2.5

    # Check level health
    if rms_db < -42.0:
        score -= 7.0
        issues.append("low vocal level")
    elif rms_db > -3.0:
        score -= 4.0

    return round(max(0.0, min(25.0, score)), 2), issues


def _detect_disfluencies(words: List[WordToken]) -> Tuple[int, List[str]]:
    """Count filler words, stumbles, or immediate word stutters."""
    notes: List[str] = []
    penalty_count = 0
    norm_seq = [_normalize_word(w.word) for w in words]

    for w_raw, nw in zip(words, norm_seq):
        if nw in _DISFLUENCY_WORDS:
            penalty_count += 1
            notes.append(f"filler '{w_raw.word}'")
        if w_raw.word.endswith("-") or "—" in w_raw.punctuation_after:
            penalty_count += 1
            notes.append(f"cut-off word '{w_raw.word}'")

    # Immediate word repetition (stutter: "the the", "we we")
    for i in range(1, len(norm_seq)):
        if norm_seq[i] and norm_seq[i] == norm_seq[i - 1] and norm_seq[i] not in ("very", "had", "that"):
            penalty_count += 1
            notes.append(f"stutter '{norm_seq[i]}'")

    return penalty_count, notes


def group_and_select_takes(
    words: List[WordToken],
    vo_signal: np.ndarray,
    sample_rate: int,
    talkback_intervals: List[TalkbackInterval],
    settings: ProcessingSettings,
) -> List[TakeGroup]:
    """
    Group utterances into TakeGroups (handling bad takes, false starts, and duplicate takes),
    apply Studio Mode talkback director hints, and select the best take for each group.
    """
    utterances = segment_words_into_utterances(words, talkback_intervals)
    if not utterances:
        return []

    # Group consecutive similar utterances into buckets
    raw_groups: List[List[List[WordToken]]] = [[utterances[0]]]

    for u_idx in range(1, len(utterances)):
        curr_u = utterances[u_idx]
        prev_group = raw_groups[-1]

        # Compare curr_u against all takes in prev_group
        max_sim = max(_utterance_similarity(curr_u, prev_u) for prev_u in prev_group)

        # Check if there was a Studio Mode talkback interval with a "retake" directive between prev_group[-1] and curr_u
        last_u = prev_group[-1]
        tbs_between = _has_talkback_between(last_u[-1].end, curr_u[0].start, talkback_intervals)
        has_retake_tb = any(tb.directive_type == "retake" for tb in tbs_between)

        if max_sim >= 0.46 or (has_retake_tb and max_sim >= 0.25):
            prev_group.append(curr_u)
        else:
            raw_groups.append([curr_u])

    take_groups: List[TakeGroup] = []

    for g_idx, u_list in enumerate(raw_groups, start=1):
        group_id = f"tg_{g_idx}"
        # Determine canonical text (longest clean take in the group)
        longest_u = max(u_list, key=lambda u: len(_normalize_phrase(u)))
        canonical_text = " ".join(
            f"{w.word}{w.punctuation_after}" for w in longest_u
        ).strip()
        max_norm_words = max(1, len(_normalize_phrase(longest_u)))

        # Collect talkback notes associated with this group (occurring inside the group or right after the last take before the next group)
        group_start_t = u_list[0][0].start
        group_end_t = u_list[-1][-1].end
        next_group_start_t = (
            raw_groups[g_idx][0][0].start if g_idx < len(raw_groups) else group_end_t + 30.0
        )

        group_tbs: List[TalkbackInterval] = [
            tb
            for tb in talkback_intervals
            if (group_start_t - 0.2) <= tb.start_time <= (next_group_start_t + 0.1)
        ]
        tb_note_strings = [
            f"[{tb.start_time:.1f}s] {tb.transcript} ({tb.directive_type})"
            for tb in group_tbs
            if tb.transcript
        ]

        candidates: List[TakeCandidate] = []
        for t_num, u_words in enumerate(u_list, start=1):
            t_id = f"{group_id}_take_{t_num}"
            t_start = u_words[0].start
            t_end = u_words[-1].end
            # Trim 15ms padding around word boundaries for clean audio cuts
            trimmed_start = max(0.0, t_start - 0.015)
            trimmed_end = min(len(vo_signal) / sample_rate if sample_rate > 0 else t_end, t_end + 0.025)

            take_text = " ".join(f"{w.word}{w.punctuation_after}" for w in u_words).strip()
            term_punct = u_words[-1].punctuation_after or "."

            # 1. Completeness score (0..35)
            norm_len = len(_normalize_phrase(u_words))
            coverage = min(1.0, norm_len / max_norm_words)
            completeness = round(35.0 * (coverage ** 1.3), 2)
            disf_count, disf_notes = _detect_disfluencies(u_words)
            if disf_count > 0:
                completeness = max(0.0, completeness - 8.5 * disf_count)

            # 2. Confidence score (0..25)
            mean_prob = float(np.mean([w.probability for w in u_words])) if u_words else 0.85
            confidence = round(25.0 * mean_prob, 2)
            if disf_count > 0:
                confidence = max(0.0, confidence - 4.0 * disf_count)

            # 3. Acoustic score (0..25)
            acoustic, acoustic_issues = _score_acoustic_slice(
                vo_signal, sample_rate, trimmed_start, trimmed_end
            )

            # 4. Director / Talkback & Recency Bonus (-25..+30)
            director_bonus = round(min(8.0, t_num * 2.0), 2)  # slight natural preference for later complete takes
            director_reason = ""

            # Check if a talkback immediately after this take asked for a retake
            if t_num < len(u_list):
                next_t_start = u_list[t_num][0].start
                tbs_after = _has_talkback_between(t_end, next_t_start, group_tbs)
                if any(tb.directive_type == "retake" for tb in tbs_after):
                    director_bonus -= 14.0
                    director_reason = "Director requested retake on talkback track"

            # Check if any talkback in this group explicitly requested a take number ("keep take 1", "take 2 was best")
            for tb in group_tbs:
                if tb.directive_type == "keep_specific" and tb.target_take_number is not None:
                    if tb.target_take_number == t_num:
                        director_bonus += 26.0
                        director_reason = f"Explicitly chosen by director talkback ('{tb.transcript}')"
                    else:
                        director_bonus -= 8.0

            total_score = round(
                completeness + confidence + acoustic + director_bonus, 2
            )

            # Build diagnostic reason if rejected
            reasons: List[str] = []
            if coverage < 0.72:
                reasons.append(f"Incomplete false start ({norm_len}/{max_norm_words} words)")
            if disf_notes:
                reasons.append("Disfluency (" + ", ".join(disf_notes[:2]) + ")")
            if acoustic_issues:
                reasons.append(", ".join(acoustic_issues))
            if director_reason and director_bonus < 0:
                reasons.append(director_reason)

            candidates.append(
                TakeCandidate(
                    id=t_id,
                    take_group_id=group_id,
                    take_number=t_num,
                    start_time=round(t_start, 4),
                    end_time=round(t_end, 4),
                    trimmed_start=round(trimmed_start, 4),
                    trimmed_end=round(trimmed_end, 4),
                    text=take_text,
                    words=u_words,
                    confidence_score=confidence,
                    acoustic_score=acoustic,
                    completeness_score=completeness,
                    director_bonus=round(director_bonus, 2),
                    total_score=total_score,
                    selected=False,
                    rejection_reason="; ".join(reasons) if reasons else "Alternate take (lower composite score)",
                    terminal_punctuation=term_punct,
                )
            )

        # Select winning take
        best_take = max(candidates, key=lambda c: c.total_score)
        best_take.selected = True
        best_take.rejection_reason = ""

        # Update rejection reasons on non-selected takes with score comparison if generic
        for c in candidates:
            if not c.selected and c.rejection_reason == "Alternate take (lower composite score)":
                c.rejection_reason = (
                    f"Duplicate take — Take {best_take.take_number} scored higher "
                    f"({best_take.total_score:.1f} vs {c.total_score:.1f})"
                )

        take_groups.append(
            TakeGroup(
                id=group_id,
                group_index=g_idx,
                canonical_text=canonical_text,
                takes=candidates,
                selected_take_id=best_take.id,
                talkback_notes=tb_note_strings,
            )
        )

    # Optional LLM refinement if user provided an API key
    if settings.llm_provider in ("openai", "gemini") and settings.llm_api_key.strip():
        _apply_optional_llm_take_refinement(take_groups, settings)

    return take_groups


def _apply_optional_llm_take_refinement(
    take_groups: List[TakeGroup],
    settings: ProcessingSettings,
) -> None:
    """
    If an OpenAI or Gemini API key is configured, query the LLM on multi-take groups
    to verify the best take selection considering script and talkback notes.
    Falls back silently to the local acoustic/talkback selection on any network error.
    """
    multi_groups = [g for g in take_groups if len(g.takes) > 1]
    if not multi_groups:
        return

    prompt_items = []
    for g in multi_groups:
        prompt_items.append(
            {
                "group_id": g.id,
                "talkback_notes": g.talkback_notes,
                "takes": [
                    {
                        "take_id": t.id,
                        "take_number": t.take_number,
                        "text": t.text,
                        "local_score": t.total_score,
                    }
                    for t in g.takes
                ],
            }
        )

    sys_msg = (
        "You are an expert voiceover audio producer. Given candidate takes and studio talkback notes, "
        "return JSON mapping each group_id to the best take_id."
    )
    user_msg = json.dumps({"reference_script": settings.reference_script, "groups": prompt_items})

    try:
        if settings.llm_provider == "openai":
            req_body = json.dumps(
                {
                    "model": "gpt-4o-mini",
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": sys_msg},
                        {"role": "user", "content": user_msg},
                    ],
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=req_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings.llm_api_key.strip()}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                mapping = json.loads(data["choices"][0]["message"]["content"])
                for g in multi_groups:
                    chosen_id = mapping.get(g.id)
                    if chosen_id and any(t.id == chosen_id for t in g.takes):
                        g.selected_take_id = chosen_id
                        for t in g.takes:
                            t.selected = t.id == chosen_id
    except Exception:
        # Preserve local selection if offline or key invalid
        pass
