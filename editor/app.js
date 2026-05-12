/**
 * app.js — プロ動画エディタ
 * Features: JKL transport, rAF 60fps playhead, Undo/Redo, Split(S),
 *           Right-click context menu, Color-coded clips, Magnetic snap,
 *           Timecode display, Keyboard shortcuts modal
 */

// ═══ グローバル状態 ════════════════════════════════════════
let state        = null;
let dirty        = false;
let mode         = "text";
let selectedId   = null;
let tlScale      = 80;          // px/秒
let exportPoll   = null;
let dragging     = null;
let seekDragging = false;
let rafId        = null;

// トラックミュート状態（clientサイド管理）
const trackMuted = { video: false, subs: false, bgm: false };

// ─── Undo/Redo ─────────────────────────────────────────────
const history  = [];
let   histIdx  = -1;
const MAX_HIST = 50;

// ─── クリップカラーパレット ────────────────────────────────
const CLIP_COLORS = [
  '#3b82f6','#22c55e','#eab308','#ef4444','#8b5cf6',
  '#f97316','#06b6d4','#ec4899','#14b8a6','#a3e635',
];

// ─── スナップ ──────────────────────────────────────────────
const SNAP_PX = 8;   // snap threshold in screen pixels

const video = () => document.getElementById("video-el");

// ═══ 起動 ══════════════════════════════════════════════════
document.addEventListener("DOMContentLoaded", async () => {
  await loadState();
  bindToolbar();
  bindPlayer();
  bindHeader();
  bindTimeline();
  bindKeyboard();
  bindExportModal();
  bindContextMenu();
  bindShortcutModal();
  bindTrackMute();
  loadFileOptions();
  renderAll();
  pushHistory();   // 初期スナップショット
  setInterval(() => { if (dirty) saveState().catch(() => {}); }, 15000);
});

// ═══ API ═══════════════════════════════════════════════════
async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (!r.ok) throw new Error(`${method} ${path} → ${r.status}`);
  return r.json();
}

async function loadState() {
  state = await api("GET", "/api/state");
  applyVideoSrc();
}

async function saveState() {
  await api("POST", "/api/state", state);
  dirty = false;
  toast("保存しました");
}

function mark() { dirty = true; }

// ═══ Undo / Redo ═══════════════════════════════════════════
function pushHistory() {
  history.splice(histIdx + 1);
  history.push(JSON.parse(JSON.stringify(state.subtitles)));
  if (history.length > MAX_HIST) history.shift();
  histIdx = history.length - 1;
  updateUndoBtn();
}

function undo() {
  if (histIdx <= 0) return;
  histIdx--;
  state.subtitles = JSON.parse(JSON.stringify(history[histIdx]));
  selectedId = null; mark(); renderAll(); updateUndoBtn();
}

function redo() {
  if (histIdx >= history.length - 1) return;
  histIdx++;
  state.subtitles = JSON.parse(JSON.stringify(history[histIdx]));
  selectedId = null; mark(); renderAll(); updateUndoBtn();
}

function updateUndoBtn() {
  const u = document.getElementById("btn-undo");
  const r = document.getElementById("btn-redo");
  const c = document.getElementById("undo-count");
  if (u) u.disabled = histIdx <= 0;
  if (r) r.disabled = histIdx >= history.length - 1;
  if (c) c.textContent = history.length > 1 ? `${histIdx + 1}/${history.length}` : "";
}

// ═══ プレーヤー ════════════════════════════════════════════
function applyVideoSrc() {
  if (!state?.source_video) return;
  const v   = video();
  const src = `/media/${state.source_video}`;
  if (v.getAttribute("data-src") !== src) {
    v.src = src; v.load();
    v.setAttribute("data-src", src);
  }
}

function bindPlayer() {
  const v = video();

  // rAF ループ管理
  v.addEventListener("play",  () => { setPlayIcon(true);  startRAF(); });
  v.addEventListener("pause", () => { setPlayIcon(false); stopRAF();  updateAll(); });
  v.addEventListener("ended", () => { setPlayIcon(false); stopRAF();  updateAll(); });

  // メタデータ読み込み後にタイムライン描画
  v.addEventListener("loadedmetadata", () => {
    document.getElementById("time-dur").textContent = fmtTC(v.duration);
    drawTimeline();
  });

  // シークバー
  const sw = document.getElementById("seekbar-wrap");
  const doSeek = e => {
    const r = sw.getBoundingClientRect();
    v.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * (v.duration || 0);
    updateAll();
  };
  sw.addEventListener("mousedown", e => { seekDragging = true; doSeek(e); });
  document.addEventListener("mousemove", e => { if (seekDragging) doSeek(e); });
  document.addEventListener("mouseup",   () => { seekDragging = false; });

  document.getElementById("play-btn").addEventListener("click", togglePlay);
  document.getElementById("btn-prev5").addEventListener("click", () => { v.currentTime -= 5; updateAll(); });
  document.getElementById("btn-next5").addEventListener("click", () => { v.currentTime += 5; updateAll(); });
}

function togglePlay() { video().paused ? video().play() : video().pause(); }
function setPlayIcon(p) { document.getElementById("play-btn").textContent = p ? "⏸" : "▶"; }

// ─── rAF 60fps ループ ──────────────────────────────────────
function startRAF() {
  if (rafId) return;
  const tick = () => {
    updateAll();
    rafId = requestAnimationFrame(tick);
  };
  rafId = requestAnimationFrame(tick);
}

function stopRAF() {
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
}

function updateAll() {
  updateSeekbar();
  updateTimeDisp();
  updatePlayhead();
  scrollTimelineToPlayhead();
  updateSubOverlay();
}

function updateSeekbar() {
  const v = video(), pct = v.duration ? (v.currentTime / v.duration) * 100 : 0;
  document.getElementById("seekbar-fill").style.width = pct + "%";
  document.getElementById("seekbar-thumb").style.left = pct + "%";
}

function updateTimeDisp() {
  document.getElementById("time-cur").textContent = fmtTC(video().currentTime);
}

