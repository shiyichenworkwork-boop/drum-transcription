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
const fileMode = window.location.protocol === "file:";

if (fileMode) {
  document.querySelector("#file-mode-card").hidden = false;
  document.querySelector("#upload-card").hidden = true;
  document.querySelector("#workspace").hidden = true;
  document.querySelector(".local-status").innerHTML = "<i></i>等待启动";
}

const allowedExtensions = new Set(["wav", "mp3", "flac", "m4a", "ogg"]);
const activeStatuses = new Set(["queued", "preprocessing", "separating", "postprocessing"]);
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
  try {
    const response = await fetch("/api/jobs", { method: "POST", body });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "上传失败。");
    uploadNotice.textContent = payload.status === "completed"
      ? "检测到相同音频，已复用现有结果。"
      : "任务已进入处理队列。";
    selectedFile = null;
    fileInput.value = "";
    uploadSelection.hidden = true;
    await refreshJobs();
  } catch (error) {
    uploadNotice.textContent = error.message || "上传失败，请重试。";
  } finally {
    uploadBusy = false;
    uploadButton.disabled = false;
    uploadButton.textContent = "开始分离鼓轨";
  }
});

function createCard(job) {
  const card = jobTemplate.content.firstElementChild.cloneNode(true);
  card.dataset.jobId = job.id;
  card.querySelector(".cancel-button").addEventListener("click", () => act(job.id, "cancel"));
  card.querySelector(".retry-button").addEventListener("click", () => act(job.id, "retry"));
  card.querySelector(".compress-button").addEventListener("click", () => compressJob(job.id));
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
  const active = activeStatuses.has(job.status);
  card.dataset.status = job.status;
  card.dataset.active = String(active);
  card.querySelector(".job-name").textContent = job.original_name;
  card.querySelector(".job-meta").textContent = [
    formatDuration(job.duration_seconds),
    formatBytes(job.storage_bytes),
  ].join(" · ");
  card.querySelector(".status-pill").textContent = statusLabels[job.status] || job.status;
  card.querySelector(".stage-text").textContent = `${job.stage} · ${job.progress}%`;
  card.querySelector(".elapsed-text").textContent = job.started_at
    ? `已用 ${formatDuration(job.elapsed_seconds)}`
    : "尚未开始";
  card.querySelector(".progress-value").style.width = `${job.progress}%`;
  card.querySelector(".progress-area").hidden = job.status === "completed";

  const error = card.querySelector(".error-message");
  error.hidden = !job.error;
  error.textContent = job.error || "";
  const warning = card.querySelector(".warning-message");
  warning.hidden = !job.warnings?.length;
  warning.textContent = job.warnings?.join("\n") || "";

  const players = card.querySelector(".players");
  const hasResults = job.status === "completed" && job.files.drums && job.files.no_drums;
  players.hidden = !hasResults;
  if (hasResults) {
    const formatLabel = job.output_format === "wav" ? "旧版 WAV" : "轻量 MP3";
    card.querySelectorAll(".format-label").forEach((label) => {
      label.textContent = formatLabel;
    });
    setAudioSource(card.querySelector(".audio-drums"), job.files.drums);
    setAudioSource(card.querySelector(".audio-no-drums"), job.files.no_drums);
    card.querySelector(".download-drums").href = `${job.files.drums}?download=true`;
    card.querySelector(".download-no-drums").href = `${job.files.no_drums}?download=true`;
  }

  card.querySelector(".cancel-button").hidden = !active;
  card.querySelector(".retry-button").hidden = !["failed", "cancelled"].includes(job.status);
  card.querySelector(".compress-button").hidden = !(job.status === "completed" && job.output_format === "wav");
  card.querySelector(".delete-button").hidden = active;
}

async function compressJob(jobId) {
  if (!window.confirm("把旧 WAV 结果压缩为高质量 MP3？完成后会删除两条旧 WAV。")) return;
  uploadNotice.textContent = "正在压缩旧结果，请稍候…";
  try {
    const response = await fetch(`/api/jobs/${jobId}/compress`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "压缩失败。");
    uploadNotice.textContent = "压缩完成，旧 WAV 已清理。";
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
}

async function refreshJobs() {
  try {
    const response = await fetch("/api/jobs", { cache: "no-store" });
    if (!response.ok) throw new Error("服务响应异常");
    renderJobs(await response.json());
    connectionWarning.hidden = true;
  } catch (_) {
    connectionWarning.hidden = false;
  }
}

if (!fileMode) {
  refreshJobs();
  window.setInterval(refreshJobs, 2000);
}
