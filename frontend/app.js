/**
 * Alpine Recording Station — Rough Cut Client Application
 */

(() => {
  "use strict";

  let currentSession = null;
  let audioStopAtTime = null;

  // DOM Elements
  const healthPill = document.getElementById("system-health-pill");
  const btnDemoStudio = document.getElementById("btn-demo-studio");
  const btnDemoNormal = document.getElementById("btn-demo-normal");

  const processForm = document.getElementById("process-form");
  const audioFileInput = document.getElementById("audio-file-input");
  const outputNameInput = document.getElementById("output-name-input");
  const whisperModelSelect = document.getElementById("whisper-model-select");
  const breathSensRange = document.getElementById("breath-sensitivity-range");
  const breathSensOutput = document.getElementById("breath-sens-output");
  const removeBreathsCheck = document.getElementById("remove-breaths-check");
  const roomToneCheck = document.getElementById("room-tone-check");
  const commaMultRange = document.getElementById("comma-mult-range");
  const commaMultOutput = document.getElementById("comma-mult-output");
  const periodMultRange = document.getElementById("period-mult-range");
  const periodMultOutput = document.getElementById("period-mult-output");
  const refScriptInput = document.getElementById("reference-script-input");
  const llmProviderSelect = document.getElementById("llm-provider-select");
  const llmApiKeyInput = document.getElementById("llm-api-key-input");
  const importSessionInput = document.getElementById("import-session-input");
  const btnProcessSubmit = document.getElementById("btn-process-submit");

  const progressEl = document.getElementById("process-progress");
  const progressStageLabel = document.getElementById("progress-stage-label");
  const progressPctText = document.getElementById("progress-percent-text");
  const progressLiveStatus = document.getElementById("progress-live-status");

  const activeSessionBadge = document.getElementById("active-session-badge");
  const metricInputInfo = document.getElementById("metric-input-info");
  const metricOutputDur = document.getElementById("metric-output-dur");
  const metricCadence = document.getElementById("metric-cadence");
  const metricPauses = document.getElementById("metric-pauses");
  const metricExcised = document.getElementById("metric-excised");

  const waveformCanvas = document.getElementById("waveform-canvas");
  const voAudioPlayer = document.getElementById("vo-audio-player");

  const exportFormatSelect = document.getElementById("export-format-select");
  const exportSrSelect = document.getElementById("export-sr-select");
  const exportBitDepthSelect = document.getElementById("export-bitdepth-select");
  const exportChannelMapSelect = document.getElementById("export-channelmap-select");

  const btnExportAudio = document.getElementById("btn-export-audio");
  const btnExportAudition = document.getElementById("btn-export-audition");
  const btnExportSesxOnly = document.getElementById("btn-export-sesx-only");
  const btnExportSessionJson = document.getElementById("btn-export-session-json");

  const takeGroupsContainer = document.getElementById("take-groups-container");
  const timelineClipsContainer = document.getElementById("timeline-clips-container");

  // ---------------------------------------------------------------------------
  // Range Sliders & Form Validation Setup (per modern-web-guidance `forms`)
  // ---------------------------------------------------------------------------
  breathSensRange.addEventListener("input", () => {
    breathSensOutput.value = `${breathSensRange.value}%`;
  });

  commaMultRange.addEventListener("input", () => {
    commaMultOutput.value = `${ Number(commaMultRange.value).toFixed(1) }x`;
  });

  periodMultRange.addEventListener("input", () => {
    periodMultOutput.value = `${ Number(periodMultRange.value).toFixed(1) }x`;
  });

  // Update output base name automatically when user picks an audio file
  audioFileInput.addEventListener("change", () => {
    audioFileInput.setCustomValidity("");
    if (audioFileInput.files && audioFileInput.files.length > 0) {
      const rawName = audioFileInput.files[0].name.replace(/\.[^/.]+$/, "");
      outputNameInput.value = `${rawName}_Rough_Cut`;
    }
  });

  // Validate on blur, clear on input
  outputNameInput.addEventListener("input", () => {
    outputNameInput.setCustomValidity("");
  });
  outputNameInput.addEventListener("blur", () => {
    if (!outputNameInput.value.trim()) {
      outputNameInput.setCustomValidity("Please provide a base filename for exported files.");
    }
  });

  // Stop audio preview at take end time when auditioning a single take
  voAudioPlayer.addEventListener("timeupdate", () => {
    if (audioStopAtTime !== null && voAudioPlayer.currentTime >= audioStopAtTime) {
      voAudioPlayer.pause();
      audioStopAtTime = null;
    }
  });

  // ---------------------------------------------------------------------------
  // System Health Check
  // ---------------------------------------------------------------------------
  async function checkSystemHealth() {
    try {
      const res = await fetch("/api/health");
      const data = await res.json();
      if (data.status === "ok") {
        healthPill.textContent = data.ffmpeg_available
          ? "Engine Ready • FFmpeg & Whisper Active"
          : "Engine Ready • Core WAV/FLAC Active";
      }
    } catch {
      healthPill.textContent = "Backend Offline";
    }
  }

  // ---------------------------------------------------------------------------
  // Progress Bar Helper
  // ---------------------------------------------------------------------------
  function setProgressState(pct, stageText, liveAnnouncement = "", isIndeterminate = false) {
    if (isIndeterminate) {
      progressEl.removeAttribute("value");
      progressPctText.textContent = "...";
    } else {
      const clamped = Math.max(0, Math.min(100, Math.round(pct)));
      progressEl.value = clamped;
      progressPctText.textContent = `${clamped}%`;
    }
    progressStageLabel.textContent = stageText;
    if (liveAnnouncement) {
      progressLiveStatus.textContent = liveAnnouncement;
    }
  }

  function gatherProcessingSettings() {
    const modeRadio = document.querySelector('input[name="mode"]:checked');
    return {
      mode: modeRadio ? modeRadio.value : "studio",
      output_name: outputNameInput.value.trim() || "Alpine_VO_Rough_Cut",
      whisper_model: whisperModelSelect.value,
      remove_breaths: removeBreathsCheck.checked,
      breath_sensitivity: Number(breathSensRange.value) / 100.0,
      use_room_tone_padding: roomToneCheck.checked,
      comma_pause_multiplier: Number(commaMultRange.value),
      period_pause_multiplier: Number(periodMultRange.value),
      min_word_gap_ms: 85.0,
      crossfade_ms: 8.0,
      reference_script: refScriptInput.value || "",
      llm_provider: llmProviderSelect.value,
      llm_api_key: llmApiKeyInput.value || "",
    };
  }

  function gatherExportSettings() {
    return {
      output_name: outputNameInput.value.trim() || "Alpine_VO_Rough_Cut",
      audio_format: exportFormatSelect.value,
      sample_rate: Number(exportSrSelect.value),
      bit_depth: exportBitDepthSelect.value,
      channel_map: exportChannelMapSelect.value,
      use_room_tone_padding: roomToneCheck.checked,
      comma_pause_multiplier: Number(commaMultRange.value),
      period_pause_multiplier: Number(periodMultRange.value),
      crossfade_ms: 8.0,
      include_rejected_track_in_sesx: true,
      include_breaths_track_in_sesx: true,
    };
  }

  // ---------------------------------------------------------------------------
  // Job Polling & Session Loading
  // ---------------------------------------------------------------------------
  async function pollJobUntilComplete(jobId) {
    btnProcessSubmit.disabled = true;
    btnDemoStudio.disabled = true;
    btnDemoNormal.disabled = true;

    try {
      while (true) {
        const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
        if (!res.ok) {
          throw new Error("Failed to check job progress.");
        }
        const job = await res.json();
        setProgressState(job.progress || 0, job.stage || "Processing...");

        if (job.status === "completed" && job.session_id) {
          await loadEditSession(job.session_id);
          setProgressState(
            100,
            "Rough cut assembly complete!",
            `Finished processing ${currentSession.source_filename}. ${currentSession.timeline_clips.length} edit clips assembled.`
          );
          break;
        } else if (job.status === "error") {
          setProgressState(0, "Processing failed", job.error || "Unknown error occurred.");
          break;
        }
        await new Promise((r) => setTimeout(r, 320));
      }
    } catch (err) {
      setProgressState(0, "Error during processing", String(err.message || err));
    } finally {
      btnProcessSubmit.disabled = false;
      btnDemoStudio.disabled = false;
      btnDemoNormal.disabled = false;
    }
  }

  async function loadEditSession(sessionId) {
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (!res.ok) {
      throw new Error("Could not load edit session.");
    }
    const session = await res.json();
    renderSessionUI(session);
  }

  // ---------------------------------------------------------------------------
  // Form Submit & Demo Triggers
  // ---------------------------------------------------------------------------
  processForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!audioFileInput.files || audioFileInput.files.length === 0) {
      audioFileInput.setCustomValidity("Please select an audio file to process (or click Load Demo above).");
      audioFileInput.reportValidity();
      return;
    }

    const settings = gatherProcessingSettings();
    const formData = new FormData();
    formData.append("file", audioFileInput.files[0]);
    formData.append("settings_json", JSON.stringify(settings));

    setProgressState(5, "Uploading audio file...", "Starting rough cut analysis...", true);
    try {
      const res = await fetch("/api/process", {
        method: "POST",
        body: formData,
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "Upload failed");
      }
      const job = await res.json();
      await pollJobUntilComplete(job.job_id);
    } catch (err) {
      setProgressState(0, "Upload failed", String(err.message || err));
    }
  });

  async function triggerDemo(mode) {
    const modeRadio = document.getElementById(mode === "studio" ? "mode-studio" : "mode-normal");
    if (modeRadio) modeRadio.checked = true;
    outputNameInput.value = `Alpine_${mode === "studio" ? "Studio" : "Normal"}_Rough_Cut`;

    const settings = gatherProcessingSettings();
    settings.mode = mode;

    setProgressState(5, `Generating ${mode} mode demo recording...`, `Starting ${mode} mode demo...`, true);
    try {
      const res = await fetch(`/api/demo/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      });
      if (!res.ok) throw new Error("Failed to start demo job.");
      const job = await res.json();
      await pollJobUntilComplete(job.job_id);
    } catch (err) {
      setProgressState(0, "Demo error", String(err.message || err));
    }
  }

  btnDemoStudio.addEventListener("click", () => triggerDemo("studio"));
  btnDemoNormal.addEventListener("click", () => triggerDemo("normal"));

  // Restore saved .ars.json session
  importSessionInput.addEventListener("change", async () => {
    if (!importSessionInput.files || importSessionInput.files.length === 0) return;
    const fd = new FormData();
    fd.append("file", importSessionInput.files[0]);
    try {
      const res = await fetch("/api/sessions/import", { method: "POST", body: fd });
      if (!res.ok) throw new Error("Invalid .ars.json file");
      const sess = await res.json();
      renderSessionUI(sess);
      setProgressState(100, "Restored preserved edit session!", `Loaded session ${sess.session_id}`);
    } catch (err) {
      setProgressState(0, "Failed to import session", String(err.message || err));
    }
  });

  // ---------------------------------------------------------------------------
  // Render Session State into UI & Canvas
  // ---------------------------------------------------------------------------
  function renderSessionUI(session) {
    currentSession = session;

    activeSessionBadge.textContent = `${session.settings.mode.toUpperCase()} MODE • ${session.session_id}`;
    activeSessionBadge.className = "badge badge-success";

    const chText = session.input_channels === 1 ? "Mono" : "Stereo (2ch)";
    metricInputInfo.textContent = `${session.input_duration_sec.toFixed(2)}s • ${(session.input_sample_rate / 1000).toFixed(1)}kHz ${session.input_subtype} ${chText}`;

    const savedPct =
      session.input_duration_sec > 0
        ? Math.max(0, Math.round((1 - session.output_duration_sec / session.input_duration_sec) * 100))
        : 0;
    metricOutputDur.textContent = `${session.output_duration_sec.toFixed(2)}s (-${savedPct}% trimmed)`;

    const cad = session.cadence_profile;
    metricCadence.textContent = `${cad.median_word_gap_ms.toFixed(0)}ms word gap • ${cad.words_per_minute.toFixed(0)} WPM`;
    metricPauses.textContent = `Comma: ${cad.comma_pause_ms.toFixed(0)}ms • Period: ${cad.period_pause_ms.toFixed(0)}ms`;

    const removedBreathsCount = session.breaths_removed.filter((b) => b.removed).length;
    metricExcised.textContent = `${session.talkback_intervals.length} Talkbacks • ${removedBreathsCount} Breaths`;

    // Enable instant export buttons
    btnExportAudio.disabled = false;
    btnExportAudition.disabled = false;
    btnExportSesxOnly.disabled = false;
    btnExportSessionJson.disabled = false;

    // Load clean isolated VO audio into player
    const audioUrl = `/api/sessions/${encodeURIComponent(session.session_id)}/source-audio`;
    if (voAudioPlayer.getAttribute("data-src") !== audioUrl) {
      voAudioPlayer.src = audioUrl;
      voAudioPlayer.setAttribute("data-src", audioUrl);
    }

    drawWaveformTimeline(session);
    renderTakeGroupsList(session);
    renderTimelineClipsList(session);
  }

  function drawWaveformTimeline(session) {
    const ctx = waveformCanvas.getContext("2d");
    const w = waveformCanvas.width;
    const h = waveformCanvas.height;
    ctx.clearRect(0, 0, w, h);

    // Background
    ctx.fillStyle = "#0b1017";
    ctx.fillRect(0, 0, w, h);

    const dur = Math.max(0.1, session.input_duration_sec);
    const isStudio = session.settings.mode === "studio" && session.waveform_peaks_tb.length > 0;
    const voCenterY = isStudio ? h * 0.34 : h * 0.5;
    const voHalfH = isStudio ? h * 0.28 : h * 0.42;

    // 1. Draw shaded regions for Takes (Green = Kept, Orange = Rejected)
    for (const tg of session.take_groups) {
      for (const take of tg.takes) {
        const x1 = (take.trimmed_start / dur) * w;
        const x2 = Math.max(x1 + 2, (take.trimmed_end / dur) * w);
        ctx.fillStyle = take.selected ? "rgba(46, 204, 113, 0.22)" : "rgba(230, 126, 34, 0.18)";
        ctx.fillRect(x1, 4, x2 - x1, isStudio ? h * 0.62 : h - 8);

        ctx.strokeStyle = take.selected ? "#2ecc71" : "#e67e22";
        ctx.lineWidth = 1;
        ctx.strokeRect(x1, 4, x2 - x1, isStudio ? h * 0.62 : h - 8);
      }
    }

    // 2. Draw excised breaths (Purple)
    for (const br of session.breaths_removed) {
      if (!br.removed) continue;
      const x1 = (br.start_time / dur) * w;
      const x2 = Math.max(x1 + 2, (br.end_time / dur) * w);
      ctx.fillStyle = "rgba(155, 89, 182, 0.42)";
      ctx.fillRect(x1, 4, x2 - x1, isStudio ? h * 0.62 : h - 8);
    }

    // 3. Draw VO Waveform Peaks
    const peaksVo = session.waveform_peaks_vo || [];
    if (peaksVo.length > 0) {
      ctx.strokeStyle = "#63b3ed";
      ctx.lineWidth = 1.3;
      ctx.beginPath();
      for (let i = 0; i < peaksVo.length; i++) {
        const x = (i / peaksVo.length) * w;
        const amp = Math.min(1.0, peaksVo[i] * 1.8) * voHalfH;
        ctx.moveTo(x, voCenterY - amp);
        ctx.lineTo(x, voCenterY + amp);
      }
      ctx.stroke();
    }

    // 4. If Studio Mode, draw Right Channel Talkback Track & Tone-Bounded Regions (Red)
    if (isStudio) {
      const dividerY = h * 0.68;
      ctx.strokeStyle = "#2a374e";
      ctx.beginPath();
      ctx.moveTo(0, dividerY);
      ctx.lineTo(w, dividerY);
      ctx.stroke();

      const tbCenterY = h * 0.84;
      const tbHalfH = h * 0.13;

      for (const tb of session.talkback_intervals) {
        const x1 = (tb.start_time / dur) * w;
        const x2 = Math.max(x1 + 3, (tb.end_time / dur) * w);
        ctx.fillStyle = "rgba(231, 76, 60, 0.30)";
        ctx.fillRect(x1, dividerY + 2, x2 - x1, h - dividerY - 4);
      }

      const peaksTb = session.waveform_peaks_tb;
      ctx.strokeStyle = "#fc8181";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      for (let i = 0; i < peaksTb.length; i++) {
        const x = (i / peaksTb.length) * w;
        const amp = Math.min(1.0, peaksTb[i] * 1.6) * tbHalfH;
        ctx.moveTo(x, tbCenterY - amp);
        ctx.lineTo(x, tbCenterY + amp);
      }
      ctx.stroke();
    }
  }

  function renderTakeGroupsList(session) {
    takeGroupsContainer.replaceChildren();
    if (!session.take_groups || session.take_groups.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No spoken take groups detected.";
      takeGroupsContainer.appendChild(empty);
      return;
    }

    for (const tg of session.take_groups) {
      const card = document.createElement("div");
      card.className = "take-group-card";

      const header = document.createElement("div");
      header.className = "take-group-header";
      const titleSpan = document.createElement("span");
      titleSpan.textContent = `Phrase Group #${tg.group_index} (${tg.takes.length} Take${tg.takes.length > 1 ? "s" : ""})`;
      header.appendChild(titleSpan);
      card.appendChild(header);

      // Show Studio Mode Talkback Director Notes if present
      if (tg.talkback_notes && tg.talkback_notes.length > 0) {
        for (const note of tg.talkback_notes) {
          const tbBanner = document.createElement("div");
          tbBanner.className = "talkback-banner";
          tbBanner.textContent = `Studio Talkback Note: ${note}`;
          card.appendChild(tbBanner);
        }
      }

      for (const take of tg.takes) {
        const item = document.createElement("div");
        item.className = `take-item${take.selected ? " is-selected" : ""}`;

        const topRow = document.createElement("div");
        topRow.className = "take-item-top";

        const labelStrong = document.createElement("strong");
        labelStrong.textContent = `Take ${take.take_number} (${take.trimmed_start.toFixed(2)}s – ${take.trimmed_end.toFixed(2)}s)`;

        const badge = document.createElement("span");
        badge.className = `badge ${take.selected ? "badge-success" : "badge-warn"}`;
        badge.textContent = take.selected
          ? `SELECTED • Score ${take.total_score.toFixed(1)}`
          : `REJECTED • Score ${take.total_score.toFixed(1)}`;

        topRow.appendChild(labelStrong);
        topRow.appendChild(badge);
        item.appendChild(topRow);

        const textP = document.createElement("p");
        textP.className = "take-text";
        textP.textContent = `"${take.text}"`;
        item.appendChild(textP);

        const metaRow = document.createElement("div");
        metaRow.className = "take-meta";

        const reasonSpan = document.createElement("span");
        reasonSpan.textContent = take.selected
          ? `Completeness ${take.completeness_score.toFixed(0)} | STT ${take.confidence_score.toFixed(0)} | Acoustic ${take.acoustic_score.toFixed(0)} | Dir ${take.director_bonus >= 0 ? "+" : ""}${take.director_bonus.toFixed(0)}`
          : take.rejection_reason;

        const btnGroup = document.createElement("div");
        btnGroup.style.display = "flex";
        btnGroup.style.gap = "0.4rem";

        const btnAudition = document.createElement("button");
        btnAudition.type = "button";
        btnAudition.className = "btn btn-secondary btn-sm";
        btnAudition.textContent = "Audition Take";
        btnAudition.addEventListener("click", () => {
          audioStopAtTime = take.trimmed_end;
          voAudioPlayer.currentTime = take.trimmed_start;
          voAudioPlayer.play();
        });
        btnGroup.appendChild(btnAudition);

        if (!take.selected) {
          const btnSelect = document.createElement("button");
          btnSelect.type = "button";
          btnSelect.className = "btn btn-primary btn-sm";
          btnSelect.textContent = "Select This Take";
          btnSelect.addEventListener("click", async () => {
            await selectAlternateTake(tg.id, take.id);
          });
          btnGroup.appendChild(btnSelect);
        }

        metaRow.appendChild(reasonSpan);
        metaRow.appendChild(btnGroup);
        item.appendChild(metaRow);
        card.appendChild(item);
      }

      takeGroupsContainer.appendChild(card);
    }
  }

  function renderTimelineClipsList(session) {
    timelineClipsContainer.replaceChildren();
    if (!session.timeline_clips || session.timeline_clips.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No timeline clips assembled.";
      timelineClipsContainer.appendChild(empty);
      return;
    }

    for (let i = 0; i < session.timeline_clips.length; i++) {
      const clip = session.timeline_clips[i];
      const card = document.createElement("div");
      card.className = "clip-card";

      const topRow = document.createElement("div");
      topRow.className = "take-item-top";

      const title = document.createElement("strong");
      title.textContent = `Edit Clip #${i + 1} (Take ${clip.take_number}) • Timeline ${clip.timeline_start.toFixed(2)}s – ${clip.timeline_end.toFixed(2)}s`;

      const pauseBadge = document.createElement("span");
      pauseBadge.className = "badge";
      pauseBadge.textContent = `Pause: +${clip.pause_after_ms.toFixed(0)}ms (${clip.pause_type})`;

      topRow.appendChild(title);
      topRow.appendChild(pauseBadge);
      card.appendChild(topRow);

      const textP = document.createElement("p");
      textP.className = "take-text";
      textP.textContent = clip.text;
      card.appendChild(textP);

      const subMeta = document.createElement("div");
      subMeta.className = "take-meta";
      subMeta.textContent = `Source In/Out: ${clip.source_start.toFixed(3)}s – ${clip.source_end.toFixed(3)}s | Cosine Crossfades: ${clip.fade_in_ms}ms / ${clip.fade_out_ms}ms`;
      card.appendChild(subMeta);

      timelineClipsContainer.appendChild(card);
    }
  }

  async function selectAlternateTake(takeGroupId, takeId) {
    if (!currentSession) return;
    const res = await fetch(`/api/sessions/${encodeURIComponent(currentSession.session_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        selected_take_ids: { [takeGroupId]: takeId },
        comma_pause_multiplier: Number(commaMultRange.value),
        period_pause_multiplier: Number(periodMultRange.value),
        use_room_tone_padding: roomToneCheck.checked,
      }),
    });
    if (res.ok) {
      const updated = await res.json();
      renderSessionUI(updated);
      progressLiveStatus.textContent = `Updated take selection for ${takeGroupId} and recalculated grammatical pacing.`;
    }
  }

  // Live update pacing multipliers on existing session when sliders finish changing
  async function syncPacingToActiveSession() {
    if (!currentSession) return;
    const res = await fetch(`/api/sessions/${encodeURIComponent(currentSession.session_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        comma_pause_multiplier: Number(commaMultRange.value),
        period_pause_multiplier: Number(periodMultRange.value),
        use_room_tone_padding: roomToneCheck.checked,
      }),
    });
    if (res.ok) {
      const updated = await res.json();
      renderSessionUI(updated);
    }
  }

  commaMultRange.addEventListener("change", syncPacingToActiveSession);
  periodMultRange.addEventListener("change", syncPacingToActiveSession);
  roomToneCheck.addEventListener("change", syncPacingToActiveSession);

  // ---------------------------------------------------------------------------
  // Multi-Format Export Triggers (No Re-Processing Required!)
  // ---------------------------------------------------------------------------
  async function downloadFromPostEndpoint(url, payload, defaultFilename) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Export failed");
    }
    const blob = await res.blob();
    let filename = defaultFilename;
    const cd = res.headers.get("content-disposition");
    if (cd) {
      const m = cd.match(/filename="?([^"]+)"?/i);
      if (m && m[1]) filename = m[1];
    }
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 5000);
  }

  btnExportAudio.addEventListener("click", async () => {
    if (!currentSession) return;
    const exp = gatherExportSettings();
    btnExportAudio.disabled = true;
    progressLiveStatus.textContent = "Rendering master audio file from preserved edit points...";
    try {
      await downloadFromPostEndpoint(
        `/api/sessions/${encodeURIComponent(currentSession.session_id)}/export/audio`,
        exp,
        `${exp.output_name}.wav`
      );
      progressLiveStatus.textContent = "Master audio file exported!";
    } catch (err) {
      progressLiveStatus.textContent = `Export error: ${err.message || err}`;
    } finally {
      btnExportAudio.disabled = false;
    }
  });

  btnExportAudition.addEventListener("click", async () => {
    if (!currentSession) return;
    const exp = gatherExportSettings();
    btnExportAudition.disabled = true;
    progressLiveStatus.textContent = "Building Adobe Audition (.sesx) multitrack session bundle...";
    try {
      await downloadFromPostEndpoint(
        `/api/sessions/${encodeURIComponent(currentSession.session_id)}/export/audition`,
        exp,
        `${exp.output_name}_Audition_Multitrack_Project.zip`
      );
      progressLiveStatus.textContent = "Adobe Audition (.sesx) multitrack bundle exported!";
    } catch (err) {
      progressLiveStatus.textContent = `Audition export error: ${err.message || err}`;
    } finally {
      btnExportAudition.disabled = false;
    }
  });

  btnExportSesxOnly.addEventListener("click", async () => {
    if (!currentSession) return;
    const exp = gatherExportSettings();
    btnExportSesxOnly.disabled = true;
    try {
      await downloadFromPostEndpoint(
        `/api/sessions/${encodeURIComponent(currentSession.session_id)}/export/sesx-only`,
        exp,
        `${exp.output_name}.sesx`
      );
      progressLiveStatus.textContent = "Downloaded Adobe Audition .sesx multitrack XML file.";
    } catch (err) {
      progressLiveStatus.textContent = `Export error: ${err.message || err}`;
    } finally {
      btnExportSesxOnly.disabled = false;
    }
  });

  btnExportSessionJson.addEventListener("click", () => {
    if (!currentSession) return;
    window.location.href = `/api/sessions/${encodeURIComponent(currentSession.session_id)}/export/project-json`;
  });

  // Initialize
  checkSystemHealth();
})();
