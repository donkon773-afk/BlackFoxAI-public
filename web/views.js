/* BlackFox AI Workstation — views: telemetry, prompt roles, users, settings */
"use strict";

// ---------------------------------------------------------------------------
// Telemetry view
// ---------------------------------------------------------------------------
const Telemetry = {
  async refresh(force) {
    if (UI.tab !== "telemetry" && !force) return;
    try { State.telemetry = await api("/api/telemetry"); this.render(); } catch (e) {}
  },
  barClass(p) { return p >= 90 ? "err" : p >= 70 ? "warn" : ""; },
  render() {
    const T = State.telemetry; if (!T) return;
    $("telTs").textContent = fmtTime(T.ts) || "—";
    const host = T.host || {};
    $("hostName").textContent = host.hostname ? `(${host.hostname}, ${host.cpu?.name || ""})` : "";
    const hs = [];
    hs.push(this.stat("CPU", `${host.cpu?.percent ?? "—"}%`, `${host.cpu?.cores || ""} ядер`, "hostCpu"));
    hs.push(this.stat("RAM", `${host.ram?.used_gb ?? "—"} / ${host.ram?.total_gb ?? "—"} GB`, `${host.ram?.percent ?? 0}%`, "hostRam"));
    (host.gpus || []).forEach(g => hs.push(this.stat(g.name.replace("NVIDIA GeForce ", ""), `${g.util_percent}% · ${g.mem_used_mb}/${g.mem_total_mb} MB`, `${g.temp_c}°C · ${g.power_w} W · ${g.clock_mhz || 0} MHz`, null)));
    $("hostStats").innerHTML = hs.join("");
    spark($("hostCpu"), host.history?.cpu, "#06b6d4"); spark($("hostRam"), host.history?.ram, "#8b5cf6");

    const grid = $("telGrid");
    const keys = new Set(Object.keys(State.nodes));
    grid.querySelectorAll("[data-node]").forEach(el => { if (!keys.has(el.dataset.node)) el.remove(); });
    State.nodeList().forEach(n => {
      const d = (T.nodes || {})[n.key] || {}; const h = d.history || {};
      let card = grid.querySelector(`[data-node="${n.key}"]`);
      if (!card) { card = document.createElement("div"); card.dataset.node = n.key; card.className = "card tel-node"; grid.appendChild(card); }
      card.style.setProperty("--nc", n.color);
      const src = d.source === "agent" ? `<span class="src agent">агент${d.agent_version ? " v" + d.agent_version : ""}</span>` : d.source === "local" ? '<span class="src local">локально</span>' : '<span class="src">нет источника</span>';
      const net = d.net || {};
      const llm = `<span class="dot ${net.llm_status === "online" ? "online" : "offline"}"></span> LLM ${net.llm_status === "online" ? net.llm_latency_ms + " мс" : "offline"}`;
      let body = "";
      if (d.online) {
        const cpu = d.cpu || {}, ram = d.ram || {};
        body += this.metric("CPU", cpu.percent ?? 0, `${cpu.percent ?? "—"}%`, `${esc(cpu.name || "")}${cpu.cores ? " · " + cpu.cores + " ядер" : ""}`, `cpu_${n.key}`, h.cpu);
        body += this.metric("ОЗУ", ram.percent ?? 0, `${ram.used_gb ?? "—"} / ${ram.total_gb ?? "—"} GB`, `${ram.percent ?? 0}%`, `ram_${n.key}`, h.ram);
        (d.gpus || []).forEach((g, i) => {
          body += `<div class="gpu-block"><div class="gname">🎮 ${esc(g.name)}<span class="t">${g.temp_c ? g.temp_c + "°C · " : ""}${g.power_w ? g.power_w + " W · " : ""}${g.clock_mhz ? g.clock_mhz + " MHz · " : ""}${g.fan_percent ? "fan " + g.fan_percent + "%" : ""}</span></div>
            ${this.metric("GPU", g.util_percent, `${g.util_percent}%`, "загрузка", i === 0 ? `gpu_${n.key}` : null, i === 0 ? h.gpu : null)}
            ${this.metric(g.unified ? "Память" : "VRAM", g.mem_percent, `${g.mem_used_mb} / ${g.mem_total_mb} MB`, `${g.mem_percent}%${g.unified ? " (unified)" : ""}`, i === 0 ? `vram_${n.key}` : null, i === 0 ? h.vram : null)}</div>`;
        });
        if (!(d.gpus || []).length) body += `<div class="muted small" style="padding:6px 0">GPU: данные недоступны (нет nvidia-smi / ioreg на узле)</div>`;
        if (Telemetry.sensorsHtml) body += Telemetry.sensorsHtml(d.sensors, n.key);
        const lat = net.local ? "локальный узел" : net.agent_latency_ms != null ? net.agent_latency_ms + " мс" : "—";
        body += this.metric("Сеть", 0, lat, "задержка до агента", `lat_${n.key}`, h.lat, "auto");
        if (!net.local) body += this.metric("Скорость", 0, net.down_mbps != null ? `↓ ${net.down_mbps} Mbit/s` : "—", net.up_mbps != null ? `↑ ${net.up_mbps} Mbit/s` : (net.down_mbps == null ? "ожидание замера…" : ""), `mbps_${n.key}`, h.mbps, "auto");
      } else {
        body += `<div class="tel-hint">${esc(d.error || "нет данных")}${d.source === "agent" ? `<br><br>Запустите агент <b>на самом узле</b> одной командой:${Agent.installHtml(n.key)}<br>Хаб опрашивает: <code>${esc(net.agent_url || n.agent_url || "")}</code>` : d.source === "none" ? "<br><br>Задайте источник телеметрии в Настройки → Узлы." : ""}</div>`;
      }
      body += this.metric("LLM пинг", 0, net.llm_status === "online" ? net.llm_latency_ms + " мс" : "offline", net.llm_error ? esc(net.llm_error.slice(0, 40)) : esc(n.endpoint.replace(/^https?:\/\//, "").replace("/v1/chat/completions", "")), `llm_${n.key}`, h.llm_lat, "auto");
      // раскрытые блоки датчиков не должны схлопываться при каждом обновлении
      const openKeys = new Set([...card.querySelectorAll("details[open][data-k]")].map(el => el.dataset.k));
      card.innerHTML = `<div class="card-h"><span>${n.avatar}</span><span>${esc(n.name)}</span><span class="muted small">${esc(d.hostname || "")}${d.platform ? " · " + esc(d.platform) : ""}</span><span class="grow"></span>${src}<span class="small muted">${llm}</span><button class="btn sm" title="Рекомендации по узлу" onclick="Telemetry.recommend('${n.key}')">💡</button></div><div class="card-b" style="padding:8px 14px">${body}</div>`;
      openKeys.forEach(k => { const el = card.querySelector(`details[data-k="${k}"]`); if (el) el.open = true; });
      spark(card.querySelector(`#cpu_${n.key}`), h.cpu, "#06b6d4"); spark(card.querySelector(`#ram_${n.key}`), h.ram, "#8b5cf6");
      spark(card.querySelector(`#gpu_${n.key}`), h.gpu, "#3b82f6"); spark(card.querySelector(`#vram_${n.key}`), h.vram, "#f59e0b");
      spark(card.querySelector(`#lat_${n.key}`), h.lat, "#10b981", "auto"); spark(card.querySelector(`#mbps_${n.key}`), h.mbps, "#22c55e", "auto"); spark(card.querySelector(`#llm_${n.key}`), h.llm_lat, "#38bdf8", "auto");
    });
  },
  stat(label, value, sub, canvasId) { return `<div class="stat"><div class="l">${esc(label)}</div><div class="v">${esc(value)}</div><div class="s">${esc(sub)}</div>${canvasId ? `<canvas class="spark" id="${canvasId}"></canvas>` : ""}</div>`; },
  metric(label, pct, value, sub, canvasId, hist, max) {
    const hasCanvas = canvasId && hist;
    return `<div class="metric"><div class="ml">${esc(label)}</div><div>${hasCanvas ? `<canvas class="spark" id="${canvasId}"></canvas>` : `<div class="bar ${this.barClass(pct)}"><i style="width:${Math.min(100, pct || 0)}%"></i></div>`}${hasCanvas && pct ? `<div class="bar ${this.barClass(pct)}" style="margin-top:3px"><i style="width:${Math.min(100, pct)}%"></i></div>` : ""}</div><div class="mv">${value}<br><small>${sub}</small></div></div>`;
  },
  async recommend(key) {
    UI.info("Рекомендации", '<div class="active-banner"><div class="spinner"></div>Анализ…</div>');
    try { const r = await api("/api/node/recommend?node_key=" + key); $("modalBody").innerHTML = Recommend.html(r.recommendation, State.nodes[key]); }
    catch (e) { $("modalBody").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
  },
};

// Shared renderer for hardware-based recommendations
const Recommend = {
  fitLabel: { ok: ["✅ помещается", "ok"], tight: ["⚠ впритык", "warn"], offload: ["🐢 выгрузка на CPU", "warn"], no: ["❌ не помещается", "err"] },
  html(r, node) {
    if (!r) return '<div class="muted">нет данных</div>';
    const fits = (r.fits || []).slice(0, 12).map(f => `<tr><td class="mono small">${esc(f.id)}</td><td class="mono small">${f.params_b || "?"}B ${esc(f.quant || "")}</td><td class="mono small">${f.total_gb} ГБ</td><td><span class="badge ${this.fitLabel[f.fit][1]}">${this.fitLabel[f.fit][0]}</span></td><td class="mono small">${f.est_tok_s ? "~" + f.est_tok_s + " tok/s" : "—"}</td></tr>`).join("");
    return `<div class="rec">
      <div class="rec-head"><b>${esc(r.profile)}</b> <span class="muted small">класс ${r.class} · ${esc(r.gpu || "без GPU")} · ${r.unified ? "unified " : "VRAM "}${r.vram_gb} ГБ · ОЗУ ${r.ram_gb} ГБ · шина ~${r.bandwidth_gbs} ГБ/с</span></div>
      <div class="grid-2" style="margin-top:8px">
        <div><div class="small muted">Подходящие модели</div><ul class="small">${(r.model_classes || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>
        <div><div class="small muted">Место в конвейере</div><div class="small">${esc(r.stage)}</div><div class="small muted" style="margin-top:6px">Рекомендуемые параметры</div><div class="small mono">контекст ${r.settings.ctx} · max_tokens ${r.settings.max_tokens} · parallel ${r.settings.parallel} · таймаут ${r.settings.timeout_s}с</div></div>
      </div>
      <div class="small muted" style="margin-top:8px">Роли, подходящие по мощности</div>
      <div class="row wrap" style="margin-top:4px">${(r.roles || []).map(x => `<span class="chip on" title="${esc(x.prompt)}">${x.avatar} ${esc(x.name)}</span>`).join("")}</div>
      ${(r.notes || []).length ? `<ul class="small" style="margin-top:8px">${r.notes.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      ${fits ? `<div class="small muted" style="margin-top:8px">Модели узла при контексте ${r.settings.ctx}${r.best_model ? ` — лучший выбор: <b class="mono">${esc(r.best_model)}</b>` : ""}</div><table class="tbl" style="margin-top:4px"><thead><tr><th>Модель</th><th>Размер</th><th>Память</th><th>Вердикт</th><th>Скорость</th></tr></thead><tbody>${fits}</tbody></table>` : ""}
    </div>`;
  },
};

// ---------------------------------------------------------------------------
// Prompt roles view
// ---------------------------------------------------------------------------
const Prompts = {
  current: null,
  render() {
    const keys = Object.keys(State.nodes); if (!keys.length) return;
    if (!this.current || !State.nodes[this.current]) this.current = keys[0];
    $("promptsList").innerHTML = keys.map(k => { const n = State.nodes[k]; return `<div class="prompt-node ${k === this.current ? "active" : ""}" style="--nc:${n.color}" onclick="Prompts.select('${k}')"><div class="pn-name">${n.avatar} ${esc(n.name)}</div><div class="pn-role">${esc(n.tag)} · ${esc(n.role)}</div><div class="pn-role mono">${esc(n.model)}</div></div>`; }).join("");
    this.renderEditor();
  },
  select(k) { this.current = k; this.render(); },
  renderEditor() {
    const n = State.nodes[this.current]; if (!n) return;
    const ro = !State.isAdmin;
    $("promptsEditor").innerHTML = `
      <div class="row"><h2 style="font-size:15px">${n.avatar} ${esc(n.name)}</h2><span class="muted small">системный промпт и роль узла${ro ? " (только просмотр — редактирует администратор)" : ""}</span><span class="grow"></span><span class="muted small mono">${esc(n.model)}</span></div>
      <div class="grid-3">
        <div class="field"><label>Имя узла</label><input type="text" id="pr_name" value="${esc(n.name)}" ${ro ? "disabled" : ""}></div>
        <div class="field"><label>Тег</label><input type="text" id="pr_tag" value="${esc(n.tag)}" ${ro ? "disabled" : ""}></div>
        <div class="field"><label>Аватар</label><input type="text" id="pr_avatar" value="${esc(n.avatar)}" maxlength="4" ${ro ? "disabled" : ""}></div>
        <div class="field" style="grid-column: span 2"><label>Роль (краткое описание)</label><input type="text" id="pr_role" value="${esc(n.role)}" ${ro ? "disabled" : ""}></div>
        <div class="field"><label>Цвет</label><input type="color" id="pr_color" value="${esc(n.color)}" style="height:34px;padding:2px" ${ro ? "disabled" : ""}></div>
        <div class="field"><label>🖥 Агент ПК — выполнение команд</label><select id="pr_exec" ${ro ? "disabled" : ""}><option value="0" ${n.exec_enabled ? "" : "selected"}>запрещено</option><option value="1" ${n.exec_enabled ? "selected" : ""}>разрешено (PowerShell/bash через агента, с подтверждением)</option></select><span class="hint">Команды идут через подписанный канал агента узла; в чате каждая ждёт подтверждения администратора и пишется в журнал безопасности</span></div>
        <div class="field"><label>🖼 Модель понимает изображения (vision)</label><select id="pr_vision" ${ro ? "disabled" : ""}><option value="0" ${n.vision ? "" : "selected"}>нет</option><option value="1" ${n.vision ? "selected" : ""}>да — приложенные картинки отправлять модели</option></select></div>
      </div>
      <div class="field"><label>Системный промпт <span class="muted" id="pr_count">(${(n.system_prompt || "").length} симв.)</span></label>
        <textarea id="pr_prompt" ${ro ? "disabled" : ""} oninput="$('pr_count').textContent='('+this.value.length+' симв.)'">${esc(n.system_prompt)}</textarea>
        <span class="hint">Передаётся узлу как <code>system</code>-сообщение (кроме режима «без ролей»). К нему добавляются инструкция skill'а и правило языка.</span></div>
      <div class="row wrap">${ro ? "" : `<button class="btn primary" onclick="Prompts.save()">💾 Сохранить</button><button class="btn" onclick="Prompts.reset()">↺ По умолчанию</button><button class="btn" onclick="Prompts.library()">📚 Из библиотеки ролей</button>`}<button class="btn" onclick="Prompts.test()">🧪 Проверить ответ</button><span class="grow"></span><span class="muted small">Проверка отправит короткий запрос узлу с этим промптом</span></div>
      <div id="pr_test"></div>`;
  },
  async save() {
    const body = { node_key: this.current, name: $("pr_name").value, tag: $("pr_tag").value, avatar: $("pr_avatar").value, role: $("pr_role").value, color: $("pr_color").value, system_prompt: $("pr_prompt").value, exec_enabled: $("pr_exec").value === "1", vision: $("pr_vision").value === "1" };
    const r = await api("/api/node/update", body); State.nodes[this.current] = { ...State.nodes[this.current], ...r.node }; UI.toast("Промпт-роль сохранена", "ok"); this.render(); Nodes.renderStrip(); Chat.fillTargets();
  },
  reset() { UI.confirm("Сбросить промпт", "Вернуть системный промпт, роль и тег этого узла к значениям по умолчанию?", async () => { const r = await api("/api/node/reset_prompt", { node_key: this.current }); State.nodes[this.current] = { ...State.nodes[this.current], ...r.node }; this.render(); UI.toast("Сброшено", "ok"); }); },
  async library() {
    const s = await api("/api/settings"); const lib = s.role_library || [];
    UI.info("Библиотека ролей", `<div class="lib">${lib.map(r => `<div class="lib-item" onclick="Prompts.applyRole(${esc(JSON.stringify(r)).replace(/'/g, "&#39;")})"><b>${r.avatar} ${esc(r.name)}</b> <span class="badge">${esc(r.tag)}</span><div class="small muted">${esc(r.prompt)}</div></div>`).join("")}</div>`, true);
  },
  applyRole(r) { $("pr_role").value = r.name; $("pr_tag").value = r.tag; $("pr_avatar").value = r.avatar; $("pr_prompt").value = r.prompt; $("pr_count").textContent = `(${r.prompt.length} симв.)`; UI.closeModal(); },
  async test() {
    const box = $("pr_test"); box.innerHTML = `<div class="active-banner"><div class="spinner"></div>Запрос узлу…</div>`;
    try {
      const r = await api("/api/node/test", { node_key: this.current, system_prompt: $("pr_prompt").value, prompt: "Кратко представься: кто ты и чем занимаешься в этом кластере? 2-3 предложения." });
      box.innerHTML = r.error ? `<div class="resp error"><div class="resp-b">❌ ${esc(r.error)}</div></div>` : `<div class="resp" style="--nc:${State.nodes[this.current].color}"><div class="resp-h"><span class="name">Ответ узла</span><span class="stats">${fmtMs(r.latency_ms)} · ${esc(r.model)}</span></div><div class="resp-b md">${md(r.content)}</div></div>`;
    } catch (e) { box.innerHTML = `<div class="resp error"><div class="resp-b">❌ ${esc(e.message)}</div></div>`; }
  },
};

// ---------------------------------------------------------------------------
// Users view
// ---------------------------------------------------------------------------
const Users = {
  init() {
    $("meNameInput").value = Client.name;
    $("meNameInput").addEventListener("change", (e) => { Client.setName(e.target.value); this.renderMe(); Client.deviceInfo().then(d => api("/api/client/hello", { device: d })).then(() => this.refresh()); UI.toast("Имя обновлено", "ok", 1500); });
    this.renderMe();
  },
  renderMe() { $("meName").textContent = Client.name; $("meAvatar").textContent = (Client.name || "?").slice(0, 1).toUpperCase(); $("meNameInput").value = Client.name; },
  async refresh() {
    if (UI.tab !== "users") return;
    try { const r = await api("/api/clients"); State.clients = r.clients; this.render(); } catch (e) {}
  },
  render() {
    const me = State.clients.find(c => c.id === Client.id);
    if (me) { $("meIp").textContent = me.ip; $("meIdInfo").textContent = `${Client.id} · ${me.ip} · ${State.isAdmin ? "администратор" : "пользователь"}`; }
    $("usersTunnel").textContent = `http://${State.tunnel}:${State.port}`;
    $("usersCount").textContent = `(активных: ${State.clients.filter(c => c.active).length}, всего: ${State.clients.length})`;
    const rows = State.clients.map(c => {
      const di = c.device_info || {}; const ts = c.tailscale;
      const device = [di.model, di.os || c.device, di.brands ? "" : ""].filter(Boolean).join(" · ") || c.device;
      const details = [
        ["Устройство", di.model || (ts && ts.hostname) || "—"], ["ОС", di.os || c.device], ["Браузер", di.browser || c.user_agent], ["Экран", di.screen], ["Язык / часовой пояс", [di.language, di.timezone].filter(Boolean).join(" / ")],
        ["CPU / RAM (браузер)", [di.cores ? di.cores + " ядер" : "", di.memory_gb ? "≥" + di.memory_gb + " ГБ" : ""].filter(Boolean).join(", ")], ["Сенсорный", di.touch ? "да" : "нет"], ["Тип связи", di.connection],
        ["Tailscale", ts ? `${ts.hostname} (${ts.os}) ${ts.dns_name || ""} ${ts.online ? "online" : "offline"}${ts.cur_addr ? " · LAN " + ts.cur_addr : ""}` : "не в списке пиров"],
        ["User-Agent", c.user_agent], ["ID клиента", c.id], ["Первый вход", fmtDate(c.first_seen)], ["Последний экран", c.last_path],
      ].filter(x => x[1]).map(x => `<div class="kv"><span>${esc(x[0])}</span><span class="mono">${esc(x[1])}</span></div>`).join("");
      return `<div class="user-row ${c.active ? "active" : ""}">
        <div class="row wrap" style="gap:10px">
          <span class="dot ${c.active ? "online" : "unknown"}" title="${c.active ? "активен" : "неактивен " + c.idle_s + " с"}"></span>
          <b>${esc(c.name || (c.is_anon ? "без имени" : "—"))}</b>${c.id === Client.id ? ' <span class="badge" style="background:var(--accent-soft);color:var(--accent)">это вы</span>' : ""}${c.is_admin ? ' <span class="badge admin-badge">ADMIN</span>' : ""}
          <span class="mono small">${esc(c.ip)}</span><span class="small">${esc(device)}</span>${ts ? `<span class="small muted">🔗 ${esc(ts.hostname)} · ${esc(ts.os)}</span>` : ""}
          <span class="small muted">${esc(di.browser || "")}${di.screen ? " · " + esc(di.screen) : ""}</span>
          <span class="grow"></span>
          <span class="small muted">${c.active ? "сейчас" : c.idle_s < 3600 ? Math.round(c.idle_s / 60) + " мин назад" : fmtDate(c.last_seen)} · ${c.requests} запр. · ${c.tasks} задач</span>
          <button class="btn ghost sm" onclick="this.closest('.user-row').classList.toggle('open')">▾</button>
          ${State.isAdmin && c.id !== Client.id ? `<button class="btn ghost sm" title="Убрать из списка" onclick="Users.forget('${esc(c.id)}')">✕</button>` : ""}
        </div>
        <div class="user-details">${details}</div></div>`;
    }).join("");
    $("usersBody").innerHTML = rows || '<div class="muted small" style="padding:12px">нет данных</div>';
  },
  async forget(id) { await api("/api/client/forget", { id }); this.refresh(); },
  async speedtest() {
    $("meSpeed").textContent = "измерение…";
    try {
      const n = 3000000; const t0 = performance.now(); const r = await fetch(`/api/speedtest?bytes=${n}&r=${Math.random()}`); const buf = await r.arrayBuffer(); const dt = (performance.now() - t0) / 1000;
      const t1 = performance.now(); await fetch("/api/state", { headers: Client.headers() }); const rtt = Math.round(performance.now() - t1);
      $("meSpeed").textContent = `↓ ${(buf.byteLength * 8 / dt / 1e6).toFixed(1)} Mbit/s · RTT ${rtt} мс`;
    } catch (e) { $("meSpeed").textContent = "ошибка"; }
  },
};

// ---------------------------------------------------------------------------
// Settings view
// ---------------------------------------------------------------------------
const Settings = {
  section: "ui", pipeline: null, skills: null,
  show(s) { this.section = s; document.querySelectorAll(".settings-nav button").forEach(b => b.classList.toggle("active", b.dataset.s === s)); document.querySelectorAll(".settings-section").forEach(x => x.classList.toggle("active", x.id === "s-" + s)); },
  render() {
    this.fillUi();
    if (!State.isAdmin) { this.show("ui"); return; }
    this.renderNodes(); this.fillLlm(); this.renderPipeline(); this.fillCouncil(); this.renderSkills(); this.fillConn();
    if (typeof Security !== "undefined") Security.render();
  },
  fillUi() {
    const u = State.ui; $("uiTheme").value = u.theme; $("uiFont").value = u.font; $("uiDensity").value = u.density; $("uiPoll").value = u.poll; $("uiSidebar").value = u.sidebar; $("uiStrip").checked = u.strip; $("uiReasoning").checked = u.reasoning; $("uiConfirmClear").checked = u.confirmClear;
    $("uiSwatches").innerHTML = UI.ACCENTS.map(c => `<div class="swatch ${c === u.accent ? "active" : ""}" style="background:${c}" onclick="State.ui.accent='${c}';UI.saveUi();Settings.fillUi()"></div>`).join("");
  },
  saveUi() { const u = State.ui; u.theme = $("uiTheme").value; u.font = +$("uiFont").value || 13; u.density = $("uiDensity").value; u.poll = Math.max(500, +$("uiPoll").value || 1500); u.sidebar = +$("uiSidebar").value || 270; u.strip = $("uiStrip").checked; u.reasoning = $("uiReasoning").checked; u.confirmClear = $("uiConfirmClear").checked; UI.saveUi(); UI.toast("Настройки интерфейса применены", "ok"); },
  resetUi() { State.ui = { theme: "dark", font: 13, density: "comfortable", accent: "#3b82f6", poll: 1500, sidebar: 270, strip: true, reasoning: false, confirmClear: true }; UI.saveUi(); this.fillUi(); },

  // ---- nodes
  renderNodes() {
    const box = $("nodesForms");
    box.innerHTML = State.nodeList().map(n => {
      const tel = n.telemetry || { type: "auto" }; const res = n.telemetry_resolved || {}; const models = State.models[n.key]?.models || [];
      const opts = models.filter(m => m.type !== "embeddings").map(m => `<option value="${esc(m.id)}" ${m.id === n.model ? "selected" : ""}>${m.loaded === true ? "● " : m.loaded === false ? "○ " : ""}${esc(m.id)}</option>`);
      if (!models.find(m => m.id === n.model)) opts.unshift(`<option value="${esc(n.model)}" selected>${esc(n.model)}</option>`);
      const resolved = res.type === "local" ? `локально${res.gpu_match ? ", GPU «" + esc(res.gpu_match) + "»" : ", все GPU хоста"}` : res.type === "agent" ? `агент ${esc(res.url || "")}${res.auto ? " (авто)" : ""}` : "нет";
      return `<div class="card node-form" style="--nc:${n.color}" data-node="${n.key}">
        <div class="card-h"><span>${n.avatar}</span><span>${esc(n.name)}</span><span class="muted small mono">${n.key} · ${esc(n.kind || "?")}</span><span class="grow"></span><span class="dot ${n.status === "online" ? "online" : "offline"}"></span><span class="small muted">${n.status}</span>
          <label class="switch"><input type="checkbox" data-f="enabled" ${n.enabled ? "checked" : ""}><span class="slider"></span></label>
          <button class="btn ghost sm danger" title="Удалить узел" onclick="Settings.deleteNode('${n.key}')">🗑</button></div>
        <div class="card-b grid-3">
          <div class="field" style="grid-column: span 2"><label>Endpoint (OpenAI-совместимый chat/completions)</label><input type="text" class="mono" data-f="endpoint" value="${esc(n.endpoint)}"></div>
          <div class="field"><label>Таймаут запроса, с (пусто = общий)</label><input type="number" data-f="timeout_s" value="${n.timeout_s ?? ""}" min="10"></div>
          <div class="field" style="grid-column: span 2"><label>Модель</label><div class="row"><select data-f="model_sel" class="grow mono" onchange="this.parentNode.parentNode.querySelector('[data-f=model]').value=this.value">${opts.join("")}</select><button class="btn sm" onclick="Nodes.loadModels('${n.key}',true).then(()=>Settings.renderNodes())">↻</button></div><input type="text" class="mono" data-f="model" value="${esc(n.model)}" placeholder="или введите id модели вручную"></div>
          <div class="field"><label>Оборудование (описание)</label><input type="text" data-f="hardware" value="${esc(n.hardware)}"></div>
          <div class="field"><label>Источник телеметрии</label><select data-f="tel_type"><option value="auto" ${tel.type === "auto" || !tel.type ? "selected" : ""}>авто (сейчас: ${resolved})</option><option value="local" ${tel.type === "local" ? "selected" : ""}>локально (эта машина)</option><option value="agent" ${tel.type === "agent" ? "selected" : ""}>агент node_agent.py</option><option value="none" ${tel.type === "none" ? "selected" : ""}>выключена</option></select></div>
          <div class="field"><label>URL агента (для «агент»)</label><input type="text" class="mono" data-f="tel_url" value="${esc(tel.url || "")}" placeholder="${esc(n.agent_url || "http://100.x.x.x:8766")}"></div>
          <div class="field"><label>Фильтр GPU (для «локально»)</label><input type="text" data-f="tel_gpu" value="${esc(tel.gpu_match || "")}" placeholder="например 3060"></div>
          <div class="field"><label>Расположение</label><input type="text" data-f="location" value="${esc(n.location)}"></div>
          <div class="row" style="grid-column: span 2; justify-content:flex-end; align-self:end"><button class="btn sm" onclick="Agent.install('${n.key}')">📡 Установить агент</button><button class="btn sm" onclick="Telemetry.recommend('${n.key}')">💡 Рекомендации</button><button class="btn sm" onclick="Nodes.ping('${n.key}')">📡 Пинг</button><button class="btn primary sm" onclick="Settings.saveNode('${n.key}')">💾 Сохранить узел</button></div>
        </div></div>`;
    }).join("");
  },
  async saveNode(key) {
    const f = document.querySelector(`.node-form[data-node="${key}"]`); const g = (n) => f.querySelector(`[data-f="${n}"]`);
    const body = { node_key: key, enabled: g("enabled").checked, endpoint: g("endpoint").value, model: g("model").value, timeout_s: g("timeout_s").value, hardware: g("hardware").value, location: g("location").value, telemetry: { type: g("tel_type").value, url: g("tel_url").value, gpu_match: g("tel_gpu").value } };
    try { const r = await api("/api/node/update", body); State.nodes[key] = { ...State.nodes[key], ...r.node }; UI.toast("Узел сохранён", "ok"); Nodes.loadModels(key, true); Nodes.renderStrip(); Chat.fillTargets(); this.renderNodes(); } catch (e) { UI.toast("Ошибка: " + esc(e.message), "err"); }
  },
  deleteNode(key) {
    const n = State.nodes[key];
    UI.confirm("Удалить узел", `Удалить узел «${esc(n.name)}» из кластера? Его настройки и промпт будут потеряны, шаги конвейера с ним удалятся.`, async () => { await api("/api/node/delete", { node_key: key }); UI.toast("Узел удалён", "ok"); await Poll.tick(); this.renderNodes(); }, "Удалить");
  },
  // ---- add node wizard
  addNodeState: null,
  openAddNode(prefill) {
    this.addNodeState = { probe: null, role: null };
    UI.modal("Добавить узел", `
      <div class="field"><label>Адрес inference-сервера</label><div class="row"><input type="text" id="an_endpoint" class="mono grow" placeholder="http://100.x.x.x:1234  (LM Studio / llama.cpp / Ollama с OpenAI API)"><button class="btn" onclick="Settings.probeNode()">🔎 Проверить</button></div><span class="hint">Для удалённого узла заранее запустите на нём <code>python3 node_agent.py</code> — тогда мастер увидит железо и предложит роль и модели.</span></div>
      <div class="field"><label>URL агента (необязательно, авто = тот же хост:${State.settings?.telemetry?.agent_port || 8766})</label><input type="text" id="an_agent" class="mono" placeholder="http://100.x.x.x:8766"></div>
      <div id="an_probe"></div>
      <div id="an_form" style="display:none">
        <div class="grid-3" style="margin-top:8px">
          <div class="field"><label>Имя узла</label><input type="text" id="an_name"></div>
          <div class="field"><label>Тег</label><input type="text" id="an_tag"></div>
          <div class="field"><label>Аватар</label><input type="text" id="an_avatar" value="🖥️" maxlength="4"></div>
          <div class="field" style="grid-column: span 2"><label>Модель</label><select id="an_model" class="mono"></select></div>
          <div class="field"><label>Цвет</label><input type="color" id="an_color" value="#a78bfa" style="height:34px;padding:2px"></div>
          <div class="field" style="grid-column: span 3"><label>Роль (выберите из рекомендованных или напишите свою)</label><div class="row wrap" id="an_roles"></div><input type="text" id="an_role" placeholder="Роль" style="margin-top:6px"></div>
          <div class="field" style="grid-column: span 3"><label>Системный промпт</label><textarea id="an_prompt" style="min-height:90px"></textarea></div>
        </div>
        <label class="row small" style="margin-top:6px"><input type="checkbox" id="an_pipeline"> Добавить последним шагом конвейера по умолчанию</label>
      </div>`, async () => {
      const st = this.addNodeState; if (!st.probe) { UI.toast("Сначала нажмите «Проверить»", "warn"); return true; }
      const body = { name: $("an_name").value.trim(), tag: $("an_tag").value.trim(), avatar: $("an_avatar").value.trim(), color: $("an_color").value, endpoint: st.probe.endpoint, model: $("an_model").value, role: $("an_role").value.trim(), system_prompt: $("an_prompt").value.trim(), hardware: st.hardwareText || "", agent_url: $("an_agent").value.trim(), add_to_pipeline: $("an_pipeline").checked };
      if (st.probe.is_local && st.gpuMatch) body.telemetry = { type: "local", gpu_match: st.gpuMatch };
      if (!body.name) { UI.toast("Укажите имя узла", "warn"); return true; }
      const r = await api("/api/node/add", body); UI.toast(`Узел «${esc(r.node.name)}» добавлен`, "ok"); await Poll.tick(); this.renderNodes();
      if (typeof Connect !== "undefined" && Connect.devices) Connect.discover();
    }, "Добавить узел", true);
    // вызов из мастера подключения: адрес уже известен — сразу проверяем
    if (prefill) {
      if (prefill.endpoint) $("an_endpoint").value = prefill.endpoint;
      if (prefill.agent_url) $("an_agent").value = prefill.agent_url;
      if (prefill.name) $("an_name").value = prefill.name;
      if (prefill.endpoint) this.probeNode();
    }
  },
  async probeNode() {
    const ep = $("an_endpoint").value.trim(); if (!ep) return;
    $("an_probe").innerHTML = '<div class="active-banner" style="margin-top:8px"><div class="spinner"></div>Проверяю адрес, модели, агент…</div>';
    try {
      const p = await api("/api/node/probe", { endpoint: ep, agent_url: $("an_agent").value.trim(), gpu_match: this.addNodeState.gpuMatch || "" });
      this.addNodeState.probe = p;
      if (!p.reachable) { $("an_probe").innerHTML = `<div class="tel-hint" style="margin-top:8px">❌ ${esc(p.error)}</div>`; return; }
      const hw = p.hardware; const r = p.recommendation;
      const gpus = (hw && hw.gpus) || [];
      this.addNodeState.hardwareText = gpus.length ? gpus.map(g => `${g.name} ${Math.round(g.mem_total_mb / 1024)}GB`).join(", ") : (hw && hw.platform) || "";
      let gpuPick = "";
      if (p.is_local && gpus.length > 1) gpuPick = `<div class="field" style="margin-top:6px"><label>Узел на этой машине: какой GPU он использует?</label><div class="row wrap">${gpus.map(g => `<button class="chip ${this.addNodeState.gpuMatch && g.name.toLowerCase().includes(this.addNodeState.gpuMatch.toLowerCase()) ? "on" : ""}" onclick="Settings.addNodeState.gpuMatch='${esc(g.name.replace("NVIDIA GeForce ", ""))}';Settings.probeNode()">${esc(g.name)} · ${Math.round(g.mem_total_mb / 1024)} GB</button>`).join("")}</div></div>`;
      $("an_probe").innerHTML = `<div class="tel-hint" style="margin-top:8px">✅ Доступен · тип: <b>${esc(p.kind || "openai")}</b> · моделей: ${p.models.length} · ${p.agent_ok ? "железо получено" + (p.is_local ? " (локально)" : " от агента") : "агент не отвечает (" + esc(p.agent_error || "") + ") — рекомендации по данным Tailscale/умолчанию"}${p.tailscale_peer ? ` · Tailscale: ${esc(p.tailscale_peer.hostname)} (${esc(p.tailscale_peer.os)})` : ""}</div>${gpuPick}${Recommend.html(r)}`;
      $("an_form").style.display = "";
      $("an_name").value = $("an_name").value || (hw && hw.hostname) || (p.tailscale_peer && p.tailscale_peer.hostname) || "";
      $("an_tag").value = $("an_tag").value || "NODE";
      $("an_model").innerHTML = p.models.filter(m => m.type !== "embeddings").map(m => `<option value="${esc(m.id)}" ${m.id === r.best_model ? "selected" : ""}>${m.loaded ? "● " : ""}${esc(m.id)}${m.params_b ? " · " + m.params_b + "B" : ""}${m.quant ? " " + esc(m.quant) : ""}</option>`).join("") || '<option value="">(моделей нет — укажите позже)</option>';
      $("an_roles").innerHTML = (r.roles || []).map(x => `<button class="chip" onclick="Settings.pickRole(${esc(JSON.stringify(x)).replace(/'/g, "&#39;")})">${x.avatar} ${esc(x.name)}</button>`).join("");
      if (r.roles && r.roles[0] && !$("an_role").value) this.pickRole(r.roles[0]);
    } catch (e) { $("an_probe").innerHTML = `<div class="tel-hint" style="margin-top:8px">❌ ${esc(e.message)}</div>`; }
  },
  pickRole(x) { $("an_role").value = x.name; $("an_tag").value = x.tag; $("an_avatar").value = x.avatar; $("an_prompt").value = x.prompt; document.querySelectorAll("#an_roles .chip").forEach(c => c.classList.toggle("on", c.textContent.includes(x.name))); },

  // ---- llm / council / pipeline / skills / conn
  fillLlm() { const l = State.settings?.llm || {}; $("llmForm").querySelectorAll("[data-k]").forEach(el => { el.value = String(l[el.dataset.k] ?? ""); }); },
  async saveLlm() {
    const llm = {}; $("llmForm").querySelectorAll("[data-k]").forEach(el => { const k = el.dataset.k; let v = el.value; if (k === "stream" || k === "force_language") v = v === "true"; else if (k !== "reasoning_effort") v = +v; llm[k] = v; });
    const r = await api("/api/settings", { llm }); State.settings = r.settings; UI.toast("Параметры LLM сохранены", "ok");
  },
  fillCouncil() {
    const c = State.settings?.council || {};
    $("councilModerator").innerHTML = '<option value="">— целевой узел —</option>' + State.nodeList().map(n => `<option value="${n.key}">${n.avatar} ${esc(n.name)}</option>`).join("");
    $("councilForm").querySelectorAll("[data-k]").forEach(el => { el.value = String(c[el.dataset.k] ?? ""); });
  },
  async saveCouncil() { const council = {}; $("councilForm").querySelectorAll("[data-k]").forEach(el => { council[el.dataset.k] = el.type === "number" ? +el.value : el.value; }); const r = await api("/api/settings", { council }); State.settings = r.settings; UI.toast("Настройки консилиума сохранены", "ok"); },
  renderPipeline() {
    if (!this.pipeline) this.pipeline = JSON.parse(JSON.stringify(State.settings?.pipeline || []));
    const nodeOpts = (sel) => State.nodeList().map(n => `<option value="${n.key}" ${n.key === sel ? "selected" : ""}>${n.avatar} ${esc(n.name)}</option>`).join("");
    $("pipelineSteps").innerHTML = this.pipeline.map((s, i) => `<div class="step-row"><div class="mono muted" style="padding-top:8px">${i + 1}.</div>
      <div class="field"><select onchange="Settings.pipeline[${i}].node=this.value">${nodeOpts(s.node)}</select><input type="text" placeholder="Название шага" value="${esc(s.label)}" oninput="Settings.pipeline[${i}].label=this.value"></div>
      <textarea placeholder="Инструкция шага" oninput="Settings.pipeline[${i}].prompt=this.value">${esc(s.prompt)}</textarea>
      <div class="row"><button class="btn ghost sm" ${i === 0 ? "disabled" : ""} onclick="Settings.moveStep(${i},-1)">▲</button><button class="btn ghost sm" ${i === this.pipeline.length - 1 ? "disabled" : ""} onclick="Settings.moveStep(${i},1)">▼</button><button class="btn ghost sm danger" onclick="Settings.pipeline.splice(${i},1);Settings.renderPipeline()">✕</button></div></div>`).join("") || '<div class="muted small">Шагов нет</div>';
  },
  addStep() { this.pipeline.push({ node: Object.keys(State.nodes)[0], label: `Шаг ${this.pipeline.length + 1}`, prompt: "" }); this.renderPipeline(); },
  moveStep(i, d) { const a = this.pipeline; const j = i + d; if (j < 0 || j >= a.length) return; [a[i], a[j]] = [a[j], a[i]]; this.renderPipeline(); },
  async savePipeline() { const r = await api("/api/settings", { pipeline: this.pipeline }); State.settings = r.settings; this.pipeline = null; this.renderPipeline(); UI.toast("Конвейер сохранён", "ok"); },
  renderSkills() {
    if (!this.skills) this.skills = JSON.parse(JSON.stringify(State.settings?.skills || []));
    $("skillRows").innerHTML = this.skills.map((s, i) => `<div class="skill-row ${s.builtin ? "builtin" : ""}">
      <div class="row wrap"><input type="text" class="sm" style="width:46px" value="${esc(s.icon || "")}" oninput="Settings.skills[${i}].icon=this.value" title="Иконка"><input type="text" class="sm" style="width:200px" value="${esc(s.name)}" placeholder="Название" oninput="Settings.skills[${i}].name=this.value"><input type="text" class="sm grow" value="${esc(s.desc || "")}" placeholder="Описание (подсказка в композере)" oninput="Settings.skills[${i}].desc=this.value">
        ${s.builtin ? '<span class="badge">встроенный</span>' : '<span class="badge" style="color:var(--info)">свой</span>'}<label class="row small"><input type="checkbox" ${s.hidden ? "checked" : ""} onchange="Settings.skills[${i}].hidden=this.checked"> скрыть</label>${s.builtin ? "" : `<button class="btn ghost sm danger" onclick="Settings.skills.splice(${i},1);Settings.renderSkills()">✕</button>`}</div>
      <div class="row wrap" style="margin-top:4px"><span class="small muted">temperature</span><input type="number" class="sm" style="width:70px" step="0.05" value="${s.params?.temperature ?? ""}" oninput="Settings.setSkillParam(${i},'temperature',this.value)"><span class="small muted">max_tokens</span><input type="number" class="sm" style="width:80px" step="128" value="${s.params?.max_tokens ?? ""}" oninput="Settings.setSkillParam(${i},'max_tokens',this.value)"><span class="small muted">reasoning</span><select class="sm" onchange="Settings.setSkillParam(${i},'reasoning_effort',this.value)"><option value="">—</option>${["none", "low", "medium", "high", "off"].map(v => `<option value="${v}" ${s.params?.reasoning_effort === v ? "selected" : ""}>${v}</option>`).join("")}</select>
        <label class="row small"><input type="checkbox" ${s.web ? "checked" : ""} onchange="Settings.skills[${i}].web=this.checked"> 🌐 поиск в интернете</label><span class="small muted">результатов</span><input type="number" class="sm" style="width:56px" min="1" max="10" value="${s.web_results ?? 5}" oninput="Settings.skills[${i}].web_results=+this.value"><span class="small muted">страниц читать</span><input type="number" class="sm" style="width:56px" min="0" max="8" value="${s.web_pages ?? 3}" oninput="Settings.skills[${i}].web_pages=+this.value"></div>
      <textarea class="sm" style="width:100%;margin-top:4px;min-height:44px" placeholder="Инструкция, добавляемая к системному промпту" oninput="Settings.skills[${i}].addendum=this.value">${esc(s.addendum || "")}</textarea></div>`).join("");
  },
  setSkillParam(i, k, v) { const s = this.skills[i]; s.params = s.params || {}; if (v === "" || v == null) delete s.params[k]; else s.params[k] = k === "reasoning_effort" ? v : +v; },
  addSkill() { this.skills.push({ id: "s_" + Date.now().toString(36), icon: "✨", name: "Новый skill", desc: "", builtin: false, params: {}, addendum: "", web: false, web_results: 5, web_pages: 3 }); this.renderSkills(); },
  async saveSkills() { const r = await api("/api/settings", { skills: this.skills }); State.settings = r.settings; this.skills = null; this.renderSkills(); Chat.fillTargets(); UI.toast("Skills сохранены", "ok"); },
  fillConn() {
    const s = State.settings || {};
    $("adminCommandNode").innerHTML = '<option value="">— целевой узел —</option>' + State.nodeList().map(n => `<option value="${n.key}">${n.avatar} ${esc(n.name)}</option>`).join("");
    $("connForm").querySelectorAll("[data-k]").forEach(el => { let v = (s[el.dataset.s] || {})[el.dataset.k]; if (Array.isArray(v)) v = v.join(", "); el.value = String(v ?? ""); });
  },
  async saveConn() {
    const body = { connection: {}, telemetry: {}, admin: {} };
    $("connForm").querySelectorAll("[data-k]").forEach(el => { if (el.disabled) return; let v = el.type === "number" ? +el.value : el.value.trim(); if (el.dataset.k === "enabled") v = v === "true"; if (el.dataset.k === "extra_ips") v = v.split(/[,\s]+/).filter(Boolean); body[el.dataset.s][el.dataset.k] = v; });
    const r = await api("/api/settings", body); State.settings = r.settings; State.tunnel = r.settings.connection.tunnel_host; UI.toast("Сохранено", "ok"); Poll.tick();
  },
  async reset(section) {
    UI.confirm("Сбросить настройки", `Вернуть раздел «${section}» к значениям по умолчанию?`, async () => { const r = await api("/api/settings/reset", { section }); State.settings = r.settings; this.pipeline = null; this.skills = null; this.render(); Chat.fillTargets(); UI.toast("Сброшено", "ok"); });
  },
};

// ---------------------------------------------------------------------------
// Agent installation helper (one command per OS, hub address baked in)
// ---------------------------------------------------------------------------
const Agent = {
  hubUrl() { return `${location.protocol}//${State.tunnel}:${State.port}`; },
  pin() { return (State.settings && State.settings.security && State.settings.security.tls_pins) || {}; },

  // Установщик отдаёт секрет кластера, поэтому новый узел скачивает его только
  // по одноразовому ключу: хаб выпускает его здесь и сам собирает команду.
  cmdsHtml(r) {
    const life = `ключ действует ${r.expires_in_min} мин, гаснет после установки`;
    return `<div class="agent-cmds">
      <div class="agent-cmd"><span class="os">Windows — PowerShell</span><code id="acw">${esc(r.install_win)}</code><button class="btn sm" onclick="Agent.copy('acw',this)">📋</button></div>
      <div class="agent-cmd"><span class="os">macOS / Linux — терминал</span><code id="acu">${esc(r.install_unix)}</code><button class="btn sm" onclick="Agent.copy('acu',this)">📋</button></div>
      <div class="small muted" style="margin-top:6px">🔑 ${life}${r.pinned ? "; загрузка идёт с проверкой ключа сертификата хаба" : ""}. Нужен ещё один узел — нажмите «Установить агент» снова.</div>
    </div>`;
  },
  copy(id, btn) { const el = $(id); if (!el) return; navigator.clipboard?.writeText(el.textContent); const t = btn.textContent; btn.textContent = "✅"; setTimeout(() => btn.textContent = t, 1500); },
  /** Врезка в карточку узла без агента: команду с ключом выдаёт диалог, здесь — только кнопка. */
  installHtml(key) {
    return State.isAdmin
      ? `<div style="margin:8px 0"><button class="btn sm" onclick="Agent.install('${esc(key || "")}')">📡 Получить команду установки</button></div>`
      : `<div class="small muted" style="margin:8px 0">Команду с одноразовым ключом выдаёт администратор (кнопка «Установить агент» в карточке узла).</div>`;
  },

  async install(key) {
    const n = typeof key === "object" && key ? key : (State.nodes[key] || {});
    const port = State.settings?.telemetry?.agent_port || 8766;
    UI.info(`📡 Установка агента${n.name ? " — " + n.name : ""}`, `
      <div class="small">Команду нужно выполнить <b>на самой машине узла</b>${n.name ? ` (${esc(n.name)})` : ""}, а не на хабе. Она скачает <code>node_agent.py</code> с этого хаба, включит автозапуск и сразу поднимет агент на порту <b>${port}</b>.</div>
      <div id="agent-cmds" class="small muted" style="margin:10px 0">⏳ выпускаю одноразовый ключ установки…</div>
      <div class="small muted">Если Python не установлен: Windows — <code>winget install -e --id Python.Python.3.12</code>; macOS — <code>xcode-select --install</code>.</div>
      <div class="small muted" style="margin-top:6px">После установки хаб начнёт опрашивать <code>${esc(n.agent_url || "http://&lt;tailscale-ip&gt;:" + port)}</code> — телеметрия появится сама в течение пары секунд.</div>
      <div class="small muted" style="margin-top:6px">Вручную, без автозапуска: скачать <a href="/node_agent.py" download>node_agent.py</a> и выполнить <code>python3 node_agent.py ${port}</code> (секрет кластера возьмите в Настройки → 🔒 Безопасность).</div>`, true);
    const box = $("agent-cmds");
    try {
      const r = await api("/api/security/install_token", { minutes: 30, uses: 1, port, note: n.name || (typeof key === "string" ? key : "") });
      if (box) box.innerHTML = this.cmdsHtml(r);
    } catch (e) {
      if (box) box.innerHTML = `<div class="warn-box">Не удалось выпустить ключ установки: ${esc(e.message || e)}.<br>
        Ключ выдаётся только администратору — войдите под паролем администратора и повторите.</div>`;
    }
  },
};
