const fileInput = document.querySelector("#file-input");
const dropZone = document.querySelector("#drop-zone");
const uploadSelection = document.querySelector("#upload-selection");
const selectedName = document.querySelector("#selected-name");
const selectedMeta = document.querySelector("#selected-meta");
const uploadButton = document.querySelector("#upload-button");
const uploadNotice = document.querySelector("#upload-notice");
const jobList = document.querySelector("#job-list");
const jobTemplate = document.querySelector("#job-template");
const emptyState = document.querySelector("#empty-state");
const jobCount = document.querySelector("#job-count");
const storageTotal = document.querySelector("#storage-total");
const connectionWarning = document.querySelector("#connection-warning");
const separationKindInputs = document.querySelectorAll('input[name="separation-kind"]');
const fileMode = window.location.protocol === "file:";

function nativeDesktopApi() {
  return window.pywebview?.api?.save_job_file || null;
}

async function saveDownloadFromDesktop(event, jobId, fileKind) {
  const saveJobFile = nativeDesktopApi();
  if (!saveJobFile) return;
  event.preventDefault();
  const link = event.currentTarget;
  const originalText = link.textContent;
  link.textContent = "正在保存…";
  link.setAttribute("aria-disabled", "true");
  try {
    const result = await saveJobFile(jobId, fileKind);
    if (result?.saved) {
      uploadNotice.textContent = `已保存 ${result.filename}。`;
    }
  } catch (error) {
    uploadNotice.textContent = error.message || "保存失败，请重试。";
  } finally {
    link.textContent = originalText;
    link.removeAttribute("aria-disabled");
  }
}

if (fileMode) {
  document.querySelector("#file-mode-card").hidden = false;
  document.querySelector("#upload-card").hidden = true;
  document.querySelector("#workspace").hidden = true;
  document.querySelector(".local-status").innerHTML = "<i></i>等待启动";
}

