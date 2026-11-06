"""Adobe Audition Multitrack (.sesx) XML, CMX 3600 EDL (.edl), and Audition Marker CSV (.csv) Exporters."""

from __future__ import annotations

import csv
import io
import os
import shutil
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path
from typing import Tuple

from backend.models import EditSession, ExportSettings


def _sec_to_samples(sec: float, sample_rate: int) -> int:
    return max(0, int(round(sec * sample_rate)))


def _sec_to_smpte(sec: float, fps: int = 30) -> str:
    """Format seconds as SMPTE timecode HH:MM:SS:FF."""
    total_frames = max(0, int(round(sec * fps)))
    ff = total_frames % fps
    total_seconds = total_frames // fps
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def export_audition_sesx(
    session: EditSession,
    export_settings: ExportSettings,
    output_dir: str,
) -> Tuple[str, str]:
    """
    Generate an Adobe Audition Multitrack Session (.sesx) file along with the isolated
    clean voiceover source WAV file in `output_dir` so all edit points are preserved
    non-destructively and zero talkback audio can ever enter the session.

    Returns (sesx_path, companion_vo_wav_path).
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_base = "".join(
        c if (c.isalnum() or c in ("-", "_", " ")) else "_"
        for c in (export_settings.output_name or "Rough_Cut_Master")
    ).strip() or "Rough_Cut_Master"

    companion_wav_name = f"{safe_base}_Clean_VO_Source.wav"
    companion_wav_path = os.path.abspath(os.path.join(output_dir, companion_wav_name))
    if os.path.abspath(session.clean_vo_path) != companion_wav_path:
        shutil.copy2(session.clean_vo_path, companion_wav_path)

    sr = session.input_sample_rate
    total_timeline_samples = _sec_to_samples(max(session.output_duration_sec, session.input_duration_sec) + 2.0, sr)
    source_total_samples = _sec_to_samples(session.input_duration_sec, sr)

    bits = "24"
    if "16" in session.input_subtype:
        bits = "16"
    elif "FLOAT" in session.input_subtype or "32" in session.input_subtype:
        bits = "32"

    sesx = ET.Element("sesx", {"version": "1.9"})

    session_el = ET.SubElement(
        sesx,
        "session",
        {
            "appBuild": "24.0.0.46",
            "appVersion": "24.0",
            "audioChannelType": "stereo",
            "bitsPerSample": bits,
            "duration": str(total_timeline_samples),
            "sampleRate": str(sr),
        },
    )

    name_el = ET.SubElement(session_el, "name")
    name_el.text = f"{safe_base}.sesx"

    tracks_el = ET.SubElement(session_el, "tracks")

    # -------------------------------------------------------------------------
    # Track 1: Primary Selected Voiceover Rough Cut (Unmuted)
    # -------------------------------------------------------------------------
    track1 = ET.SubElement(
        tracks_el,
        "audioTrack",
        {
            "automationLaneOpenState": "false",
            "id": "10001",
            "index": "1",
            "select": "true",
            "visible": "true",
        },
    )
    t1_params = ET.SubElement(
        track1,
        "trackParameters",
        {
            "trackHeight": "130",
            "trackHue": "205",
            "trackMinimized": "false",
        },
    )
    t1_name = ET.SubElement(t1_params, "name")
    t1_name.text = "VO Rough Cut (Selected Takes)"

    t1_audio_params = ET.SubElement(
        track1,
        "trackAudioParameters",
        {
            "audioChannelType": "mono",
            "automationMode": "1",
            "monitoring": "false",
            "recordArmed": "false",
            "solo": "false",
            "soloSafe": "false",
        },
    )
    ET.SubElement(t1_audio_params, "trackOutput", {"outputID": "10000", "type": "trackID"})
    ET.SubElement(t1_audio_params, "trackInput", {"inputID": "1"})
    t1_comp = ET.SubElement(
        t1_audio_params,
        "component",
        {
            "componentID": "Audition.Fader",
            "id": "trackFader",
            "name": "volume",
            "powered": "true",
        },
    )
    ET.SubElement(t1_comp, "parameter", {"index": "0", "name": "volume", " parameterValue": "1"})
    ET.SubElement(t1_comp, "parameter", {"index": "1", "name": "static gain", "parameterValue": "1"})
    t1_mute = ET.SubElement(
        t1_audio_params,
        "component",
        {"componentID": "Audition.Mute", "id": "trackMute", "name": "Mute", "powered": "true"},
    )
    ET.SubElement(t1_mute, "parameter", {"index": "0", "name": "mute", "parameterValue": "0"})

    for idx, clip in enumerate(session.timeline_clips, start=1):
        start_pt = _sec_to_samples(clip.timeline_start, sr)
        end_pt = _sec_to_samples(clip.timeline_end, sr)
        src_in = _sec_to_samples(clip.source_start, sr)
        src_out = _sec_to_samples(clip.source_end, sr)
        if end_pt <= start_pt or src_out <= src_in:
            continue

        clip_el = ET.SubElement(
            track1,
            "audioClip",
            {
                "clipAutoCrossfade": "true",
                "crossFadeHeadClipID": "-1",
                "crossFadeTailClipID": "-1",
                "endPoint": str(end_pt),
                "fileID": "1",
                "Hue": "-1",
                "id": str(idx),
                "lockedInTime": "false",
                "looped": "false",
                " mute": "false",
                "name": f"Take {clip.take_number}: {clip.text[:48]}",
                "offline": "false",
                "select": "false",
                "sourceInPoint": str(src_in),
                "sourceOutPoint": str(src_out),
                "startPoint": str(start_pt),
                "zOrder": str(idx),
            },
        )
        fade_in_samples = _sec_to_samples(clip.fade_in_ms / 1000.0, sr)
        fade_out_samples = _sec_to_samples(clip.fade_out_ms / 1000.0, sr)
        ET.SubElement(
            clip_el,
            "fadeIn",
            {
                "crossFadeLinkType": "linkedAsymmetric",
                "endPoint": str(min(end_pt - start_pt, fade_in_samples)),
                "shape": "0",
                "startPoint": "0",
                "type": "cosine",
            },
        )
        ET.SubElement(
            clip_el,
            "fadeOut",
            {
                "crossFadeLinkType": "linkedAsymmetric",
                "endPoint": str(end_pt - start_pt),
                "shape": "0",
                "startPoint": str(max(0, (end_pt - start_pt) - fade_out_samples)),
                "type": "cosine",
            },
        )

    # -------------------------------------------------------------------------
    # Track 2: Rejected / Alternate Takes Reference Track (Muted by default)
    # -------------------------------------------------------------------------
    track_index_counter = 2
    if export_settings.include_rejected_track_in_sesx:
        track2 = ET.SubElement(
            tracks_el,
            "audioTrack",
            {
                "automationLaneOpenState": "false",
                "id": "10002",
                "index": str(track_index_counter),
                "select": "false",
                "visible": "true",
            },
        )
        track_index_counter += 1
        t2_params = ET.SubElement(
            track2,
            "trackParameters",
            {"trackHeight": "95", "trackHue": "35", "trackMinimized": "false"},
        )
        t2_name = ET.SubElement(t2_params, "name")
        t2_name.text = "VO Rejected & Alternate Takes (Muted)"

        t2_audio_params = ET.SubElement(
            track2,
            "trackAudioParameters",
            {
                "audioChannelType": "mono",
                "automationMode": "1",
                "monitoring": "false",
                "recordArmed": "false",
                "solo": "false",
                "soloSafe": "false",
            },
        )
        ET.SubElement(t2_audio_params, "trackOutput", {"outputID": "10000", "type": "trackID"})
        t2_mute = ET.SubElement(
            t2_audio_params,
            "component",
            {"componentID": "Audition.Mute", "id": "trackMute", "name": "Mute", "powered": "true"},
        )
        ET.SubElement(t2_mute, "parameter", {"index": "0", "name": "mute", "parameterValue": "1"})

        alt_id = 1000
        for g in session.take_groups:
            for t in g.takes:
                if t.selected:
                    continue
                s_pt = _sec_to_samples(t.trimmed_start, sr)
                e_pt = _sec_to_samples(t.trimmed_end, sr)
                if e_pt <= s_pt:
                    continue
                ET.SubElement(
                    track2,
                    "audioClip",
                    {
                        "clipAutoCrossfade": "true",
                        "endPoint": str(e_pt),
                        "fileID": "1",
                        "id": str(alt_id),
                        "lockedInTime": "false",
                        "looped": "false",
                        "mute": "true",
                        "name": f"[REJECTED Group {g.group_index} T{t.take_number}] {t.text[:36]}",
                        "offline": "false",
                        "select": "false",
                        "sourceInPoint": str(s_pt),
                        "sourceOutPoint": str(e_pt),
                        "startPoint": str(s_pt),
                        "zOrder": str(alt_id),
                    },
                )
                alt_id += 1

    # -------------------------------------------------------------------------
    # Track 3: Removed Breaths & Mouth Noises Track (Muted by default)
    # -------------------------------------------------------------------------
    if export_settings.include_breaths_track_in_sesx and session.breaths_removed:
        track3 = ET.SubElement(
            tracks_el,
            "audioTrack",
            {
                "automationLaneOpenState": "false",
                "id": "10003",
                "index": str(track_index_counter),
                "select": "false",
                "visible": "true",
            },
        )
        t3_params = ET.SubElement(
            track3,
            "trackParameters",
            {"trackHeight": "80", "trackHue": "300", "trackMinimized": "false"},
        )
        t3_name = ET.SubElement(t3_params, "name")
        t3_name.text = "Excised Breaths & Mouth Noises (Muted)"

        t3_audio_params = ET.SubElement(
            track3,
            "trackAudioParameters",
            {
                "audioChannelType": "mono",
                "automationMode": "1",
                "monitoring": "false",
                "recordArmed": "false",
                "solo": "false",
                "soloSafe": "false",
            },
        )
        ET.SubElement(t3_audio_params, "trackOutput", {"outputID": "10000", "type": "trackID"})
        t3_mute = ET.SubElement(
            t3_audio_params,
            "component",
            {"componentID": "Audition.Mute", "id": "trackMute", "name": "Mute", "powered": "true"},
        )
        ET.SubElement(t3_mute, "parameter", {"index": "0", "name": "mute", "parameterValue": "1"})

        for b_idx, br in enumerate(session.breaths_removed, start=2000):
            s_pt = _sec_to_samples(br.start_time, sr)
            e_pt = _sec_to_samples(br.end_time, sr)
            if e_pt <= s_pt:
                continue
            ET.SubElement(
                track3,
                "audioClip",
                {
                    "clipAutoCrossfade": "true",
                    "endPoint": str(e_pt),
                    "fileID": "1",
                    "id": str(b_idx),
                    "lockedInTime": "false",
                    "looped": "false",
                    "mute": "true",
                    "name": f"[{br.kind.upper()}] {br.rms_db} dBFS",
                    "offline": "false",
                    "select": "false",
                    "sourceInPoint": str(s_pt),
                    "sourceOutPoint": str(e_pt),
                    "startPoint": str(s_pt),
                    "zOrder": str(b_idx),
                },
            )

    # -------------------------------------------------------------------------
    # Master Track
    # -------------------------------------------------------------------------
    master_track = ET.SubElement(
        tracks_el,
        "masterTrack",
        {
            "automationLaneOpenState": "false",
            "id": "10000",
            "index": str(track_index_counter + 1),
            "select": "false",
            "visible": "true",
        },
    )
    m_params = ET.SubElement(
        master_track,
        "trackParameters",
        {"trackHeight": "90", "trackHue": "-1", "trackMinimized": "false"},
    )
    m_name = ET.SubElement(m_params, "name")
    m_name.text = "Master"

    # -------------------------------------------------------------------------
    # Markers (Edit Points, Grammatical Pauses, & Talkback Notes)
    # -------------------------------------------------------------------------
    markers_el = ET.SubElement(session_el, "markers")
    m_counter = 1
    for clip in session.timeline_clips:
        s_pt = _sec_to_samples(clip.timeline_start, sr)
        e_pt = _sec_to_samples(clip.timeline_end, sr)
        marker = ET.SubElement(
            markers_el,
            "marker",
            {
                "duration": str(max(0, e_pt - s_pt)),
                "id": str(m_counter),
                "name": f"Cut {m_counter} [Take {clip.take_number} | {clip.pause_type} +{int(clip.pause_after_ms)}ms]: {clip.text[:40]}",
                "startPoint": str(s_pt),
                "type": "cue",
            },
        )
        m_comment = ET.SubElement(marker, "comment")
        m_comment.text = (
            f"Source: {clip.source_start:.3f}s - {clip.source_end:.3f}s | "
            f"Grammatical Pause After: {clip.pause_after_ms:.0f}ms ({clip.pause_type})"
        )
        m_counter += 1

    # Add talkback note transcript markers on the source reference timeline
    for tb in session.talkback_intervals:
        if tb.transcript:
            s_pt = _sec_to_samples(tb.start_time, sr)
            e_pt = _sec_to_samples(tb.end_time, sr)
            marker = ET.SubElement(
                markers_el,
                "marker",
                {
                    "duration": str(max(0, e_pt - s_pt)),
                    "id": str(m_counter),
                    "name": f"[TALKBACK NOTE - {tb.directive_type.upper()}] {tb.transcript[:50]}",
                    "startPoint": str(s_pt),
                    "type": "comment",
                },
            )
            m_comment = ET.SubElement(marker, "comment")
            m_comment.text = f"Talkback ({tb.start_time:.2f}s-{tb.end_time:.2f}s): {tb.transcript}"
            m_counter += 1

    # -------------------------------------------------------------------------
    # Files Reference Section (Points strictly to Clean Isolated VO Source)
    # -------------------------------------------------------------------------
    files_el = ET.SubElement(sesx, "files")
    ET.SubElement(
        files_el,
        "file",
        {
            "absolutePath": companion_wav_path.replace("\\", "/"),
            "id": "1",
            "mediaHandler": "AmioWav",
            "relativePath": companion_wav_name,
            "sourceDuration": str(source_total_samples),
        },
    )

    raw_xml = ET.tostring(sesx, encoding="utf-8")
    parsed_xml = minidom.parseString(raw_xml)
    pretty_xml = parsed_xml.toprettyxml(indent="  ", encoding="UTF-8").decode("utf-8")

    sesx_path = os.path.join(output_dir, f"{safe_base}.sesx")
    with open(sesx_path, "w", encoding="utf-8") as f:
        f.write(pretty_xml)

    return sesx_path, companion_wav_path


def export_edl_and_markers_csv(
    session: EditSession,
    export_settings: ExportSettings,
    output_dir: str,
) -> Tuple[str, str]:
    """
    Export a CMX 3600 EDL (.edl) and an Adobe Audition Cue Marker CSV (.csv)
    preserving all edit points, source timestamps, and grammatical pause notes.
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_base = "".join(
        c if (c.isalnum() or c in ("-", "_", " ")) else "_"
        for c in (export_settings.output_name or "Rough_Cut_Master")
    ).strip() or "Rough_Cut_Master"

    edl_path = os.path.join(output_dir, f"{safe_base}.edl")
    csv_path = os.path.join(output_dir, f"{safe_base}_Audition_Markers.csv")

    # 1. Write CMX 3600 EDL
    lines = [
        f"TITLE: {safe_base}",
        "FCM: NON-DROP FRAME",
        "",
    ]
    for idx, clip in enumerate(session.timeline_clips, start=1):
        src_in = _sec_to_smpte(clip.source_start)
        src_out = _sec_to_smpte(clip.source_end)
        rec_in = _sec_to_smpte(clip.timeline_start)
        rec_out = _sec_to_smpte(clip.timeline_end)
        lines.append(f"{idx:03d}  AX       AA/V  C        {src_in} {src_out} {rec_in} {rec_out}")
        lines.append(f"* FROM CLIP NAME: {session.source_filename} (Take {clip.take_number})")
        lines.append(f"* COMMENT: {clip.text} [Pause After: {clip.pause_after_ms:.0f}ms ({clip.pause_type})]")
        lines.append("")

    with open(edl_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # 2. Write Adobe Audition Marker CSV (Tab-delimited or standard CSV header: Name, Start, Duration, Time Format, Type, Description)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["Name", "Start", "Duration", "Time Format", "Type", "Description"])
        for idx, clip in enumerate(session.timeline_clips, start=1):
            dur = max(0.0, clip.timeline_end - clip.timeline_start)
            writer.writerow(
                [
                    f"Clip {idx} (Take {clip.take_number})",
                    f"{clip.timeline_start:.3f}",
                    f"{dur:.3f}",
                    "decimal",
                    "Cue",
                    f"{clip.text} | Source {clip.source_start:.2f}s-{clip.source_end:.2f}s | Pause +{clip.pause_after_ms:.0f}ms ({clip.pause_type})",
                ]
            )

    return edl_path, csv_path
