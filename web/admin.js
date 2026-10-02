/* BlackFox AI Workstation — Admin tab (hub machine only): overview, node control, NL commands */
"use strict";

const Admin = {
  overview: null, node: null, nodeInfo: null, log: [],
  async open() {
    if (!State.isAdmin) { $("adminWrap").innerHTML = '<div class="tel-hint">Доступно только с хост-машины кластера.</div>'; return; }
    if (!this.node || !State.nodes[this.node]) this.node = Object.keys(State.nodes)[0];
    this.render();
    if (typeof Connect !== "undefined") Connect.load();
    this.loadOverview();
    this.loadNode(this.node);
  },
  render() {
    $("adminWrap").innerHTML = `
      <div class="card"><div class="card-h">🔗 Подключение к платформе <span class="muted small">адрес для людей и устройств · подключение новых рабочих узлов</span><span class="grow"></span><button class="btn sm" onclick="Connect.load(true)">↻</button></div><div class="card-b" id="adminConnect"><div class="active-banner"><div class="spinner"></div>Адреса…</div></div></div>
      <div class="card" style="margin-top:10px"><div class="card-h">🌐 Сеть кластера и расположение узлов <span class="muted small">Tailscale, локальные сети, публичные адреса (геолокация по IP — приблизительная)</span><span class="grow"></span><button class="btn sm" onclick="Admin.loadOverview(true)">↻</button></div><div class="card-b" id="adminOverview"><div class="active-banner"><div class="spinner"></div>Опрос узлов…</div></div></div>
      <div class="card" style="margin-top:10px"><div class="card-h">🛠 Управление узлом
          <div class="seg" style="margin-left:8px">${State.nodeList().map(n => `<button class="${n.key === this.node ? "active" : ""}" onclick="Admin.selectNode('${n.key}')">${n.avatar} ${esc(n.name)}</button>`).join("")}</div><span class="grow"></span><button class="btn sm" onclick="Admin.loadNode(Admin.node,true)">↻</button></div>
        <div class="card-b" id="adminNode"><div class="active-banner"><div class="spinner"></div>Информация об узле…</div></div></div>
      <div class="card" style="margin-top:10px"><div class="card-h">💬 Команда узлу на естественном языке <span class="muted small">LLM переводит команду в одно действие из белого списка; выполняется только после вашего подтверждения</span></div>
        <div class="card-b"><div class="row wrap"><input type="text" id="admCmd" class="grow" placeholder="Например: выгрузи все модели · загрузи qwen3.5-2b с контекстом 8k · покажи свободное место · скачай unsloth/Qwen3.5-2B-GGUF q8" onkeydown="if(event.key==='Enter')Admin.ask()">
          <select id="admVia" class="sm" title="Какая LLM разбирает команду"><option value="">разбор: сам узел</option>${State.nodeList().map(n => `<option value="${n.key}" ${(State.settings?.admin?.command_node || "") === n.key ? "selected" : ""}>разбор: ${n.avatar} ${esc(n.name)}</option>`).join("")}</select><button class="btn primary" onclick="Admin.ask()">Разобрать</button></div>
          <div id="admProposal" style="margin-top:8px"></div></div></div>
      <div class="card" style="margin-top:10px"><div class="card-h">📜 Журнал команд<span class="grow"></span><button class="btn sm ghost" onclick="Admin.log=[];Admin.renderLog()">очистить</button></div><div class="card-b" id="adminLog"><div class="muted small">пусто</div></div></div>`;
    this.renderLog();
  },
  async loadOverview(force) {
    try { this.overview = await api("/api/admin/overview"); this.renderOverview(); } catch (e) { $("adminOverview").innerHTML = `<div class="tel-hint">${esc(e.message)}</div>`; }
  },
  geoText(g) { return g ? `${g.city || ""}${g.regionName ? ", " + g.regionName : ""}${g.country ? ", " + g.country : ""} · ${g.isp || g.org || ""}` : "—"; },
  renderOverview() {
    const o = this.overview; const ts = o.tailscale || {}; const host = o.host || {}; const hs = host.sysinfo || {}; const hn = hs.network || {};
    const peers = (ts.peers || []).map(p => `<tr><td>${esc(p.hostname)}</td><td>${esc(p.os)}</td><td class="mono small">${(p.ips || []).filter(x => x.includes(".")).join(", ")}</td><td><span class="dot ${p.online ? "online" : "offline"}"></span> ${p.online ? "online" : "offline"}</td><td class="mono small">${esc(p.cur_addr || "")}</td><td class="mono small">${esc(p.relay || "")}</td><td class="small muted">${p.online ? "" : fmtDate(p.last_seen)}</td><td class="small muted">${esc(p.dns_name || "")}</td></tr>`).join("");
    const nodes = Object.entries(o.nodes || {}).map(([k, v]) => {
      const n = v.node; const si = v.sysinfo || {}; const net = si.network || {}; const peer = v.tailscale_peer; const hw = v.hardware || {};
      return `<tr><td>${n.avatar} <b>${esc(n.name)}</b><div class="muted" style="font-size:10px">${esc(n.endpoint.replace(/^https?:\/\//, "").replace("/v1/chat/completions", ""))}</div></td>
        <td class="small">${esc(si.hostname || hw.hostname || (peer && peer.hostname) || "—")}<div class="muted" style="font-size:10px">${esc(si.platform || hw.platform || (peer && peer.os) || "")}${si.user ? " · " + esc(si.user) : ""}</div></td>
        <td class="mono small">${(net.local_ips || []).join("<br>") || "—"}</td><td class="mono small">${esc(net.gateway || "—")}${net.wifi_ssid ? "<br>📶 " + esc(net.wifi_ssid) : ""}</td>
        <td class="mono small">${esc(net.public_ip || "—")}<div class="muted" style="font-size:10px">${esc(this.geoText(v.geo))}</div></td>
        <td class="small">${peer ? `${esc(peer.hostname)} · ${peer.online ? "online" : "offline"}${peer.cur_addr ? "<br><span class='mono muted'>" + esc(peer.cur_addr) + "</span>" : ""}` : n.is_local ? "хост" : "—"}</td>
        <td class="small">${si.uptime_s ? Math.round(si.uptime_s / 3600) + " ч" : "—"}</td><td class="small" style="color:${v.error ? "var(--err)" : "inherit"}">${v.error ? esc(v.error) : "✓"}</td></tr>`;
    }).join("");
    $("adminOverview").innerHTML = `
      <div class="grid-2"><div class="tel-hint"><b>Хаб</b> ${esc(hs.hostname || "")} · ${esc(hs.platform || "")} · пользователь ${esc(hs.user || "")}<br>Локальные IP: <span class="mono">${(hn.local_ips || []).join(", ")}</span><br>Публичный IP: <span class="mono">${esc(hn.public_ip || "—")}</span> — ${esc(this.geoText(host.geo))}${hn.wifi_ssid ? "<br>Wi-Fi: " + esc(hn.wifi_ssid) : ""}</div>
      <div class="tel-hint"><b>Tailscale</b> ${ts.available ? `${esc((ts.self || {}).hostname || "")} · ${esc((ts.self || {}).dns_name || "")} · сеть ${esc(ts.magic_dns || "")} · состояние ${esc(ts.backend_state || "")} · пиров: ${(ts.peers || []).length}` : "недоступен (" + esc(ts.error || "не установлен") + ")"}</div></div>
      <div class="small muted" style="margin-top:10px">Узлы кластера</div>
      <div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Узел</th><th>Хост</th><th>Локальные IP</th><th>Шлюз / Wi-Fi</th><th>Публичный IP / регион</th><th>Tailscale</th><th>Uptime</th><th>Агент</th></tr></thead><tbody>${nodes}</tbody></table></div>
      ${peers ? `<div class="small muted" style="margin-top:10px">Все устройства в Tailscale-сети</div><div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Устройство</th><th>ОС</th><th>Tailscale IP</th><th>Статус</th><th>Прямой адрес</th><th>Relay</th><th>Был</th><th>DNS</th></tr></thead><tbody>${peers}</tbody></table></div>` : ""}`;
  },
  selectNode(k) { this.node = k; this.render(); if (this.overview) this.renderOverview(); this.loadNode(k); },
  async loadNode(key, force) {
    try { this.nodeInfo = await api("/api/admin/node?node_key=" + key); this.renderNode(); } catch (e) { $("adminNode").innerHTML = `<div class="tel-hint">${esc(e.message)}</div>`; }
  },
  renderNode() {
    const v = this.nodeInfo; const n = v.node; const si = v.sysinfo || {}; const lms = si.lms || {}; const net = si.network || {};
    if (si.error) { $("adminNode").innerHTML = `<div class="tel-hint">❌ ${esc(si.error)}<br><br>Для удалённого узла на нём должен быть запущен <code>node_agent.py</code> (порт ${State.settings?.telemetry?.agent_port || 8766}); хаб обращается к <span class="mono">${esc(n.agent_url || "")}</span>.</div>`; return; }
    const loaded = (lms.loaded || []).map(m => `<span class="chip on">${esc(m.modelKey || m.identifier)}${m.contextLength ? " · ctx " + m.contextLength : ""}</span>`).join(" ") || '<span class="muted small">ничего не загружено</span>';
    const models = (lms.models || []).map(m => `<option value="${esc(m.modelKey)}">${esc(m.modelKey)} (${esc(m.paramsString || "")} ${fmtGB(m.sizeBytes)})</option>`).join("");
    const files = ((si.lms && si.lms.models) || []).map(m => m.path).filter(Boolean);
    const procs = (si.processes || []).map(p => `<div class="mono small">${p.pid} · ${esc(p.name)} <span class="muted">${esc((p.cmd || "").slice(0, 140))}</span>${/llama-server/i.test(p.name || "") ? ` <button class="btn sm danger" onclick="Admin.exec('llama_server_stop',{pid:${p.pid}})">stop</button>` : ""}</div>`).join("") || '<span class="muted small">нет</span>';
    const disks = (si.disks || []).map(d => `<span class="small">${esc(d.path)}: свободно <b>${d.free_gb}</b> из ${d.total_gb} ГБ</span>`).join(" · ");
    const launch = n.launch || {};
    $("adminNode").innerHTML = `
      <div class="grid-2">
        <div class="tel-hint"><b>${n.avatar} ${esc(n.name)}</b> · ${esc(si.hostname || "")} · ${esc(si.platform || "")}<br>CPU ${esc((si.cpu || {}).name || "")} (${(si.cpu || {}).cores || "?"} ядер) · ОЗУ ${(si.ram || {}).total_gb || "?"} ГБ · GPU: ${(si.gpus || []).map(g => esc(g.name) + " " + Math.round((g.mem_total_mb || 0) / 1024) + " ГБ").join(", ") || "—"}<br>
          IP: <span class="mono">${(net.local_ips || []).join(", ")}</span> · шлюз <span class="mono">${esc(net.gateway || "—")}</span> ${net.wifi_ssid ? "· Wi-Fi " + esc(net.wifi_ssid) : ""} · публичный <span class="mono">${esc(net.public_ip || "—")}</span><br>${esc(this.geoText(v.geo))}<br>${disks}</div>
        <div class="tel-hint"><b>LM Studio</b>: ${lms.path ? `lms найден · моделей на диске ${(lms.models || []).length} · каталог <span class="mono">${esc(lms.models_dir || "")}</span>` : "lms CLI не найден на узле"}<br>Загружено в память: ${loaded}<br><div style="margin-top:6px"><b>Процессы</b>:<br>${procs}</div></div>
      </div>
      <div class="row wrap" style="margin-top:10px">
        <button class="btn sm" onclick="Admin.exec('lms_ps')">lms ps</button><button class="btn sm" onclick="Admin.exec('lms_ls')">lms ls</button>
        <button class="btn sm" onclick="Admin.exec('lms_server',{cmd:'status'})">сервер: статус</button><button class="btn sm" onclick="Admin.exec('lms_server',{cmd:'start'})">сервер: старт</button><button class="btn sm danger" onclick="Admin.exec('lms_server',{cmd:'stop'})">сервер: стоп</button>
        <button class="btn sm danger" onclick="UI.confirm('Выгрузить все модели','Освободить память узла ${esc(n.name)}?',()=>Admin.exec('lms_unload',{all:true}))">⏏ выгрузить все</button>
        <button class="btn sm" onclick="Admin.exec('nvidia_smi')">nvidia-smi</button><button class="btn sm" onclick="Admin.exec('disk_usage')">диски</button><button class="btn sm" onclick="Admin.exec('list_models_dir')">файлы моделей</button><button class="btn sm" onclick="Admin.exec('tailscale_status')">tailscale</button><button class="btn sm" onclick="Admin.exec('processes')">процессы</button>
      </div>
      <div class="grid-2" style="margin-top:10px">
        <div class="tel-hint"><b>Загрузить модель в память (lms load)</b><div class="row wrap" style="margin-top:6px"><select id="admLoadModel" class="sm grow mono">${models || '<option value="">нет данных lms ls</option>'}</select><input type="number" id="admLoadCtx" class="sm" style="width:90px" placeholder="ctx" value="8192"><select id="admLoadGpu" class="sm"><option value="">gpu авто</option><option value="max">gpu max</option><option value="off">cpu</option></select><button class="btn sm primary" onclick="Admin.exec('lms_load',{model:$('admLoadModel').value,ctx:+$('admLoadCtx').value||undefined,gpu:$('admLoadGpu').value||undefined})">▶ Загрузить</button></div>
          <div class="row wrap" style="margin-top:6px"><select id="admUnload" class="sm grow mono">${(lms.loaded || []).map(m => `<option value="${esc(m.identifier || m.modelKey)}">${esc(m.identifier || m.modelKey)}</option>`).join("")}</select><button class="btn sm" onclick="Admin.exec('lms_unload',{identifier:$('admUnload').value})">⏏ Выгрузить</button></div></div>
        <div class="tel-hint"><b>llama-server</b> (отдельный inference-процесс, напр. для второго GPU)<div class="row wrap" style="margin-top:6px"><select id="admLlamaModel" class="sm grow mono">${files.map(f => `<option value="${esc(f)}" ${launch.model_path === f ? "selected" : ""}>${esc(f)}</option>`).join("")}</select></div>
          <div class="row wrap" style="margin-top:6px"><input type="number" id="admLlamaPort" class="sm" style="width:80px" placeholder="порт" value="${launch.port || 1235}"><input type="number" id="admLlamaCtx" class="sm" style="width:80px" placeholder="ctx" value="${launch.ctx || 8192}"><input type="number" id="admLlamaNgl" class="sm" style="width:70px" placeholder="ngl" value="${launch.ngl ?? 99}"><input type="text" id="admLlamaDev" class="sm" style="width:90px" placeholder="device (Vulkan1)" value="${esc(launch.device || "")}"><input type="text" id="admLlamaAlias" class="sm" style="width:130px" placeholder="alias" value="${esc(launch.alias || "")}">
          <button class="btn sm primary" onclick="Admin.llamaStart()">▶ Запустить</button><button class="btn sm danger" onclick="Admin.exec('llama_server_stop',{port:+$('admLlamaPort').value})">■ Остановить</button></div><div class="small muted" style="margin-top:4px">Параметры запоминаются в настройках узла.</div></div>
      </div>`;
  },
  async llamaStart() {
    const args = { model_path: $("admLlamaModel").value, port: +$("admLlamaPort").value, ctx: +$("admLlamaCtx").value, ngl: +$("admLlamaNgl").value, device: $("admLlamaDev").value.trim(), alias: $("admLlamaAlias").value.trim() };
    await api("/api/node/update", { node_key: this.node, launch: args }).catch(() => {});
    await this.exec("llama_server_start", args);
  },
  async exec(action, args, silent) {
    const entry = { t: new Date(), node: this.node, action, args, status: "…" }; this.log.unshift(entry); this.renderLog();
    try {
      const r = await api("/api/admin/exec", { node_key: this.node, action, args: args || {} }); const res = r.result || {};
      entry.status = res.ok ? "ok" : "err"; entry.output = res.output; entry.error = res.error;
      if (!silent) UI.toast(res.ok ? `✅ ${action}` : `❌ ${action}: ${esc(res.error || "")}`, res.ok ? "ok" : "err", 4000);
      this.renderLog(); this.loadNode(this.node, true);
      if (typeof Models !== "undefined") Models.catalog = {};
      return res;
    } catch (e) { entry.status = "err"; entry.error = e.message; this.renderLog(); }
  },
  renderLog() {
    const box = $("adminLog"); if (!box) return;
    box.innerHTML = this.log.slice(0, 30).map((e, i) => { const out = e.output == null ? "" : typeof e.output === "string" ? e.output : JSON.stringify(e.output, null, 1); return `<div class="log-entry"><div class="row wrap"><span class="mono small muted">${e.t.toLocaleTimeString("ru-RU")}</span><b>${esc(State.nodes[e.node]?.name || e.node)}</b><span class="mono small">${esc(e.action)} ${esc(JSON.stringify(e.args || {}))}</span><span class="badge ${e.status === "ok" ? "ok" : e.status === "err" ? "err" : ""}">${esc(e.status)}</span></div>${e.error ? `<div class="small" style="color:var(--err)">${esc(e.error)}</div>` : ""}${out ? `<details data-k="l${i}"><summary class="small muted">вывод (${out.length} симв.)</summary><pre class="mono small" style="white-space:pre-wrap;max-height:300px;overflow:auto">${esc(out.slice(0, 20000))}</pre></details>` : ""}</div>`; }).join("") || '<div class="muted small">пусто</div>';
  },
  async ask() {
    const text = $("admCmd").value.trim(); if (!text) return;
    $("admProposal").innerHTML = '<div class="active-banner"><div class="spinner"></div>LLM разбирает команду…</div>';
    try {
      const r = await api("/api/admin/ask", { node_key: this.node, text, via_node: $("admVia").value || undefined });
      if (r.error) { $("admProposal").innerHTML = `<div class="tel-hint">❌ ${esc(r.error)}</div>`; return; }
      const p = r.proposal || {};
      if (!p.action) { $("admProposal").innerHTML = `<div class="tel-hint">🤔 Действие не выбрано: ${esc(p.explanation || r.raw || "модель не вернула JSON")}</div>`; return; }
      const argsJson = JSON.stringify(p.args || {}, null, 1);
      $("admProposal").innerHTML = `<div class="proposal"><div class="row wrap"><span class="badge">${esc(p.action)}</span><span class="small">${esc(p.explanation || "")}</span><span class="small muted">уверенность ${p.confidence != null ? Math.round(p.confidence * 100) + "%" : "?"} · разобрал: ${esc(State.nodes[r.executor]?.name || r.executor)} за ${fmtMs(r.latency_ms)}</span></div>
        <div class="row" style="margin-top:6px"><textarea id="admArgs" class="mono sm grow" style="min-height:60px">${esc(argsJson)}</textarea></div>
        <div class="row" style="margin-top:6px"><button class="btn primary" onclick="Admin.execProposal('${esc(p.action)}')">▶ Выполнить на ${esc(State.nodes[this.node].name)}</button><button class="btn" onclick="$('admProposal').innerHTML=''">Отмена</button><span class="small muted">аргументы можно поправить перед запуском</span></div></div>`;
    } catch (e) { $("admProposal").innerHTML = `<div class="tel-hint">❌ ${esc(e.message)}</div>`; }
  },
  async execProposal(action) {
    let args = {}; try { args = JSON.parse($("admArgs").value || "{}"); } catch (e) { UI.toast("Аргументы — невалидный JSON", "err"); return; }
    $("admProposal").innerHTML = ""; $("admCmd").value = "";
    await this.exec(action, args);
  },
};
