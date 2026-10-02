/* BlackFox AI Workstation — Models tab: calculator, placement matrix, HF search, downloads */
"use strict";

const Calc = {
  QUANT: [["iq4_xs", 4.25], ["q2_k", 3.35], ["q3_k_s", 3.5], ["q3_k_m", 3.9], ["q3_k_l", 4.3], ["q4_k_s", 4.58], ["q4_k_m", 4.85], ["q4_0", 4.55], ["q4_1", 5.0], ["q5_k_s", 5.5], ["q5_k_m", 5.7], ["q5_0", 5.5], ["q6_k", 6.6], ["q8_0", 8.5], ["mxfp4", 4.25], ["bf16", 16], ["f16", 16], ["f32", 32], ["8bit", 8.5], ["6bit", 6.5], ["4bit", 4.5], ["3bit", 3.5], ["2bit", 2.5]],
  bpw(q) { q = (q || "").toLowerCase().replace(/-/g, "_"); for (const [k, v] of this.QUANT) if (q.includes(k)) return v; return 4.85; },
  parseParams(id) { let m = /(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*[bB](?![a-zA-Z0-9])/.exec(id || ""); if (m) { const v = +m[1]; return v >= 0.1 && v <= 2000 ? v : null; } m = /(?<![\d.])(\d{3,4})\s*[mM](?![a-zA-Z0-9])/.exec(id || ""); return m ? +(m[1] / 1000).toFixed(2) : null; },
  parseQuant(s) { const m = /(iq\d_[a-z]+|q\d_k_[sml]|q\d_k|q\d_\d|mxfp4|bf16|f16|f32|\d+bit)/i.exec(s || ""); return m ? m[1].toUpperCase() : ""; },
  estimate(params_b, bpw, ctx, vram_gb, ram_gb, bandwidth, unified, size_bytes) {
    if (!params_b) return null;
    const weights = size_bytes ? size_bytes / 1024 ** 3 : params_b * bpw / 8;
    const kv = 0.012 * params_b * (ctx / 1000);
    const total = weights + kv + 0.6;
    const budget = unified ? ram_gb * 0.75 : vram_gb;
    const fit = total <= budget * 0.9 ? "ok" : total <= budget ? "tight" : total <= budget + ram_gb * 0.8 ? "offload" : "no";
    let tok = 0;
    if (fit === "ok" || fit === "tight") tok = bandwidth / Math.max(0.5, weights + kv * 0.3) * 0.7;
    else if (fit === "offload") { const f = Math.max(0.05, Math.min(0.95, budget * 0.9 / total)); const eff = 1 / (f / bandwidth + (1 - f) / 45); tok = eff / Math.max(0.5, weights) * 0.7; }
    const quality = params_b * (bpw < 3.6 ? 0.65 : bpw < 4.4 ? 0.8 : bpw < 5.4 ? 0.9 : bpw < 7 ? 0.96 : 1.0);
    return { weights_gb: +weights.toFixed(2), kv_gb: +kv.toFixed(2), total_gb: +total.toFixed(2), budget_gb: +budget.toFixed(1), fit, est_tok_s: +tok.toFixed(1), quality: +quality.toFixed(1) };
  },
  stars(q) { const n = q >= 60 ? 5 : q >= 25 ? 4 : q >= 12 ? 3 : q >= 6 ? 2 : 1; return "★".repeat(n) + "☆".repeat(5 - n); },
  hwOf(hw) {
    const gpus = hw.gpus || []; const ram = +((hw.ram || {}).total_gb || 0);
    const unified = gpus.some(g => g.unified) || (/darwin/i.test(hw.platform || "") && !gpus.some(g => /nvidia/i.test(g.name || "")));
    const gpu = gpus.length ? gpus.reduce((a, b) => (b.mem_total_mb || 0) > (a.mem_total_mb || 0) ? b : a) : null;
    const vram = unified ? +(ram * 0.75).toFixed(1) : gpu ? +(gpu.mem_total_mb / 1024).toFixed(1) : 0;
    return { gpu: gpu ? gpu.name : "", vram, ram, unified, bandwidth: hw.bandwidth_gbs || 300 };
  },
};

const Models = {
  node: null, ctx: 8192, catalog: {}, hf: { q: "", results: [], repo: null, files: [] }, downloads: {}, loading: false,
  open() {
    if (!this.node || !State.nodes[this.node]) this.node = Object.keys(State.nodes)[0];
    this.render();
    this.loadCatalog(this.node);
    if (State.isAdmin) this.pollDownloads();
  },
  async loadCatalog(key, force) {
    this.loading = true; this.renderTable();
    try { this.catalog[key] = await api(`/api/models/catalog?node_key=${key}${force ? "&force=1" : ""}`); }
    catch (e) { this.catalog[key] = { error: e.message, api_models: [] }; }
    this.loading = false; this.renderTable();
  },
  async loadAllCatalogs() { await Promise.all(Object.keys(State.nodes).map(k => this.catalog[k] ? null : api(`/api/models/catalog?node_key=${k}`).then(c => { this.catalog[k] = c; }).catch(() => {}))); this.renderMatrix(); },
  async pollDownloads() { if (!State.isAdmin || UI.tab !== "models") return; try { const r = await api("/api/models/downloads"); this.downloads = r.downloads || {}; this.renderDownloads(); } catch (e) {} },
  select(key) { this.node = key; this.render(); this.loadCatalog(key); },
  render() {
    const admin = State.isAdmin;
    $("modelsWrap").innerHTML = `
      <div class="row wrap"><div class="seg" id="modelsNodeSeg">${State.nodeList().map(n => `<button class="${n.key === this.node ? "active" : ""}" style="--nc:${n.color}" onclick="Models.select('${n.key}')">${n.avatar} ${esc(n.name)}</button>`).join("")}</div>
        <span class="grow"></span><label class="row small muted">контекст <input type="range" id="ctxRange" min="2048" max="131072" step="2048" value="${this.ctx}" oninput="Models.ctx=+this.value;$('ctxVal').textContent=Models.ctx;Models.renderTable();Models.renderMatrix()"> <b id="ctxVal" class="mono">${this.ctx}</b> ток.</label>
        <button class="btn sm" onclick="Models.loadCatalog(Models.node,true)">↻ Обновить</button></div>
      <div id="modelsHw" class="card" style="margin-top:10px"></div>
      <div class="card" style="margin-top:10px"><div class="card-h">🧮 Модели на узле и расчёт <span class="muted small">оценка памяти и скорости для выбранного контекста; ● загружена в память</span></div><div class="card-b" style="padding:0;overflow-x:auto" id="modelsTable"></div></div>
      <div class="card" style="margin-top:10px"><div class="card-h">🗺 Что куда ставить <span class="muted small">все известные модели × железо всех узлов</span><span class="grow"></span><button class="btn sm" onclick="Models.loadAllCatalogs()">↻</button></div><div class="card-b" style="padding:0;overflow-x:auto" id="modelsMatrix"><div class="muted small" style="padding:12px">нажмите ↻ для расчёта по всем узлам</div></div></div>
      ${admin ? `<div class="card" style="margin-top:10px"><div class="card-h">⬇ Скачать модель на узел <span class="muted small">поиск на HuggingFace, загрузка через lms (LM Studio) или прямой ссылкой в каталог моделей</span></div><div class="card-b" id="modelsHf"></div></div>
      <div class="card" style="margin-top:10px"><div class="card-h">📥 Загрузки</div><div class="card-b" id="modelsDownloads"><div class="muted small">нет активных загрузок</div></div></div>` : `<div class="tel-hint" style="margin-top:10px">Загрузка моделей и управление памятью узлов доступны администратору (интерфейс на хост-машине).</div>`}`;
    this.renderTable(); if (admin) { this.renderHf(); this.renderDownloads(); }
  },
  hwText(c) { const h = Calc.hwOf(c.hardware || {}); return `${h.gpu || (h.unified ? "Apple GPU (unified)" : "GPU не найден")} · ${h.unified ? "память под модели" : "VRAM"} ${h.vram} ГБ · ОЗУ ${h.ram} ГБ · шина ~${h.bandwidth} ГБ/с`; },
  renderTable() {
    const c = this.catalog[this.node]; const n = State.nodes[this.node];
    if (!c) { $("modelsTable").innerHTML = '<div class="active-banner" style="margin:10px"><div class="spinner"></div>Загрузка каталога…</div>'; return; }
    const h = Calc.hwOf(c.hardware || {}); h.bandwidth = c.bandwidth_gbs || h.bandwidth;
    const online = (c.hardware || {}).online;
    $("modelsHw").innerHTML = `<div class="card-b row wrap"><b>${n.avatar} ${esc(n.name)}</b><span class="small">${esc(this.hwText(c))}</span><span class="badge">${esc(c.kind || "?")}</span>${!online ? '<span class="badge warn">телеметрия недоступна — железо неизвестно, расчёт по умолчанию</span>' : ""}${c.disks ? `<span class="small muted">диск: свободно ${c.disks[0]?.free_gb} ГБ</span>` : ""}<span class="grow"></span><span class="small mono">активная: ${esc(n.model)}</span></div>`;
    const lmsByKey = {}; ((c.lms && c.lms.models) || []).forEach(m => { lmsByKey[m.modelKey] = m; });
    const loaded = new Set(((c.lms && c.lms.loaded) || []).map(m => m.modelKey || m.identifier));
    const rows = (c.api_models || []).filter(m => m.type !== "embeddings").map(m => {
      const lm = lmsByKey[m.id] || {};
      const params = m.params_b || Calc.parseParams(lm.paramsString || "") || Calc.parseParams(m.id);
      const quant = m.quant || lm.quantization || Calc.parseQuant(m.id);
      const size = m.size_bytes || lm.sizeBytes || null;
      const est = Calc.estimate(params, Calc.bpw(quant), this.ctx, h.vram, h.ram, h.bandwidth, h.unified, size);
      const isLoaded = m.loaded === true || loaded.has(m.id);
      const fit = est ? Recommend.fitLabel[est.fit] : ["?", ""];
      const act = State.isAdmin ? `<div class="row" style="gap:3px">${m.id !== n.model ? `<button class="btn sm" title="Сделать активной моделью узла" onclick="Nodes.setModel('${this.node}','${esc(m.id)}').then(()=>Models.loadCatalog(Models.node,true))">✔ активная</button>` : '<span class="badge ok">активная</span>'}
        ${c.kind === "lmstudio" ? (isLoaded ? `<button class="btn sm" onclick="Models.exec('lms_unload',{identifier:'${esc(m.id)}'})">⏏ выгрузить</button>` : `<button class="btn sm" onclick="Models.loadModel('${esc(m.id)}')">▶ в память</button>`) + `<button class="btn sm" title="Точная оценка ресурсов через lms" onclick="Models.exactEstimate('${esc(m.id)}')">📐</button>` : ""}
        ${lm.path ? `<button class="btn sm danger" title="Удалить файл модели с диска" onclick="Models.deleteModel('${esc(lm.path)}','${esc(m.id)}')">🗑</button>` : ""}</div>` : "";
      return `<tr class="${isLoaded ? "loaded" : ""}"><td class="mono small">${isLoaded ? "● " : ""}${esc(m.id)}${lm.displayName ? `<div class="muted" style="font-size:10px">${esc(lm.displayName)}${lm.architecture ? " · " + esc(lm.architecture) : ""}${lm.vision ? " · vision" : ""}</div>` : m.arch ? `<div class="muted" style="font-size:10px">${esc(m.arch)}</div>` : ""}</td>
        <td class="mono small">${params ? params + "B" : "?"}<br><span class="muted">${esc(quant || "")}</span></td><td class="mono small">${size ? fmtGB(size) : est ? "~" + est.weights_gb + " ГБ" : "—"}</td>
        <td class="mono small">${est ? `${est.total_gb} ГБ<br><span class="muted">KV ${est.kv_gb} ГБ</span>` : "—"}</td><td>${est ? `<span class="badge ${fit[1]}">${fit[0]}</span>` : ""}</td>
        <td class="mono small">${est && est.est_tok_s ? "~" + est.est_tok_s + " tok/s" : "—"}</td><td class="small" title="Условная сила модели: параметры × качество квантизации">${est ? Calc.stars(est.quality) + " " + est.quality : ""}</td>
        <td class="mono small">${m.ctx || lm.maxContextLength || "—"}</td><td>${act}</td></tr>`;
    }).join("");
    $("modelsTable").innerHTML = `${c.api_error ? `<div class="tel-hint" style="margin:10px">Узел не ответил: ${esc(c.api_error)}</div>` : ""}<table class="tbl"><thead><tr><th>Модель</th><th>Размер</th><th>Веса</th><th>Память@${this.ctx}</th><th>Вердикт</th><th>Скорость</th><th>Сила</th><th>Макс. контекст</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="9" class="muted small">моделей нет</td></tr>'}</tbody></table>
      <div class="small muted" style="padding:8px 12px">Оценка: веса = параметры × бит/параметр (или точный размер файла); KV-кэш ≈ 0.012 ГБ × B × (контекст/1k); скорость ≈ пропускная способность памяти / объём весов × 0.7. Для точной цифры на LM Studio-узле — 📐.</div>`;
  },
  renderMatrix() {
    const keys = Object.keys(State.nodes).filter(k => this.catalog[k]);
    if (!keys.length) return;
    const models = {};
    keys.forEach(k => { (this.catalog[k].api_models || []).forEach(m => { if (m.type === "embeddings") return; const lm = ((this.catalog[k].lms || {}).models || []).find(x => x.modelKey === m.id) || {}; const id = m.id; if (!models[id]) models[id] = { id, params: m.params_b || Calc.parseParams(lm.paramsString || "") || Calc.parseParams(id), quant: m.quant || lm.quantization || Calc.parseQuant(id), size: m.size_bytes || lm.sizeBytes || null, on: [] }; models[id].on.push(k); }); });
    const hws = {}; keys.forEach(k => { const h = Calc.hwOf(this.catalog[k].hardware || {}); h.bandwidth = this.catalog[k].bandwidth_gbs || h.bandwidth; hws[k] = h; });
    const rows = Object.values(models).sort((a, b) => (b.params || 0) - (a.params || 0)).map(m => {
      let best = null;
      const cells = keys.map(k => { const h = hws[k]; const e = Calc.estimate(m.params, Calc.bpw(m.quant), this.ctx, h.vram, h.ram, h.bandwidth, h.unified, m.size); if (!e) return "<td>?</td>"; if (e.fit !== "no" && (!best || ({ ok: 0, tight: 1, offload: 2 })[e.fit] < ({ ok: 0, tight: 1, offload: 2 })[best.fit] || (e.fit === best.fit && e.est_tok_s > best.tok))) best = { k, fit: e.fit, tok: e.est_tok_s }; const f = Recommend.fitLabel[e.fit]; return `<td class="mono small ${m.on.includes(k) ? "has" : ""}"><span class="badge ${f[1]}">${f[0].split(" ")[0]}</span> ${e.est_tok_s ? e.est_tok_s + " t/s" : ""}${m.on.includes(k) ? " <span title='есть на узле'>💾</span>" : ""}</td>`; });
      return `<tr><td class="mono small">${esc(m.id)}<div class="muted" style="font-size:10px">${m.params ? m.params + "B" : "?"} ${esc(m.quant || "")}</div></td>${cells.join("")}<td class="small">${best ? `${State.nodes[best.k].avatar} ${esc(State.nodes[best.k].name)}` : "—"}</td></tr>`;
    }).join("");
    $("modelsMatrix").innerHTML = `<table class="tbl"><thead><tr><th>Модель</th>${keys.map(k => `<th>${State.nodes[k].avatar} ${esc(State.nodes[k].name)}<div class="muted" style="font-size:10px;font-weight:400">${hws[k].vram} ГБ · ${hws[k].bandwidth} ГБ/с</div></th>`).join("")}<th>Лучший узел</th></tr></thead><tbody>${rows}</tbody></table>`;
  },
  // ---- admin actions
  async exec(action, args) {
    UI.toast("Выполняю: " + action + "…", "", 2000);
    try { const r = await api("/api/admin/exec", { node_key: this.node, action, args }); const res = r.result || {}; UI.toast(res.ok ? `✅ ${action}: ${esc(typeof res.output === "string" ? res.output.slice(0, 160) : "готово")}` : `❌ ${action}: ${esc(res.error || "")}`, res.ok ? "ok" : "err", 6000); this.loadCatalog(this.node, true); return res; }
    catch (e) { UI.toast("Ошибка: " + esc(e.message), "err"); }
  },
  loadModel(id) {
    UI.modal("Загрузить модель в память", `<div class="grid-2"><div class="field"><label>Контекст (токенов)</label><input type="number" id="lm_ctx" value="${this.ctx}" step="1024"></div><div class="field"><label>GPU offload</label><select id="lm_gpu"><option value="">авто</option><option value="max">max (всё на GPU)</option><option value="0.5">0.5</option><option value="off">off (CPU)</option></select></div></div><div class="small muted" style="margin-top:6px">Модель: <span class="mono">${esc(id)}</span>. Загрузка идёт через lms на узле; может занять до минуты.</div>`,
      async () => { await this.exec("lms_load", { model: id, ctx: +$("lm_ctx").value || undefined, gpu: $("lm_gpu").value || undefined }); }, "Загрузить");
  },
  async exactEstimate(id) {
    UI.info("Точная оценка (lms --estimate-only)", '<div class="active-banner"><div class="spinner"></div>Запрос к узлу…</div>');
    try { const r = await api("/api/admin/exec", { node_key: this.node, action: "lms_estimate", args: { model: id, ctx: this.ctx } }); const res = r.result || {}; $("modalBody").innerHTML = `<pre class="mono small" style="white-space:pre-wrap">${esc(res.output || res.error || "нет вывода")}</pre>`; } catch (e) { $("modalBody").innerHTML = esc(e.message); }
  },
  deleteModel(path, id) { UI.confirm("Удалить файл модели", `Удалить с диска узла <span class="mono">${esc(path)}</span>? Это необратимо.`, () => this.exec("delete_model", { path }), "Удалить"); },
  // ---- HuggingFace search + download
  renderHf() {
    const n = State.nodes[this.node]; const c = this.catalog[this.node] || {};
    const fmt = /darwin|mac/i.test((c.hardware || {}).platform || "") ? "mlx" : "gguf";
    $("modelsHf").innerHTML = `<div class="row wrap"><input type="text" id="hfQuery" class="grow" placeholder="Поиск на HuggingFace: например qwen3.5 9b, gemma 3, deepseek coder…" value="${esc(this.hf.q)}" onkeydown="if(event.key==='Enter')Models.hfSearch()">
        <select id="hfFormat" class="sm"><option value="gguf" ${fmt === "gguf" ? "selected" : ""}>GGUF (LM Studio / llama.cpp)</option><option value="mlx" ${fmt === "mlx" ? "selected" : ""}>MLX (Apple Silicon)</option></select>
        <button class="btn primary" onclick="Models.hfSearch()">🔎 Найти</button></div>
      <div class="row wrap" style="margin-top:6px"><span class="small muted">Или напрямую:</span><input type="text" id="dlSource" class="mono grow sm" placeholder="lms:qwen/qwen3.5-9b@q4_k_m  ·  или  https://huggingface.co/<repo>/resolve/main/<file>.gguf"><button class="btn sm" onclick="Models.download($('dlSource').value)">⬇ Скачать на ${esc(n.name)}</button></div>
      <div id="hfResults" style="margin-top:8px"></div><div id="hfFiles" style="margin-top:8px"></div>`;
    if (this.hf.results.length) this.renderHfResults();
  },
  async hfSearch() {
    this.hf.q = $("hfQuery").value.trim(); if (!this.hf.q) return;
    $("hfResults").innerHTML = '<div class="active-banner"><div class="spinner"></div>Поиск на HuggingFace…</div>';
    try { const r = await api(`/api/models/hf_search?q=${encodeURIComponent(this.hf.q)}&format=${$("hfFormat").value}&limit=15`); this.hf.results = r.models || []; this.hf.error = r.error; this.renderHfResults(); } catch (e) { $("hfResults").innerHTML = `<div class="tel-hint">${esc(e.message)}</div>`; }
  },
  renderHfResults() {
    const c = this.catalog[this.node] || {}; const h = Calc.hwOf(c.hardware || {}); h.bandwidth = c.bandwidth_gbs || h.bandwidth;
    $("hfResults").innerHTML = this.hf.results.length ? `<table class="tbl"><thead><tr><th>Репозиторий</th><th>Параметры</th><th>Загрузок</th><th>❤</th><th>Оценка для узла (Q4)</th><th></th></tr></thead><tbody>${this.hf.results.map(m => { const e = Calc.estimate(m.params_b, 4.85, this.ctx, h.vram, h.ram, h.bandwidth, h.unified); const f = e ? Recommend.fitLabel[e.fit] : null; return `<tr><td class="mono small"><a href="https://huggingface.co/${esc(m.id)}" target="_blank" rel="noopener">${esc(m.id)}</a></td><td class="mono small">${m.params_b ? m.params_b + "B" : "?"}</td><td class="mono small">${(m.downloads || 0).toLocaleString("ru-RU")}</td><td class="mono small">${m.likes || 0}</td><td>${f ? `<span class="badge ${f[1]}">${f[0]}</span> ${e.est_tok_s ? "~" + e.est_tok_s + " t/s" : ""}` : "—"}</td><td><button class="btn sm" onclick="Models.hfFiles('${esc(m.id)}')">Файлы ▸</button></td></tr>`; }).join("")}</tbody></table>` : `<div class="muted small">${this.hf.error ? esc(this.hf.error) : "ничего не найдено"}</div>`;
  },
  async hfFiles(repo) {
    $("hfFiles").innerHTML = '<div class="active-banner"><div class="spinner"></div>Список файлов…</div>';
    try {
      const r = await api(`/api/models/hf_files?repo=${encodeURIComponent(repo)}`); this.hf.repo = repo; this.hf.files = r.files || [];
      const c = this.catalog[this.node] || {}; const h = Calc.hwOf(c.hardware || {}); h.bandwidth = c.bandwidth_gbs || h.bandwidth; const params = r.params_b;
      const isLms = c.kind === "lmstudio";
      $("hfFiles").innerHTML = `<div class="row wrap"><b class="mono">${esc(repo)}</b><span class="muted small">${params ? params + "B" : ""} · ${this.hf.files.length} файлов</span><span class="grow"></span>${isLms ? `<button class="btn sm primary" onclick="Models.download('lms:${esc(repo)}')">⬇ через lms (lms выберет квант под железо)</button>` : ""}</div>
        <table class="tbl" style="margin-top:6px"><thead><tr><th>Файл</th><th>Квант</th><th>Размер</th><th>Оценка для узла</th><th></th></tr></thead><tbody>${this.hf.files.filter(f => !/config\.json$/.test(f.file)).map(f => { const e = Calc.estimate(params, Calc.bpw(f.quant), this.ctx, h.vram, h.ram, h.bandwidth, h.unified, f.size_bytes); const fl = e ? Recommend.fitLabel[e.fit] : null; const q = (f.quant || "").toLowerCase(); return `<tr><td class="mono small">${esc(f.file)}</td><td class="mono small">${esc(f.quant || "")}</td><td class="mono small">${f.size_bytes ? fmtGB(f.size_bytes) : "—"}</td><td>${fl ? `<span class="badge ${fl[1]}">${fl[0]}</span> ${e.est_tok_s ? "~" + e.est_tok_s + " t/s" : ""}` : "—"}</td><td class="row" style="gap:3px">${isLms && q ? `<button class="btn sm" title="lms get ${repo}@${q}" onclick="Models.download('lms:${esc(repo)}@${esc(q)}')">⬇ lms</button>` : ""}<button class="btn sm" title="Прямая загрузка файла в каталог моделей" onclick="Models.download('url:${esc(f.url)}','${esc(repo)}')">⬇ файл</button></td></tr>`; }).join("") || '<tr><td colspan="5" class="muted small">файлов моделей не найдено</td></tr>'}</tbody></table>`;
    } catch (e) { $("hfFiles").innerHTML = `<div class="tel-hint">${esc(e.message)}</div>`; }
  },
  async download(source, subdir) {
    source = (source || "").trim(); if (!source) return;
    const n = State.nodes[this.node];
    UI.confirm("Скачать модель", `На узел <b>${esc(n.name)}</b>:<br><span class="mono">${esc(source)}</span><br><br><span class="small muted">lms: в каталог LM Studio (${esc((this.catalog[this.node]?.lms || {}).models_dir || "~/.lmstudio/models")}); url: файл в подкаталог ${esc(subdir || "downloads")} того же каталога.</span>`, async () => {
      const r = await api("/api/models/download", { node_key: this.node, source, dest_subdir: subdir || "" }); const res = r.result || {};
      if (res.ok === false || res.error) UI.toast("❌ " + esc(res.error || "ошибка"), "err", 6000); else UI.toast("Загрузка запущена", "ok");
      this.pollDownloads();
    }, "Скачать");
  },
  renderDownloads() {
    const box = $("modelsDownloads"); if (!box) return;
    const all = []; Object.entries(this.downloads).forEach(([k, list]) => (list || []).forEach(j => all.push({ ...j, node: k })));
    all.sort((a, b) => (b.started || 0) - (a.started || 0));
    box.innerHTML = all.length ? all.map(j => { const n = State.nodes[j.node] || {}; const pct = j.progress != null ? Math.round(j.progress) : null; const st = { running: "warn", done: "ok", error: "err", cancelled: "", queued: "" }[j.status] || ""; return `<div class="dl"><div class="row wrap"><span>${n.avatar || ""} <b>${esc(n.name || j.node)}</b></span><span class="mono small">${esc(j.source)}</span><span class="badge ${st}">${esc(j.status)}</span><span class="mono small">${pct != null ? pct + "%" : ""} ${j.speed_mbps ? "· " + j.speed_mbps + " Mbit/s" : ""} ${j.done_bytes ? "· " + fmtGB(j.done_bytes) + (j.total_bytes ? " / " + fmtGB(j.total_bytes) : "") : ""}</span><span class="grow"></span>${j.status === "running" || j.status === "queued" ? `<button class="btn sm danger" onclick="Models.cancelDownload('${j.node}','${j.id}')">✕</button>` : ""}</div>
      ${pct != null ? `<div class="bar ${st}" style="margin-top:4px"><i style="width:${pct}%"></i></div>` : j.status === "running" ? '<div class="bar" style="margin-top:4px"><i style="width:100%;opacity:.4"></i></div>' : ""}${j.error ? `<div class="small" style="color:var(--err)">${esc(j.error)}</div>` : ""}${(j.log || []).length ? `<div class="mono muted" style="font-size:10px;white-space:pre-wrap">${esc((j.log || []).slice(-2).join("\n"))}</div>` : ""}</div>`; }).join("") : '<div class="muted small">нет активных загрузок</div>';
  },
  async cancelDownload(node, id) { await api("/api/models/download/cancel", { node_key: node, id }); this.pollDownloads(); },
};