// ═══ 字幕オーバーレイ ══════════════════════════════════════
function updateSubOverlay() {
  const div = document.getElementById("subtitle-overlay");
  if (trackMuted.subs) { div.innerHTML = ""; return; }

  const t   = video().currentTime;
  const seg = (state?.subtitles || []).find(s => t >= s.start && t < s.end);
  if (!seg) { div.innerHTML = ""; return; }

  const fontColor = seg.emphasis ? "#f5c518"
    : seg.fontcolor === "red"  ? "#ff5c5c"
    : seg.fontcolor === "cyan" ? "#5ce0f5"
    : "#ffffff";

  const bgEnabled = seg.bg_enabled ?? true;
  const bgName    = seg.bg_color   ?? "black";
  const bgOp      = seg.bg_opacity ?? 0.65;
  const bgPad     = seg.bg_padding ?? 10;
  const bgCssMap  = {
    black:    [0,   0,   0  ],
    darkgray: [26,  26,  26 ],
    gray:     [85,  85,  85 ],
    white:    [255, 255, 255],
  };
  const [r, g, b] = bgCssMap[bgName] || [0, 0, 0];
  const bg    = bgEnabled ? `rgba(${r},${g},${b},${bgOp})` : "transparent";
  const padPx = bgEnabled
    ? `${Math.round(bgPad * 0.18)}px ${Math.round(bgPad * 0.36)}px`
    : "4px 0";

  const fs     = seg.fontsize ?? 52;
  const vw     = video().clientWidth || 640;
  const scaled = Math.round(fs * vw / 1920);
  const clamp  = Math.max(11, Math.min(scaled, 72));

  // 位置・不透明度（per-clip）
  const posY   = seg.pos_y   ?? 85;   // % from top
  const opacity = seg.opacity ?? 1.0;

  div.style.cssText = `
    position: absolute;
    top: ${posY}%;
    left: 50%;
    transform: translate(-50%, -50%);
    pointer-events: none;
    text-align: center;
    max-width: 92%;
    z-index: 10;
    opacity: ${opacity};
  `;

  div.innerHTML = `
    <span class="sub-text ${seg.emphasis ? 'emphasis' : ''}"
      style="color:${fontColor};background:${bg};padding:${padPx};font-size:${clamp}px">
      ${esc(seg.text)}
    </span>`;
}

// ═══ ツールバー ════════════════════════════════════════════
function bindToolbar() {
  document.querySelectorAll(".tool-btn[data-mode]").forEach(btn => {
    btn.addEventListener("click", () => {
      mode = btn.dataset.mode;
      document.querySelectorAll(".tool-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      selectedId = null;
      renderProps();
      renderTimeline();
    });
  });
}

// ═══ ヘッダー ══════════════════════════════════════════════
function bindHeader() {
  document.getElementById("btn-save").addEventListener("click", saveState);
  document.getElementById("btn-export").addEventListener("click", openExport);
  document.getElementById("btn-undo").addEventListener("click", undo);
  document.getElementById("btn-redo").addEventListener("click", redo);
  document.getElementById("btn-shortcuts").addEventListener("click", () => toggleShortcutModal(true));

  // ズームスライダー
  const zoomSl = document.getElementById("zoom-slider");
  zoomSl.addEventListener("input", () => setZoom(parseInt(zoomSl.value)));
  // Ctrl+ホイールでもズーム
  document.getElementById("tl-scroll").addEventListener("wheel", e => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    setZoom(tlScale * (e.deltaY < 0 ? 1.12 : 0.89));
  }, { passive: false });

  updateUndoBtn();
}

function setZoom(s) {
  tlScale = Math.max(20, Math.min(400, s));
  document.getElementById("zoom-label").textContent = (tlScale / 80).toFixed(1) + "×";
  const sl = document.getElementById("zoom-slider");
  sl.value = Math.round(tlScale);
  drawTimeline();
}

// ═══ トラックミュート ══════════════════════════════════════
function bindTrackMute() {
  document.querySelectorAll(".tl-mute-btn").forEach(btn => {
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const track = btn.dataset.track;
      trackMuted[track] = !trackMuted[track];
      btn.textContent = trackMuted[track] ? "🔇" : "🔊";
      btn.classList.toggle("muted", trackMuted[track]);
      // トラック全体を dim
      const trackEl = document.getElementById(`track-${track}`);
      if (trackEl) trackEl.classList.toggle("muted", trackMuted[track]);
      // テロップがミュートされたらオーバーレイも消す
      if (track === "subs") updateSubOverlay();
    });
  });
}

// ═══ プロパティパネル ═══════════════════════════════════════
function renderProps() {
  const title = document.getElementById("props-title");
  const body  = document.getElementById("props-body");
  body.innerHTML = "";

  if (mode === "text") {
    title.textContent = "テキスト";
    const sel = selectedId != null ? state.subtitles.find(s => s.id === selectedId) : null;
    if (sel) {
      renderSubProps(body, sel);
    } else {
      body.innerHTML = `
        <div style="color:var(--text-dim);font-size:12px;line-height:1.7;text-align:center;padding:30px 0">
          タイムライン上のテロップクリップを<br>選択してください
        </div>
        <button class="btn btn-primary" style="width:100%" id="props-add-sub">＋ テロップを追加</button>
      `;
      document.getElementById("props-add-sub")?.addEventListener("click", addSubAtCurrentTime);
    }
  } else if (mode === "audio") {
    title.textContent = "音声";
    renderAudioProps(body);
  } else if (mode === "settings") {
    title.textContent = "エンドカード";
    renderEndCardProps(body);
  } else if (mode === "thumbnail") {
    title.textContent = "サムネイル";
    renderThumbnailProps(body);
  }
}

