/**
 * Anansi Mini App — vanilla JS (no framework).
 *
 * Renders a parameter-editing form inside a Telegram Mini App popup.
 * Flow: open → GET /api/mini-app/form-data → render fields → user edits →
 *       MainButton "Submit" → POST /api/mini-app/submit → close.
 *
 * Sign mode (view=sign):
 * Flow: open → GET /api/mini-app/sign-data → PDF.js render → place sig →
 *       signature_pad draw → MainButton "Submit signature" →
 *       POST /api/mini-app/sign/submit → show accepted → close.
 */
import "./assets/anansi-styles.css";

// ---------------------------------------------------------------------------
// Telegram SDK helpers
// ---------------------------------------------------------------------------

// SDK reference — ready() and expand() are already called in index.html
// immediately after the SDK script loads (per Telegram docs: "as early as possible").
let tg = window.Telegram?.WebApp || null;

function getInitData() {
  return tg?.initData || "";
}

function getThemeParams() {
  return tg?.themeParams || {};
}

function showMainButton(text, onClick) {
  if (!tg) return;
  tg.MainButton.setText(text);
  tg.MainButton.onClick(onClick);
  tg.MainButton.show();
}

function setMainButtonLoading(loading) {
  if (!tg) return;
  if (loading) {
    tg.MainButton.showProgress();
    tg.MainButton.disable();
  } else {
    tg.MainButton.hideProgress();
    tg.MainButton.enable();
  }
}