const allowedExtensions = new Set(["wav", "mp3", "flac", "m4a", "ogg"]);
const activeStatuses = new Set(["queued", "preprocessing", "separating", "postprocessing"]);
const activeOperationStates = new Set(["queued", "running"]);
const statusLabels = {
  queued: "等待中",
  preprocessing: "预处理中",
  separating: "分离中",
  postprocessing: "整理中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
const cards = new Map();
let selectedFile = null;
let uploadBusy = false;
let refreshInFlight = false;
let refreshRequested = false;
let pollTimer = null;
let hasActiveWork = true;

function selectedSeparationKind() {
  return document.querySelector('input[name="separation-kind"]:checked')?.value || "drums";
}

function updateModuleSelection() {
  const kind = selectedSeparationKind();
  document.querySelectorAll(".module-option").forEach((option) => {
    const selected = option.querySelector("input").checked;
    option.querySelector("i").textContent = selected ? "已选择" : "选择";
  });
  if (!uploadBusy) {
    uploadButton.textContent = kind === "vocals" ? "开始分离人声" : "开始分离鼓轨";
  }
}

separationKindInputs.forEach((input) => input.addEventListener("change", updateModuleSelection));
updateModuleSelection();

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / (1024 ** index);
  return `${value.toFixed(index === 0 || value >= 100 ? 0 : 1)} ${units[index]}`;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return "--:--";
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${minutes}:${String(secs).padStart(2, "0")}`;
}

function formatBarOffset(value) {
  const offset = Number(value) || 0;
  if (offset === 0) return "自动小节线";
  return `小节线${offset > 0 ? "后移" : "前移"} ${Math.abs(offset)} 拍`;
}

function updateMidiButton(card) {
  const button = card.querySelector(".midi-generate-button");
  if (card.dataset.midiBusy === "true") {
    button.textContent = card.dataset.operationState === "queued" ? "MIDI 已排队" : "正在生成 MIDI…";
    return;
  }
  const offsetSelect = card.querySelector(".midi-bar-offset");
  const modelSelect = card.querySelector(".midi-model");
  const meterSelect = card.querySelector(".midi-meter");
  const midiReady = button.dataset.force === "true";
  const optionsChanged = (
    offsetSelect.value !== offsetSelect.dataset.savedOffset
    || modelSelect.value !== modelSelect.dataset.savedModel
    || meterSelect.value !== meterSelect.dataset.savedMeter
  );
  button.textContent = midiReady
    ? (optionsChanged ? "应用并生成" : "重新生成")
    : "生成 MIDI";
}

function fileExtension(name) {
  return name.includes(".") ? name.split(".").pop().toLowerCase() : "";
}

function chooseFile(file) {
  uploadNotice.textContent = "";
  if (!file) return;
  if (!allowedExtensions.has(fileExtension(file.name))) {
    uploadNotice.textContent = "请选择 WAV、MP3、FLAC、M4A 或 OGG 文件。";
    return;
  }
  if (file.size > 500 * 1024 * 1024) {
    uploadNotice.textContent = "文件超过 500MB 限制。";
    return;
  }
  selectedFile = file;
  selectedName.textContent = file.name;
  selectedMeta.textContent = `${formatBytes(file.size)} · 上传后自动检查时长`;
  uploadSelection.hidden = false;
}

fileInput.addEventListener("change", () => chooseFile(fileInput.files[0]));
["dragenter", "dragover"].forEach((eventName) => {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add("dragging");
  });
});
["dragleave", "drop"].forEach((eventName) => {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
  });
});
dropZone.addEventListener("drop", (event) => chooseFile(event.dataTransfer.files[0]));

uploadButton.addEventListener("click", async () => {
  if (!selectedFile || uploadBusy) return;
  uploadBusy = true;
  uploadButton.disabled = true;
  uploadButton.textContent = "正在上传…";
  uploadNotice.textContent = "正在检查音频，请保持页面打开。";
  const body = new FormData();
  body.append("file", selectedFile, selectedFile.name);
  body.append("separation_kind", selectedSeparationKind());
  try {
    const response = await fetch("/api/jobs", { method: "POST", body });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "上传失败。");
    const moduleName = payload.separation_kind === "vocals" ? "人声分轨" : "鼓轨分轨";
    uploadNotice.textContent = payload.status === "completed"
      ? "检测到相同音频，已复用现有结果。"
      : `${moduleName}任务已进入处理队列。`;
    selectedFile = null;
    fileInput.value = "";
    uploadSelection.hidden = true;
    await refreshJobs();
  } catch (error) {
    uploadNotice.textContent = error.message || "上传失败，请重试。";
  } finally {
    uploadBusy = false;
    uploadButton.disabled = false;
    updateModuleSelection();
  }
});

function createCard(job) {
  const card = jobTemplate.content.firstElementChild.cloneNode(true);
  card.dataset.jobId = job.id;
  card.querySelector(".cancel-button").addEventListener("click", () => act(job.id, "cancel"));
  card.querySelector(".retry-button").addEventListener("click", () => act(job.id, "retry"));
  card.querySelector(".compress-button").addEventListener("click", () => compressJob(job.id));
  card.querySelector(".midi-generate-button").addEventListener("click", (event) => {
    generateMidi(job.id, event.currentTarget);
  });
  card.querySelector(".midi-bar-offset").addEventListener("change", () => {
    updateMidiButton(card);
  });
  card.querySelector(".midi-model").addEventListener("change", () => {
    updateMidiButton(card);
  });
  card.querySelector(".midi-meter").addEventListener("change", () => {
    updateMidiButton(card);
  });
  [
    [".download-drums", "drums"],
    [".download-no-drums", "no_drums"],
    [".download-vocals", "vocals"],
    [".download-instrumental", "instrumental"],
    [".midi-download", "midi"],
  ].forEach(([selector, fileKind]) => {
    card.querySelector(selector).addEventListener("click", (event) => {
      saveDownloadFromDesktop(event, job.id, fileKind);
    });
  });
  const midiSlider = card.querySelector(".midi-window-slider");
  let midiSliderTimer = null;
  midiSlider.addEventListener("input", () => {
    card.querySelector(".midi-window-value").textContent = previewPositionLabel(
      Number(midiSlider.value),
    );
    window.clearTimeout(midiSliderTimer);
    midiSliderTimer = window.setTimeout(() => {
      if (card.latestJob) {
        loadMidiPreview(card, card.latestJob, Number(midiSlider.value));
      }
    }, 120);
  });
  card.querySelector(".delete-button").addEventListener("click", () => removeJob(job.id));
  cards.set(job.id, card);
  return card;
}

function setAudioSource(element, source) {
  const absolute = source ? new URL(source, window.location.href).href : "";
  if (element.src !== absolute) {
    element.src = source || "";
  }
}

function updateCard(card, job) {
  card.latestJob = job;
  const operationActive = activeOperationStates.has(job.operation_state);
  const operationFailed = job.operation_state === "failed";
  const active = activeStatuses.has(job.status) || operationActive;
  const midiWorking = operationActive && job.operation_kind === "midi";
  const visibleStage = (operationActive || operationFailed) ? (job.operation_stage || job.stage) : job.stage;
  const visibleProgress = operationActive ? job.operation_progress : job.progress;
  card.dataset.status = job.status;
  card.dataset.active = String(active);
  card.dataset.operationState = job.operation_state || "idle";
  card.dataset.midiBusy = String(midiWorking);
  card.querySelector(".job-name").textContent = job.original_name;
  const moduleName = job.separation_kind === "vocals" ? "人声分轨" : "鼓轨分轨";
  card.querySelector(".job-meta").textContent = [
    moduleName,
    formatDuration(job.duration_seconds),
    formatBytes(job.storage_bytes),
  ].join(" · ");
  const operationLabel = job.operation_kind === "midi" ? "MIDI" : "压缩";
  card.querySelector(".status-pill").textContent = operationActive
    ? `${operationLabel}${job.operation_state === "queued" ? "排队中" : "处理中"}`
    : (operationFailed ? `${operationLabel}失败` : (statusLabels[job.status] || job.status));
  card.querySelector(".stage-text").textContent = `${visibleStage} · ${visibleProgress}%`;
  card.querySelector(".elapsed-text").textContent = job.started_at
    ? `已用 ${formatDuration(job.elapsed_seconds)}`
    : "尚未开始";
  card.querySelector(".progress-value").style.width = `${visibleProgress}%`;
  card.querySelector(".progress-area").hidden = job.status === "completed" && !operationActive;

  const error = card.querySelector(".error-message");
  const errorMessage = job.error || job.operation_error;
  error.hidden = !errorMessage;
  error.textContent = errorMessage || "";
  const warning = card.querySelector(".warning-message");
  warning.hidden = !job.warnings?.length;
  warning.textContent = job.warnings?.join("\n") || "";

  const players = card.querySelector(".players");
  const isVocalJob = job.separation_kind === "vocals";
  const hasDrumResults = !isVocalJob && job.files.drums && job.files.no_drums;
  const hasVocalResults = isVocalJob && job.files.vocals && job.files.instrumental;
  const hasResults = job.status === "completed" && (hasDrumResults || hasVocalResults);
  players.hidden = !hasResults;
  card.querySelector(".drum-results").hidden = !hasDrumResults;
  card.querySelector(".vocal-results").hidden = !hasVocalResults;
  if (hasResults) {
    const formatLabel = job.output_format === "wav" ? "旧版 WAV" : "轻量 MP3";
    card.querySelectorAll(".format-label").forEach((label) => {
      label.textContent = formatLabel;
    });
  }
  if (hasVocalResults) {
    setAudioSource(card.querySelector(".audio-vocals"), job.files.vocals);
    setAudioSource(card.querySelector(".audio-instrumental"), job.files.instrumental);
    card.querySelector(".download-vocals").href = `${job.files.vocals}?download=true`;
    card.querySelector(".download-instrumental").href = `${job.files.instrumental}?download=true`;
  }
  if (hasDrumResults) {
    setAudioSource(card.querySelector(".audio-drums"), job.files.drums);
    setAudioSource(card.querySelector(".audio-no-drums"), job.files.no_drums);
    card.querySelector(".download-drums").href = `${job.files.drums}?download=true`;
    card.querySelector(".download-no-drums").href = `${job.files.no_drums}?download=true`;
    const midiReady = Boolean(job.files.midi);
    const midiButton = card.querySelector(".midi-generate-button");
    const midiDownload = card.querySelector(".midi-download");
    const barOffset = card.querySelector(".midi-bar-offset");
    const midiModel = card.querySelector(".midi-model");
    const midiMeter = card.querySelector(".midi-meter");
    const savedOffset = String(job.midi_bar_offset_beats ?? 0);
    if (barOffset.dataset.savedOffset === undefined || barOffset.value === barOffset.dataset.savedOffset) {
      barOffset.value = savedOffset;
    }
    barOffset.dataset.savedOffset = savedOffset;
    const savedModel = job.midi_model || "adtof";
    if (midiModel.dataset.savedModel === undefined || midiModel.value === midiModel.dataset.savedModel) {
      midiModel.value = savedModel;
    }
    midiModel.dataset.savedModel = savedModel;
    const savedMeter = job.midi_meter || "auto";
    if (midiMeter.dataset.savedMeter === undefined || midiMeter.value === midiMeter.dataset.savedMeter) {
      midiMeter.value = savedMeter;
    }
    midiMeter.dataset.savedMeter = savedMeter;
    midiButton.hidden = false;
    midiButton.dataset.force = String(midiReady);
    midiButton.disabled = operationActive;
    barOffset.disabled = operationActive;
    midiModel.disabled = operationActive;
    midiMeter.disabled = operationActive;
    updateMidiButton(card);
    midiDownload.hidden = !midiReady;
    if (midiReady) {
      midiDownload.href = `${job.files.midi}?download=true`;
      card.querySelector(".midi-meta").textContent = [
        `${job.midi_event_count} 个鼓点`,
        job.midi_tempo_bpm
          ? `${job.midi_beat_unit === 8 ? "♩. " : ""}${job.midi_tempo_bpm} BPM`
          : null,
        job.midi_beats_per_bar
          ? `${job.midi_beats_per_bar}/${job.midi_beat_unit || 4}`
          : null,
        formatBarOffset(job.midi_bar_offset_beats),
        job.midi_quantized ? "已量化" : null,
        job.midi_engine,
        job.midi_warning ? "使用回退" : null,
      ].filter(Boolean).join(" · ");
      card.querySelector(".midi-meta").title = job.midi_warning || "";
      renderMidiPreview(card, job);
    } else {
      card.querySelector(".midi-meta").textContent = "快速五鼓件 / STRUM 高精度八鼓件";
      card.querySelector(".midi-meta").title = "";
      card.querySelector(".midi-preview").hidden = true;
      card.dataset.midiPreviewBaseKey = "";
      card.dataset.midiPreviewRequestKey = "";
    }
  }

  card.querySelector(".cancel-button").hidden = !active;
  card.querySelector(".retry-button").hidden = !["failed", "cancelled"].includes(job.status);
  card.querySelector(".compress-button").hidden = !(job.status === "completed" && job.output_format === "wav" && !operationActive);
  card.querySelector(".delete-button").hidden = active;
}

const SVG_NS = "http://www.w3.org/2000/svg";

function svgElement(name, attributes = {}) {
  const element = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
  return element;
}

async function renderMidiPreview(card, job) {
  const baseKey = `${job.files.midi}:${job.updated_at}`;
  if (card.dataset.midiPreviewBaseKey === baseKey) return;
  card.dataset.midiPreviewBaseKey = baseKey;
  await loadMidiPreview(card, job, null);
}

function previewPositionLabel(startBar) {
  return startBar === 0 ? "弱起小节" : `第 ${startBar} 小节`;
}

async function loadMidiPreview(card, job, requestedStartBar) {
  const baseKey = `${job.files.midi}:${job.updated_at}`;
  const requestKey = `${baseKey}:${requestedStartBar ?? "auto"}`;
  card.dataset.midiPreviewRequestKey = requestKey;
  const preview = card.querySelector(".midi-preview");
  const canvas = card.querySelector(".midi-preview-canvas");
  const slider = card.querySelector(".midi-window-slider");
  preview.hidden = false;
  canvas.textContent = "正在读取 MIDI 预览…";
  slider.disabled = true;
  try {
    const params = new URLSearchParams({ bars: "4" });
    if (requestedStartBar !== null) {
      params.set("start_bar", String(requestedStartBar));
    }
    const response = await fetch(`/api/jobs/${job.id}/midi/preview?${params}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "无法生成预览。");
    if (!card.isConnected || card.dataset.midiPreviewRequestKey !== requestKey) return;
    drawMidiPreview(canvas, payload);
    slider.min = String(payload.min_start_bar);
    slider.max = String(payload.max_start_bar);
    slider.value = String(payload.start_bar);
    slider.disabled = payload.min_start_bar === payload.max_start_bar;
    const firstBar = previewPositionLabel(payload.start_bar);
    preview.querySelector(".midi-window-value").textContent = firstBar;
    preview.querySelector(".midi-preview-heading span").textContent = `${firstBar}起 · ${payload.notes.length} 个可见鼓点`;
  } catch (error) {
    canvas.textContent = error.message || "MIDI 预览加载失败。";
    slider.disabled = false;
  }
}