function renderSubProps(body, seg) {
  const dur     = Math.max(0.1, seg.end - seg.start).toFixed(1);
  const fs      = seg.fontsize   ?? 52;
  const bgOn    = seg.bg_enabled ?? true;
  const bgColor = seg.bg_color   ?? "black";
  const bgOp    = seg.bg_opacity ?? 0.65;
  const bgPad   = seg.bg_padding ?? 10;
  const opacity = seg.opacity    ?? 1.0;
  const posY    = seg.pos_y      ?? 85;
  const fadeIn  = seg.fade_in    ?? 0;
  const fadeOut = seg.fade_out   ?? 0;

  const fcWhite  = (!seg.emphasis && !seg.fontcolor) ? "active" : "";
  const fcYellow = seg.emphasis                       ? "active" : "";
  const fcRed    = seg.fontcolor === "red"            ? "active" : "";
  const fcCyan   = seg.fontcolor === "cyan"           ? "active" : "";

  body.innerHTML = `
    <!-- ══ テキスト ══ -->
    <div class="prop-section-title">テキスト</div>
    <div class="prop-group">
      <textarea id="sub-text" rows="3">${esc(seg.text)}</textarea>
    </div>

    <div class="prop-group">
      <div class="prop-label">文字サイズ</div>
      <div class="range-row">
        <input type="range" id="sub-size" min="20" max="100" step="2" value="${fs}">
        <span class="range-val" id="sub-size-val">${fs}px</span>
      </div>
    </div>

    <div class="prop-group">
      <div class="prop-label">文字色</div>
      <div class="color-row">
        <button class="color-btn ${fcWhite}"  data-fc="white"  style="background:#fff;border-color:#888" title="白"></button>
        <button class="color-btn ${fcYellow}" data-fc="yellow" style="background:#f5c518" title="黄（強調）"></button>
        <button class="color-btn ${fcRed}"    data-fc="red"    style="background:#ff5c5c" title="赤"></button>
        <button class="color-btn ${fcCyan}"   data-fc="cyan"   style="background:#5ce0f5" title="水色"></button>
      </div>
    </div>

    <!-- ══ 表示位置・不透明度 ══ -->
    <div class="prop-section-title">表示位置 / 不透明度</div>
    <div class="prop-group">
      <div class="prop-label">縦位置（上 ↕ 下）</div>
      <div class="range-row">
        <input type="range" id="sub-posy" min="10" max="95" step="1" value="${posY}">
        <span class="range-val" id="sub-posy-val">${posY}%</span>
      </div>
    </div>
    <div class="prop-group">
      <div class="prop-label">不透明度</div>
      <div class="range-row">
        <input type="range" id="sub-opacity" min="0.1" max="1" step="0.05" value="${opacity}">
        <span class="range-val" id="sub-opacity-val">${Math.round(opacity * 100)}%</span>
      </div>
    </div>

    <!-- ══ 表示時間 ══ -->
    <div class="prop-section-title">表示時間</div>
    <div class="prop-group">
      <div class="dur-display" id="dur-display">${dur} 秒</div>
      <div class="range-row">
        <input type="range" id="sub-dur" min="0.3" max="15" step="0.1" value="${dur}">
        <span class="range-val" id="sub-dur-val">${dur}s</span>
      </div>
      <div class="prop-label" style="margin-top:6px">開始 / 終了（秒）</div>
      <div class="time-pair">
        <input type="number" id="sub-start" step="0.1" min="0" value="${seg.start.toFixed(2)}">
        <span>→</span>
        <input type="number" id="sub-end" step="0.1" min="0" value="${seg.end.toFixed(2)}">
      </div>
    </div>

    <div class="prop-group">
      <div class="prop-label">フェードイン（秒）</div>
      <div class="range-row">
        <input type="range" id="sub-fadein" min="0" max="2" step="0.05" value="${fadeIn}">
        <span class="range-val" id="sub-fadein-val">${fadeIn.toFixed(2)}s</span>
      </div>
    </div>
    <div class="prop-group">
      <div class="prop-label">フェードアウト（秒）</div>
      <div class="range-row">
        <input type="range" id="sub-fadeout" min="0" max="2" step="0.05" value="${fadeOut}">
        <span class="range-val" id="sub-fadeout-val">${fadeOut.toFixed(2)}s</span>
      </div>
    </div>

    <!-- ══ テキスト背景 ══ -->
    <div class="prop-section-title">テキスト背景</div>
    <div class="prop-group">
      <div class="bg-toggle-row">
        <button class="bg-mode-btn ${bgOn ? 'active' : ''}"  data-bg-on="true">背景あり</button>
        <button class="bg-mode-btn ${!bgOn ? 'active' : ''}" data-bg-on="false">背景なし</button>
      </div>
    </div>

    <div class="prop-group" id="bg-controls" style="${bgOn ? '' : 'opacity:.35;pointer-events:none'}">
      <div class="prop-label">背景色</div>
      <div class="color-row">
        <button class="bg-color-btn ${bgColor === 'black'    ? 'active' : ''}" data-bg="black"    style="background:#000;border-color:#666" title="黒"></button>
        <button class="bg-color-btn ${bgColor === 'darkgray' ? 'active' : ''}" data-bg="darkgray" style="background:#1a1a1a;border-color:#666" title="ダークグレー"></button>
        <button class="bg-color-btn ${bgColor === 'gray'     ? 'active' : ''}" data-bg="gray"     style="background:#555" title="グレー"></button>
        <button class="bg-color-btn ${bgColor === 'white'    ? 'active' : ''}" data-bg="white"    style="background:#fff;border-color:#888" title="白"></button>
      </div>

      <div class="prop-label" style="margin-top:8px">背景不透明度</div>
      <div class="range-row">
        <input type="range" id="bg-opacity" min="0" max="1" step="0.05" value="${bgOp}">
        <span class="range-val" id="bg-opacity-val">${Math.round(bgOp * 100)}%</span>
      </div>

      <div class="prop-label" style="margin-top:4px">余白（padding）</div>
      <div class="range-row">
        <input type="range" id="bg-padding" min="0" max="40" step="1" value="${bgPad}">
        <span class="range-val" id="bg-padding-val">${bgPad}px</span>
      </div>
    </div>

    <div class="divider" style="margin-top:4px"></div>
    <div class="row-between">
      <button class="btn btn-ghost btn-sm" id="sub-dup">複製</button>
      <button class="btn btn-danger btn-sm" id="sub-del">削除</button>
    </div>
  `;

  // テキスト
  document.getElementById("sub-text").addEventListener("input", e => {
    seg.text = e.target.value; mark(); renderTimeline();
  });

  // 文字サイズ
  const sizeEl  = document.getElementById("sub-size");
  const sizeVal = document.getElementById("sub-size-val");
  sizeEl.addEventListener("input", () => {
    seg.fontsize = parseInt(sizeEl.value);
    sizeVal.textContent = sizeEl.value + "px";
    mark(); updateSubOverlay();
  });

  // 縦位置
  const posYEl  = document.getElementById("sub-posy");
  const posYVal = document.getElementById("sub-posy-val");
  posYEl.addEventListener("input", () => {
    seg.pos_y = parseInt(posYEl.value);
    posYVal.textContent = posYEl.value + "%";
    mark(); updateSubOverlay();
  });
  posYEl.addEventListener("change", pushHistory);

  // 不透明度
  const subOpEl  = document.getElementById("sub-opacity");
  const subOpVal = document.getElementById("sub-opacity-val");
  subOpEl.addEventListener("input", () => {
    seg.opacity = parseFloat(subOpEl.value);
    subOpVal.textContent = Math.round(seg.opacity * 100) + "%";
    mark(); updateSubOverlay();
  });
  subOpEl.addEventListener("change", pushHistory);

  // フェードイン
  const fadeInEl  = document.getElementById("sub-fadein");
  const fadeInVal = document.getElementById("sub-fadein-val");
  fadeInEl.addEventListener("input", () => {
    seg.fade_in = parseFloat(fadeInEl.value);
    fadeInVal.textContent = seg.fade_in.toFixed(2) + "s";
    mark();
  });
  fadeInEl.addEventListener("change", pushHistory);

  // フェードアウト
  const fadeOutEl  = document.getElementById("sub-fadeout");
  const fadeOutVal = document.getElementById("sub-fadeout-val");
  fadeOutEl.addEventListener("input", () => {
    seg.fade_out = parseFloat(fadeOutEl.value);
    fadeOutVal.textContent = seg.fade_out.toFixed(2) + "s";
    mark();
  });
  fadeOutEl.addEventListener("change", pushHistory);

  // 文字色
  body.querySelectorAll("[data-fc]").forEach(btn => {
    btn.addEventListener("click", () => {
      body.querySelectorAll("[data-fc]").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const c = btn.dataset.fc;
      seg.emphasis  = (c === "yellow");
      seg.fontcolor = (c === "white" || c === "yellow") ? null : c;
      mark(); updateSubOverlay(); renderTimeline();
    });
  });

  // 表示時間スライダー
  const durEl  = document.getElementById("sub-dur");
  const durVal = document.getElementById("sub-dur-val");
  const durDisp= document.getElementById("dur-display");
  const endEl  = document.getElementById("sub-end");
  durEl.addEventListener("input", () => {
    const d = parseFloat(durEl.value);
    seg.end = parseFloat((seg.start + d).toFixed(2));
    durVal.textContent  = d.toFixed(1) + "s";
    durDisp.textContent = d.toFixed(1) + " 秒";
    endEl.value = seg.end.toFixed(2);
    mark(); renderTimeline();
  });
  durEl.addEventListener("change", pushHistory);

  // タイムコード直接入力
  document.getElementById("sub-start").addEventListener("change", e => {
    seg.start = parseFloat(e.target.value);
    const d = Math.max(0.3, seg.end - seg.start);
    durEl.value = d.toFixed(1);
    durVal.textContent  = durEl.value + "s";
    durDisp.textContent = durEl.value + " 秒";
    mark(); renderTimeline(); pushHistory();
  });
  endEl.addEventListener("change", e => {
    seg.end = parseFloat(e.target.value);
    const d = Math.max(0.3, seg.end - seg.start);
    durEl.value = d.toFixed(1);
    durVal.textContent  = durEl.value + "s";
    durDisp.textContent = durEl.value + " 秒";
    mark(); renderTimeline(); pushHistory();
  });

  // 背景 ON/OFF
  body.querySelectorAll("[data-bg-on]").forEach(btn => {
    btn.addEventListener("click", () => {
      body.querySelectorAll("[data-bg-on]").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      seg.bg_enabled = btn.dataset.bgOn === "true";
      document.getElementById("bg-controls").style.cssText =
        seg.bg_enabled ? "" : "opacity:.35;pointer-events:none";
      mark(); updateSubOverlay(); pushHistory();
    });
  });

  // 背景色
  body.querySelectorAll("[data-bg]").forEach(btn => {
    btn.addEventListener("click", () => {
      body.querySelectorAll("[data-bg]").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      seg.bg_color = btn.dataset.bg;
      mark(); updateSubOverlay(); pushHistory();
    });
  });

  // 不透明度
  const opEl  = document.getElementById("bg-opacity");
  const opVal = document.getElementById("bg-opacity-val");
  opEl.addEventListener("input", () => {
    seg.bg_opacity = parseFloat(opEl.value);
    opVal.textContent = Math.round(seg.bg_opacity * 100) + "%";
    mark(); updateSubOverlay();
  });
  opEl.addEventListener("change", pushHistory);

  // 余白
  const padEl  = document.getElementById("bg-padding");
  const padVal = document.getElementById("bg-padding-val");
  padEl.addEventListener("input", () => {
    seg.bg_padding = parseInt(padEl.value);
    padVal.textContent = padEl.value + "px";
    mark(); updateSubOverlay();
  });
  padEl.addEventListener("change", pushHistory);

  // 複製
  document.getElementById("sub-dup").addEventListener("click", () => {
    pushHistory();
    const copy = { ...seg, id: Date.now(), start: seg.end, end: seg.end + (seg.end - seg.start) };
    state.subtitles.push(copy);
    state.subtitles.sort((a, b) => a.start - b.start);
    selectedId = copy.id;
    mark(); renderAll(); pushHistory();
  });

  // 削除
  document.getElementById("sub-del").addEventListener("click", () => {
    pushHistory();
    state.subtitles.splice(state.subtitles.indexOf(seg), 1);
    selectedId = null;
    mark(); renderAll(); pushHistory();
  });
}

