/* BlackFox AI Workstation — «Подключение к платформе» (вкладка Админ).

   Две задачи администратора в одном месте:
     * по какому адресу людям открывать платформу (QR для телефона, требования);
     * как подключить новый рабочий узел: найти устройства тейлнета, выдать
       команду установки агента, добавить найденный LLM-сервер как узел.      */
"use strict";

const Connect = {
  info: null, devices: null, busy: false,

  async load(force) {
    const box = $("adminConnect"); if (!box) return;
    if (!this.info || force) {
      try { this.info = await api("/api/admin/connect", {}); } catch (e) { box.innerHTML = `<div class="tel-hint">${esc(e.message)}</div>`; return; }
    }
    this.render();
  },

  copy(text, btn) {
    navigator.clipboard?.writeText(text);
    if (btn) { const t = btn.textContent; btn.textContent = "✅"; setTimeout(() => btn.textContent = t, 1400); }
    UI.toast("Скопировано: " + esc(text), "ok", 1800);
  },

  qr(url, size) { try { return QR.svg(url, size || 168, "#0b1220", "#ffffff"); } catch (e) { return `<div class="muted small">${esc(e.message)}</div>`; } },

  showQr(url) {
    UI.info("Адрес платформы", `<div style="text-align:center"><div style="display:inline-block;padding:10px;background:#fff;border-radius:12px">${this.qr(url, 300)}</div>
      <div class="mono" style="margin-top:12px;word-break:break-all">${esc(url)}</div>
      <div class="small muted" style="margin-top:8px">Наведите камеру телефона. Устройство должно быть в той же сети Tailscale.</div></div>`);
  },

  render() {
    const i = this.info, p = i.primary;
    const others = i.addresses.filter(a => a.url !== p.url);
    const addrRows = others.map(a => `<div class="row" style="gap:8px;align-items:flex-start"><span class="mono small" style="flex:1;word-break:break-all">${esc(a.url)}</span>
        <span class="small muted" style="flex:1.2">${a.kind === "lan" ? "локальная сеть" : a.kind === "tailscale_ip" ? "Tailscale IP" : ""}${a.note ? " — " + esc(a.note) : ""}</span>
        <button class="btn sm ghost" onclick="Connect.copy('${esc(a.url)}',this)">📋</button></div>`).join("");

    const reqs = [
      { ok: i.tailscale_ok, t: i.tailscale_ok ? `Tailscale на хабе работает, сеть <b>${esc(i.tailnet)}</b>` : "Tailscale на хабе недоступен — адрес по имени работать не будет" },
      { ok: i.tls && !i.self_signed, t: !i.tls ? "TLS выключен — соединение не шифруется" : i.self_signed ? "Сертификат самоподписанный: браузеры предупредят, пока сертификат не добавлен в доверенные" : "Сертификат Let's Encrypt через Tailscale — замок чистый на всех устройствах" },
      { ok: i.auth_enabled, t: i.auth_enabled ? `Вход по паролю включён${i.user_password_set ? "; есть отдельный пароль пользователя" : "; пароль пользователя не задан — все входят как администратор"}` : "Вход по паролю выключен — любой в сети получит доступ" },
    ];

    $("adminConnect").innerHTML = `
      <div class="grid-2" style="gap:14px;align-items:start">
        <div>
          <div class="small muted" style="margin-bottom:6px">📱 АДРЕС ДЛЯ ЛЮДЕЙ И УСТРОЙСТВ</div>
          <div class="row" style="gap:14px;align-items:flex-start">
            <div style="padding:8px;background:#fff;border-radius:10px;cursor:zoom-in;flex:0 0 auto" title="Увеличить" onclick="Connect.showQr('${esc(p.url)}')">${this.qr(p.url, 148)}</div>
            <div style="min-width:0;flex:1">
              <div class="mono" style="font-size:15px;font-weight:700;word-break:break-all;color:var(--info)">${esc(p.url)}</div>
              <div class="small muted" style="margin:4px 0 8px">${esc(p.note || "")}</div>
              <div class="row wrap"><button class="btn sm primary" onclick="Connect.copy('${esc(p.url)}',this)">📋 Скопировать</button>
                <button class="btn sm" onclick="Connect.showQr('${esc(p.url)}')">🔍 QR крупно</button>
                <button class="btn sm" onclick="Connect.shareText()">📤 Инструкция для нового пользователя</button></div>
            </div>
          </div>
          ${addrRows ? `<div class="small muted" style="margin:12px 0 4px">Другие адреса</div><div style="display:flex;flex-direction:column;gap:5px">${addrRows}</div>` : ""}
          <div class="small muted" style="margin:12px 0 4px">Условия подключения</div>
          <div style="display:flex;flex-direction:column;gap:4px">${reqs.map(r => `<div class="sec-item ${r.ok ? "good" : "bad"}"><span>${r.ok ? "✅" : "⚠️"}</span><span>${r.t}</span></div>`).join("")}</div>
          <div class="small muted" style="margin-top:10px;line-height:1.5">Чтобы устройство увидело платформу: на нём установлен Tailscale и выполнен вход в эту же сеть (ваша учётная запись — или чужое устройство добавлено через <b>Share</b> в <a href="https://login.tailscale.com/admin/machines" target="_blank" rel="noopener">админке Tailscale</a>). Пароли задаются в Настройки → 🔒 Безопасность.</div>
        </div>

        <div>
          <div class="row" style="margin-bottom:6px"><span class="small muted">🖥 ПОДКЛЮЧИТЬ РАБОЧИЙ УЗЕЛ</span><span class="grow"></span>
            <button class="btn sm primary" onclick="Connect.discover()" ${this.busy ? "disabled" : ""}>${this.busy ? "⏳ Ищу…" : "🔎 Найти устройства"}</button>
            <button class="btn sm" onclick="Connect.pairCode()" title="Код для приложения BlackFox на новой машине">🔑 Парный код</button>
            <button class="btn sm" onclick="Settings.openAddNode()">➕ По адресу вручную</button></div>
          <div class="small muted" style="line-height:1.5;margin-bottom:8px">Узел = машина в тейлнете, на которой запущен OpenAI-совместимый сервер (LM Studio, llama.cpp, Ollama) и агент <code>node_agent.py</code> (порт ${i.agent_port}). Порядок: <b>1</b> поставить агент командой с одноразовым ключом → <b>2</b> запустить LLM-сервер → <b>3</b> «Добавить как узел».</div>
          <div id="connectDevices">${this.devices ? this.devicesHtml() : '<div class="muted small">Нажмите «Найти устройства» — хаб опросит все машины тейлнета: агент, открытые порты LLM-серверов, уже добавленные узлы.</div>'}</div>
        </div>
      </div>`;
  },

  /** Парный код для приложения: новая машина вводит его в мастере и получает секрет кластера. */
  async pairCode() {
    let r;
    try { r = await api("/api/security/pair_code", { minutes: 10 }); }
    catch (e) { UI.toast("Не удалось выпустить код: " + esc(e.message), "err"); return; }
    UI.info("Парный код для приложения BlackFox", `
      <div style="text-align:center">
        <div class="mono" style="font-size:34px;font-weight:800;letter-spacing:4px;color:var(--info);margin:6px 0 10px">${esc(r.code)}</div>
        <div style="display:inline-block;padding:10px;background:#fff;border-radius:12px">${this.qr(r.link, 200)}</div>
        <div class="small muted" style="margin-top:10px;line-height:1.5">На новой машине: установить приложение BlackFox → мастер первого запуска → «Рабочая станция» → выбрать этот хаб (<span class="mono">${esc(r.hub)}</span>) → ввести код.<br>Код действует <b>${r.expires_in_min} мин</b> и гаснет после использования. Узел появится в Настройки → Узлы выключенным — укажите LLM-сервер и включите.</div>
        <div class="row" style="justify-content:center;margin-top:10px"><button class="btn sm" onclick="Connect.copy('${esc(r.code)}',this)">📋 Код</button><button class="btn sm" onclick="Connect.copy('${esc(r.link)}',this)">📋 Ссылка blackfox://</button></div>
      </div>`);
  },

  async discover() {
    this.busy = true; this.render();
    try { this.devices = await api("/api/admin/discover", {}); }
    catch (e) { UI.toast("Поиск не удался: " + esc(e.message), "err"); }
    this.busy = false; this.render();
  },

  devicesHtml() {
    const d = this.devices; if (!d) return "";
    if (!d.devices.length) return '<div class="tel-hint">В тейлнете нет других устройств. Установите Tailscale на новую машину и войдите в ту же сеть.</div>';
    const rows = d.devices.map(x => {
      const st = x.is_hub ? '<span class="badge">хаб</span>' : x.online ? '<span class="dot online"></span> online' : `<span class="dot offline"></span> ${x.last_seen ? "был " + fmtDate(x.last_seen) : "offline"}`;
      const ag = x.agent ? (x.agent.ok ? `✅ агент v${esc(String(x.agent.version || ""))}<div class="muted" style="font-size:10px">${esc([x.agent.cpu, x.agent.ram_gb ? x.agent.ram_gb + " ГБ" : "", ...(x.agent.gpus || []).map(g => `${g.name} ${g.vram_gb} ГБ`)].filter(Boolean).join(" · "))}</div>` : `⚠️ порт открыт, но: ${esc(x.agent.error || "")}`) : (x.online ? '<span class="muted">нет</span>' : "—");
      const llm = x.llm_ports.length ? x.llm_ports.map(pt => `<div><b>:${pt.port}</b> ${esc(pt.label)}${pt.kind ? " · " + esc(pt.kind) : ""}${pt.error ? ` <span class="muted">(${esc(pt.error)})</span>` : ` · моделей: ${pt.models.length}`}</div>`).join("") : (x.online ? '<span class="muted">не найдено</span>' : "—");
      const nodes = x.nodes.length ? x.nodes.map(k => `<span class="chip on">${State.nodes[k] ? State.nodes[k].avatar + " " + esc(State.nodes[k].name) : esc(k)}</span>`).join(" ") : '<span class="muted">—</span>';
      const mobile = /android|ios/i.test(x.os || "");
      const actions = x.is_hub || mobile ? (mobile ? '<span class="muted small">телефон/планшет — только клиент</span>' : "") : `
        ${!x.agent || !x.agent.ok ? `<button class="btn sm" title="Одноразовый ключ и команда установки агента для этой машины" onclick='Agent.install(${JSON.stringify({ name: x.hostname, agent_url: "http://" + x.ip + ":" + d.agent_port })})'>📡 Агент</button>` : ""}
        ${x.llm_ports.map(pt => `<button class="btn sm primary" onclick='Settings.openAddNode(${JSON.stringify({ endpoint: pt.endpoint, agent_url: "http://" + x.ip + ":" + d.agent_port, name: x.hostname })})'>➕ Узел :${pt.port}</button>`).join(" ")}
        ${x.online && !x.llm_ports.length ? `<button class="btn sm ghost" onclick='Settings.openAddNode(${JSON.stringify({ endpoint: "http://" + x.ip + ":1234", agent_url: "http://" + x.ip + ":" + d.agent_port, name: x.hostname })})'>➕ Узел (адрес вручную)</button>` : ""}`;
      return `<tr><td><b>${esc(x.hostname || "?")}</b><div class="muted" style="font-size:10px">${esc(x.os || "")} · <span class="mono">${esc(x.ip || "")}</span></div></td><td class="small">${st}</td><td class="small">${ag}</td><td class="small">${llm}</td><td class="small">${nodes}</td><td class="small" style="white-space:nowrap">${actions}</td></tr>`;
    }).join("");
    return `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Устройство</th><th>Статус</th><th>Агент</th><th>LLM-сервер</th><th>Узлы</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="small muted" style="margin-top:6px">Обновлено ${esc(d.ts || "")}. Порты проверяются: 1234/1235 LM Studio, 8080 llama-server, 11434 Ollama, 8000 vLLM. Если сервер запущен, но не найден — в его настройках должно быть включено «Serve on local network» (LM Studio) или привязка к 0.0.0.0.</div>`;
  },

  shareText() {
    const i = this.info, p = i.primary;
    const text = `Платформа BlackFox AI Workstation
Адрес: ${p.url}

1. Установите Tailscale (tailscale.com/download) и войдите в сеть ${i.tailnet || "администратора"} — приглашение пришлёт администратор.
2. Откройте адрес выше в браузере. На телефоне можно добавить на домашний экран: Android — меню браузера → «Установить приложение», iPhone — «Поделиться» → «На экран Домой».
3. Пароль для входа спросите у администратора.`;
    UI.info("Инструкция для нового пользователя", `<textarea class="mono" style="width:100%;min-height:190px" readonly>${esc(text)}</textarea>
      <div class="row" style="margin-top:8px"><button class="btn primary" onclick="Connect.copy(${JSON.stringify(text).replace(/'/g, "&#39;")},this)">📋 Скопировать текст</button>${navigator.share ? `<button class="btn" onclick='navigator.share({title:"BlackFox AI Workstation",text:${JSON.stringify(text)}})'>📤 Поделиться</button>` : ""}</div>`);
  },
};