function closeMiniApp() {
  if (tg) tg.close();
  else window.close();
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function apiFetch(path, options = {}) {
  const initData = getInitData();
  const headers = {
    "Content-Type": "application/json",
    ...options.headers,
  };
  // Only add auth header if initData is available (may be empty on desktop/web)
  if (initData) {
    headers.Authorization = `tma ${initData}`;
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const resp = await fetch(path, { ...options, headers, signal: controller.signal });
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${resp.status}`);
    }
    return resp.json();
  } catch (err) {
    if (err.name === "AbortError") throw new Error("Request timed out");
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}

// ---------------------------------------------------------------------------
// DOM rendering
// ---------------------------------------------------------------------------

const root = document.getElementById("app");

function applyTheme() {
  const tp = getThemeParams();
  const bg = tp.bg_color || "#ffffff";
  const text = tp.text_color || "#000000";
  const s = root.style;
  s.setProperty("--tg-bg", bg);
  s.setProperty("--tg-text", text);
  s.setProperty("--tg-hint", tp.hint_color || "#999999");
  s.setProperty("--tg-button", tp.button_color || "#3390ec");
  s.setProperty("--tg-button-text", tp.button_text_color || "#ffffff");
  s.setProperty("--tg-theme-secondary-bg-color", tp.secondary_bg_color || "#f5f5f5");
  s.setProperty("--tg-theme-section-bg-color", tp.section_bg_color || tp.secondary_bg_color || "#f5f5f5");
  s.setProperty("--tg-theme-section-header-text-color", tp.section_header_text_color || "#999999");
  s.setProperty("--tg-theme-section-separator-color", tp.section_separator_color || "#e0e0e0");
  s.setProperty("--tg-theme-link-color", tp.link_color || "#3390ec");
  s.backgroundColor = bg;
  s.color = text;
  // Set html/body background to match — on Telegram Web the iframe defaults
  // to white, causing a visible border around the app content in dark mode.
  document.documentElement.style.backgroundColor = bg;
  document.body.style.backgroundColor = bg;
}

function showState(cls, message) {
  root.innerHTML = `<div class="state-screen ${cls}"><p>${message}</p></div>`;
}

function renderForm(data, values) {
  let html = `<div class="form-container">
    <h2 class="form-title">${esc(data.packet_title)}</h2>
    <p class="form-hint">Leave blank to use calculated defaults.</p>`;

  for (const field of data.fields) {
    const val = values[field.key] ?? "";
    const suffix = field.suffix
      ? `<span class="field-suffix">${esc(field.suffix)}</span>`
      : "";
    const placeholder = field.suffix ? `Auto (${esc(field.suffix)})` : "Auto";
    html += `
    <div class="anansi-form-row">
      <label class="anansi-form-label" for="${esc(field.key)}">
        ${esc(field.label)} ${suffix}
      </label>
      <input
        id="${esc(field.key)}"
        name="${esc(field.key)}"
        type="${field.type || "text"}"
        ${field.step != null ? `step="${field.step}"` : ""}
        ${field.min != null ? `min="${field.min}"` : ""}
        value="${esc(String(val))}"
        class="anansi-input"
        placeholder="${placeholder}"
      />
    </div>`;
  }

  html += `<button class="cancel-btn" type="button">Cancel</button></div>`;
  root.innerHTML = html;

  root.querySelector(".cancel-btn").addEventListener("click", closeMiniApp);
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function collectValues(fields) {
  const out = {};
  for (const f of fields) {
    const el = document.getElementById(f.key);
    if (el && el.value !== "") out[f.key] = el.value;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const params = new URLSearchParams(window.location.search);
const packetId = params.get("packet_id");
const instanceId = params.get("instance_id");
const viewMode = params.get("view");
const formType = params.get("form_type");
const urlSig = params.get("sig") || "";
let formData = null;

async function loadForm() {
  if (!packetId || !formType) {
    showState("error", "Missing packet_id or form_type");
    return;
  }
  try {
    formData = await apiFetch(
      `/api/mini-app/form-data?packet_id=${encodeURIComponent(packetId)}&form_type=${encodeURIComponent(formType)}`
    );
    renderForm(formData, formData.values || {});
  } catch (err) {
    showState("error", esc(err.message));
  }
}

async function submitForm() {
  if (!formData) return;
  setMainButtonLoading(true);
  try {
    await apiFetch("/api/mini-app/submit", {
      method: "POST",
      body: JSON.stringify({
        packet_id: packetId,
        form_type: formType,
        values: collectValues(formData.fields),
      }),
    });
    showState("success", "Parameters updated");
    setTimeout(closeMiniApp, 800);
  } catch (err) {
    showState("error", esc(err.message));
  } finally {
    setMainButtonLoading(false);
  }
}

function stateDataUrl() {
  let url = `/api/mini-app/state-data?packet_id=${encodeURIComponent(packetId)}`;
  if (urlSig) url += `&sig=${encodeURIComponent(urlSig)}`;
  return url;
}

function agentStateDataUrl() {
  let url = `/api/mini-app/agent-state?instance_id=${encodeURIComponent(instanceId)}`;
  if (urlSig) url += `&sig=${encodeURIComponent(urlSig)}`;
  return url;
}

async function loadStateView() {
  if (!packetId) {
    showState("error", "Missing packet_id");
    return;
  }
  try {
    const data = await apiFetch(stateDataUrl());
    renderStateView(data);
    if (!isTerminalStatus(data.packet_status)) startPolling();
  } catch (err) {
    showState("error", esc(err.message));
  }
}

async function loadAgentStateView() {
  if (!instanceId) {
    showState("error", "Missing instance_id");
    return;
  }
  try {
    const data = await apiFetch(agentStateDataUrl());
    renderStateView(data);
    // Agents don't reach terminal status — poll at a slower interval (30s)
    startPolling(30000, agentStateDataUrl);
  } catch (err) {
    showState("error", esc(err.message));
  }
}

// ---------------------------------------------------------------------------
// Image helpers
// ---------------------------------------------------------------------------

function isBase64Image(key, val) {
  if (typeof val !== "string") return false;
  // Detect by key name pattern (*_b64, *_base64, *_png_b64)
  if (/_b64$/i.test(key) || /_base64$/i.test(key)) return val.length > 100;
  // Detect data URI
  if (/^data:image\//.test(val)) return true;
  return false;
}

function isDriveImage(key, val) {
  if (typeof val !== "string") return false;
  // Keys ending in _image_url with a proxy URL value (set by backend for _drive_id keys)
  return /_image_url$/i.test(key) && val.includes("/api/mini-app/drive-image");
}

function isImageEntry(key, val) {
  return isBase64Image(key, val) || isDriveImage(key, val);
}

function toImageSrc(key, val) {
  // Drive proxy URLs are already valid src attributes
  if (isDriveImage(key, val)) return val;
  if (/^data:image\//.test(val)) return val;
  return `data:image/png;base64,${val}`;
}

let lightboxEl = null;

function openLightbox(src) {
  if (lightboxEl) lightboxEl.remove();

  lightboxEl = document.createElement("div");
  lightboxEl.className = "lightbox";
  lightboxEl.innerHTML = `<div class="lightbox-content">
    <img src="${src}" class="lightbox-img" />
  </div>`;
  root.appendChild(lightboxEl);

  const img = lightboxEl.querySelector(".lightbox-img");
  let scale = 1, lastScale = 1, posX = 0, posY = 0;
  let startDist = 0, startX = 0, startY = 0, isDragging = false;

  function applyTransform() {
    img.style.transform = `translate(${posX}px, ${posY}px) scale(${scale})`;
  }

  function getTouchDist(touches) {
    const dx = touches[0].clientX - touches[1].clientX;
    const dy = touches[0].clientY - touches[1].clientY;
    return Math.hypot(dx, dy);
  }

  img.addEventListener("touchstart", (e) => {
    if (e.touches.length === 2) {
      e.preventDefault();
      startDist = getTouchDist(e.touches);
      lastScale = scale;
    } else if (e.touches.length === 1) {
      isDragging = true;
      startX = e.touches[0].clientX - posX;
      startY = e.touches[0].clientY - posY;
    }
  }, { passive: false });

  img.addEventListener("touchmove", (e) => {
    if (e.touches.length === 2) {
      e.preventDefault();
      const dist = getTouchDist(e.touches);
      scale = Math.min(Math.max(lastScale * (dist / startDist), 0.5), 5);
      applyTransform();
    } else if (e.touches.length === 1 && isDragging) {
      e.preventDefault();
      posX = e.touches[0].clientX - startX;
      posY = e.touches[0].clientY - startY;
      applyTransform();
    }
  }, { passive: false });

  img.addEventListener("touchend", () => { isDragging = false; });

  // Double-tap to reset
  let lastTap = 0;
  img.addEventListener("click", () => {
    const now = Date.now();
    if (now - lastTap < 300) {
      scale = 1; posX = 0; posY = 0;
      applyTransform();
    }
    lastTap = now;
  });

  // Mouse wheel zoom (desktop)
  img.addEventListener("wheel", (e) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    scale = Math.min(Math.max(scale * delta, 0.5), 5);
    applyTransform();
  }, { passive: false });

  // Close on backdrop click (not on image)
  lightboxEl.addEventListener("click", (e) => {
    if (e.target === lightboxEl || e.target.classList.contains("lightbox-content")) {
      lightboxEl.remove();
      lightboxEl = null;
    }
  });
}

// ---------------------------------------------------------------------------
// State view rendering
// ---------------------------------------------------------------------------

// Track last state data for diffing
let lastStateData = null;
let pollTimer = null;

function stepIcon(status) {
  return status === "success" ? "\u2713"
    : status === "failed" ? "\u2717"
    : status === "in_progress" ? "\u22EF"
    : "\u25CB";
}

function safeStatus(status) {
  return (status || "").replace(/[^a-z_]/g, "");
}

function formatValue(val, numFmt) {
  if (typeof val === "number") return numFmt.format(val);
  if (typeof val === "boolean") return val ? "true" : "false";
  if (Array.isArray(val)) {
    // Format arrays of objects nicely (e.g. sites_to_process)
    return esc(val.map((v) => (typeof v === "object" && v !== null ? v.name || JSON.stringify(v) : String(v))).join(", "));
  }
  if (typeof val === "object" && val !== null) {
    return esc(val.name || JSON.stringify(val));
  }
  if (typeof val === "string" && val.startsWith("http")) {
    return `<a href="${esc(val)}" target="_blank">${esc(
      val.length > 40 ? val.substring(0, 40) + "\u2026" : val
    )}</a>`;
  }
  return esc(String(val));
}

function renderStateView(data) {
  lastStateData = data;
  const lang = tg?.initDataUnsafe?.user?.language_code || "en";
  const numFmt = new Intl.NumberFormat(lang, { maximumFractionDigits: 2 });

  // Separate image entries from regular entries
  const imageEntries = [];
  const regularEntries = [];
  for (const entry of (data.state || [])) {
    if (isImageEntry(entry.key, entry.value)) {
      imageEntries.push(entry);
    } else {
      regularEntries.push(entry);
    }
  }

  let html = `<div class="state-container">`;

  // Header
  html += `<div class="state-header">
    <h2 class="state-title">${esc(data.packet_title || data.packet_type)}</h2>
    <span class="state-badge state-badge--${safeStatus(data.packet_status)}" data-role="status-badge">${esc(data.packet_status)}</span>
  </div>`;

  // Stale workflow warning
  if (data.is_stale) {
    html += `<div class="state-stale-warning" data-role="stale-warning">
      Workflow appears stuck (no activity for ${data.stale_minutes || "?"}m).
      Send a new command in the chat to retry.
    </div>`;
  }

  // Workflow progress checklist
  if (data.workflow_steps && data.workflow_steps.length > 0) {
    html += `<div class="state-section" data-role="workflow-section">
      <h3 class="state-section-title">Workflow Progress</h3>`;
    for (const step of data.workflow_steps) {
      const ss = safeStatus(step.status);
      html += `<div class="step-item step-item--${ss}" data-step="${esc(step.name)}">
        <span class="step-icon">${stepIcon(step.status)}</span>
        <span class="step-name">${esc(step.description || step.name)}</span>
      </div>`;
    }
    html += `</div>`;
  }

  // State data cards (non-image values)
  if (regularEntries.length > 0) {
    html += `<div class="state-section" data-role="params-section">
      <h3 class="state-section-title">Parameters &amp; Values</h3>`;
    for (const entry of regularEntries) {
      html += `<div class="state-card" data-key="${esc(entry.key)}">
        <div class="state-card-label">${esc(entry.label)}</div>
        <div class="state-card-value">${formatValue(entry.value, numFmt)}</div>
      </div>`;
    }
    html += `</div>`;
  }

  // Image cards
  if (imageEntries.length > 0) {
    html += `<div class="state-section" data-role="images-section">
      <h3 class="state-section-title">Images</h3>`;
    for (const entry of imageEntries) {
      const src = toImageSrc(entry.key, entry.value);
      html += `<div class="state-image-card" data-key="${esc(entry.key)}">
        <div class="state-card-label">${esc(entry.label)}</div>
        <img src="${src}" class="state-image" data-src="${src}" alt="${esc(entry.label)}" />
      </div>`;
    }
    html += `</div>`;
  }

  html += `<button class="cancel-btn" type="button">Close</button></div>`;
  root.innerHTML = html;
  root.querySelector(".cancel-btn").addEventListener("click", () => {
    stopPolling();
    closeMiniApp();
  });

  // Attach lightbox tap handlers to images
  root.querySelectorAll(".state-image").forEach((img) => {
    img.addEventListener("click", () => openLightbox(img.dataset.src));
  });
}

function updateStateView(data) {
  const prev = lastStateData;
  lastStateData = data;
  const lang = tg?.initDataUnsafe?.user?.language_code || "en";
  const numFmt = new Intl.NumberFormat(lang, { maximumFractionDigits: 2 });

  // Update status badge
  if (data.packet_status !== prev.packet_status) {
    const badge = root.querySelector('[data-role="status-badge"]');
    if (badge) {
      badge.className = `state-badge state-badge--${safeStatus(data.packet_status)}`;
      badge.textContent = data.packet_status;
    }
  }

  // Show or hide stale warning
  const existingWarning = root.querySelector('[data-role="stale-warning"]');
  if (data.is_stale && !existingWarning) {
    const header = root.querySelector(".state-header");
    if (header) {
      const div = document.createElement("div");
      div.className = "state-stale-warning";
      div.dataset.role = "stale-warning";
      div.textContent = `Workflow appears stuck (no activity for ${data.stale_minutes || "?"}m). Send a new command in the chat to retry.`;
      header.insertAdjacentElement("afterend", div);
    }
  } else if (!data.is_stale && existingWarning) {
    existingWarning.remove();
  }

  // Update workflow steps in-place
  const prevSteps = new Map((prev.workflow_steps || []).map((s) => [s.name, s]));
  for (const step of (data.workflow_steps || [])) {
    const prevStep = prevSteps.get(step.name);
    const el = root.querySelector(`[data-step="${CSS.escape(step.name)}"]`);
    if (el && (!prevStep || prevStep.status !== step.status)) {
      const ss = safeStatus(step.status);
      el.className = `step-item step-item--${ss}`;
      el.querySelector(".step-icon").textContent = stepIcon(step.status);
    }
    if (!el) {
      // New step appeared — append to workflow section
      const section = root.querySelector('[data-role="workflow-section"]');
      if (section) {
        const div = document.createElement("div");
        const ss = safeStatus(step.status);
        div.className = `step-item step-item--${ss}`;
        div.dataset.step = step.name;
        div.innerHTML = `<span class="step-icon">${stepIcon(step.status)}</span>
          <span class="step-name">${esc(step.description || step.name)}</span>`;
        section.appendChild(div);
      }
    }
  }

  // Update state cards in-place, append new ones
  const prevState = new Map((prev.state || []).map((e) => [e.key, e]));
  for (const entry of (data.state || [])) {
    if (isImageEntry(entry.key, entry.value)) continue; // skip images for diff
    const prevEntry = prevState.get(entry.key);
    const el = root.querySelector(`.state-card[data-key="${CSS.escape(entry.key)}"]`);
    if (el) {
      if (!prevEntry || String(prevEntry.value) !== String(entry.value)) {
        const valEl = el.querySelector(".state-card-value");
        if (valEl) valEl.innerHTML = formatValue(entry.value, numFmt);
      }
    } else {
      // New entry — append to params section (create section if needed)
      let section = root.querySelector('[data-role="params-section"]');
      if (!section) {
        section = document.createElement("div");
        section.className = "state-section";
        section.dataset.role = "params-section";
        section.innerHTML = `<h3 class="state-section-title">Parameters &amp; Values</h3>`;
        const container = root.querySelector(".state-container");
        const closeBtn = container?.querySelector(".cancel-btn");
        if (closeBtn) container.insertBefore(section, closeBtn);
      }
      const card = document.createElement("div");
      card.className = "state-card";
      card.dataset.key = entry.key;
      card.innerHTML = `<div class="state-card-label">${esc(entry.label)}</div>
        <div class="state-card-value">${formatValue(entry.value, numFmt)}</div>`;
      section.appendChild(card);
    }
  }

  // Append new images
  for (const entry of (data.state || [])) {
    if (!isImageEntry(entry.key, entry.value)) continue;
    if (root.querySelector(`.state-image-card[data-key="${CSS.escape(entry.key)}"]`)) continue;
    let section = root.querySelector('[data-role="images-section"]');
    if (!section) {
      section = document.createElement("div");
      section.className = "state-section";
      section.dataset.role = "images-section";
      section.innerHTML = `<h3 class="state-section-title">Images</h3>`;
      const container = root.querySelector(".state-container");
      const closeBtn = container?.querySelector(".cancel-btn");
      if (closeBtn) container.insertBefore(section, closeBtn);
    }
    const src = toImageSrc(entry.key, entry.value);
    const card = document.createElement("div");
    card.className = "state-image-card";
    card.dataset.key = entry.key;
    card.innerHTML = `<div class="state-card-label">${esc(entry.label)}</div>
      <img src="${src}" class="state-image" data-src="${src}" alt="${esc(entry.label)}" />`;
    section.appendChild(card);
    card.querySelector(".state-image").addEventListener("click", () => openLightbox(src));
  }
}

function isTerminalStatus(status) {
  return ["completed", "failed", "cancelled", "terminated"].includes(status);
}

function startPolling(interval = 5000, urlFn = stateDataUrl) {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const data = await apiFetch(urlFn());
      updateStateView(data);
      if (isTerminalStatus(data.packet_status)) stopPolling();
    } catch {
      // Silently ignore poll errors — next poll will retry
    }
  }, interval);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

// ---------------------------------------------------------------------------
// Timeline View (chat chronology)
// ---------------------------------------------------------------------------

async function loadTimelineView() {
  if (!packetId) {
    showState("error", "Missing packet_id");
    return;
  }
  try {
    const data = await apiFetch(stateDataUrl());
    renderTimelineView(data);
  } catch (err) {
    showState("error", esc(err.message));
  }
}

function renderTimelineView(data) {
  const state = {};
  for (const entry of data.state || []) {
    state[entry.key] = entry.value;
  }
  const inputs = {};
  for (const entry of data.state || []) {
    if (["grid_name", "organization", "days_back"].includes(entry.key)) {
      inputs[entry.key] = entry.value;
    }
  }

  const timeline = state.timeline || [];
  const sources = state.sources || [];
  const gridName = inputs.grid_name || data.packet_title || "Timeline";
  const orgName = inputs.organization || "";

  let html = `<div class="timeline-container">`;

  // Header
  html += `<div class="timeline-header">
    <h2 class="timeline-title">${esc(gridName)}</h2>
    <div class="timeline-subtitle">${esc(orgName)} &middot; ${timeline.length} messages</div>
  </div>`;

  // Source chips
  if (sources.length > 0) {
    html += `<div class="timeline-sources">`;
    const sourceColors = {
      group_topic: "#5794F2",
      developer_group: "#73BF69",
      individual: "#FF9830",
    };
    for (const src of sources) {
      const color = sourceColors[src.type] || "#888";
      html += `<span class="timeline-chip" style="border-color:${color};color:${color}">
        ${esc(src.name)} (${src.message_count})
      </span>`;
    }
    html += `</div>`;
  }

  // Timeline
  if (timeline.length === 0) {
    html += `<div class="timeline-empty">No messages found for this period.</div>`;
  } else {
    html += `<div class="timeline-track">`;
    let lastDate = "";
    for (const msg of timeline) {
      // Date separator
      const msgDate = (msg.timestamp || "").substring(0, 10);
      if (msgDate !== lastDate) {
        lastDate = msgDate;
        const dateLabel = formatDateLabel(msgDate);
        html += `<div class="timeline-date-sep">${esc(dateLabel)}</div>`;
      }

      const time = (msg.timestamp || "").substring(11, 16);
      const isBot = msg.role === "model";
      const sourceColor = msg.source_type === "group_topic" ? "#5794F2"
        : msg.source_type === "developer_group" ? "#73BF69" : "#FF9830";

      html += `<div class="timeline-item ${isBot ? "timeline-item--bot" : "timeline-item--user"}">
        <div class="timeline-dot" style="background:${sourceColor}"></div>
        <div class="timeline-card">
          <div class="timeline-card-header">
            <span class="timeline-source" style="color:${sourceColor}">${esc(msg.source)}</span>
            <span class="timeline-time">${esc(time)}</span>
          </div>
          <div class="timeline-card-body">${esc(msg.content)}</div>
          ${isBot ? '<div class="timeline-bot-tag">Bot</div>' : ""}
        </div>
      </div>`;
    }
    html += `</div>`;
  }

  html += `<button class="cancel-btn" type="button">Close</button></div>`;
  root.innerHTML = html;
  root.querySelector(".cancel-btn").addEventListener("click", closeMiniApp);
}

function formatDateLabel(dateStr) {
  try {
    const d = new Date(dateStr + "T00:00:00Z");
    return d.toLocaleDateString("en-US", {
      weekday: "short", month: "short", day: "numeric",
    });
  } catch {
    return dateStr;
  }
}

// ---------------------------------------------------------------------------
// Sign mode
// ---------------------------------------------------------------------------

async function initSignMode() {
  if (!packetId) {
    showState("error", "Missing packet_id");
    return;
  }

  // Dynamic imports — only loaded when sign mode is active.
  const [{ getDocument, GlobalWorkerOptions }, SignaturePad] = await Promise.all([
    import("pdfjs-dist"),
    import("signature_pad").then((m) => m.default),
  ]);

  // PDF.js worker must be set before calling getDocument.
  // Vite copies worker files into the build output directory.
  GlobalWorkerOptions.workerSrc = new URL(
    "pdfjs-dist/build/pdf.worker.min.mjs",
    import.meta.url,
  ).href;

  showState("", "Loading document...");

  // Fetch PDF bytes from our backend (proxied — never raw Drive URL).
  let pdfBytes;
  let requesterName = "";
  let docName = "";
  try {
    const resp = await fetch(
      `/api/mini-app/sign-data?packet_id=${encodeURIComponent(packetId)}`,
      {
        headers: getInitData() ? { Authorization: `tma ${getInitData()}` } : {},
      },
    );
    if (resp.status === 403) {
      showState("error", "This signing link is not valid for your account.");
      return;
    }
    if (resp.status === 409) {
      showState("error", "This document has already been signed.");
      return;
    }
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    requesterName = resp.headers.get("x-requester-name") || "";
    docName = resp.headers.get("x-document-name") || "";
    pdfBytes = await resp.arrayBuffer();
  } catch (err) {
    showState("error", esc(err.message));
    return;
  }

  const pdfDoc = await getDocument({ data: pdfBytes }).promise;
  const totalPages = pdfDoc.numPages;

  // Placement + signature state
  let placedX = null; // normalised [0,1]
  let placedY = null;
  let placedPage = null;
  let sigDataUrl = null;
  let placementMode = false;

  // Build UI — continuous scroll, no pagination
  const bannerHtml = requesterName
    ? `<div class="sign-requester-banner">Signing request from <strong>${esc(requesterName)}</strong>${docName ? ` · ${esc(docName)}` : ""}</div>`
    : "";
  root.innerHTML = `
    ${bannerHtml}
    <div class="sign-container">
      <div class="sign-pages-scroll" id="sign-pages-scroll"></div>
      <div class="sign-footer">
        <button class="btn btn--primary sign-here-btn" id="sign-here-btn">Place signature</button>
      </div>
    </div>
    <div class="sign-pad-modal" id="sign-pad-modal" style="display:none">
      <div class="card sign-pad-card">
        <p class="sign-pad-hint">Draw your signature below</p>
        <canvas id="sig-canvas"></canvas>
        <div class="sign-pad-actions">
          <button class="btn btn--ghost" id="sig-clear">Clear</button>
          <button class="btn btn--primary" id="sig-confirm">Confirm</button>
        </div>
      </div>
    </div>`;

  const scrollContainer = document.getElementById("sign-pages-scroll");
  const sigCanvas = document.getElementById("sig-canvas");
  // Solid white background — transparent renders black in dark mode on mobile
  const sigPad = new SignaturePad(sigCanvas, {
    backgroundColor: "rgb(255,255,255)",
    penColor: "#000000",
  });

  // Signature rect dimensions — mutable so resize handle can update them.
  // Defaults match backend SignSubmission field defaults.
  let sigWFrac = 0.25;
  let sigHFrac = 0.08;

  // Create a page wrapper + canvas for every page
  const pageWraps = [];
  for (let i = 0; i < totalPages; i++) {
    const wrap = document.createElement("div");
    wrap.className = "sign-page-wrap";
    wrap.dataset.pageIndex = String(i);
    const canvas = document.createElement("canvas");
    canvas.className = "sign-pdf-canvas";
    wrap.appendChild(canvas);
    scrollContainer.appendChild(wrap);
    pageWraps.push(wrap);
  }

  // Single shared placement overlay — moved into the clicked page wrap
  const placementEl = document.createElement("div");
  placementEl.className = "sign-placement";
  placementEl.style.display = "none";

  // Resize handle — bottom-right corner of placementEl
  const resizeHandle = document.createElement("div");
  resizeHandle.className = "sign-resize-handle";

  // Helper: (re-)populate placementEl content and ensure resize handle stays
  function updatePlacementContent() {
    placementEl.innerHTML = sigDataUrl
      ? `<img src="${sigDataUrl}" style="width:100%;height:100%;object-fit:contain" />`
      : '<span class="sign-placement-label">Sign here</span>';
    placementEl.appendChild(resizeHandle);
  }

  // Drag state
  let isDragging = false;
  let isResizing = false;
  let dragStartX = 0, dragStartY = 0;
  let dragStartPlacedX = 0, dragStartPlacedY = 0;
  let resizeStartX = 0, resizeStartY = 0;
  let resizeStartW = 0, resizeStartH = 0;

  placementEl.addEventListener("pointerdown", (e) => {
    if (e.target === resizeHandle) return;
    isDragging = true;
    dragStartX = e.clientX;
    dragStartY = e.clientY;
    dragStartPlacedX = placedX ?? 0;
    dragStartPlacedY = placedY ?? 0;
    placementEl.setPointerCapture(e.pointerId);
    e.stopPropagation();
  });

  placementEl.addEventListener("pointermove", (e) => {
    if (!isDragging) return;
    const canvas = placementEl.parentElement?.querySelector("canvas");
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const dx = (e.clientX - dragStartX) / rect.width;
    const dy = (e.clientY - dragStartY) / rect.height;
    placedX = Math.max(0, Math.min(1 - sigWFrac, dragStartPlacedX + dx));
    placedY = Math.max(0, Math.min(1 - sigHFrac, dragStartPlacedY + dy));
    placementEl.style.left = `${placedX * 100}%`;
    placementEl.style.top = `${placedY * 100}%`;
  });

  placementEl.addEventListener("pointerup", () => { isDragging = false; });

  resizeHandle.addEventListener("pointerdown", (e) => {
    isResizing = true;
    resizeStartX = e.clientX;
    resizeStartY = e.clientY;
    resizeStartW = sigWFrac;
    resizeStartH = sigHFrac;
    resizeHandle.setPointerCapture(e.pointerId);
    e.stopPropagation();
  });

  resizeHandle.addEventListener("pointermove", (e) => {
    if (!isResizing) return;
    const canvas = placementEl.parentElement?.querySelector("canvas");
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const dw = (e.clientX - resizeStartX) / rect.width;
    const dh = (e.clientY - resizeStartY) / rect.height;
    sigWFrac = Math.max(0.07, Math.min(0.6, resizeStartW + dw));
    sigHFrac = Math.max(0.03, Math.min(0.35, resizeStartH + dh));
    // Keep box within page after resize
    if (placedX !== null) placedX = Math.min(placedX, 1 - sigWFrac);
    if (placedY !== null) placedY = Math.min(placedY, 1 - sigHFrac);
    placementEl.style.width = `${sigWFrac * 100}%`;
    placementEl.style.height = `${sigHFrac * 100}%`;
    if (placedX !== null) placementEl.style.left = `${placedX * 100}%`;
    if (placedY !== null) placementEl.style.top = `${placedY * 100}%`;
  });

  resizeHandle.addEventListener("pointerup", () => { isResizing = false; });

  // Render all pages full-width
  const displayW = scrollContainer.clientWidth || window.innerWidth || 360;
  for (let i = 0; i < totalPages; i++) {
    const page = await pdfDoc.getPage(i + 1);
    const baseVp = page.getViewport({ scale: 1 });
    const scale = displayW / baseVp.width;
    const vp = page.getViewport({ scale });
    const canvas = pageWraps[i].querySelector("canvas");
    canvas.width = vp.width;
    canvas.height = vp.height;
    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
  }

  // Placement mode: tap on any page to position the signature box
  const signHereBtn = document.getElementById("sign-here-btn");

  function enterPlacementMode() {
    placementMode = true;
    scrollContainer.style.cursor = "crosshair";
    signHereBtn.textContent = "Tap document to place";
  }

  signHereBtn.addEventListener("click", enterPlacementMode);

  scrollContainer.addEventListener("click", (e) => {
    if (!placementMode) return;
    const canvas = e.target.closest(".sign-pdf-canvas");
    if (!canvas) return;

    const rect = canvas.getBoundingClientRect();
    const nx = (e.clientX - rect.left) / rect.width;
    const ny = (e.clientY - rect.top) / rect.height;
    placedX = Math.max(0, Math.min(1 - sigWFrac, nx));
    placedY = Math.max(0, Math.min(1 - sigHFrac, ny));

    const wrap = canvas.parentElement;
    placedPage = parseInt(wrap.dataset.pageIndex, 10);
    placementMode = false;
    scrollContainer.style.cursor = "";
    signHereBtn.textContent = "Place signature";

    // Attach placement overlay to this page
    placementEl.style.display = "none";
    wrap.appendChild(placementEl);
    placementEl.style.left = `${placedX * 100}%`;
    placementEl.style.top = `${placedY * 100}%`;
    placementEl.style.width = `${sigWFrac * 100}%`;
    placementEl.style.height = `${sigHFrac * 100}%`;
    updatePlacementContent();
    placementEl.style.display = "flex";

    // Open signature pad — size canvas after modal is visible
    const modal = document.getElementById("sign-pad-modal");
    modal.style.display = "flex";
    sigCanvas.width = sigCanvas.parentElement?.clientWidth ?? 320;
    sigCanvas.height = 160;
    sigPad.clear();
  });

  // Signature pad actions
  document.getElementById("sig-clear").addEventListener("click", () => sigPad.clear());
  document.getElementById("sig-confirm").addEventListener("click", () => {
    if (sigPad.isEmpty()) return;
    sigDataUrl = sigPad.toDataURL("image/png");
    updatePlacementContent();
    document.getElementById("sign-pad-modal").style.display = "none";
    showMainButton("Submit signature", submitSignature);
  });

  async function submitSignature() {
    if (placedX === null || !sigDataUrl) return;
    setMainButtonLoading(true);

    // Extract base64 from data URL
    const b64 = sigDataUrl.replace(/^data:image\/png;base64,/, "");

    try {
      const resp = await apiFetch("/api/mini-app/sign/submit", {
        method: "POST",
        body: JSON.stringify({
          packet_id: packetId,
          page: placedPage,
          x: placedX,
          y: placedY,
          sig_png_b64: b64,
          w_frac: sigWFrac,
          h_frac: sigHFrac,
        }),
      });

      if (resp.status === "accepted") {
        showState("success", "Signature submitted — you'll receive a confirmation shortly.");
        setTimeout(closeMiniApp, 2000);
      }
    } catch (err) {
      if (err.message && err.message.includes("409")) {
        showState("error", "This document has already been signed.");
      } else if (err.message && (err.message.includes("403") || err.message.includes("401"))) {
        showState("error", "This signing link is not valid for your account.");
      } else {
        showState("error", esc(err.message || "Submission failed. Please try again."));
        setMainButtonLoading(false);
      }
    }
  }
}

(async () => {
  root.className = "mini-app";
  showState("", "Loading...");
  try {
    applyTheme();

    if (viewMode === "sign") {
      // PDF signing — requires SDK for initData auth
      if (!tg) {
        showState("error", "Please open this from the Telegram mobile app.");
        return;
      }
      await initSignMode();
    } else if (viewMode === "timeline") {
      // Timeline view for chat chronology
      await loadTimelineView();
    } else if (viewMode === "agent_state") {
      // Agent state view — same rendering, different data source
      await loadAgentStateView();
    } else if (viewMode === "state") {
      // State view works without SDK (uses sig-based auth)
      await loadStateView();
    } else if (tg) {
      // Form editing requires SDK for initData auth
      await loadForm();
      showMainButton("Submit", submitForm);
    } else {
      showState("error", "Please open this from the Telegram mobile app.");
    }
  } catch (err) {
    showState("error", esc(err.message || String(err)));
  }
})();
