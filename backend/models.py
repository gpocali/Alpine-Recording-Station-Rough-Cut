"""Pydantic data models for Alpine Recording Station Rough Cut."""

from __future__ import annotations

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class RecordingMode(str, Enum):
    NORMAL = "normal"
    STUDIO = "studio"


class AudioFormatOption(str, Enum):
    MATCH_INPUT = "match_input"
    WAV = "wav"
    FLAC = "flac"
    MP3 = "mp3"
    OGG = "ogg"
    M4A = "m4a"


class BitDepthOption(str, Enum):
    MATCH_INPUT = "match_input"
    PCM_16 = "PCM_16"
    PCM_24 = "PCM_24"
    FLOAT = "FLOAT"


class ChannelMapOption(str, Enum):
    MATCH_INPUT = "match_input"
    MONO = "mono"
    STEREO_DUAL_MONO = "stereo_dual_mono"
    STEREO_LEFT_ONLY = "stereo_left_only"
    STEREO_RIGHT_ONLY = "stereo_right_only"


class ToneMarker(BaseModel):
    start_time: float
    end_time: float
    dominant_freq_hz: float
    rms_db: float


class TalkbackInterval(BaseModel):
    id: str
    start_time: float
    end_time: float
    note_start: float
    note_end: float
    tone_start_freq_hz: float
    tone_end_freq_hz: float
    transcript: str = ""
    directive_type: str = "note"  # "retake", "keep_specific", "approve", "note"
    target_take_number: Optional[int] = None


class WordToken(BaseModel):
    word: str
    start: float
    end: float
    probability: float = 0.95
    punctuation_after: str = ""


class BreathOrNoiseEvent(BaseModel):
    id: str
    start_time: float
    end_time: float
    kind: str = "breath"  # "breath" or "mouth_noise"
    rms_db: float = -35.0
    zcr: float = 0.15
    removed: bool = True


class TakeCandidate(BaseModel):
    id: str
    take_group_id: str
    take_number: int
    start_time: float
    end_time: float
    trimmed_start: float
    trimmed_end: float
    text: str
    words: List[WordToken] = Field(default_factory=list)
    confidence_score: float = 0.0
    acoustic_score: float = 0.0
    completeness_score: float = 0.0
    director_bonus: float = 0.0
    total_score: float = 0.0
    selected: bool = False
    rejection_reason: str = ""
    terminal_punctuation: str = "."
    custom_pause_after_ms: Optional[float] = None


class TakeGroup(BaseModel):
    id: str
    group_index: int
    canonical_text: str
    takes: List[TakeCandidate] = Field(default_factory=list)
    selected_take_id: Optional[str] = None
    talkback_notes: List[str] = Field(default_factory=list)


class CadenceProfile(BaseModel):
    median_word_gap_ms: float = 95.0
    mean_word_gap_ms: float = 105.0
    words_per_minute: float = 145.0
    comma_pause_ms: float = 340.0
    semicolon_pause_ms: float = 420.0
    period_pause_ms: float = 720.0
    paragraph_pause_ms: float = 1100.0
    room_tone_rms_db: float = -62.0


class TimelineEditClip(BaseModel):
    clip_id: str
    take_id: str
    take_group_id: str
    take_number: int
    source_start: float
    source_end: float
    timeline_start: float
    timeline_end: float
    pause_after_ms: float
    pause_type: str = "period"  # "word", "comma", "semicolon", "period", "paragraph"
    fade_in_ms: float = 8.0
    fade_out_ms: float = 8.0
    text: str = ""


class ProcessingSettings(BaseModel):
    mode: RecordingMode = RecordingMode.NORMAL
    output_name: str = "Rough_Cut_Master"
    whisper_model: str = "base.en"
    remove_breaths: bool = True
    breath_sensitivity: float = 0.65  # 0.0 to 1.0
    use_room_tone_padding: bool = True
    comma_pause_multiplier: float = 3.2
    period_pause_multiplier: float = 6.8
    min_word_gap_ms: float = 85.0
    crossfade_ms: float = 8.0
    reference_script: str = ""
    llm_provider: str = "local"  # "local", "openai", "gemini"
    llm_api_key: str = ""


class ExportSettings(BaseModel):
    output_name: str = "Rough_Cut_Master"
    audio_format: AudioFormatOption = AudioFormatOption.MATCH_INPUT
    sample_rate: int = 0  # 0 = match input
    bit_depth: BitDepthOption = BitDepthOption.MATCH_INPUT
    channel_map: ChannelMapOption = ChannelMapOption.MATCH_INPUT
    use_room_tone_padding: bool = True
    comma_pause_multiplier: float = 3.2
    period_pause_multiplier: float = 6.8
    crossfade_ms: float = 8.0
    include_rejected_track_in_sesx: bool = True
    include_breaths_track_in_sesx: bool = True


class SessionPatchRequest(BaseModel):
    selected_take_ids: Optional[dict[str, str]] = None  # take_group_id -> take_id
    removed_breath_ids: Optional[dict[str, bool]] = None  # breath_id -> removed
    custom_pauses_ms: Optional[dict[str, Optional[float]]] = None  # take_id -> ms
    comma_pause_multiplier: Optional[float] = None
    period_pause_multiplier: Optional[float] = None
    use_room_tone_padding: Optional[bool] = None
    crossfade_ms: Optional[float] = None


class EditSession(BaseModel):
    session_id: str
    created_at: str
    source_filename: str
    source_path: str
    clean_vo_path: str
    input_sample_rate: int
    input_channels: int
    input_subtype: str
    input_format: str
    input_duration_sec: float
    settings: ProcessingSettings
    tone_markers: List[ToneMarker] = Field(default_factory=list)
    talkback_intervals: List[TalkbackInterval] = Field(default_factory=list)
    breaths_removed: List[BreathOrNoiseEvent] = Field(default_factory=list)
    take_groups: List[TakeGroup] = Field(default_factory=list)
    cadence_profile: CadenceProfile = Field(default_factory=CadenceProfile)
    timeline_clips: List[TimelineEditClip] = Field(default_factory=list)
    output_duration_sec: float = 0.0
    waveform_peaks_vo: List[float] = Field(default_factory=list)
    waveform_peaks_tb: List[float] = Field(default_factory=list)
    waveform_peaks_output: List[float] = Field(default_factory=list)


class OutputPreviewInfo(BaseModel):
    session_id: str
    filename: str
    audio_format: str
    sample_rate: int
    channels: int
    bit_depth: str
    duration_sec: float
    file_size_bytes: int
    preview_audio_url: str
    waveform_peaks_output: List[float] = Field(default_factory=list)