function renderAudioProps(body) {
  const bgm = state.bgm || {};
  const se  = state.se  || {};
  body.innerHTML = `
    <div class="prop-group">
      <div class="row-between"><span class="prop-label">BGM</span>
        <label class="toggle"><input type="checkbox" id="bgm-on" ${bgm.enabled ? "checked" : ""}><div class="toggle-track"></div></label>
      </div>
      <select id="bgm-file"></select>
      <div>
        <div class="prop-label" style="margin-top:8px">音量</div>
        <div class="range-row">
          <input type="range" id="bgm-vol" min="0" max="0.5" step="0.01" value="${bgm.volume ?? 0.12}">
          <span class="range-val" id="bgm-vol-val">${(bgm.volume ?? 0.12).toFixed(2)}</span>
        </div>
      </div>
    </div>
    <div class="divider"></div>
    <div class="prop-group">
      <div class="row-between"><span class="prop-label">SE 効果音</span>
        <label class="toggle"><input type="checkbox" id="se-on" ${se.enabled ? "checked" : ""}><div class="toggle-track"></div></label>
      </div>
      <select id="se-file"></select>
      <div id="se-events-list" style="display:flex;flex-direction:column;gap:4px;margin-top:6px"></div>
      <button class="btn btn-ghost btn-sm" id="btn-add-se" style="width:100%;margin-top:4px">
        ＋ 現在位置に SE 追加
      </button>
    </div>
    <div class="divider"></div>
    <div class="prop-group">
      <div class="row-between"><span class="prop-label">無音カット</span>
        <label class="toggle"><input type="checkbox" id="silence-on" ${state.cut_silence ? "checked" : ""}><div class="toggle-track"></div></label>
      </div>
      <p style="font-size:11px;color:var(--text-dim);line-height:1.5">silencedetect で無音部分を自動除去します</p>
    </div>
  `;

  document.getElementById("bgm-on").addEventListener("change", e => { state.bgm.enabled = e.target.checked; mark(); });
  document.getElementById("bgm-vol").addEventListener("input", e => {
    state.bgm.volume = parseFloat(e.target.value);
    document.getElementById("bgm-vol-val").textContent = parseFloat(e.target.value).toFixed(2);
    mark();
  });
  document.getElementById("se-on").addEventListener("change", e => { state.se.enabled = e.target.checked; mark(); });
  document.getElementById("btn-add-se").addEventListener("click", () => {
    if (!state.se) state.se = { enabled: true, file: "", events: [] };
    state.se.events.push({ time: parseFloat(video().currentTime.toFixed(2)), volume: 0.7 });
    mark(); renderSeEvents();
  });
  document.getElementById("silence-on").addEventListener("change", e => { state.cut_silence = e.target.checked; mark(); });

  loadFileOptions(document.getElementById("bgm-file"), "bgm");
  loadFileOptions(document.getElementById("se-file"), "se");
  renderSeEvents();
}