function drawMidiPreview(container, payload) {
  const width = 820;
  const height = 260;
  const left = 76;
  const right = 16;
  const top = 20;
  const bottom = 20;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const beatUnit = payload.beat_unit || 4;
  const totalTicks = (
    payload.bar_count
    * payload.beats_per_bar
    * payload.ticks_per_beat
    * 4
    / beatUnit
  );
  const lanes = payload.lanes || [];
  const laneHeight = plotHeight / Math.max(1, lanes.length);
  const svg = svgElement("svg", {
    viewBox: `0 0 ${width} ${height}`,
    width,
    height,
    "aria-hidden": "true",
  });

  lanes.forEach((lane, index) => {
    const y = top + index * laneHeight;
    const label = svgElement("text", { x: left - 10, y: y + laneHeight * 0.66, "text-anchor": "end", class: "midi-lane-label" });
    label.textContent = lane.label;
    svg.append(label);
    svg.append(svgElement("line", { x1: left, y1: y + laneHeight, x2: width - right, y2: y + laneHeight, class: "midi-lane-line" }));
  });

  const beatCount = payload.bar_count * payload.beats_per_bar;
  for (let beat = 0; beat <= beatCount; beat += 1) {
    const x = left + (beat / beatCount) * plotWidth;
    const isBar = beat % payload.beats_per_bar === 0;
    const isCompoundGroup = (
      !isBar
      && beatUnit === 8
      && payload.beats_per_bar % 3 === 0
      && beat % 3 === 0
    );
    svg.append(svgElement("line", {
      x1: x,
      y1: top,
      x2: x,
      y2: height - bottom,
      class: isBar
        ? "midi-bar-line"
        : (isCompoundGroup ? "midi-compound-beat-line" : "midi-beat-line"),
    }));
    if (isBar && beat < beatCount) {
      const barNumber = payload.start_bar === 0 && beat === 0
        ? "弱起"
        : String(Math.max(1, payload.start_bar + beat / payload.beats_per_bar));
      const label = svgElement("text", { x: x + 5, y: 13, class: "midi-bar-label" });
      label.textContent = barNumber;
      svg.append(label);
    }
  }

  const laneIndex = new Map(lanes.map((lane, index) => [lane.id, index]));
  (payload.notes || []).forEach((note) => {
    const index = laneIndex.get(note.lane) ?? 0;
    const x = left + (Math.max(0, note.tick) / Math.max(1, totalTicks)) * plotWidth;
    const y = top + (index + 0.5) * laneHeight;
    const radius = 3.6 + Math.max(0, Math.min(1, note.velocity / 127)) * 2.5;
    svg.append(svgElement("circle", { cx: x, cy: y, r: radius, class: `midi-note midi-note-${note.lane}` }));
  });
  container.replaceChildren(svg);
}

