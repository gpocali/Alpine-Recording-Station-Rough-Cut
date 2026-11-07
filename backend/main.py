"""FastAPI Server & AI Rough Cut Pipeline Orchestrator."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional

import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.audio_engine import (
    _find_ffmpeg,
    compute_assembled_output_peaks,
    compute_waveform_peaks,
    detect_breaths_and_noises,
    detect_talkback_tones,
    extract_room_tone_profile,
    isolate_voiceover_channel,
    load_audio_file,
    pair_talkback_tones,
    render_edited_audio,
    render_output_preview,
)
from backend.audition_exporter import export_audition_sesx, export_edl_and_markers_csv
from backend.demo_generator import generate_normal_mode_sample, generate_studio_mode_sample
from backend.models import (
    EditSession,
    ExportSettings,
    OutputPreviewInfo,
    ProcessingSettings,
    RecordingMode,
    SessionPatchRequest,
)
from backend.pacing_engine import analyze_uncut_cadence, build_timeline_clips
from backend.stt_engine import transcribe_voiceover_and_talkback
from backend.take_selector import group_and_select_takes

BASE_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DATA_DIR = BASE_DIR / "workspace_data"
UPLOADS_DIR = WORKSPACE_DATA_DIR / "uploads"
SESSIONS_DIR = WORKSPACE_DATA_DIR / "sessions"
OUTPUTS_DIR = WORKSPACE_DATA_DIR / "outputs"
FRONTEND_DIR = BASE_DIR / "frontend"

for d in (UPLOADS_DIR, SESSIONS_DIR, OUTPUTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# In-memory job tracker & session cache
JOBS: Dict[str, dict] = {}
SESSIONS_CACHE: Dict[str, EditSession] = {}


def save_session(session: EditSession) -> str:
    """Persist EditSession to disk (.ars.json) and in-memory cache."""
    SESSIONS_CACHE[session.session_id] = session
    session_file = SESSIONS_DIR / f"{session.session_id}.ars.json"
    session_file.write_text(session.model_dump_json(indent=2), encoding="utf-8")
    return str(session_file)


def load_session(session_id: str) -> EditSession:
    """Load an EditSession from memory or disk."""
    if session_id in SESSIONS_CACHE:
        return SESSIONS_CACHE[session_id]
    session_file = SESSIONS_DIR / f"{session_id}.ars.json"
    if not session_file.exists():
        raise KeyError(f"Session '{session_id}' not found.")
    data = json.loads(session_file.read_text(encoding="utf-8"))
    sess = EditSession(**data)
    SESSIONS_CACHE[session_id] = sess
    return sess


def recalculate_session_timeline(session: EditSession) -> EditSession:
    """
    Recompute cadence metrics and timeline edit clips from preserved edit points
    without re-running Speech-to-Text or audio analysis.
    """
    cadence = analyze_uncut_cadence(
        take_groups=session.take_groups,
        breaths_removed=session.breaths_removed,
        comma_multiplier=session.settings.comma_pause_multiplier,
        period_multiplier=session.settings.period_pause_multiplier,
        min_word_gap_ms=session.settings.min_word_gap_ms,
        room_tone_rms_db=session.cadence_profile.room_tone_rms_db,
    )
    clips, total_out_sec = build_timeline_clips(
        take_groups=session.take_groups,
        breaths_removed=session.breaths_removed,
        cadence=cadence,
        crossfade_ms=session.settings.crossfade_ms,
    )
    session.cadence_profile = cadence
    session.timeline_clips = clips
    session.output_duration_sec = total_out_sec
    session.waveform_peaks_output = compute_assembled_output_peaks(
        clean_vo_path=session.clean_vo_path,
        timeline_clips=clips,
        breaths_removed=session.breaths_removed,
        num_bins=500,
    )
    save_session(session)
    return session


def run_rough_cut_pipeline(
    source_path: str,
    settings: ProcessingSettings,
    source_filename: Optional[str] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> EditSession:
    """
    Execute the complete AI Voiceover Rough Cut pipeline on `source_path` and return
    a non-destructive `EditSession` with preserved edit points.
    """
    def report(pct: int, msg: str) -> None:
        if progress_callback:
            progress_callback(pct, msg)

    report(8, "Loading audio file and inspecting channel geometry...")
    audio_2d, sr, channels, subtype, fmt = load_audio_file(source_path)
    duration_sec = round(len(audio_2d) / sr, 4) if sr > 0 else 0.0
    fname = source_filename or Path(source_path).name

    tone_markers = []
    talkback_intervals = []
    right_channel = None
    waveform_peaks_tb: List[float] = []

    if settings.mode == RecordingMode.STUDIO and channels >= 2:
        report(18, "Studio Mode: Detecting talkback audio tones on Right channel...")
        right_channel = audio_2d[:, 1]
        waveform_peaks_tb = compute_waveform_peaks(right_channel, num_bins=500)
        tone_markers = detect_talkback_tones(right_channel, sr)
        talkback_intervals = pair_talkback_tones(tone_markers, right_channel, sr)
        report(
            26,
            f"Studio Mode: Isolated {len(talkback_intervals)} talkback region(s) between {len(tone_markers)} tones.",
        )
    else:
        report(24, "Normal Mode: Preparing microphone channel...")

    # Strictly isolate the voiceover channel (Left channel in Studio Mode, with talkback windows zeroed out)
    vo_signal = isolate_voiceover_channel(audio_2d, sr, settings.mode, talkback_intervals)
    waveform_peaks_vo = compute_waveform_peaks(vo_signal, num_bins=500)

    session_id = f"ars_{uuid.uuid4().hex[:10]}"
    clean_vo_path = str(SESSIONS_DIR / f"{session_id}_clean_vo.wav")
    sf.write(clean_vo_path, vo_signal[:, None], sr, subtype=subtype if subtype in ("PCM_16", "PCM_24", "FLOAT") else "PCM_24")

    report(36, "Running Speech-to-Text (STT) on voiceover and talkback tracks...")
    words, talkback_intervals = transcribe_voiceover_and_talkback(
        vo_signal=vo_signal,
        right_channel=right_channel,
        sample_rate=sr,
        talkback_intervals=talkback_intervals,
        whisper_model_name=settings.whisper_model,
        reference_script=settings.reference_script,
        source_path=source_path,
    )

    report(62, "Scanning inter-word gaps for breaths and mouth noises...")
    if settings.remove_breaths:
        breaths = detect_breaths_and_noises(
            vo_signal=vo_signal,
            sample_rate=sr,
            words=words,
            talkback_intervals=talkback_intervals,
            sensitivity=settings.breath_sensitivity,
        )
    else:
        breaths = []

    _, room_tone_db = extract_room_tone_profile(vo_signal, sr, talkback_intervals)

    report(76, "Evaluating candidate takes, disfluencies, and director talkback cues...")
    take_groups = group_and_select_takes(
        words=words,
        vo_signal=vo_signal,
        sample_rate=sr,
        talkback_intervals=talkback_intervals,
        settings=settings,
    )

    report(88, "Calculating speaker cadence and English grammatical pauses (commas/periods)...")
    cadence = analyze_uncut_cadence(
        take_groups=take_groups,
        breaths_removed=breaths,
        comma_multiplier=settings.comma_pause_multiplier,
        period_multiplier=settings.period_pause_multiplier,
        min_word_gap_ms=settings.min_word_gap_ms,
        room_tone_rms_db=room_tone_db,
    )

    timeline_clips, out_dur = build_timeline_clips(
        take_groups=take_groups,
        breaths_removed=breaths,
        cadence=cadence,
        crossfade_ms=settings.crossfade_ms,
    )
    waveform_peaks_output = compute_assembled_output_peaks(
        clean_vo_path=clean_vo_path,
        timeline_clips=timeline_clips,
        breaths_removed=breaths,
        num_bins=500,
    )

    report(96, "Preserving non-destructive edit session and timeline markers...")
    session = EditSession(
        session_id=session_id,
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        source_filename=fname,
        source_path=str(Path(source_path).resolve()),
        clean_vo_path=str(Path(clean_vo_path).resolve()),
        input_sample_rate=sr,
        input_channels=channels,
        input_subtype=subtype,
        input_format=fmt,
        input_duration_sec=duration_sec,
        settings=settings,
        tone_markers=tone_markers,
        talkback_intervals=talkback_intervals,
        breaths_removed=breaths,
        take_groups=take_groups,
        cadence_profile=cadence,
        timeline_clips=timeline_clips,
        output_duration_sec=out_dur,
        waveform_peaks_vo=waveform_peaks_vo,
        waveform_peaks_tb=waveform_peaks_tb,
        waveform_peaks_output=waveform_peaks_output,
    )
    save_session(session)
    report(100, "Rough cut assembly complete! Ready for preview and multi-format export.")
    return session


# =============================================================================
# FastAPI Application & Endpoints
# =============================================================================

app = FastAPI(
    title="Alpine Recording Station Rough Cut",
    description="AI Voiceover Rough Cut Editor with Studio Mode Talkback Isolation, Grammatical Pacing, and Adobe Audition (.sesx) Multitrack Export",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def get_health():
    ffmpeg_path = _find_ffmpeg()
    return {
        "status": "ok",
        "version": "1.0.0",
        "ffmpeg_available": bool(ffmpeg_path),
        "ffmpeg_path": ffmpeg_path,
    }


def _background_job_runner(
    job_id: str,
    file_path: str,
    orig_filename: str,
    settings: ProcessingSettings,
) -> None:
    def update_progress(pct: int, stage: str) -> None:
        JOBS[job_id]["progress"] = pct
        JOBS[job_id]["stage"] = stage

    try:
        JOBS[job_id]["status"] = "processing"
        session = run_rough_cut_pipeline(
            source_path=file_path,
            settings=settings,
            source_filename=orig_filename,
            progress_callback=update_progress,
        )
        JOBS[job_id]["status"] = "completed"
        JOBS[job_id]["progress"] = 100
        JOBS[job_id]["stage"] = "Completed"
        JOBS[job_id]["session_id"] = session.session_id
    except Exception as exc:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["stage"] = f"Error: {exc}"
        JOBS[job_id]["error"] = str(exc)


@app.post("/api/process")
async def process_uploaded_audio(
    file: UploadFile = File(...),
    settings_json: str = Form("{}"),
):
    try:
        settings_dict = json.loads(settings_json)
        settings = ProcessingSettings(**settings_dict)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid processing settings: {exc}")

    ext = Path(file.filename or "input.wav").suffix or ".wav"
    upload_id = uuid.uuid4().hex[:10]
    saved_path = UPLOADS_DIR / f"upload_{upload_id}{ext}"

    with open(saved_path, "wb") as out_f:
        content = await file.read()
        out_f.write(content)

    job_id = f"job_{uuid.uuid4().hex[:10]}"
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "progress": 2,
        "stage": "Audio file uploaded. Starting analysis...",
        "session_id": None,
        "error": None,
    }

    worker = threading.Thread(
        target=_background_job_runner,
        args=(job_id, str(saved_path), file.filename or saved_path.name, settings),
        daemon=True,
    )
    worker.start()
    return JOBS[job_id]


@app.post("/api/demo/{mode}")
def run_demo_recording(mode: str, settings: Optional[ProcessingSettings] = None):
    rec_mode = RecordingMode.STUDIO if mode.lower() == "studio" else RecordingMode.NORMAL
    proc_settings = settings or ProcessingSettings(
        mode=rec_mode,
        output_name=f"Alpine_{rec_mode.value.capitalize()}_Master",
    )
    proc_settings.mode = rec_mode

    if rec_mode == RecordingMode.STUDIO:
        demo_wav = generate_studio_mode_sample(str(UPLOADS_DIR))
    else:
        demo_wav = generate_normal_mode_sample(str(UPLOADS_DIR))

    job_id = f"job_{uuid.uuid4().hex[:10]}"
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "progress": 5,
        "stage": f"Generated {rec_mode.value.capitalize()} Mode sample recording...",
        "session_id": None,
        "error": None,
    }

    worker = threading.Thread(
        target=_background_job_runner,
        args=(job_id, demo_wav, Path(demo_wav).name, proc_settings),
        daemon=True,
    )
    worker.start()
    return JOBS[job_id]


@app.get("/api/jobs/{job_id}")
def get_job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return JOBS[job_id]


@app.get("/api/sessions")
def list_saved_sessions():
    items = []
    for p in sorted(SESSIONS_DIR.glob("*.ars.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            items.append(
                {
                    "session_id": data.get("session_id"),
                    "created_at": data.get("created_at"),
                    "source_filename": data.get("source_filename"),
                    "mode": data.get("settings", {}).get("mode", "normal"),
                    "input_duration_sec": data.get("input_duration_sec", 0.0),
                    "output_duration_sec": data.get("output_duration_sec", 0.0),
                    "take_groups_count": len(data.get("take_groups", [])),
                }
            )
        except Exception:
            continue
    return {"sessions": items}


@app.get("/api/sessions/{session_id}")
def get_session_details(session_id: str):
    try:
        return load_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Edit session not found")


@app.patch("/api/sessions/{session_id}")
def patch_session_edits(session_id: str, patch: SessionPatchRequest):
    """
    Update preserved edit points (select alternate takes, restore/remove breaths,
    adjust grammatical pause multipliers or custom clip pauses) without re-running STT.
    """
    try:
        session = load_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Edit session not found")

    if patch.selected_take_ids:
        for tg in session.take_groups:
            if tg.id in patch.selected_take_ids:
                chosen_id = patch.selected_take_ids[tg.id]
                if any(t.id == chosen_id for t in tg.takes):
                    tg.selected_take_id = chosen_id
                    for t in tg.takes:
                        t.selected = t.id == chosen_id

    if patch.removed_breath_ids:
        for br in session.breaths_removed:
            if br.id in patch.removed_breath_ids:
                br.removed = bool(patch.removed_breath_ids[br.id])

    if patch.custom_pauses_ms:
        for tg in session.take_groups:
            for t in tg.takes:
                if t.id in patch.custom_pauses_ms:
                    t.custom_pause_after_ms = patch.custom_pauses_ms[t.id]

    if patch.comma_pause_multiplier is not None:
        session.settings.comma_pause_multiplier = patch.comma_pause_multiplier
    if patch.period_pause_multiplier is not None:
        session.settings.period_pause_multiplier = patch.period_pause_multiplier
    if patch.use_room_tone_padding is not None:
        session.settings.use_room_tone_padding = patch.use_room_tone_padding
    if patch.crossfade_ms is not None:
        session.settings.crossfade_ms = patch.crossfade_ms

    updated = recalculate_session_timeline(session)
    return updated


@app.get("/api/sessions/{session_id}/source-audio")
def get_clean_source_audio(session_id: str):
    """Stream the isolated clean voiceover WAV so the browser can audition any take or edit point."""
    try:
        session = load_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Edit session not found")
    if not os.path.exists(session.clean_vo_path):
        raise HTTPException(status_code=404, detail="Clean voiceover audio file missing")
    return FileResponse(session.clean_vo_path, media_type="audio/wav")


@app.post("/api/sessions/{session_id}/preview", response_model=OutputPreviewInfo)
def generate_final_output_preview_endpoint(
    session_id: str,
    export_settings: Optional[ExportSettings] = None,
):
    """
    Render the assembled final output file according to `export_settings` (before export)
    and return preview metadata, waveform peaks, and a streamable preview URL.
    """
    try:
        session = load_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Edit session not found")

    exp = export_settings or ExportSettings(
        output_name=session.settings.output_name,
        use_room_tone_padding=session.settings.use_room_tone_padding,
        comma_pause_multiplier=session.settings.comma_pause_multiplier,
        period_pause_multiplier=session.settings.period_pause_multiplier,
        crossfade_ms=session.settings.crossfade_ms,
    )

    session.settings.comma_pause_multiplier = exp.comma_pause_multiplier
    session.settings.period_pause_multiplier = exp.period_pause_multiplier
    session.settings.use_room_tone_padding = exp.use_room_tone_padding
    session.settings.crossfade_ms = exp.crossfade_ms
    recalculate_session_timeline(session)

    preview_dir = OUTPUTS_DIR / session_id / "preview"
    _, preview_info = render_output_preview(session, exp, str(preview_dir))
    session.waveform_peaks_output = preview_info.waveform_peaks_output
    save_session(session)
    return preview_info


@app.get("/api/sessions/{session_id}/preview-audio")
def stream_final_output_preview_audio(session_id: str, file: Optional[str] = None):
    """
    Stream the rendered final output audio file for in-browser pre-export playback.
    If not yet rendered on disk, renders a default preview automatically.
    """
    try:
        session = load_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Edit session not found")

    preview_dir = OUTPUTS_DIR / session_id / "preview"
    target_path: Optional[Path] = None

    if file:
        # Sanitize filename to prevent directory traversal
        safe_file = Path(file).name
        candidate = preview_dir / safe_file
        if candidate.exists():
            target_path = candidate

    if target_path is None or not target_path.exists():
        default_exp = ExportSettings(
            output_name=session.settings.output_name,
            use_room_tone_padding=session.settings.use_room_tone_padding,
            comma_pause_multiplier=session.settings.comma_pause_multiplier,
            period_pause_multiplier=session.settings.period_pause_multiplier,
            crossfade_ms=session.settings.crossfade_ms,
        )
        rendered_str, _ = render_output_preview(session, default_exp, str(preview_dir))
        target_path = Path(rendered_str)

    ext = target_path.suffix.lower().lstrip(".")
    mime_map = {
        "wav": "audio/wav",
        "flac": "audio/flac",
        "mp3": "audio/mpeg",
        "ogg": "audio/ogg",
        "m4a": "audio/mp4",
    }
    media_type = mime_map.get(ext, "audio/wav")
    return FileResponse(str(target_path), media_type=media_type)


@app.post("/api/sessions/{session_id}/export/audio")
def export_master_audio_endpoint(session_id: str, export_settings: ExportSettings):
    """Render and download the edited audio file in any requested format and channel map."""
    try:
        session = load_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Edit session not found")

    # Apply any updated pacing multipliers from export_settings before rendering
    session.settings.comma_pause_multiplier = export_settings.comma_pause_multiplier
    session.settings.period_pause_multiplier = export_settings.period_pause_multiplier
    session.settings.use_room_tone_padding = export_settings.use_room_tone_padding
    session.settings.crossfade_ms = export_settings.crossfade_ms
    recalculate_session_timeline(session)

    out_dir = OUTPUTS_DIR / session_id
    rendered_path = render_edited_audio(session, export_settings, str(out_dir))
    return FileResponse(
        rendered_path,
        filename=Path(rendered_path).name,
        media_type="application/octet-stream",
    )


@app.post("/api/sessions/{session_id}/export/audition")
def export_audition_bundle_endpoint(session_id: str, export_settings: ExportSettings):
    """
    Generate an Adobe Audition Multitrack Session (.sesx) package containing:
      - `<output_name>.sesx` (Non-destructive Audition Multitrack XML project)
      - `<output_name>_Clean_VO_Source.wav` (Isolated voiceover source with zero talkback)
      - `<output_name>_Audition_Markers.csv` (Cue markers for all cuts and pauses)
      - `<output_name>.edl` (CMX 3600 Edit Decision List)
    """
    try:
        session = load_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Edit session not found")

    session.settings.comma_pause_multiplier = export_settings.comma_pause_multiplier
    session.settings.period_pause_multiplier = export_settings.period_pause_multiplier
    session.settings.crossfade_ms = export_settings.crossfade_ms
    recalculate_session_timeline(session)

    out_dir = OUTPUTS_DIR / session_id / "audition_package"
    sesx_path, wav_path = export_audition_sesx(session, export_settings, str(out_dir))
    edl_path, csv_path = export_edl_and_markers_csv(session, export_settings, str(out_dir))

    safe_base = Path(sesx_path).stem
    zip_path = OUTPUTS_DIR / session_id / f"{safe_base}_Audition_Multitrack_Project.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(sesx_path, arcname=Path(sesx_path).name)
        zf.write(wav_path, arcname=Path(wav_path).name)
        zf.write(csv_path, arcname=Path(csv_path).name)
        zf.write(edl_path, arcname=Path(edl_path).name)

    return FileResponse(
        str(zip_path),
        filename=zip_path.name,
        media_type="application/zip",
    )


@app.post("/api/sessions/{session_id}/export/sesx-only")
def export_sesx_file_only_endpoint(session_id: str, export_settings: ExportSettings):
    """Download just the `.sesx` XML file directly."""
    try:
        session = load_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Edit session not found")
    out_dir = OUTPUTS_DIR / session_id / "audition_package"
    sesx_path, _ = export_audition_sesx(session, export_settings, str(out_dir))
    return FileResponse(
        sesx_path,
        filename=Path(sesx_path).name,
        media_type="application/xml",
    )


@app.get("/api/sessions/{session_id}/export/project-json")
def export_preserved_session_json(session_id: str):
    """Download the preserved `.ars.json` edit project file."""
    try:
        session = load_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Edit session not found")
    session_file = save_session(session)
    safe_name = Path(session.source_filename).stem
    return FileResponse(
        session_file,
        filename=f"{safe_name}.ars.json",
        media_type="application/json",
    )


@app.post("/api/sessions/import")
async def import_preserved_session_json(file: UploadFile = File(...)):
    """Import a previously saved `.ars.json` session file to restore edit points."""
    try:
        raw = await file.read()
        data = json.loads(raw.decode("utf-8"))
        session = EditSession(**data)
        save_session(session)
        return session
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid .ars.json file: {exc}")


# Mount static frontend files
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def serve_index():
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return JSONResponse({"message": "Alpine Recording Station Rough Cut API is running."})