function renderSeEvents() {
  const list = document.getElementById("se-events-list");
  if (!list) return;
  const events = state.se?.events || [];
  list.innerHTML = "";
  events.forEach((ev, i) => {
    const row = document.createElement("div");
    row.className = "se-row";
    row.innerHTML = `
      <span class="se-time">${fmt(ev.time)}</span>
      <input type="range" min="0" max="1" step="0.05" value="${ev.volume}" style="flex:1;accent-color:var(--orange-9)">
      <span style="min-width:28px;font-size:11px">${ev.volume.toFixed(1)}</span>
      <button class="btn btn-xs btn-danger btn-icon">✕</button>
    `;
    row.querySelector("input").addEventListener("input", e => {
      events[i].volume = parseFloat(e.target.value);
      e.target.nextElementSibling.textContent = parseFloat(e.target.value).toFixed(1);
      mark();
    });
    row.querySelector("button").addEventListener("click", () => { events.splice(i, 1); mark(); renderSeEvents(); });
    list.appendChild(row);
  });
}

function renderEndCardProps(body) {
  const ec = state.end_card || {};
  body.innerHTML = `
    <div class="prop-group">
      <div class="row-between">
        <span class="prop-label">エンドカード表示</span>
        <label class="toggle"><input type="checkbox" id="ec-on" ${ec.enabled ? "checked" : ""}><div class="toggle-track"></div></label>
      </div>
    </div>
    <div class="prop-group">
      <div class="prop-label">メインテキスト</div>
      <input type="text" id="ec-text" value="${esc(ec.text || '')}">
    </div>
    <div class="prop-group">
      <div class="prop-label">サブテキスト</div>
      <input type="text" id="ec-subtext" value="${esc(ec.subtext || '')}">
    </div>
    <div class="prop-group">
      <div class="prop-label">表示秒数</div>
      <div class="range-row">
        <input type="range" id="ec-dur" min="2" max="20" step="1" value="${ec.duration || 5}">
        <span class="range-val" id="ec-dur-val">${ec.duration || 5}</span>
      </div>
    </div>
  `;
  document.getElementById("ec-on").addEventListener("change", e => { state.end_card.enabled = e.target.checked; mark(); renderTimeline(); });
  document.getElementById("ec-text").addEventListener("input", e => { state.end_card.text = e.target.value; mark(); });
  document.getElementById("ec-subtext").addEventListener("input", e => { state.end_card.subtext = e.target.value; mark(); });
  const ecDur = document.getElementById("ec-dur");
  ecDur.addEventListener("input", () => {
    state.end_card.duration = parseInt(ecDur.value);
    document.getElementById("ec-dur-val").textContent = ecDur.value;
    mark(); renderTimeline();
  });
}

function renderThumbnailProps(body) {
  const th = state.thumbnail || {};
  body.innerHTML = `
    <div class="prop-group">
      <div class="row-between">
        <span class="prop-label">サムネイル生成</span>
        <label class="toggle"><input type="checkbox" id="th-on" ${th.enabled ? "checked" : ""}><div class="toggle-track"></div></label>
      </div>
    </div>
    <div class="prop-group">
      <div class="prop-label">タイトル文字</div>
      <input type="text" id="th-title" placeholder="空白で文字なし" value="${esc(th.title || '')}">
    </div>
    <div class="prop-group">
      <div class="prop-label">使用フレーム（秒）</div>
      <div class="range-row">
        <input type="range" id="th-seek" min="0" max="300" step="1" value="${th.seek_time || 30}">
        <span class="range-val" id="th-seek-val">${th.seek_time || 30}</span>
      </div>
    </div>
    <button class="btn btn-ghost btn-sm" id="btn-th-preview" style="width:100%">このフレームを確認</button>
    <img id="thumb-preview" alt="サムネイルプレビュー">
  `;
  document.getElementById("th-on").addEventListener("change", e => { state.thumbnail.enabled = e.target.checked; mark(); });
  document.getElementById("th-title").addEventListener("input", e => { state.thumbnail.title = e.target.value; mark(); });
  const thSeek = document.getElementById("th-seek");
  if (video().duration > 0) thSeek.max = Math.floor(video().duration);
  thSeek.addEventListener("input", () => {
    state.thumbnail.seek_time = parseInt(thSeek.value);
    document.getElementById("th-seek-val").textContent = thSeek.value;
    mark();
  });
  document.getElementById("btn-th-preview").addEventListener("click", previewThumb);
}

async function previewThumb() {
  const btn = document.getElementById("btn-th-preview");
  btn.disabled = true; btn.textContent = "生成中...";
  try {
    const r = await fetch("/api/thumbnail_preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seek_time: state.thumbnail?.seek_time ?? 30, source: state.source_video }),
    });
    const img = document.getElementById("thumb-preview");
    img.src = URL.createObjectURL(await r.blob());
    img.style.display = "block";
  } finally {
    btn.disabled = false; btn.textContent = "このフレームを確認";
  }
}

// ═══ タイムライン ═══════════════════════════════════════════
function bindTimeline() {
  const scroll = document.getElementById("tl-scroll");

  scroll.addEventListener("click", e => {
    if (dragging) return;
    const inner = document.getElementById("tl-inner");
    const lw    = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--label-w"));
    const rect  = inner.getBoundingClientRect();
    const x     = e.clientX - rect.left + scroll.scrollLeft - lw;
    if (x < 0) return;
    video().currentTime = Math.max(0, x / tlScale);
    updateAll();
  });

  document.getElementById("btn-add-sub-tl").addEventListener("click", addSubAtCurrentTime);
}

function addSubAtCurrentTime() {
  const t = video().currentTime || 0;
  pushHistory();
  const seg = {
    id: Date.now(),
    start: parseFloat(t.toFixed(2)),
    end:   parseFloat((t + 3).toFixed(2)),
    text:  "新しいテロップ",
    emphasis: false,
    fontsize: 52,
  };
  state.subtitles.push(seg);
  state.subtitles.sort((a, b) => a.start - b.start);
  selectedId = seg.id;
  mark(); renderAll(); pushHistory();
  setTimeout(() => scrollTlToTime(t), 50);
}