async function generateMidi(jobId, button) {
  const force = button.dataset.force === "true";
  const card = button.closest(".job-card");
  const barOffset = card.querySelector(".midi-bar-offset");
  const midiModel = card.querySelector(".midi-model");
  const midiMeter = card.querySelector(".midi-meter");
  const offset = Number(barOffset.value) || 0;
  const model = midiModel.value;
  const meter = midiMeter.value;
  const modelName = model === "strum" ? "STRUM 高精度" : "ADTOF 快速";
  card.dataset.midiBusy = "true";
  button.disabled = true;
  barOffset.disabled = true;
  midiModel.disabled = true;
  midiMeter.disabled = true;
  button.textContent = model === "strum" ? "STRUM 正在识别…" : (force ? "正在重新生成…" : "正在识别鼓点…");
  const meterLabel = meter === "auto" ? "自动识别拍号" : `${meter} 拍号`;
  uploadNotice.textContent = `正在使用 ${modelName} 生成 MIDI · ${meterLabel} · ${formatBarOffset(offset)}。`;
  try {
    const params = new URLSearchParams({
      bar_offset_beats: String(offset),
      midi_model: model,
      meter,
    });
    if (force) params.set("force", "true");
    const response = await fetch(`/api/jobs/${jobId}/midi?${params}`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "MIDI 生成失败。");
    uploadNotice.textContent = payload.operation_state === "queued"
      ? (payload.operation_stage || "MIDI 已进入本地计算队列。")
      : `MIDI 已就绪：${payload.midi_event_count} 个鼓点，${payload.midi_tempo_bpm} BPM。`;
    await refreshJobs();
  } catch (error) {
    uploadNotice.textContent = error.message || "MIDI 生成失败，请重试。";
  } finally {
    const stillBusy = card.dataset.midiBusy === "true";
    button.disabled = stillBusy;
    barOffset.disabled = stillBusy;
    midiModel.disabled = stillBusy;
    midiMeter.disabled = stillBusy;
    updateMidiButton(card);
  }
}

