/* BlackFox AI Workstation — frontend v3: core, threads, chat, nodes strip, polling */
"use strict";

// ---------------------------------------------------------------------------
// Client identity + API wrapper
// ---------------------------------------------------------------------------
const Client = {
  id: null, name: "",
  init() {
    try {
      this.id = localStorage.getItem("bf.client.id");
      if (!this.id) { this.id = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2)).replace(/-/g, "").slice(0, 16); localStorage.setItem("bf.client.id", this.id); }
      this.name = localStorage.getItem("bf.client.name") || "";
    } catch (e) { this.id = "tmp" + Date.now(); }
    if (!this.name) { this.name = "Пользователь-" + this.id.slice(0, 4); this.setName(this.name); }
  },
  setName(n) { this.name = (n || "").trim().slice(0, 40); try { localStorage.setItem("bf.client.name", this.name); } catch (e) {} },
  headers() { return { "X-BF-Client-Id": this.id, "X-BF-Client-Name": encodeURIComponent(this.name) }; },
  async deviceInfo() {
    const d = { platform: navigator.platform || "", language: navigator.language, timezone: (Intl.DateTimeFormat().resolvedOptions().timeZone || ""),
      screen: `${screen.width}×${screen.height}@${window.devicePixelRatio || 1}x`, cores: navigator.hardwareConcurrency || 0, memory_gb: navigator.deviceMemory || 0,
      touch: (navigator.maxTouchPoints || 0) > 0, connection: (navigator.connection && navigator.connection.effectiveType) || "", browser: "", os: "", brands: "", mobile: false, model: "" };
    const ua = navigator.userAgent;
    const b = /Edg\/([\d.]+)/.exec(ua) ? "Edge " + RegExp.$1 : /OPR\/([\d.]+)/.exec(ua) ? "Opera " + RegExp.$1 : /Firefox\/([\d.]+)/.exec(ua) ? "Firefox " + RegExp.$1 : /Chrome\/([\d.]+)/.exec(ua) ? "Chrome " + RegExp.$1 : /Version\/([\d.]+).*Safari/.exec(ua) ? "Safari " + RegExp.$1 : "";
    d.browser = b.split(".")[0];
    try {
      if (navigator.userAgentData) {
        d.mobile = !!navigator.userAgentData.mobile; d.brands = (navigator.userAgentData.brands || []).map(x => x.brand + " " + x.version).filter(x => !/Not/.test(x)).join(", ");
        const h = await navigator.userAgentData.getHighEntropyValues(["platform", "platformVersion", "model", "architecture"]);
        d.os = `${h.platform} ${h.platformVersion || ""}`.trim(); d.model = h.model || ""; if (h.architecture) d.os += ` (${h.architecture})`;
      }
    } catch (e) {}
    if (!d.os) d.os = /Windows NT 10/.test(ua) ? "Windows 10/11" : /Mac OS X ([\d_]+)/.exec(ua) ? "macOS " + RegExp.$1.replace(/_/g, ".") : /Android ([\d.]+)/.exec(ua) ? "Android " + RegExp.$1 : /iPhone OS ([\d_]+)/.exec(ua) ? "iOS " + RegExp.$1.replace(/_/g, ".") : /Linux/.test(ua) ? "Linux" : "";
    return d;
  },
};

async function api(path, body, opt) {
  const opts = { headers: { ...Client.headers(), ...(typeof Auth !== "undefined" ? Auth.headers() : {}) } };
  if (body !== undefined) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { data = {}; }
  if (r.status === 401 && typeof Auth !== "undefined" && !(opt && opt.noAuthRetry)) { Auth.required(opts.headers["X-BF-Auth"] || null); throw new Error(data.error || "Требуется вход"); }
  if (!r.ok) throw new Error(data.message || data.error || ("HTTP " + r.status));
  return data;
}

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const fmtTime = (iso) => { if (!iso) return ""; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }); };
const fmtDate = (iso) => { if (!iso) return ""; const d = new Date(iso); if (isNaN(d)) return iso; const t = new Date(); return d.toDateString() === t.toDateString() ? fmtTime(iso) : d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }) + " " + fmtTime(iso); };
const fmtMs = (ms) => ms == null ? "—" : ms >= 1000 ? (ms / 1000).toFixed(1) + " с" : ms + " мс";
const fmtGB = (b) => b == null ? "—" : (b / 1024 ** 3).toFixed(2) + " ГБ";
const MODE_LABEL = { single: "🎯 Один узел", all: "⚡ Все узлы", pipeline: "🔄 Конвейер", hybrid: "🧩 Гибрид", council: "🤝 Консилиум" };

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------
const State = {
  nodes: {}, settings: null, threads: [], active: [], tunnel: "", port: 8765, you: { is_admin: false },
  currentThreadId: null, thread: null, models: {}, telemetry: null, clients: [],
  ui: { theme: "dark", font: 13, density: "comfortable", accent: "#3b82f6", poll: 1500, sidebar: 270, strip: true, reasoning: false, confirmClear: true },
  get isAdmin() { return !!(this.you && this.you.is_admin); },
  nodeList() { return Object.values(this.nodes); },
};

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------
const UI = {
  tab: "chat",
  ACCENTS: ["#3b82f6", "#8b5cf6", "#06b6d4", "#10b981", "#f59e0b", "#f43f5e", "#64748b"],
  init() {
    try { Object.assign(State.ui, JSON.parse(localStorage.getItem("bf.ui") || "{}")); } catch (e) {}
    this.applyUi();
    const t = location.hash.replace("#", "");
    if (t && $("view-" + t)) this.showTab(t, true);
    if (localStorage.getItem("bf.sidebar") === "0") $("app").classList.add("sidebar-collapsed");
    window.addEventListener("hashchange", () => { const h = location.hash.replace("#", ""); if (h && h !== this.tab && $("view-" + h)) this.showTab(h, true); });
    document.addEventListener("click", (e) => { if (!e.target.closest(".pop-anchor")) document.querySelectorAll(".popover.open").forEach(p => p.classList.remove("open")); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { this.closeModal(); document.querySelectorAll(".popover.open").forEach(p => p.classList.remove("open")); } });
  },
  applyUi() {
    const u = State.ui;
    const sys = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    document.documentElement.dataset.theme = u.theme === "system" ? sys : u.theme;
    document.documentElement.dataset.density = u.density;
    document.documentElement.style.setProperty("--font-size", u.font + "px");
    document.documentElement.style.setProperty("--accent", u.accent);
    document.documentElement.style.setProperty("--accent-2", u.accent);
    document.documentElement.style.setProperty("--accent-soft", u.accent + "24");
    document.documentElement.style.setProperty("--sidebar-w", u.sidebar + "px");
    $("app").classList.toggle("hide-strip", !u.strip);
    $("themeBtn").textContent = document.documentElement.dataset.theme === "dark" ? "🌙" : "☀️";
  },
  saveUi() { try { localStorage.setItem("bf.ui", JSON.stringify(State.ui)); } catch (e) {} this.applyUi(); },
  toggleTheme() { State.ui.theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; this.saveUi(); if (typeof Settings !== "undefined") Settings.fillUi(); },
  toggleSidebar() { const c = $("app").classList.toggle("sidebar-collapsed"); try { localStorage.setItem("bf.sidebar", c ? "0" : "1"); } catch (e) {} },
  applyAdmin() {
    const a = State.isAdmin;
    document.querySelectorAll(".admin-only").forEach(el => { el.style.display = a ? "" : "none"; });
    document.querySelectorAll(".non-admin-only").forEach(el => { el.style.display = a ? "none" : ""; });
    $("meAdminBadge").style.display = a ? "" : "none";
    const lo = $("logoutBtn"); if (lo) lo.style.display = (State.you && State.you.auth_enabled) ? "" : "none";
    if (!a && this.tab === "admin") this.showTab("chat");
  },
  showTab(name, silent) {
    if (!$("view-" + name)) return;
    this.tab = name;
    document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + name));
    if (!silent) history.replaceState(null, "", "#" + name);
    const hooks = { telemetry: () => Telemetry.refresh(true), users: () => Users.refresh(), prompts: () => Prompts.render(), settings: () => Settings.render(),
      models: () => Models.open(), admin: () => Admin.open(), help: () => Help.render() };
    try { if (hooks[name]) hooks[name](); } catch (e) { console.error(e); }
  },
  togglePopover(id) { const p = $(id); const open = p.classList.contains("open"); document.querySelectorAll(".popover.open").forEach(x => x.classList.remove("open")); if (!open) p.classList.add("open"); },
  toast(msg, kind = "", ms = 3200) {
    const el = document.createElement("div"); el.className = "toast " + kind; el.innerHTML = msg; $("toasts").appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; setTimeout(() => el.remove(), 300); }, ms);
  },
  copyTunnel() { const url = `${location.protocol}//${State.tunnel}:${State.port}`; navigator.clipboard?.writeText(url); this.toast("Скопировано: " + url, "ok"); },
  modal(title, bodyHtml, onOk, okLabel = "OK", wide = false) {
    $("modalTitle").textContent = title; $("modalBody").innerHTML = bodyHtml; $("modalBox").classList.toggle("wide", !!wide);
    $("modalFoot").innerHTML = onOk ? `<button class="btn" onclick="UI.closeModal()">Отмена</button><button class="btn primary" id="modalOk">${esc(okLabel || "OK")}</button>` : '<button class="btn" onclick="UI.closeModal()">Закрыть</button>';
    if (onOk) $("modalOk").onclick = async () => { try { const keep = await onOk(); if (keep !== true) this.closeModal(); } catch (e) { UI.toast("Ошибка: " + esc(e.message), "err"); } };
    $("modal").classList.add("open");
    const inp = $("modalBody").querySelector("input[type=text],textarea"); if (inp) { inp.focus(); inp.select && inp.select(); if (inp.tagName === "INPUT" && onOk) inp.addEventListener("keydown", e => { if (e.key === "Enter") $("modalOk").click(); }); }
  },
  closeModal() { $("modal").classList.remove("open"); },
  confirm(title, text, onOk, okLabel = "Да") { this.modal(title, `<div style="line-height:1.5">${text}</div>`, onOk, okLabel); },
  prompt(title, value, onOk) { this.modal(title, `<input type="text" id="modalInput" style="width:100%" value="${esc(value)}">`, () => onOk($("modalInput").value), "Сохранить"); },
  info(title, html, wide) { this.modal(title, html, null, "", wide); },
};