function drawTimeline() {
  const dur    = video().duration || 60;
  const lw     = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--label-w"));
  const totalW = Math.max(dur * tlScale + 200, 800);

  const inner = document.getElementById("tl-inner");
  inner.style.width  = (totalW + lw) + "px";
  inner.style.height = "100%";

  const canvas = document.getElementById("tl-ruler");
  canvas.width  = totalW + lw;
  canvas.height = 24;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, 24);
  ctx.fillStyle = "#141414";
  ctx.fillRect(0, 0, canvas.width, 24);
  ctx.fillStyle = "#555";
  ctx.font = "10px monospace";
  ctx.textBaseline = "middle";
  const step = tlScale >= 60 ? 2 : tlScale >= 30 ? 5 : tlScale >= 12 ? 10 : 30;
  for (let t = 0; t <= dur + step; t += step) {
    const x = lw + t * tlScale;
    ctx.fillStyle = "#444";
    ctx.fillRect(x, 16, 1, 8);
    ctx.fillStyle = "#666";
    ctx.fillText(fmt(t, true), x + 3, 12);
  }

  renderTimeline();
}

function renderTimeline() {
  const dur = video().duration || 60;
  const lw  = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--label-w"));

  // 映像クリップ
  const trackVid = document.getElementById("track-video");
  trackVid.innerHTML = "";
  if (state?.source_video) {
    const cl = makeClip("tl-clip-video", 0, dur, state.source_video.split("/").pop(), dur);
    cl.style.pointerEvents = "none";
    trackVid.appendChild(cl);
  }

  // 字幕クリップ（カラーコード付き）
  const trackSub = document.getElementById("track-subs");
  trackSub.innerHTML = "";
  (state?.subtitles || []).forEach((seg, i) => {
    const cl = makeClip(
      `tl-clip-sub${seg.emphasis ? " emp" : ""}`,
      seg.start, seg.end,
      seg.text || "", dur,
    );
    // インデックスベースのカラー
    cl.style.setProperty("--clip-color", CLIP_COLORS[i % CLIP_COLORS.length]);
    if (seg.id === selectedId) cl.classList.add("selected");

    // 左端トリムハンドル
    const leftHandle = document.createElement("div");
    leftHandle.className = "tl-clip-left-handle";
    cl.appendChild(leftHandle);
    leftHandle.addEventListener("mousedown", e => {
      e.stopPropagation();
      selectedId = seg.id;
      renderProps(); renderTimeline();
      dragging = { seg, clip: cl, startX: e.clientX,
        origStart: seg.start, origEnd: seg.end, mode: "trim_left" };
      document.body.style.cursor = "ew-resize";
      const onMove = ev => {
        if (!dragging) return;
        const dx = (ev.clientX - dragging.startX) / tlScale;
        dragging.seg.start = Math.max(0, Math.min(dragging.origStart + dx, dragging.origEnd - 0.1));
        positionClip(dragging.clip, dragging.seg.start, dragging.seg.end);
        mark();
      };
      const onUp = () => {
        state.subtitles.sort((a, b) => a.start - b.start);
        dragging = null; document.body.style.cursor = "";
        renderProps(); renderTimeline(); pushHistory();
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });

    // カーソルフィードバック（ホバー）
    cl.addEventListener("mousemove", e => {
      const r = cl.getBoundingClientRect();
      if (e.clientX - r.left < 12 || r.right - e.clientX < 12) {
        cl.style.cursor = "ew-resize";
      } else {
        cl.style.cursor = "grab";
      }
    });

    cl.addEventListener("mousedown", e => onClipMouseDown(e, seg, i));
    cl.addEventListener("contextmenu", e => { e.preventDefault(); showCtxMenu(e.clientX, e.clientY, seg); });
    trackSub.appendChild(cl);
  });

  // BGM クリップ
  const trackBgm = document.getElementById("track-bgm");
  trackBgm.innerHTML = "";
  const bgm = state?.bgm;
  if (bgm?.enabled && bgm?.file) {
    const cl = makeClip("tl-clip-bgm", 0, dur, bgm.file.split("/").pop(), dur);
    cl.style.pointerEvents = "none";
    trackBgm.appendChild(cl);
  }

  // エンドカード
  const ec = state?.end_card;
  if (ec?.enabled && ec?.duration && dur > 0) {
    const ecStart = Math.max(0, dur - ec.duration);
    const cl = makeClip("tl-clip-sub", ecStart, dur, "EC", dur);
    cl.style.background  = "rgba(255,92,92,0.55)";
    cl.style.color       = "#fff";
    cl.style.pointerEvents = "none";
    trackVid.appendChild(cl);
  }

  updatePlayhead();
}

function makeClip(className, start, end, label, totalDur) {
  const cl = document.createElement("div");
  cl.className = `tl-clip ${className}`;
  positionClip(cl, start, end);
  // ラベルはspan要素で（子要素追加を想定）
  const lbl = document.createElement("span");
  lbl.className = "tl-clip-label";
  lbl.style.cssText = "flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:4px;pointer-events:none;";
  lbl.textContent = label;
  cl.appendChild(lbl);
  return cl;
}

function positionClip(el, start, end) {
  el.style.left  = start * tlScale + "px";
  el.style.width = Math.max((end - start) * tlScale, 4) + "px";
}

// ─── クリップドラッグ + 磁気スナップ ─────────────────────────
function onClipMouseDown(e, seg, colorIdx) {
  // ハンドル要素経由のクリックは専用ハンドラへ
  if (e.target.classList.contains("tl-clip-left-handle") ||
      e.target.classList.contains("tl-clip-resize")) return;

  e.stopPropagation();

  // 選択
  selectedId = seg.id;
  mode = "text";
  document.querySelectorAll(".tool-btn").forEach(b => b.classList.remove("active"));
  document.querySelector('[data-mode="text"]')?.classList.add("active");
  renderProps();
  renderTimeline();

  const clip    = e.currentTarget;
  const rect    = clip.getBoundingClientRect();
  const isLeft  = e.clientX < rect.left + 12;
  const isRight = e.clientX > rect.right - 12;

  let dragMode = "move";
  if (isLeft)  dragMode = "trim_left";
  if (isRight) dragMode = "resize";

  dragging = {
    seg, clip,
    startX:    e.clientX,
    origStart: seg.start,
    origEnd:   seg.end,
    mode:      dragMode,
  };
  document.body.style.cursor = (isLeft || isRight) ? "ew-resize" : "grabbing";

  const snapLine = document.getElementById("tl-snap-line");

  const onMove = e => {
    if (!dragging) return;
    const dx  = (e.clientX - dragging.startX) / tlScale;
    const dur = video().duration || 9999;

    if (dragging.mode === "move") {
      let newStart = Math.max(0, Math.min(dragging.origStart + dx, dur - 0.1));
      let newEnd   = newStart + (dragging.origEnd - dragging.origStart);

      // スナップ（開始端・終了端）
      const snapS = findSnap(newStart, dragging.seg.id);
      const snapE = findSnap(newEnd,   dragging.seg.id);
      const dS = snapS != null ? Math.abs(newStart - snapS) * tlScale : Infinity;
      const dE = snapE != null ? Math.abs(newEnd   - snapE) * tlScale : Infinity;

      if (dS < SNAP_PX && dS <= dE) {
        const diff = snapS - newStart; newStart += diff; newEnd += diff;
        showSnapLine(snapS);
      } else if (dE < SNAP_PX) {
        const diff = snapE - newEnd; newStart += diff; newEnd += diff;
        showSnapLine(snapE);
      } else {
        hideSnapLine();
      }

      dragging.seg.start = newStart;
      dragging.seg.end   = Math.min(dur, newEnd);

    } else if (dragging.mode === "trim_left") {
      // 左端トリム: 開始点のみ変更、終端は固定
      let newStart = Math.max(0, Math.min(dragging.origStart + dx, dragging.origEnd - 0.1));
      const snap = findSnap(newStart, dragging.seg.id);
      if (snap != null && Math.abs(newStart - snap) * tlScale < SNAP_PX) {
        newStart = snap; showSnapLine(snap);
      } else {
        hideSnapLine();
      }
      dragging.seg.start = newStart;

    } else {
      // resize: 右端のみ変更
      let newEnd = Math.max(dragging.seg.start + 0.1, dragging.origEnd + dx);
      const snap = findSnap(newEnd, dragging.seg.id);
      if (snap != null && Math.abs(newEnd - snap) * tlScale < SNAP_PX) {
        newEnd = snap; showSnapLine(snap);
      } else {
        hideSnapLine();
      }
      dragging.seg.end = newEnd;
    }

    positionClip(dragging.clip, dragging.seg.start, dragging.seg.end);
    mark();
  };

  const onUp = () => {
    if (!dragging) return;
    hideSnapLine();
    state.subtitles.sort((a, b) => a.start - b.start);
    dragging = null;
    document.body.style.cursor = "";
    renderProps();
    renderTimeline();
    pushHistory();
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  };

  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
}

function findSnap(time, excludeId) {
  const thresh = SNAP_PX / tlScale;
  const targets = [video().currentTime];
  (state.subtitles || []).forEach(s => {
    if (s.id === excludeId) return;
    targets.push(s.start, s.end);
  });
  let best = null, bestDist = thresh;
  for (const t of targets) {
    const d = Math.abs(time - t);
    if (d < bestDist) { bestDist = d; best = t; }
  }
  return best;
}

function showSnapLine(t) {
  const lw = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--label-w"));
  const sl = document.getElementById("tl-snap-line");
  sl.style.display = "block";
  sl.style.left    = (lw + t * tlScale) + "px";
}

function hideSnapLine() {
  document.getElementById("tl-snap-line").style.display = "none";
}

// ─── プレイヘッド ──────────────────────────────────────────
function updatePlayhead() {
  const v  = video();
  const lw = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--label-w"));
  const ph = document.getElementById("tl-playhead");
  ph.style.left = (lw + (v.currentTime || 0) * tlScale) + "px";
}

function scrollTlToTime(t) {
  const scroll = document.getElementById("tl-scroll");
  const lw     = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--label-w"));
  const x      = lw + t * tlScale;
  scroll.scrollLeft = Math.max(0, x - scroll.clientWidth / 2);
}

function scrollTimelineToPlayhead() {
  const scroll = document.getElementById("tl-scroll");
  const lw     = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--label-w"));
  const ph     = lw + video().currentTime * tlScale;
  const sl     = scroll.scrollLeft;
  const sw     = scroll.clientWidth;
  if (ph < sl || ph > sl + sw - 40) scroll.scrollLeft = ph - sw * 0.35;
}

// ═══ クリップ分割 ═══════════════════════════════════════════
function splitAtPlayhead() {
  if (selectedId == null) {
    toast("テロップを選択してください", "err"); return;
  }
  const t   = video().currentTime;
  const idx = state.subtitles.findIndex(s => s.id === selectedId);
  if (idx < 0) return;
  const seg = state.subtitles[idx];
  if (t <= seg.start + 0.05 || t >= seg.end - 0.05) {
    toast("再生位置がクリップ内にありません", "err"); return;
  }
  pushHistory();
  const copy = { ...seg, id: Date.now(), start: parseFloat(t.toFixed(2)) };
  seg.end = parseFloat(t.toFixed(2));
  state.subtitles.splice(idx + 1, 0, copy);
  selectedId = copy.id;
  mark(); renderAll(); pushHistory();
  toast("分割しました");
}

// ═══ コンテキストメニュー ═══════════════════════════════════
let ctxTargetSeg = null;

function bindContextMenu() {
  document.getElementById("ctx-split").addEventListener("click", () => {
    hideCtxMenu();
    if (ctxTargetSeg) { selectedId = ctxTargetSeg.id; splitAtPlayhead(); }
  });
  document.getElementById("ctx-dup").addEventListener("click", () => {
    hideCtxMenu();
    if (!ctxTargetSeg) return;
    pushHistory();
    const seg  = ctxTargetSeg;
    const copy = { ...seg, id: Date.now(), start: seg.end, end: seg.end + (seg.end - seg.start) };
    state.subtitles.push(copy);
    state.subtitles.sort((a, b) => a.start - b.start);
    selectedId = copy.id;
    mark(); renderAll(); pushHistory();
  });
  document.getElementById("ctx-del").addEventListener("click", () => {
    hideCtxMenu();
    if (!ctxTargetSeg) return;
    pushHistory();
    const i = state.subtitles.findIndex(s => s.id === ctxTargetSeg.id);
    if (i >= 0) {
      state.subtitles.splice(i, 1);
      if (selectedId === ctxTargetSeg.id) selectedId = null;
    }
    mark(); renderAll(); pushHistory();
  });
  document.addEventListener("click", e => {
    const menu = document.getElementById("context-menu");
    if (!menu.contains(e.target)) hideCtxMenu();
  });
  document.addEventListener("keydown", e => {
    if (e.code === "Escape") hideCtxMenu();
  });
}

function showCtxMenu(x, y, seg) {
  ctxTargetSeg = seg;
  selectedId   = seg.id;
  renderProps(); renderTimeline();
  const menu = document.getElementById("context-menu");
  // 画面端からはみ出さないよう調整
  const mw = 190, mh = 120;
  const left = Math.min(x, window.innerWidth  - mw - 6);
  const top  = Math.min(y, window.innerHeight - mh - 6);
  menu.style.left = left + "px";
  menu.style.top  = top  + "px";
  menu.classList.add("show");
}

function hideCtxMenu() {
  document.getElementById("context-menu").classList.remove("show");
  ctxTargetSeg = null;
}

// ═══ ショートカットモーダル ════════════════════════════════
function bindShortcutModal() {
  document.getElementById("shortcut-close").addEventListener("click", () => toggleShortcutModal(false));
  document.getElementById("shortcut-modal").addEventListener("click", e => {
    if (e.target === e.currentTarget) toggleShortcutModal(false);
  });
}

function toggleShortcutModal(show) {
  const m = document.getElementById("shortcut-modal");
  if (show === undefined) show = !m.classList.contains("show");
  m.classList.toggle("show", show);
}

// ═══ ファイルオプション ══════════════════════════════════════
async function loadFileOptions(sel, type) {
  try {
    const files = await api("GET", "/api/files");
    if (!sel) {
      // 初回（audio パネル外から呼ばれた場合）
      const bgmSel = document.getElementById("bgm-file");
      const seSel  = document.getElementById("se-file");
      fill(bgmSel, files.bgm, state?.bgm?.file || "");
      fill(seSel,  files.se,  state?.se?.file  || "");
      if (bgmSel) bgmSel.addEventListener("change", e => { state.bgm.file = e.target.value; mark(); renderTimeline(); });
      if (seSel)  seSel.addEventListener("change",  e => { state.se.file  = e.target.value; mark(); });
    } else {
      const cur = type === "bgm" ? state?.bgm?.file : state?.se?.file;
      fill(sel, type === "bgm" ? files.bgm : files.se, cur || "");
      sel.addEventListener("change", e => {
        if (type === "bgm") { state.bgm.file = e.target.value; mark(); renderTimeline(); }
        else                { state.se.file  = e.target.value; mark(); }
      });
    }
  } catch(e) { /* ファイルなし */ }
}

function fill(el, list, cur) {
  if (!el || !list) return;
  el.innerHTML = '<option value="">── 選択 ──</option>' +
    list.map(f => `<option value="${f}" ${f === cur ? "selected" : ""}>${f.split("/").pop()}</option>`).join("");
}

// ═══ キーボード ═════════════════════════════════════════════
function bindKeyboard() {
  document.addEventListener("keydown", e => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;

    const v = video();

    // 再生コントロール
    if (e.code === "Space") { e.preventDefault(); togglePlay(); }
    if (e.code === "ArrowLeft")  { e.preventDefault(); v.currentTime = Math.max(0, v.currentTime - 2); updateAll(); }
    if (e.code === "ArrowRight") { e.preventDefault(); v.currentTime += 2; updateAll(); }

    // JKL トランスポート
    if (e.code === "KeyJ") { e.preventDefault(); v.currentTime = Math.max(0, v.currentTime - 5); updateAll(); }
    if (e.code === "KeyK") { e.preventDefault(); v.paused ? v.play() : v.pause(); }
    if (e.code === "KeyL") { e.preventDefault(); v.currentTime += 5; updateAll(); }

    // 分割
    if (e.code === "KeyS") { e.preventDefault(); splitAtPlayhead(); }

    // Undo/Redo
    if ((e.metaKey || e.ctrlKey) && e.code === "KeyZ") { e.preventDefault(); undo(); }
    if ((e.metaKey || e.ctrlKey) && (e.code === "KeyY" || (e.shiftKey && e.code === "KeyZ"))) {
      e.preventDefault(); redo();
    }

    // 保存
    if ((e.metaKey || e.ctrlKey) && e.code === "KeyS") { e.preventDefault(); saveState(); }

    // 削除
    if (e.code === "Delete" || e.code === "Backspace") {
      if (selectedId != null) {
        pushHistory();
        const i = state.subtitles.findIndex(s => s.id === selectedId);
        if (i >= 0) { state.subtitles.splice(i, 1); selectedId = null; mark(); renderAll(); pushHistory(); }
      }
    }

    // ショートカットヘルプ
    if (e.code === "Slash" && e.shiftKey) { toggleShortcutModal(); }  // ?
    if (e.code === "Escape") { toggleShortcutModal(false); hideCtxMenu(); }
  });
}