async function compressJob(jobId) {
  if (!window.confirm("把旧 WAV 结果压缩为高质量 MP3？完成后会删除两条旧 WAV。")) return;
  uploadNotice.textContent = "正在压缩旧结果，请稍候…";
  try {
    const response = await fetch(`/api/jobs/${jobId}/compress`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "压缩失败。");
    uploadNotice.textContent = payload.operation_state === "queued"
      ? "压缩任务已进入本地计算队列。"
      : "压缩完成，旧 WAV 已清理。";
    await refreshJobs();
  } catch (error) {
    uploadNotice.textContent = error.message || "压缩失败，请重试。";
  }
}

async function act(jobId, action) {
  try {
    const response = await fetch(`/api/jobs/${jobId}/${action}`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "操作失败。");
    await refreshJobs();
  } catch (error) {
    uploadNotice.textContent = error.message || "操作失败。";
  }
}

async function removeJob(jobId) {
  if (!window.confirm("删除这条任务及其本地音频文件？此操作无法撤销。")) return;
  try {
    const response = await fetch(`/api/jobs/${jobId}`, { method: "DELETE" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "删除失败。");
    await refreshJobs();
  } catch (error) {
    uploadNotice.textContent = error.message || "删除失败。";
  }
}

function renderJobs(payload) {
  const jobs = payload.jobs || [];
  const ids = new Set(jobs.map((job) => job.id));
  for (const [id, card] of cards) {
    if (!ids.has(id)) {
      card.remove();
      cards.delete(id);
    }
  }
  jobs.forEach((job, index) => {
    const card = cards.get(job.id) || createCard(job);
    updateCard(card, job);
    const expectedNode = jobList.children[index];
    if (expectedNode !== card) jobList.insertBefore(card, expectedNode || null);
  });
  emptyState.hidden = jobs.length > 0;
  jobCount.textContent = String(jobs.length);
  storageTotal.textContent = formatBytes(payload.total_storage_bytes || 0);
  return jobs.some((job) => (
    activeStatuses.has(job.status) || activeOperationStates.has(job.operation_state)
  ));
}

async function refreshJobs() {
  if (refreshInFlight) {
    refreshRequested = true;
    return;
  }
  refreshInFlight = true;
  if (pollTimer) window.clearTimeout(pollTimer);
  try {
    const response = await fetch("/api/jobs", { cache: "no-store" });
    if (!response.ok) throw new Error("服务响应异常");
    hasActiveWork = renderJobs(await response.json());
    connectionWarning.hidden = true;
  } catch (_) {
    connectionWarning.hidden = false;
  } finally {
    refreshInFlight = false;
    const delay = refreshRequested
      ? 0
      : (document.hidden ? 30000 : (hasActiveWork ? 1500 : 12000));
    refreshRequested = false;
    pollTimer = window.setTimeout(refreshJobs, delay);
  }
}

if (!fileMode) {
  refreshJobs();
  document.addEventListener("visibilitychange", () => {
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(refreshJobs, document.hidden ? 30000 : 0);
  });
}