// ---------------------------------------------------------------------------
// Sparklines + markdown
// ---------------------------------------------------------------------------
function spark(canvas, data, color, max = 100) {
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 120, h = canvas.clientHeight || 28;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
  const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
  if (!data || data.length < 2) return;
  const pts = data.slice(-80); const mx = max === "auto" ? Math.max(1, ...pts) * 1.1 : max;
  const step = w / (pts.length - 1);
  ctx.beginPath();
  pts.forEach((v, i) => { const y = h - 2 - Math.min(1, Math.max(0, (v || 0) / mx)) * (h - 4); i ? ctx.lineTo(i * step, y) : ctx.moveTo(0, y); });
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke();
  ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath(); ctx.fillStyle = color + "22"; ctx.fill();
}

function md(src) {
  if (!src) return "";
  const fences = [];
  let s = src.replace(/```([\w+-]*)[ \t]*\n([\s\S]*?)```/g, (m, lang, code) => { fences.push({ lang, code }); return ` F${fences.length - 1} `; });
  s = s.replace(/```([\w+-]*)[ \t]*\n([\s\S]*)$/g, (m, lang, code) => { fences.push({ lang, code }); return ` F${fences.length - 1} `; });
  s = esc(s);
  const inline = (t) => t
    .replace(/`([^`\n]+)`/g, (m, c) => `<code>${c}</code>`)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  const lines = s.split("\n"); const out = []; let i = 0;
  const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  while (i < lines.length) {
    let l = lines[i];
    if (/^\s*$/.test(l)) { i++; continue; }
    let m;
    if ((m = l.match(/^\s*(#{1,4})\s+(.*)$/))) { out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); i++; continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(l)) { out.push("<hr>"); i++; continue; }
    if (/^\s*&gt;/.test(l)) { const b = []; while (i < lines.length && /^\s*&gt;/.test(lines[i])) { b.push(lines[i].replace(/^\s*&gt;\s?/, "")); i++; } out.push(`<blockquote>${inline(b.join("<br>"))}</blockquote>`); continue; }
    if (l.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const cells = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => inline(c.trim()));
      const head = cells(l); i += 2; const rows = [];
      while (i < lines.length && lines[i].includes("|") && !/^\s*$/.test(lines[i])) { rows.push(cells(lines[i])); i++; }
      out.push(`<table><thead><tr>${head.map(c => `<th>${c}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    if ((m = l.match(/^\s*([-*+•]|\d+[.)])\s+/))) {
      const ordered = /\d/.test(m[1]); const items = [];
      while (i < lines.length && (m = lines[i].match(/^\s*([-*+•]|\d+[.)])\s+(.*)$/))) {
        let item = m[2]; i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*+•]|\d+[.)])\s+/.test(lines[i])) { item += " " + lines[i].trim(); i++; }
        items.push(`<li>${inline(item)}</li>`);
      }
      out.push(`<${ordered ? "ol" : "ul"}>${items.join("")}</${ordered ? "ol" : "ul"}>`); continue;
    }
    if (/^ F\d+ \s*$/.test(l)) { out.push(l.trim()); i++; continue; }
    const p = []; while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^\s*(#{1,4}\s|&gt;|([-*+•]|\d+[.)])\s| F)/.test(lines[i]) && !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1]))) { p.push(lines[i]); i++; }
    if (p.length) out.push(`<p>${inline(p.join("<br>"))}</p>`);
  }
  let html = out.join("\n");
  html = html.replace(/ F(\d+) /g, (m, idx) => { const f = fences[+idx]; const id = "c" + Math.random().toString(36).slice(2, 9); return `<pre><span class="lang">${esc(f.lang)}</span><button class="btn sm copy" onclick="copyCode('${id}',this)">📋</button><code id="${id}">${esc(f.code.replace(/\n$/, ""))}</code></pre>`; });
  return html;
}
function copyCode(id, btn) { const el = $(id); if (!el) return; navigator.clipboard?.writeText(el.textContent).then(() => { btn.textContent = "✅"; setTimeout(() => btn.textContent = "📋", 1500); }); }

// ---------------------------------------------------------------------------
// Threads (sidebar)
// ---------------------------------------------------------------------------
const Threads = {
  filter: localStorage.getItem("bf.threadFilter") || "all",
  async create() {
    const r = await api("/api/threads/create", { title: "Новый чат", mode: Chat.mode, node: $("targetNode").value || "node_1" });
    State.threads.unshift({ ...r.thread, count: 0, preview: "" });
    this.select(r.thread.id); this.render(); UI.showTab("chat"); $("taskInput").focus();
  },
  setFilter(f) { this.filter = f; try { localStorage.setItem("bf.threadFilter", f); } catch (e) {} this.render(); },
  select(id) {
    if (!id) return;
    if (State.currentThreadId !== id) { State.thread = null; Chat.msgEls = {}; $("messages").querySelectorAll(".msg,.active-banner").forEach(e => e.remove()); }
    State.currentThreadId = id; try { localStorage.setItem("bf.thread", id); } catch (e) {}
    this.render(); Chat.loadThread(true, true);
    if (window.innerWidth < 900) $("app").classList.add("sidebar-collapsed");
  },
  render() {
    document.querySelectorAll("#threadFilter button").forEach(b => b.classList.toggle("active", b.dataset.f === this.filter));
    const q = ($("threadSearch").value || "").toLowerCase();
    const list = $("threadList"); list.innerHTML = "";
    let items = State.threads.filter(t => !q || (t.title || "").toLowerCase().includes(q) || (t.preview || "").toLowerCase().includes(q));
    if (this.filter === "mine") items = items.filter(t => !t.owner_id || t.owner_id === Client.id || t.id === State.currentThreadId);
    const pinned = items.filter(t => t.pinned), rest = items.filter(t => !t.pinned);
    const group = (label, arr) => {
      if (!arr.length) return;
      if (label) { const g = document.createElement("div"); g.className = "thread-group-label"; g.textContent = label; list.appendChild(g); }
      arr.forEach(t => {
        const el = document.createElement("div"); el.className = "thread-item" + (t.id === State.currentThreadId ? " active" : "");
        const mine = !t.owner_id || t.owner_id === Client.id;
        el.innerHTML = `<div class="thread-title">${t.pinned ? '<span class="pin">📌</span> ' : ""}${esc(t.title || "Без названия")}</div>
          <div class="thread-meta">${t.busy ? '<span class="busy">⏳</span>' : ""}<span>${MODE_LABEL[t.mode] || ""}</span><span>· ${t.count}</span><span>· ${fmtDate(t.updated_at)}</span>${!mine ? `<span class="owner" title="Владелец ветки">· 👤 ${esc(t.owner_name || "?")}</span>` : ""}</div>
          <div class="thread-actions">
            <button class="btn ghost sm" title="Переименовать" onclick="event.stopPropagation();Threads.rename('${t.id}')">✎</button>
            <button class="btn ghost sm" title="${t.pinned ? "Открепить" : "Закрепить"}" onclick="event.stopPropagation();Threads.pin('${t.id}',${!t.pinned})">📌</button>
            <button class="btn ghost sm" title="Удалить ветку" onclick="event.stopPropagation();Threads.remove('${t.id}')">🗑</button>
          </div>`;
        el.onclick = () => this.select(t.id);
        list.appendChild(el);
      });
    };
    group(pinned.length ? "Закреплённые" : "", pinned); group(pinned.length ? "Остальные" : "", rest);
    if (!items.length) list.innerHTML = `<div class="muted small" style="padding:10px">${this.filter === "mine" ? "У вас пока нет веток — создайте новый чат" : "Ничего не найдено"}</div>`;
  },
  rename(id) { const t = State.threads.find(x => x.id === id); UI.prompt("Название ветки", t?.title || "", async (v) => { await api("/api/threads/rename", { thread_id: id, title: v }); if (t) t.title = v; this.render(); if (id === State.currentThreadId) $("chatTitle").value = v; }); },
  async renameCurrent(v) { if (!State.currentThreadId) return; await api("/api/threads/rename", { thread_id: State.currentThreadId, title: v }); const t = State.threads.find(x => x.id === State.currentThreadId); if (t) t.title = v; this.render(); },
  async pin(id, val) { await api("/api/threads/meta", { thread_id: id, pinned: val }); const t = State.threads.find(x => x.id === id); if (t) t.pinned = val; this.render(); },
  remove(id) {
    const t = State.threads.find(x => x.id === id);
    UI.confirm("Удалить ветку", `Удалить «${esc(t?.title || "")}» со всеми сообщениями?${t && t.owner_id && t.owner_id !== Client.id ? "<br><b>Это ветка другого пользователя.</b>" : ""}`, async () => {
      const r = await api("/api/threads/delete", { thread_id: id }); State.threads = r.threads;
      if (State.currentThreadId === id) { State.currentThreadId = null; if (State.threads[0]) this.select(State.threads[0].id); else this.create(); } else this.render();
      UI.toast("Ветка удалена", "ok");
    }, "Удалить");
  },
};

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------
const Chat = {
  mode: "single", msgEls: {}, busy: false, panel: null, planDraft: null, councilDraft: null, saveTimer: null,
  init() {
    $("taskInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); this.submit(); } });
    $("taskInput").addEventListener("input", (e) => { const t = e.target; t.style.height = "auto"; t.style.height = Math.min(260, t.scrollHeight) + "px"; });
    try { const p = JSON.parse(localStorage.getItem("bf.params") || "{}"); $("pTemp").value = p.temperature ?? ""; $("pMax").value = p.max_tokens ?? ""; $("pReason").value = p.reasoning_effort ?? ""; $("pHist").value = p.history_turns ?? ""; } catch (e) {}
    ["pTemp", "pMax", "pReason", "pHist"].forEach(id => $(id).addEventListener("change", () => this.saveParams()));
  },
  saveParams() { try { localStorage.setItem("bf.params", JSON.stringify(this.params())); } catch (e) {} },
  resetParams() { $("pTemp").value = ""; $("pMax").value = ""; $("pReason").value = ""; $("pHist").value = ""; this.saveParams(); },
  params() { const o = {}; const t = $("pTemp").value, m = $("pMax").value, h = $("pHist").value, r = $("pReason").value; if (t !== "") o.temperature = +t; if (m !== "") o.max_tokens = +m; if (h !== "") o.history_turns = +h; if (r !== "") o.reasoning_effort = r; return o; },

  // ---- thread meta <-> composer (the thread is the source of truth)
  applyThreadToComposer(t) {
    this.mode = t.mode || "single";
    document.querySelectorAll("#modeSeg button").forEach(b => b.classList.toggle("active", b.dataset.mode === this.mode));
    if (t.node && State.nodes[t.node]) $("targetNode").value = t.node;
    $("skillSelect").value = t.skill || "default"; if (!$("skillSelect").value) $("skillSelect").value = "default";
    $("noRoles").checked = !!t.no_roles;
    this.planDraft = t.plan ? JSON.parse(JSON.stringify(t.plan)) : null;
    this.councilDraft = t.council ? JSON.parse(JSON.stringify(t.council)) : null;
    this.fillTargetModels();
    this.refreshModeUi();
  },
  saveMeta(patch) {
    if (!State.currentThreadId) return;
    api("/api/threads/meta", { thread_id: State.currentThreadId, ...patch }).catch(() => {});
    const t = State.threads.find(x => x.id === State.currentThreadId); if (t) Object.assign(t, patch);
    if (State.thread) Object.assign(State.thread, patch);
  },
  setMode(mode) {
    this.mode = mode;
    document.querySelectorAll("#modeSeg button").forEach(b => b.classList.toggle("active", b.dataset.mode === mode));
    this.saveMeta({ mode });
    if (mode === "hybrid") { if (!(this.planDraft && this.planDraft.stages && this.planDraft.stages.length)) { this.planDraft = this.pipelineAsPlan(); this.saveMeta({ plan: this.planDraft }); } this.togglePanel("plan", true); }
    else if (mode === "council") { if (!this.councilDraft) { this.councilDraft = { nodes: State.nodeList().filter(n => n.enabled).map(n => n.key), moderator: $("targetNode").value, rounds: (State.settings?.council?.rounds ?? 1) }; this.saveMeta({ council: this.councilDraft }); } this.togglePanel("council", true); }
    else this.togglePanel(null);
    this.refreshModeUi();
  },
  refreshModeUi() {
    const m = this.mode;
    $("targetNode").style.display = (m === "single" || m === "council") ? "" : "none";
    $("targetModel").style.display = (m === "single" && State.isAdmin) ? "" : "none";
    $("planBtn").style.display = m === "hybrid" ? "" : "none";
    $("councilBtn").style.display = m === "council" ? "" : "none";
    $("targetNode").title = m === "council" ? "Модератор консилиума по умолчанию" : "Целевой узел";
    const hints = { single: "Запрос уйдёт выбранному узлу вместе с историей этой ветки", all: "Запрос уйдёт параллельно на все включённые узлы — ответ каждого отдельно",
      pipeline: `Конвейер из настроек: ${(State.settings?.pipeline || []).map(s => State.nodes[s.node]?.avatar || "?").join(" → ") || "нет шагов"}. Изменить только для этой ветки — режим «Гибрид»`,
      hybrid: "Свой план ветки: этапы выполняются по очереди, узлы внутри этапа — параллельно", council: "Черновики → обсуждение между узлами → один общий ответ модератора" };
    $("composerHint").textContent = hints[m] || "";
    $("planSummary").textContent = m === "hybrid" ? this.planSummary(this.planDraft) : m === "council" ? this.councilSummary(this.councilDraft) : "";
    if ((this.panel === "plan" && m !== "hybrid") || (this.panel === "council" && m !== "council")) this.togglePanel(null);
  },
  planSummary(p) { if (!p || !p.stages || !p.stages.length) return "план пуст"; return p.stages.map(s => (s.nodes || []).map(n => State.nodes[typeof n === "string" ? n : n.key]?.avatar || "?").join("+") || "∅").join(" → "); },
  councilSummary(c) { if (!c) return ""; const parts = (c.nodes || []).map(k => State.nodes[k]?.avatar || "?").join(""); return `${parts} · модератор ${State.nodes[c.moderator]?.avatar || "?"} · раундов ${c.rounds ?? 1}`; },
  pipelineAsPlan() { return { stages: (State.settings?.pipeline || []).filter(s => State.nodes[s.node]).map((s, i) => ({ name: s.label || `Этап ${i + 1}`, nodes: [{ key: s.node, instruction: "" }], instruction: s.prompt || "", context: "all" })) }; },
  onTargetChange() { const nk = $("targetNode").value; try { localStorage.setItem("bf.node", nk); } catch (e) {} this.fillTargetModels(); this.saveMeta({ node: nk }); },
  onSkillChange(v) { this.saveMeta({ skill: v }); const s = (State.settings?.skills || []).find(x => x.id === v); if (s) UI.toast(`${s.icon} <b>${esc(s.name)}</b>: ${esc(s.desc)}`, "", 2500); },
  onNoRoles(v) { this.saveMeta({ no_roles: v }); },

  // ---- panels: plan editor / council editor
  togglePanel(name, forceOpen) {
    const open = !!name && (forceOpen || this.panel !== name);
    this.panel = open ? name : null;
    $("panel-plan").style.display = this.panel === "plan" ? "" : "none";
    $("panel-council").style.display = this.panel === "council" ? "" : "none";
    if (this.panel === "plan") this.renderPlanEditor();
    if (this.panel === "council") this.renderCouncilEditor();
  },
  renderPlanEditor() {
    const p = (this.planDraft && this.planDraft.stages) ? this.planDraft : (this.planDraft = { stages: [] });
    const nodes = State.nodeList();
    const allNodesPlan = JSON.stringify({ stages: [{ name: "Все узлы", nodes: nodes.filter(n => n.enabled).map(n => ({ key: n.key, instruction: "" })), instruction: "", context: "none" }] });
    $("panel-plan").innerHTML = `<div class="row wrap" style="margin-bottom:6px"><b>🧩 План этой ветки</b><span class="muted small">этапы идут по очереди, узлы внутри этапа — параллельно; следующий этап получает результаты предыдущих</span><span class="grow"></span>
        <button class="btn sm" onclick="Chat.planDraft=Chat.pipelineAsPlan();Chat.planChanged()">Взять конвейер</button>
        <button class="btn sm" onclick='Chat.planDraft=${allNodesPlan};Chat.planChanged()'>Все узлы</button>
        <button class="btn sm" onclick="Chat.addStage()">＋ Этап</button><button class="btn ghost sm" onclick="Chat.togglePanel(null)">✕</button></div>` +
      (p.stages.length ? p.stages.map((st, i) => `<div class="stage">
        <div class="row wrap"><span class="badge stage-n">${i + 1}</span><input type="text" class="sm" value="${esc(st.name || "")}" placeholder="Название этапа" style="width:170px" oninput="Chat.planDraft.stages[${i}].name=this.value;Chat.planChanged(true)">
          <div class="row wrap" style="gap:4px">${nodes.map(n => { const sel = (st.nodes || []).some(x => (typeof x === "string" ? x : x.key) === n.key); return `<button class="chip ${sel ? "on" : ""} ${n.enabled ? "" : "off"}" style="--nc:${n.color}" title="${esc(n.name)}${n.enabled ? "" : " (выключен)"}" onclick="Chat.toggleStageNode(${i},'${n.key}')">${n.avatar} ${esc(n.name.length > 16 ? n.name.slice(0, 15) + "…" : n.name)}</button>`; }).join("")}</div>
          <span class="grow"></span>
          <select class="sm" onchange="Chat.planDraft.stages[${i}].context=this.value;Chat.planChanged()" title="Какие результаты предыдущих этапов передавать"><option value="all" ${st.context === "all" ? "selected" : ""}>контекст: все этапы</option><option value="last" ${st.context === "last" ? "selected" : ""}>контекст: предыдущий</option><option value="none" ${st.context === "none" ? "selected" : ""}>без контекста</option></select>
          <button class="btn ghost sm" ${i === 0 ? "disabled" : ""} onclick="Chat.moveStage(${i},-1)">▲</button><button class="btn ghost sm" ${i === p.stages.length - 1 ? "disabled" : ""} onclick="Chat.moveStage(${i},1)">▼</button><button class="btn ghost sm danger" onclick="Chat.planDraft.stages.splice(${i},1);Chat.planChanged()">✕</button></div>
        <input type="text" class="sm" style="width:100%;margin-top:4px" value="${esc(st.instruction || "")}" placeholder="Инструкция этапа (добавляется перед задачей), например: «Проверь черновики и собери лучший ответ»" oninput="Chat.planDraft.stages[${i}].instruction=this.value;Chat.planChanged(true)">
        </div>`).join("") : '<div class="muted small">Добавьте этап и выберите узлы</div>');
  },
  addStage() { this.planDraft.stages.push({ name: `Этап ${this.planDraft.stages.length + 1}`, nodes: [], instruction: "", context: "all" }); this.planChanged(); },
  moveStage(i, d) { const a = this.planDraft.stages; const j = i + d; if (j < 0 || j >= a.length) return; [a[i], a[j]] = [a[j], a[i]]; this.planChanged(); },
  toggleStageNode(i, key) {
    const st = this.planDraft.stages[i]; st.nodes = (st.nodes || []).map(x => typeof x === "string" ? { key: x, instruction: "" } : x);
    const idx = st.nodes.findIndex(x => x.key === key); if (idx >= 0) st.nodes.splice(idx, 1); else st.nodes.push({ key, instruction: "" });
    this.planChanged();
  },
  planChanged(quiet) {
    if (!quiet) this.renderPlanEditor();
    $("planSummary").textContent = this.planSummary(this.planDraft);
    clearTimeout(this.saveTimer); this.saveTimer = setTimeout(() => this.saveMeta({ plan: this.planDraft }), 400);
  },
  renderCouncilEditor() {
    const c = this.councilDraft || (this.councilDraft = { nodes: State.nodeList().filter(n => n.enabled).map(n => n.key), moderator: $("targetNode").value, rounds: 1 });
    const nodes = State.nodeList();
    $("panel-council").innerHTML = `<div class="row wrap"><b>🤝 Консилиум</b><span class="muted small">участники пишут черновики и обсуждают их между собой; модератор выдаёт один общий ответ</span><span class="grow"></span><button class="btn ghost sm" onclick="Chat.togglePanel(null)">✕</button></div>
      <div class="row wrap" style="margin-top:6px"><span class="small muted">Участники:</span>${nodes.map(n => `<button class="chip ${(c.nodes || []).includes(n.key) ? "on" : ""} ${n.enabled ? "" : "off"}" style="--nc:${n.color}" onclick="Chat.toggleCouncilNode('${n.key}')">${n.avatar} ${esc(n.name)}</button>`).join("")}</div>
      <div class="row wrap" style="margin-top:6px"><span class="small muted">Модератор:</span><select class="sm" onchange="Chat.councilDraft.moderator=this.value;Chat.councilChanged()">${nodes.map(n => `<option value="${n.key}" ${c.moderator === n.key ? "selected" : ""}>${n.avatar} ${esc(n.name)}</option>`).join("")}</select>
        <span class="small muted">Раундов обсуждения:</span><select class="sm" onchange="Chat.councilDraft.rounds=+this.value;Chat.councilChanged()">${[0, 1, 2, 3].map(r => `<option value="${r}" ${(c.rounds ?? 1) === r ? "selected" : ""}>${r}</option>`).join("")}</select>
        <span class="small muted">0 — сразу синтез черновиков; 1–2 обычно достаточно</span></div>`;
  },
  toggleCouncilNode(key) { const c = this.councilDraft; c.nodes = c.nodes || []; const i = c.nodes.indexOf(key); if (i >= 0) c.nodes.splice(i, 1); else c.nodes.push(key); this.councilChanged(); },
  councilChanged() { this.renderCouncilEditor(); $("planSummary").textContent = this.councilSummary(this.councilDraft); this.saveMeta({ council: this.councilDraft }); },

  // ---- selects
  fillTargets() {
    const sel = $("targetNode"); const cur = sel.value || State.thread?.node || localStorage.getItem("bf.node") || "node_1";
    sel.innerHTML = State.nodeList().map(n => `<option value="${n.key}" ${n.enabled ? "" : "disabled"}>${n.avatar} ${esc(n.name)} — ${esc(n.role)}${n.enabled ? "" : " (выкл.)"}</option>`).join("");
    sel.value = State.nodes[cur] ? cur : Object.keys(State.nodes)[0];
    this.fillTargetModels();
    const ss = $("skillSelect"); const skills = (State.settings?.skills || []).filter(s => !s.hidden); const curS = ss.value || State.thread?.skill || "default";
    ss.innerHTML = skills.map(s => `<option value="${esc(s.id)}" title="${esc(s.desc)}">${s.icon} ${esc(s.name)}${s.web ? " 🌐" : ""}</option>`).join("");
    ss.value = skills.find(s => s.id === curS) ? curS : "default";
  },
  fillTargetModels() {
    const nk = $("targetNode").value; const n = State.nodes[nk]; const sel = $("targetModel"); if (!n) return;
    const models = State.models[nk]?.models || [];
    const opts = models.filter(m => m.type !== "embeddings").map(m => `<option value="${esc(m.id)}">${m.loaded === true ? "● " : m.loaded === false ? "○ " : ""}${esc(m.id)}${m.quant ? " · " + esc(m.quant) : ""}</option>`);
    if (!models.find(m => m.id === n.model)) opts.unshift(`<option value="${esc(n.model)}">${esc(n.model)}</option>`);
    sel.innerHTML = opts.join(""); sel.value = n.model;
  },
  async onModelPick(model) { await Nodes.setModel($("targetNode").value, model); },

  // ---- submit / stop / clear / export
  async submit() {
    const input = $("taskInput"); const prompt = input.value.trim(); if (!prompt && !this.attachments.length) return;
    if (!State.currentThreadId) await Threads.create();
    try {
      const body = { thread_id: State.currentThreadId, mode: this.mode, target_node: $("targetNode").value, prompt: prompt || "Проанализируй приложенные файлы.", skill: $("skillSelect").value || "default", no_roles: $("noRoles").checked, ...this.params() };
      if (this.attachments.length) body.files = await this.uploadAttachments(State.currentThreadId);
      if (State.isAdmin && $("toolsOn") && $("toolsOn").checked && this.mode === "single") body.tools = true;
      if (this.mode === "hybrid") body.plan = this.planDraft;
      if (this.mode === "council") body.council = this.councilDraft;
      const r = await api("/api/execute", body);
      input.value = ""; input.style.height = "auto"; this.attachments = []; this.renderAttach();
      if (r.thread_id !== State.currentThreadId) State.currentThreadId = r.thread_id;
      this.loadThread();
    } catch (e) { UI.toast("Ошибка: " + esc(e.message), "err", 5000); }
  },
  async stop() { if (!State.currentThreadId) return; await api("/api/cancel", { thread_id: State.currentThreadId }); UI.toast("Остановка генерации…", "warn"); },
  clearThread() {
    if (!State.currentThreadId) return;
    const doClear = async () => { await api("/api/threads/clear", { thread_id: State.currentThreadId }); this.msgEls = {}; $("messages").querySelectorAll(".msg,.active-banner").forEach(e => e.remove()); this.loadThread(); UI.toast("Окно чата очищено", "ok"); };
    if (State.ui.confirmClear) UI.confirm("Очистить чат", "Удалить все сообщения этой ветки? Ветка и её настройки останутся.", doClear, "Очистить"); else doClear();
  },
  exportThread() {
    const t = State.thread; if (!t) return;
    let mdText = `# ${t.title}\n\n`;
    t.messages.forEach(m => {
      if (m.role === "user") mdText += `## 🧑 ${m.author || "Запрос"} (${m.time || ""})\n\n${m.content}\n\n`;
      else {
        mdText += `## ${m.node_avatar || ""} ${m.node_name || "Узел"}${m.step_label ? " — " + m.step_label : ""} (${m.model || ""}, ${fmtMs(m.latency_ms)})\n\n${m.error ? "**Ошибка:** " + m.error : m.content}\n\n`;
        if (m.discussion && m.discussion.length) mdText += `<details><summary>Обсуждение консилиума</summary>\n\n` + m.discussion.map(d => `### ${d.kind === "draft" ? "Черновик" : "Замечания"} — ${d.node_name} (раунд ${d.round})\n\n${d.content}\n`).join("\n") + `\n</details>\n\n`;
        if (m.sources && m.sources.length) mdText += "Источники:\n" + m.sources.map(s => `- [${s.n}] ${s.title} — ${s.url}`).join("\n") + "\n\n";
      }
    });
    const blob = new Blob([mdText], { type: "text/markdown" }); const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = (t.title || "chat").replace(/[^\w\dа-яё -]/gi, "_") + ".md"; a.click();
  },

  // ---- load / render
  async loadThread(scrollBottom, applyMeta) {
    if (!State.currentThreadId) return;
    try {
      const r = await api("/api/threads/" + State.currentThreadId);
      const first = !State.thread || State.thread.id !== r.thread.id;
      State.thread = r.thread; this.busy = r.busy;
      if (applyMeta || first) this.applyThreadToComposer(r.thread);
      this.renderMessages(r.thread, r.active, scrollBottom);
    } catch (e) { if (String(e.message).includes("404") || String(e.message).includes("not found")) { State.currentThreadId = null; if (State.threads[0]) Threads.select(State.threads[0].id); } }
  },
  renderMessages(t, active, forceScroll) {
    const box = $("messages"); const wasBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (document.activeElement !== $("chatTitle")) $("chatTitle").value = t.title;
    const owner = t.owner_name && t.owner_id !== Client.id ? ` · владелец: ${t.owner_name}` : "";
    $("chatMeta").textContent = `${t.messages.length} сообщ. · создана ${fmtDate(t.created_at)}${owner}`;
    $("chatEmpty").style.display = t.messages.length ? "none" : "";
    const seen = new Set();
    let lastEl = $("chatEmpty");
    t.messages.forEach(m => {
      seen.add(m.id);
      let el = this.msgEls[m.id];
      const sig = `${m.status}|${(m.content || "").length}|${(m.reasoning || "").length}|${m.error || ""}|${m.phase || ""}|${(m.discussion || []).map(d => d.status + (d.content || "").length).join(",")}|${(m.output || "").length}|${State.isAdmin}`;
      if (!el) { el = document.createElement("div"); el.className = "msg " + m.role; this.msgEls[m.id] = el; el.dataset.sig = ""; box.insertBefore(el, lastEl.nextSibling); }
      else if (el.previousSibling !== lastEl) box.insertBefore(el, lastEl.nextSibling);
      if (el.dataset.sig !== sig) {
        const openDetails = [...el.querySelectorAll("details[open]")].map(d => d.dataset.k);
        el.innerHTML = m.role === "user" ? this.userHtml(m) : m.role === "tool" ? this.toolHtml(m) : this.assistantHtml(m); el.dataset.sig = sig;
        openDetails.forEach(k => { const d = el.querySelector(`details[data-k="${k}"]`); if (d) d.open = true; });
      }
      lastEl = el;
    });
    Object.keys(this.msgEls).forEach(id => { if (!seen.has(id)) { this.msgEls[id].remove(); delete this.msgEls[id]; } });
    let banner = box.querySelector(".active-banner");
    if (active && active.length) {
      if (!banner) { banner = document.createElement("div"); banner.className = "active-banner"; }
      const a = active[0]; const stage = a.stages > 1 ? ` · этап ${a.stage || 1}/${a.stages}` : "";
      banner.innerHTML = `<div class="spinner"></div><div><b>Задача #${a.task_id}</b> ${MODE_LABEL[a.mode] || ""}${stage} · ${esc(a.status)}</div>`;
      box.appendChild(banner);
    } else if (banner) banner.remove();
    const busy = !!(active && active.length);
    $("btnSend").style.display = busy ? "none" : ""; $("btnStop").style.display = busy ? "" : "none";
    $("statusText").innerHTML = busy ? `<span style="color:var(--warn)">⏳ ${esc(active[0].status)}</span>` : "Кластер готов к приёму задач";
    if (forceScroll || (wasBottom && $("autoScroll").checked)) box.scrollTop = box.scrollHeight;
  },
  fileChip(f, removable) {
    const kb = f.size >= 1048576 ? (f.size / 1048576).toFixed(1) + " МБ" : Math.max(1, Math.round((f.size || 0) / 1024)) + " КБ";
    const icon = f.kind === "image" ? "🖼" : /\.(pdf|docx|xlsx|pptx)$/i.test(f.name) ? "📄" : "📎";
    const name = f.id && !removable ? `<a href="/api/files/get?id=${esc(f.id)}" target="_blank" rel="noopener" title="Скачать">${esc(f.name)}</a>` : esc(f.name);
    return `<span class="attach-chip ${f.id ? "up" : ""}" title="${esc(f.name)} · ${kb}${f.chars ? " · " + f.chars + " симв. текста" : ""}${f.note ? " · " + esc(f.note) : ""}">${icon} <span class="n">${name}</span><span class="muted">${kb}</span>${removable ? `<span class="x" onclick="Chat.removeFile('${esc(f._key)}')">✕</span>` : ""}</span>`;
  },
  userHtml(m) {
    const skill = (State.settings?.skills || []).find(s => s.id === m.skill);
    const files = m.files && m.files.length ? `<div class="files">${m.files.map(f => this.fileChip(f, false)).join("")}</div>` : "";
    return `${files}<div class="bubble">${esc(m.content)}</div><div class="meta"><span>${m.author ? "👤 " + esc(m.author) + " ·" : ""} ${MODE_LABEL[m.mode] || ""}${skill && skill.id !== "default" ? " · " + skill.icon + " " + esc(skill.name) : ""}${m.tools ? " · 🖥 Агент ПК" : ""}</span><span>${m.time || fmtTime(m.ts)}</span><span class="actions"><button class="btn ghost sm" title="Повторить" onclick="Chat.reuse('${m.id}')">↺</button><button class="btn ghost sm" title="Удалить" onclick="Chat.delMsg('${m.id}')">🗑</button></span></div>`;
  },
  assistantHtml(m) {
    const streaming = m.status === "streaming"; const err = m.status === "error";
    const stats = [];
    if (m.latency_ms) stats.push("⏱ " + fmtMs(m.latency_ms)); if (m.first_token_ms) stats.push("TTFT " + fmtMs(m.first_token_ms)); if (m.tok_per_s) stats.push(m.tok_per_s + " tok/s"); if (m.tokens) stats.push(m.tokens + " tok");
    if (m.status === "cancelled") stats.push("остановлено");
    const reasoning = m.reasoning ? `<details class="reasoning" data-k="r" ${State.ui.reasoning || (streaming && !m.content) ? "open" : ""}><summary>💭 Размышления (${m.reasoning.length} симв.)</summary><div>${esc(m.reasoning)}</div></details>` : "";
    let council = "";
    if (m.council) {
      const disc = m.discussion || []; const phase = { drafts: "черновики", synthesis: "итоговый ответ", done: "" }[m.phase] || "";
      council = `<details class="council" data-k="c"><summary>🗣 Обсуждение узлов (${disc.length}${phase && streaming ? " · " + phase + "…" : ""}) — участники: ${(m.participants || []).map(p => `<span title="${esc(p.name)}">${p.avatar}</span>`).join(" ")}</summary><div class="council-body">${disc.map(d => `<div class="disc ${d.kind}" style="--nc:${d.color || "var(--line-2)"}"><div class="disc-h">${d.avatar || ""} <b>${esc(d.node_name)}</b> · ${d.kind === "draft" ? "черновик" : "замечания, раунд " + d.round}${d.latency_ms ? " · " + fmtMs(d.latency_ms) : ""}${d.status === "error" ? ' · <span style="color:var(--err)">ошибка: ' + esc(d.error) + "</span>" : d.status === "streaming" ? " · ⏳" : ""}</div><div class="md">${md(d.content)}</div></div>`).join("") || '<div class="muted small">ещё нет</div>'}</div></details>`;
    }
    const sources = m.sources && m.sources.length ? `<details class="sources" data-k="s"><summary>🌐 Источники (${m.sources.length})</summary><ol>${m.sources.map(s => `<li value="${s.n}"><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a>${s.fetched ? "" : ' <span class="muted small">(только сниппет)</span>'}${s.snippet ? `<div class="muted small">${esc(s.snippet)}</div>` : ""}</li>`).join("")}</ol></details>` : "";
    const body = err ? `<div>❌ ${esc(m.error)}</div>${m.content ? '<div class="md" style="margin-top:8px;color:var(--text)">' + md(this.stripRun(m.content)) + "</div>" : ""}` : `<div class="md">${md(this.stripRun(m.content)) || (streaming ? (m.council ? '<span class="muted">' + (m.phase === "synthesis" ? "модератор пишет итог…" : "узлы обсуждают…") + "</span>" : "") : '<span class="muted">пустой ответ</span>')}</div>`;
    const title = m.council ? `<span class="name" style="color:var(--warn)">🤝 Консилиум</span><span class="role">${esc(m.node_role || "")}</span>` : `<span class="name" style="color:${m.node_color || "inherit"}">${esc(m.node_name || "Узел")}</span><span class="role">${esc(m.node_tag || "")} · ${esc(m.model || "")}</span>`;
    return `<div class="resp ${err ? "error" : ""} ${m.council ? "council-msg" : ""}" style="--nc:${m.council ? "var(--warn)" : (m.node_color || "var(--line-2)")}">
      <div class="resp-h"><span>${m.council ? "" : (m.node_avatar || "💻")}</span>${m.step_label ? `<span class="step">${esc(m.step_label)}</span>` : ""}${title}<span class="stats">${stats.map(esc).join("<span class='muted'>|</span>")}</span>
        <span class="actions"><button class="btn ghost sm" title="Копировать ответ" onclick="Chat.copyMsg('${m.id}')">📋</button><button class="btn ghost sm" title="Удалить" onclick="Chat.delMsg('${m.id}')">🗑</button></span></div>
      <div class="resp-b ${streaming ? "streaming" : ""}">${council}${reasoning}${body}${sources}</div></div>`;
  },
  // блоки ```run:…``` в ответе показывает карточка команды ниже — в тексте оставляем короткую пометку
  stripRun(text) {
    return (text || "").replace(/```run:(powershell|pwsh|cmd|bash|sh)(?:@([\w.\-]+))?[ \t]*\r?\n[\s\S]*?```/gi, (_, sh, node) => `\n> 🖥 команда ${sh} → узел **${node || "?"}** (см. карточку ниже)\n`);
  },
  toolHtml(m) {
    const pending = m.status === "pending", running = m.status === "running", done = m.status === "done";
    const st = pending ? (State.isAdmin ? "ждёт вашего подтверждения" : "ждёт подтверждения администратора") : running ? "выполняется…" : m.denied ? "отклонена" : m.timed_out ? "превышено время" : `код выхода ${m.exit_code ?? "?"}${m.duration_ms ? " · " + fmtMs(m.duration_ms) : ""}`;
    const out = done ? `<details data-k="o" ${(m.output || "").length < 1500 || !m.ok ? "open" : ""}><summary>Вывод (${(m.output || "").length} симв.)</summary><pre class="tool-out">${esc(m.output || "(пусто)")}</pre></details>` : "";
    const actions = pending && State.isAdmin ? `<div class="tool-actions"><button class="btn sm primary" onclick="Chat.approveTool('${m.id}', true)">✅ Выполнить</button><button class="btn sm danger" onclick="Chat.approveTool('${m.id}', false)">⛔ Отклонить</button><span class="muted small">Команда выполнится на узле от имени агента. Проверьте её перед подтверждением.</span></div>` : "";
    return `<div class="tool-card ${m.status} ${m.ok ? "ok" : done ? "fail" : ""}"><div class="tool-h"><span>🖥</span><b>${esc(m.node_name || m.node_key || "узел")}</b><span class="muted">${esc(m.shell || "")}</span><span class="st">${pending || running ? "⏳ " : ""}${esc(st)}</span></div><pre class="tool-cmd">${esc(m.command || "")}</pre>${out}${actions}</div>`;
  },
  async approveTool(id, ok) {
    try { await api("/api/tools/approve", { call_id: id, approve: ok }); UI.toast(ok ? "Команда подтверждена" : "Команда отклонена", ok ? "ok" : "warn", 1500); this.loadThread(); }
    catch (e) { UI.toast(esc(e.message), "err"); }
  },
  onTools(on) { localStorage.setItem("bf.tools", on ? "1" : ""); if (on) UI.toast("Агент ПК: модель сможет предлагать команды для узлов с разрешённым выполнением; каждая ждёт вашего подтверждения", "warn", 5000); },

  // ---- приложенные файлы
  attachments: [],
  pickFiles() { $("fileInput").click(); },
  addFiles(list) {
    const maxMb = State.settings?.files?.max_mb || 25;
    [...(list || [])].forEach(f => {
      if (f.size > maxMb * 1048576) { UI.toast(`${esc(f.name)}: больше ${maxMb} МБ`, "err"); return; }
      if (this.attachments.length >= 12) { UI.toast("Не больше 12 файлов за раз", "warn"); return; }
      const name = f.name || ("вставка-" + Date.now() + (f.type === "image/png" ? ".png" : ".txt"));
      this.attachments.push({ _key: Math.random().toString(36).slice(2), file: f, name, size: f.size, kind: /^image\//.test(f.type) ? "image" : "text" });
    });
    this.renderAttach();
  },
  removeFile(key) { this.attachments = this.attachments.filter(a => a._key !== key); this.renderAttach(); },
  renderAttach() {
    const box = $("attachChips"); if (!box) return;
    box.style.display = this.attachments.length ? "" : "none";
    box.innerHTML = this.attachments.map(a => this.fileChip(a, true)).join("");
  },
  async uploadAttachments(threadId) {
    const ids = [];
    for (const a of this.attachments) {
      if (a.id) { ids.push(a.id); continue; }
      $("composerHint").textContent = `Загрузка ${a.name}…`;
      const data = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(a.file); });
      const r = await api("/api/files/upload", { name: a.name, data, thread_id: threadId });
      a.id = r.file.id; a.chars = r.file.chars; a.note = r.file.note; a.kind = r.file.kind;
      if (r.file.kind !== "image" && !r.file.chars) UI.toast(`${esc(a.name)}: ${esc(r.file.note || "текст не извлечён — модель увидит только имя файла")}`, "warn", 5000);
      ids.push(a.id);
    }
    $("composerHint").textContent = "Готово к работе";
    return ids;
  },
  initAttach() {
    const comp = document.querySelector(".composer"); const ta = $("taskInput"); if (!comp || !ta) return;
    ["dragenter", "dragover"].forEach(ev => comp.addEventListener(ev, e => { if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); comp.classList.add("dragover"); } }));
    ["dragleave", "drop"].forEach(ev => comp.addEventListener(ev, e => { comp.classList.remove("dragover"); }));
    comp.addEventListener("drop", e => { if (e.dataTransfer.files.length) { e.preventDefault(); this.addFiles(e.dataTransfer.files); } });
    ta.addEventListener("paste", e => {
      const items = [...(e.clipboardData?.items || [])].filter(i => i.kind === "file");
      if (items.length) { e.preventDefault(); this.addFiles(items.map(i => i.getAsFile()).filter(Boolean)); }
    });
    if ($("toolsOn")) $("toolsOn").checked = localStorage.getItem("bf.tools") === "1";
  },
  reuse(id) { const m = State.thread?.messages.find(x => x.id === id); if (m) { $("taskInput").value = m.content; $("taskInput").dispatchEvent(new Event("input")); $("taskInput").focus(); } },
  copyMsg(id) { const m = State.thread?.messages.find(x => x.id === id); if (m) { navigator.clipboard?.writeText(m.content || ""); UI.toast("Ответ скопирован", "ok", 1500); } },
  async delMsg(id) { await api("/api/messages/delete", { thread_id: State.currentThreadId, message_id: id }); this.loadThread(); },
};

// ---------------------------------------------------------------------------
// Nodes strip + model switching
// ---------------------------------------------------------------------------
const Nodes = {
  chipSig: {},
  renderStrip() {
    const strip = $("nodesStrip"); const admin = State.isAdmin;
    const keys = new Set(Object.keys(State.nodes));
    strip.querySelectorAll("[data-node]").forEach(el => { if (!keys.has(el.dataset.node)) el.remove(); });
    State.nodeList().forEach(n => {
      const models = State.models[n.key]?.models || [];
      const sig = `${n.enabled}|${n.status}|${n.latency_ms}|${n.model}|${models.map(m => m.id + m.loaded).join(",")}|${n.name}|${n.color}|${admin}`;
      let chip = strip.querySelector(`[data-node="${n.key}"]`);
      if (chip && this.chipSig[n.key] === sig) return;
      if (!chip) { chip = document.createElement("div"); chip.dataset.node = n.key; strip.appendChild(chip); }
      this.chipSig[n.key] = sig;
      chip.className = "node-chip" + (n.enabled ? "" : " off"); chip.style.setProperty("--nc", n.color);
      const opts = models.filter(m => m.type !== "embeddings").map(m => `<option value="${esc(m.id)}" ${m.id === n.model ? "selected" : ""}>${m.loaded === true ? "● " : m.loaded === false ? "○ " : ""}${esc(m.id)}</option>`);
      if (!models.find(m => m.id === n.model)) opts.unshift(`<option value="${esc(n.model)}" selected>${esc(n.model)}</option>`);
      chip.innerHTML = `<div class="nc-top"><span>${n.avatar}</span><span class="nc-name" title="${esc(n.role)} · ${esc(n.hardware)}">${esc(n.name)}</span>
          <span class="dot ${n.status === "online" ? "online" : n.status === "offline" ? "offline" : "unknown"}" title="${esc(n.last_error || n.status)}"></span><span class="nc-lat">${n.status === "online" ? n.latency_ms + " мс" : n.status === "offline" ? "offline" : "…"}</span>
          ${admin ? `<label class="switch" title="Включить / отключить узел"><input type="checkbox" ${n.enabled ? "checked" : ""} onchange="Nodes.toggle('${n.key}',this.checked)"><span class="slider"></span></label>` : ""}</div>
        <div class="nc-row">${admin ? `<select title="Активная модель узла (● загружена, ○ доступна)" onchange="Nodes.setModel('${n.key}',this.value)" ${n.enabled ? "" : "disabled"}>${opts.join("")}</select>
          <button class="btn sm icon" title="Обновить список моделей" onclick="Nodes.loadModels('${n.key}',true)">↻</button>` : `<span class="nc-model" title="${esc(n.model)}">${esc(n.model)}</span>`}
          <button class="btn sm icon" title="Пинг" onclick="Nodes.ping('${n.key}')">📡</button></div>`;
    });
  },
  async loadModels(key, force) {
    try { const r = await api(`/api/node/models?node_key=${key}${force ? "&force=1" : ""}`); State.models[key] = r; this.renderStrip(); if ($("targetNode").value === key) Chat.fillTargetModels(); if (r.error && force) UI.toast(`${esc(State.nodes[key]?.name)}: ${esc(r.error)}`, "err"); }
    catch (e) { State.models[key] = { models: [], error: e.message }; }
  },
  async loadAllModels(force) { await Promise.all(Object.keys(State.nodes).map(k => this.loadModels(k, force))); },
  async setModel(key, model) {
    const n = State.nodes[key]; if (!n || n.model === model) return;
    const info = (State.models[key]?.models || []).find(m => m.id === model);
    const needWarm = info && info.loaded === false;
    UI.toast(`${esc(n.name)}: модель → <b>${esc(model)}</b>${needWarm ? " (загружаю…)" : ""}`, "");
    try {
      const r = await api("/api/node/set_model", { node_key: key, model, warmup: !!needWarm });
      n.model = model;
      if (r.warmup) { if (r.warmup.ok) UI.toast(`${esc(n.name)}: модель загружена за ${fmtMs(r.warmup.latency_ms)}`, "ok"); else UI.toast(`${esc(n.name)}: не удалось загрузить: ${esc(r.warmup.error)}`, "err", 6000); }
      this.loadModels(key, true);
    } catch (e) { UI.toast("Ошибка смены модели: " + esc(e.message), "err"); }
  },
  async toggle(key, enabled) { try { await api("/api/node/toggle", { node_key: key, enabled }); State.nodes[key].enabled = enabled; this.renderStrip(); Chat.fillTargets(); } catch (e) { UI.toast(esc(e.message), "err"); } },
  async ping(key) { const r = await api("/api/node/ping", { node_key: key }); const res = r.result || {}; UI.toast(`${esc(State.nodes[key].name)}: ${res.status === "online" ? "online, " + res.latency_ms + " мс" : "offline — " + esc(res.error || "")}`, res.status === "online" ? "ok" : "err"); Poll.tick(); },
  renderClusterPill() {
    const ns = State.nodeList(); const on = ns.filter(n => n.status === "online").length;
    $("clusterDots").innerHTML = ns.map(n => `<span class="dot ${n.status === "online" ? "online" : n.status === "offline" ? "offline" : "unknown"}" style="${n.enabled ? "" : "opacity:.3"}" title="${esc(n.name)}: ${n.status}"></span>`).join("");
    $("clusterText").textContent = `${on}/${ns.length} online`;
  },
};

// ---------------------------------------------------------------------------
// Polling loop
// ---------------------------------------------------------------------------
const Poll = {
  timer: null, n: 0, firstLoad: true,
  async tick() {
    clearTimeout(this.timer);
    try {
      const s = await api("/api/state");
      const nodesChanged = JSON.stringify(Object.keys(s.nodes)) !== JSON.stringify(Object.keys(State.nodes));
      const adminChanged = !!State.you?.is_admin !== !!s.you?.is_admin || this.firstLoad;
      State.nodes = s.nodes; State.settings = s.settings; State.threads = s.threads; State.active = s.active; State.tunnel = s.tunnel_host; State.port = s.port; State.you = s.you || {};
      $("tunnelText").textContent = `${s.tunnel_host}:${s.port}`;
      if (adminChanged) { UI.applyAdmin(); Chat.refreshModeUi(); }
      Nodes.renderClusterPill(); Nodes.renderStrip(); Threads.render();
      if (this.firstLoad || nodesChanged) { Chat.fillTargets(); Nodes.loadAllModels(); }
      else if (this.n % 40 === 0) Nodes.loadAllModels();
      if (this.firstLoad) {
        this.firstLoad = false;
        const saved = localStorage.getItem("bf.thread");
        State.currentThreadId = s.threads.find(t => t.id === saved)?.id || s.threads.find(t => !t.owner_id || t.owner_id === Client.id)?.id || s.threads[0]?.id || null;
        if (State.currentThreadId) { Threads.render(); Chat.loadThread(true, true); } else Threads.create();
      } else if (UI.tab === "chat" && State.currentThreadId) {
        const cur = State.threads.find(t => t.id === State.currentThreadId);
        if (!cur) { if (State.threads[0]) Threads.select(State.threads[0].id); else Threads.create(); }
        else if (cur.busy || Chat.busy || this.n % 3 === 0) Chat.loadThread();
      }
      if (UI.tab === "telemetry" && this.n % 2 === 0) Telemetry.refresh();
      if (UI.tab === "users" && this.n % 2 === 0) Users.refresh();
      if (UI.tab === "models" && this.n % 3 === 0 && typeof Models !== "undefined") Models.pollDownloads();
    } catch (e) {
      $("statusText").innerHTML = '<span style="color:var(--err)">⚠ нет связи с хабом</span>';
    }
    this.n++;
    this.timer = setTimeout(() => this.tick(), State.ui.poll || 1500);
  },
};

const Boot = {
  async start() {
    Client.init(); UI.init(); Chat.init(); Users.init(); Chat.initAttach();
    await Native.init();          // контекст оболочки приложения, если мы внутри неё
    Auth.load();
    const st = await Auth.refreshStatus();
    if (st && (st.needs_setup || (st.enabled && !st.authenticated))) { Auth.show(); if (st.needs_setup && !st.can_setup) return; }
    Client.deviceInfo().then(d => api("/api/client/hello", { device: d }).catch(() => {}));
    Poll.tick();
    window.addEventListener("resize", () => { if (UI.tab === "telemetry") Telemetry.render(); });
  },
};