// ═══ エクスポート ═══════════════════════════════════════════
function bindExportModal() {
  document.getElementById("btn-export").addEventListener("click", openExport);
  document.getElementById("btn-modal-close").addEventListener("click", () => {
    document.getElementById("export-modal").classList.remove("show");
    if (exportPoll) { clearInterval(exportPoll); exportPoll = null; }
  });
  document.getElementById("btn-export-run").addEventListener("click", startExport);
}

function openExport() {
  document.getElementById("export-modal").classList.add("show");
  document.getElementById("export-log").textContent = "「開始」ボタンを押してください...";
  document.getElementById("export-progress").style.display = "none";
  document.getElementById("btn-export-run").disabled = false;
}

async function startExport() {
  document.getElementById("btn-export-run").disabled = true;
  document.getElementById("export-progress").style.display = "block";
  document.getElementById("export-log").textContent = "";
  try {
    await api("POST", "/api/export/start", state);
    exportPoll = setInterval(async () => {
      const st  = await api("GET", "/api/export/status");
      const log = document.getElementById("export-log");
      log.textContent = st.log.join("\n");
      log.scrollTop   = log.scrollHeight;
      if (!st.running) {
        clearInterval(exportPoll); exportPoll = null;
        document.getElementById("export-progress").style.display = "none";
        document.getElementById("btn-export-run").disabled = false;
        if (st.done) {
          video().src = `/media/output/final_16x9.mp4?t=${Date.now()}`;
          video().load();
          log.textContent += "\n✅ 完了！ output/final_16x9.mp4 を確認してください。";
        }
      }
    }, 800);
  } catch(e) {
    document.getElementById("export-log").textContent = "❌ " + e.message;
    document.getElementById("btn-export-run").disabled = false;
  }
}

