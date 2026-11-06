"""Comprehensive unit and integration tests for Alpine Recording Station Rough Cut."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import soundfile as sf
from backend.audio_engine import (
    detect_talkback_tones,
    isolate_voiceover_channel,
    load_audio_file,
    pair_talkback_tones,
    render_edited_audio,
)
from backend.audition_exporter import export_audition_sesx, export_edl_and_markers_csv
from backend.demo_generator import generate_normal_mode_sample, generate_studio_mode_sample
from backend.main import (
    export_audition_bundle_endpoint,
    export_master_audio_endpoint,
    get_health,
    get_session_details,
    patch_session_edits,
    run_rough_cut_pipeline,
    serve_index,
)
from backend.models import (
    AudioFormatOption,
    BitDepthOption,
    ChannelMapOption,
    ExportSettings,
    ProcessingSettings,
    RecordingMode,
    SessionPatchRequest,
)


def test_studio_mode_tone_detection_and_talkback_isolation(tmp_path: Path):
    """
    Verify Studio Mode:
      1. Detects start & end audio tones on the Right (talkback) channel.
      2. Pairs them into talkback intervals.
      3. Completely isolates the Left (voiceover) channel and zeroes out talkback intervals
         so talkback audio is NEVER included in output audio.
    """
    studio_wav = generate_studio_mode_sample(str(tmp_path))
    data_2d, sr, channels, subtype, fmt = load_audio_file(studio_wav)
    assert channels == 2

    right_ch = data_2d[:, 1]
    tones = detect_talkback_tones(right_ch, sr)
    assert len(tones) == 4, f"Expected 4 talkback boundary tones (2 pairs), got {len(tones)}"
    for t in tones:
        assert 920.0 <= t.dominant_freq_hz <= 1080.0

    intervals = pair_talkback_tones(tones, right_ch, sr)
    assert len(intervals) == 2

    clean_vo = isolate_voiceover_channel(data_2d, sr, RecordingMode.STUDIO, intervals)
    # Verify that during talkback intervals, clean_vo is completely silent (0.0)
    for tb in intervals:
        s_idx = int((tb.start_time + 0.02) * sr)
        e_idx = int((tb.end_time - 0.02) * sr)
        max_amp_in_tb = float(np.max(np.abs(clean_vo[s_idx:e_idx])))
        assert max_amp_in_tb == 0.0, f"Expected 0.0 VO bleed during talkback, got {max_amp_in_tb}"


def test_full_studio_mode_pipeline_take_selection_and_pacing(tmp_path: Path):
    """
    Verify full Studio Mode pipeline:
      - Selects complete Take 2 over aborted false-start Take 1 in Group 1
      - Selects clean Take 1 over disfluent Take 2 in Group 2 (honoring talkback directive 'keep the first take')
      - Detects and removes inter-word inhalation breath
      - Enforces English grammatical pause hierarchy: word < comma < semicolon < period
    """
    studio_wav = generate_studio_mode_sample(str(tmp_path))
    settings = ProcessingSettings(
        mode=RecordingMode.STUDIO,
        output_name="Studio_Test_Out",
        remove_breaths=True,
        comma_pause_multiplier=3.2,
        period_pause_multiplier=6.8,
    )
    progress_log = []
    session = run_rough_cut_pipeline(
        source_path=studio_wav,
        settings=settings,
        progress_callback=lambda pct, msg: progress_log.append((pct, msg)),
    )

    assert progress_log[-1][0] == 100
    assert len(session.talkback_intervals) == 2
    assert session.talkback_intervals[0].directive_type == "retake"
    assert session.talkback_intervals[1].directive_type == "keep_specific"
    assert session.talkback_intervals[1].target_take_number == 1

    # Verify 3 phrase groups
    assert len(session.take_groups) == 3

    # Group 1: Take 1 is false start ("Welcome to the Alpine—"), Take 2 is complete keeper
    g1 = session.take_groups[0]
    assert len(g1.takes) == 2
    assert not g1.takes[0].selected
    assert g1.takes[1].selected

    # Group 2: Take 1 is clean, Take 2 has 'um' + talkback says keep Take 1
    g2 = session.take_groups[1]
    assert len(g2.takes) == 2
    assert g2.takes[0].selected
    assert not g2.takes[1].selected

    # Group 3: Single clean final sentence
    g3 = session.take_groups[2]
    assert len(g3.takes) == 1
    assert g3.takes[0].selected

    # Verify breath detection found the injected breath
    assert len(session.breaths_removed) >= 1
    assert any(b.removed for b in session.breaths_removed)

    # Verify English grammatical pause hierarchy in CadenceProfile
    cad = session.cadence_profile
    assert cad.median_word_gap_ms < cad.comma_pause_ms < cad.semicolon_pause_ms < cad.period_pause_ms

    # Verify comma pause was inserted where the breath after "Station," was excised
    comma_clips = [c for c in session.timeline_clips if c.pause_type == "comma"]
    assert len(comma_clips) >= 1
    for cc in comma_clips:
        assert cc.pause_after_ms >= 220.0


def test_audio_export_formats_channel_maps_and_audition_sesx(tmp_path: Path):
    """
    Verify:
      1. Default 'match_input' export preserves input format, sample rate, bit depth, and channel count
         while containing ZERO talkback audio.
      2. Custom format conversion (48kHz, 16-bit, Mono, FLAC) works from preserved edit points without re-running STT.
      3. Adobe Audition (.sesx) XML export produces valid multitrack XML with clips, fades, and markers.
    """
    studio_wav = generate_studio_mode_sample(str(tmp_path))
    session = run_rough_cut_pipeline(
        source_path=studio_wav,
        settings=ProcessingSettings(mode=RecordingMode.STUDIO, output_name="Export_Test"),
    )

    # 1. Match Input Export
    exp_match = ExportSettings(
        output_name="Export_Match_Input",
        audio_format=AudioFormatOption.MATCH_INPUT,
        sample_rate=0,
        bit_depth=BitDepthOption.MATCH_INPUT,
        channel_map=ChannelMapOption.MATCH_INPUT,
    )
    out_match_path = render_edited_audio(session, exp_match, str(tmp_path / "out1"))
    info_match = sf.info(out_match_path)
    assert info_match.samplerate == session.input_sample_rate
    assert info_match.channels == session.input_channels
    assert info_match.subtype == session.input_subtype

    # Check that L and R in Studio Mode Match-Input are identical clean VO (no talkback tone at 1000Hz!)
    rendered_2d, _ = sf.read(out_match_path, always_2d=True)
    assert np.allclose(rendered_2d[:, 0], rendered_2d[:, 1], atol=1e-4)

    # 2. Custom Format & Channel Conversion (48kHz, PCM_16, Mono, FLAC)
    exp_custom = ExportSettings(
        output_name="Export_Custom_48k_Mono",
        audio_format=AudioFormatOption.FLAC,
        sample_rate=48000,
        bit_depth=BitDepthOption.PCM_16,
        channel_map=ChannelMapOption.MONO,
    )
    out_custom_path = render_edited_audio(session, exp_custom, str(tmp_path / "out2"))
    info_custom = sf.info(out_custom_path)
    assert Path(out_custom_path).suffix == ".flac"
    assert info_custom.samplerate == 48000
    assert info_custom.channels == 1
    assert info_custom.subtype == "PCM_16"

    # 3. Adobe Audition (.sesx) Multitrack XML + EDL + CSV export
    sesx_path, companion_wav = export_audition_sesx(session, exp_match, str(tmp_path / "audition"))
    edl_path, csv_path = export_edl_and_markers_csv(session, exp_match, str(tmp_path / "audition"))

    assert os.path.exists(sesx_path)
    assert os.path.exists(companion_wav)
    assert os.path.exists(edl_path)
    assert os.path.exists(csv_path)

    tree = ET.parse(sesx_path)
    root = tree.getroot()
    assert root.tag == "sesx"
    audio_tracks = root.findall("./session/tracks/audioTrack")
    assert len(audio_tracks) >= 2  # Active VO track + Muted Rejected Takes track
    active_clips = audio_tracks[0].findall("audioClip")
    assert len(active_clips) == len(session.timeline_clips)


def test_normal_mode_and_fastapi_endpoints(tmp_path: Path):
    """Verify Normal Mode processing and FastAPI non-destructive session patch & export endpoints."""
    health = get_health()
    assert health["status"] == "ok"

    normal_wav = generate_normal_mode_sample(str(tmp_path))
    session = run_rough_cut_pipeline(
        source_path=normal_wav,
        settings=ProcessingSettings(mode=RecordingMode.NORMAL, output_name="Normal_Test"),
    )
    assert len(session.take_groups) == 2
    assert session.take_groups[0].takes[1].selected

    # Test get_session_details
    loaded = get_session_details(session.session_id)
    assert loaded.session_id == session.session_id

    # Test patch_session_edits (swap take selection without re-processing)
    tg1_id = session.take_groups[0].id
    alt_take_id = session.take_groups[0].takes[0].id
    patched = patch_session_edits(
        session.session_id,
        SessionPatchRequest(
            selected_take_ids={tg1_id: alt_take_id},
            comma_pause_multiplier=3.5,
        ),
    )
    assert patched.take_groups[0].selected_take_id == alt_take_id

    # Test export_audition_bundle_endpoint & export_master_audio_endpoint
    aud_resp = export_audition_bundle_endpoint(
        session.session_id,
        ExportSettings(output_name="Normal_Audition_Export"),
    )
    assert os.path.exists(aud_resp.path)
    assert os.path.getsize(aud_resp.path) > 1000

    audio_resp = export_master_audio_endpoint(
        session.session_id,
        ExportSettings(output_name="Normal_Master_Audio"),
    )
    assert os.path.exists(audio_resp.path)

    idx_resp = serve_index()
    assert os.path.exists(idx_resp.path)