// ═══ 全体レンダリング ══════════════════════════════════════
function renderAll() {
  renderProps();
  drawTimeline();
}

// ═══ ユーティリティ ════════════════════════════════════════

/** タイムコード MM:SS.mmm (monospace 向け) */
function fmtTC(t) {
  if (!isFinite(t) || t == null) return "0:00.000";
  const m   = Math.floor(t / 60);
  const s   = Math.floor(t % 60);
  const ms  = Math.floor((t % 1) * 1000);
  return `${m}:${String(s).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
}

/** ルーラー用の短いフォーマット */
function fmt(t, short = false) {
  if (!isFinite(t) || t == null) return short ? "0:00" : "0:00.0";
  const m  = Math.floor(t / 60);
  const s  = Math.floor(t % 60);
  if (short) return `${m}:${String(s).padStart(2, "0")}`;
  const ds = Math.floor((t % 1) * 10);
  return `${m}:${String(s).padStart(2, "0")}.${ds}`;
}

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function toast(msg, type = "ok") {
  const el = document.createElement("div");
  el.style.cssText = `
    position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
    background:${type === "ok" ? "#22c55e" : "#ef4444"};
    color:${type === "ok" ? "#000" : "#fff"};
    padding:7px 18px;border-radius:99px;
    font-size:13px;font-weight:700;z-index:9999;
    box-shadow:0 4px 14px rgba(0,0,0,.5);pointer-events:none;
  `;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2200);
}
