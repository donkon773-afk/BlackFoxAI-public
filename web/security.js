/* BlackFox AI Workstation — authentication gate + security settings */
"use strict";

// ---------------------------------------------------------------------------
// Auth: token storage, login / first-run setup overlay
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Native: мост с оболочкой приложения (Tauri). Интерфейс живёт во фрейме
// оболочки на другом origin (tauri://localhost ↔ http://127.0.0.1:<control>),
// поэтому прямое присваивание window.__BF_NATIVE__ снаружи не работает и
// приходит слишком поздно. Контракт:
//   фрейм  → оболочка: {type:"bf-ready"} при загрузке,
//                      {type:"bf-auth-required", ...status} — хаб требует вход,
//                      {type:"bf-logout"} — пользователь вышел из интерфейса;
//   оболочка → фрейм:  {type:"bf-native", token, role, mode, controlPort}.
// window.__BF_NATIVE__ тоже принимается, если оболочка успела его задать
// (тот же origin). В браузере и PWA (нет родителя) мост молчит.
// ---------------------------------------------------------------------------
const Native = {
  ctx: null, waiting: false,          // waiting — уже попросили оболочку показать вход
  get active() { return !!this.ctx; },
  get mode() { return this.ctx ? this.ctx.mode : null; },
  init() {
    const w = window.__BF_NATIVE__;
    if (w && typeof w === "object" && w.token) this.apply(w);
    window.addEventListener("message", (e) => {
      if (e.source !== window.parent || !e.data || e.data.type !== "bf-native") return;
      this.apply(e.data);
    });
    if (window.parent === window || this.active) return Promise.resolve();
    // Во фрейме: просим контекст и ждём ответ, чтобы не показать лишний экран входа.
    this.send("bf-ready");
    return new Promise((res) => {
      const done = () => { window.removeEventListener("message", h); res(); };
      const h = (e) => { if (e.source === window.parent && e.data && e.data.type === "bf-native") done(); };
      window.addEventListener("message", h);
      setTimeout(done, 600);
    });
  },
  apply(d) {
    const wasWaiting = this.waiting;
    this.ctx = { token: d.token || null, role: d.role || null, mode: d.mode || null, controlPort: d.controlPort || null };
    this.waiting = false;
    window.__BF_NATIVE__ = this.ctx;
    document.documentElement.dataset.native = this.ctx.mode || "app";
    // Источник правды о сессии — оболочка: токен держим только в памяти,
    // в localStorage фрейма ничего не остаётся («запомнить» решает оболочка).
    try { localStorage.removeItem("bf.auth"); } catch (e) {}
    Auth.token = this.ctx.token; Auth.role = this.ctx.role;
    if (this.ctx.token && (Auth.shown || wasWaiting)) { Auth.hide(); Poll.firstLoad = true; Poll.tick(); }
  },
  send(type, extra) {
    if (window.parent === window) return;
    try { window.parent.postMessage({ type, ...(extra || {}) }, "*"); } catch (e) {}
  },
};

const Auth = {
  token: null, role: null, status: null, shown: false,
  load() {
    if (Native.active) { this.token = Native.ctx.token; this.role = Native.ctx.role; return this.token; }
    try { this.token = localStorage.getItem("bf.auth") || null; } catch (e) {}
    return this.token;
  },
  save(t) {
    this.token = t || null;
    if (Native.active) { Native.ctx.token = this.token; return; }   // в приложении не персистим
    try { t ? localStorage.setItem("bf.auth", t) : localStorage.removeItem("bf.auth"); } catch (e) {}
  },
  headers() { const t = (Native.ctx && Native.ctx.token) || this.token; return t ? { "X-BF-Auth": t } : {}; },

  async refreshStatus() {
    try { this.status = await api("/api/auth/status", {}); return this.status; } catch (e) { return null; }
  },
  // called by api() when the hub answers 401; usedToken — токен, с которым
  // ушёл отказанный запрос. Если он уже не текущий (запрос стартовал до того,
  // как оболочка передала сессию), это не повод сбрасывать вход.
  async required(usedToken) {
    if (usedToken !== undefined && usedToken !== (this.token || null)) return;
    this.save(null);
    await this.refreshStatus();
    this.show();
  },
  show() {
    if (this.shown) return;
    const s = this.status || {};
    // В приложении вход делает оболочка — свой экран не рисуем, а сообщаем ей.
    if (Native.active) {
      Native.ctx.token = null;
      if (!Native.waiting) { Native.waiting = true; Native.send("bf-auth-required", { needs_setup: !!s.needs_setup, can_setup: !!s.can_setup, enabled: !!s.enabled }); }
      return;
    }
    this.shown = true;
    const setup = s.needs_setup;
    const canSetup = s.can_setup;
    document.body.insertAdjacentHTML("beforeend", `<div class="auth-overlay" id="authOverlay"><div class="auth-card">
      <div class="auth-brand"><span class="brand-logo">🦊</span><div><div class="brand-title">BLACKFOX AI</div><div class="brand-sub">Кластер LLM-узлов</div></div></div>
      ${setup ? (canSetup ? `
        <h3>🔒 Первичная настройка защиты</h3>
        <div class="small muted">Задайте пароли. После этого хаб будет требовать вход у всех, а команды узлам начнут подписываться секретным ключом кластера.</div>
        <div class="field"><label>Пароль администратора (минимум 8 символов)</label><input type="password" id="auSetupAdmin" autocomplete="new-password"><span class="hint">Полный доступ: настройки, модели, управление узлами</span></div>
        <div class="field"><label>Пароль пользователя (необязательно, минимум 6)</label><input type="password" id="auSetupUser" autocomplete="new-password"><span class="hint">Только чат и просмотр. Оставьте пустым — тогда вход будет лишь у администратора</span></div>
        <div class="auth-err" id="authErr"></div>
        <button class="btn primary" id="authBtn" onclick="Auth.doSetup()">Включить защиту</button>
        <div class="small muted">Забыли пароль? На хост-машине: <code>python hub_server.py --reset-auth</code></div>`
        : `<h3>🔒 Защита не настроена</h3>
        <div class="small">Первичная настройка возможна только с хост-машины кластера (${esc(s.ip || "")} — не она). Откройте интерфейс на машине, где запущен хаб, и задайте пароли.</div>`)
      : `<h3>🔒 Вход</h3>
        <div class="small muted">Хаб защищён паролем. Введите пароль администратора или пользователя.</div>
        <div class="field"><label>Пароль</label><input type="password" id="auPass" autocomplete="current-password" onkeydown="if(event.key==='Enter')Auth.doLogin()"></div>
        <div class="field"><label>Ваше имя (для списка подключений)</label><input type="text" id="auName" value="${esc(Client.name || "")}"></div>
        <div class="auth-err" id="authErr"></div>
        <button class="btn primary" id="authBtn" onclick="Auth.doLogin()">Войти</button>
        <div class="small muted">Подключение: ${esc(location.host)}${s.tls ? " · TLS включён 🔐" : ""}</div>`}
      </div></div>`);
    setTimeout(() => { const el = $("auPass") || $("auSetupAdmin"); if (el) el.focus(); }, 50);
  },
  hide() { const el = $("authOverlay"); if (el) el.remove(); this.shown = false; },
  err(msg) { const el = $("authErr"); if (el) el.textContent = msg || ""; },
  busy(on, label) { const b = $("authBtn"); if (b) { b.disabled = on; b.textContent = on ? "…" : label; } },

  async doLogin() {
    const pass = ($("auPass") || {}).value || "";
    if (!pass) return this.err("Введите пароль");
    const name = ($("auName") || {}).value || Client.name;
    this.busy(true, "Войти"); this.err("");
    try {
      const r = await api("/api/auth/login", { password: pass, name }, { noAuthRetry: true });
      this.save(r.token); this.role = r.role;
      if (name && name !== Client.name) { Client.setName(name); Users.renderMe(); }
      this.hide(); UI.toast(`Вход выполнен: ${r.role === "admin" ? "администратор" : "пользователь"}`, "ok");
      Poll.firstLoad = true; Poll.tick();
    } catch (e) { this.err(e.message); this.busy(false, "Войти"); }
  },
  async doSetup() {
    const a = ($("auSetupAdmin") || {}).value || "", u = ($("auSetupUser") || {}).value || "";
    if (a.length < 8) return this.err("Пароль администратора: минимум 8 символов");
    this.busy(true, "Включить защиту"); this.err("");
    try {
      const r = await api("/api/auth/setup", { admin_password: a, user_password: u }, { noAuthRetry: true });
      this.save(r.token); this.role = "admin";
      this.hide(); UI.toast("Защита включена. Команды узлам теперь подписываются.", "ok", 6000);
      Poll.firstLoad = true; Poll.tick();
    } catch (e) { this.err(e.message); this.busy(false, "Включить защиту"); }
  },
  async logout() {
    try { await api("/api/auth/logout", {}); } catch (e) {}
    this.save(null); this.role = null;
    if (Native.active) { Native.ctx.token = null; Native.send("bf-logout"); return; }
    await this.refreshStatus(); this.show();
  },
};

// ---------------------------------------------------------------------------
// Settings → Security
// ---------------------------------------------------------------------------
const Security = {
  data: null, audit: [],
  async render() {
    const box = $("s-security"); if (!box) return;
    if (!State.isAdmin) { box.innerHTML = '<h2>Безопасность</h2><div class="tel-hint">Раздел доступен администратору.</div>'; return; }
    const s = (State.settings || {}).security || {};
    const st = Auth.status || await Auth.refreshStatus() || {};
    box.innerHTML = `<h2>Безопасность</h2>
      <div class="desc">Защита от посторонних подключений и перехвата управления узлами.</div>
      <div class="sec-status" id="secStatus">${this.statusHtml(s, st)}</div>

      <div class="card" style="margin-top:12px"><div class="card-h">🔑 Доступ по паролю</div><div class="card-b grid-3">
        <div class="field"><label>Вход обязателен</label><select id="secEnabled"><option value="true" ${s.enabled ? "selected" : ""}>включён</option><option value="false" ${!s.enabled ? "selected" : ""}>выключен (опасно)</option></select><span class="hint">Без входа хаб доступен любому, кто дотянется до порта</span></div>
        <div class="field"><label>Срок сессии, часов</label><input type="number" id="secHours" min="1" max="720" value="${s.session_hours ?? 72}"></div>
        <div class="field"><label>Блокировка IP: попыток / минут</label><div class="row"><input type="number" id="secFails" min="3" max="50" value="${s.lockout_fails ?? 8}" style="width:70px"><input type="number" id="secLockMin" min="1" max="1440" value="${s.lockout_minutes ?? 15}" style="width:80px"></div></div>
        <div class="field"><label>Сменить пароль</label><select id="secPwWhich"><option value="user">пользователя</option><option value="admin">администратора</option></select></div>
        <div class="field"><label>Новый пароль</label><input type="password" id="secPwNew" autocomplete="new-password" placeholder="пусто = убрать пароль пользователя"></div>
        <div class="field"><label>Текущий пароль администратора</label><div class="row"><input type="password" id="secPwCur" autocomplete="current-password" class="grow"><button class="btn sm" onclick="Security.changePassword()">Сменить</button></div></div>
      </div></div>

      <div class="card" style="margin-top:12px"><div class="card-h">🌐 Сеть и транспорт</div><div class="card-b grid-3">
        <div class="field"><label>Откуда принимать подключения</label><select id="secRestrict"><option value="true" ${s.restrict_network !== false ? "selected" : ""}>только приватные сети (Tailscale/LAN/localhost)</option><option value="false" ${s.restrict_network === false ? "selected" : ""}>отовсюду</option></select></div>
        <div class="field"><label>Дополнительные сети (CIDR)</label><input type="text" id="secCidrs" class="mono" value="${esc((s.allow_cidrs || []).join(", "))}" placeholder="например 203.0.113.7/32"></div>
        <div class="field"><label>Слушать интерфейс</label><select id="secBind"><option value="0.0.0.0" ${s.bind === "0.0.0.0" || !s.bind ? "selected" : ""}>все интерфейсы</option><option value="tailscale" ${s.bind === "tailscale" ? "selected" : ""}>только Tailscale</option><option value="127.0.0.1" ${s.bind === "127.0.0.1" ? "selected" : ""}>только эта машина</option></select><span class="hint">Смена требует перезапуска хаба</span></div>
        <div class="field" style="grid-column: span 3"><label>TLS — шифрование канала браузер ↔ хаб</label>
          <div class="row wrap"><span class="badge ${s.tls?.enabled ? "ok" : "warn"}">${s.tls?.enabled ? "включён" : "выключен — браузер пишет «Не защищено»"}</span>
            <button class="btn sm primary" onclick="Security.tlsCert()">① Сертификат Tailscale (без предупреждений)</button>
            <button class="btn sm" onclick="Security.tlsSelfSigned()">② Самоподписанный (работает сразу)</button>
            ${s.tls?.cert ? `<button class="btn sm" onclick="Security.downloadCert()">⬇ Сертификат для устройств</button>` : ""}
            <button class="btn sm danger" onclick="Security.tlsOff()" ${s.tls?.enabled ? "" : "disabled"}>Выключить</button></div>
          <span class="hint">Внутри Tailscale трафик и так шифруется WireGuard, но браузер об этом не знает и помечает http как незащищённый. <b>①</b> даёт настоящий сертификат и чистый замок — требуется один раз включить HTTPS Certificates в админке тейлнета. <b>②</b> шифрует канал немедленно, но браузер будет предупреждать, пока сертификат не установлен на устройстве (кнопка «Сертификат для устройств»).</span>
          ${s.tls?.cert ? `<span class="hint mono">${esc(s.tls.cert)}</span>` : ""}</div>
      </div></div>

      <div class="card" style="margin-top:12px"><div class="card-h">🛡 Управление узлами</div><div class="card-b">
        <div class="row wrap"><div class="field grow"><label>Подпись команд узлам (HMAC-SHA256 + метка времени + nonce)</label>
          <select id="secSign"><option value="true" ${s.sign_agents !== false ? "selected" : ""}>включена</option><option value="false" ${s.sign_agents === false ? "selected" : ""}>выключена</option></select>
          <span class="hint">Агент выполняет команду, только если она подписана секретом кластера. Подмена тела запроса и повтор перехваченной команды отбиваются.</span></div>
          <div class="field"><label>Секрет кластера</label><span class="badge ${s.agent_secret_set ? "ok" : "err"}">${s.agent_secret_set ? "задан" : "не задан"}</span></div></div>
        <div class="row wrap" style="margin-top:8px"><button class="btn" onclick="Security.checkAgents()">🔍 Проверить каналы к узлам</button><button class="btn danger" onclick="Security.rotateSecret()">♻ Сменить секрет кластера</button></div>
        <div id="secAgents" style="margin-top:8px"></div>
      </div></div>

      <div class="card" style="margin-top:12px"><div class="card-h">👤 Активные сессии<span class="grow"></span><button class="btn sm" onclick="Security.loadSessions()">↻</button><button class="btn sm danger" onclick="Security.revokeAll()">Отозвать все, кроме моей</button></div><div class="card-b" id="secSessions" style="padding:0"></div></div>

      <div class="card" style="margin-top:12px"><div class="card-h">📜 Журнал безопасности<span class="grow"></span><button class="btn sm" onclick="Security.loadAudit()">↻</button></div><div class="card-b" id="secAudit" style="padding:0"></div></div>

      <div class="row" style="margin-top:12px"><button class="btn primary" onclick="Security.save()">💾 Сохранить настройки безопасности</button><button class="btn" onclick="Auth.logout()">Выйти из системы</button></div>`;
    this.loadSessions(); this.loadAudit();
  },
  statusHtml(s, st) {
    const items = [
      [s.enabled, "Вход по паролю", s.enabled ? "включён" : "ВЫКЛЮЧЕН — доступ без пароля"],
      [s.restrict_network !== false, "Сетевой фильтр", s.restrict_network !== false ? "только приватные сети" : "принимаются любые адреса"],
      [!!s.agent_secret_set && s.sign_agents !== false, "Подпись команд узлам", s.agent_secret_set ? (s.sign_agents !== false ? "включена" : "секрет есть, подпись выключена") : "секрет не задан"],
      [!!(s.tls && s.tls.enabled), "TLS браузер ↔ хаб", s.tls && s.tls.enabled ? "включён" : "выключен (в Tailscale трафик всё равно шифруется)"],
      [s.bind !== "0.0.0.0" && !!s.bind, "Привязка порта", s.bind === "tailscale" ? "только Tailscale" : s.bind === "127.0.0.1" ? "только localhost" : "все интерфейсы"],
    ];
    return items.map(([good, name, detail]) => `<div class="sec-item ${good ? "good" : "bad"}"><span>${good ? "✅" : "⚠️"}</span><b>${esc(name)}</b><span class="muted small">${esc(detail)}</span></div>`).join("");
  },
  async save() {
    const body = {
      enabled: $("secEnabled").value === "true", session_hours: +$("secHours").value || 72,
      lockout_fails: +$("secFails").value || 8, lockout_minutes: +$("secLockMin").value || 15,
      restrict_network: $("secRestrict").value === "true", allow_cidrs: $("secCidrs").value,
      bind: $("secBind").value, sign_agents: $("secSign").value === "true",
    };
    try {
      const r = await api("/api/security/update", body);
      State.settings = r.settings; UI.toast("Настройки безопасности сохранены" + (r.restart_required ? " — привязка порта применится после перезапуска хаба" : ""), "ok", 5000);
      this.render();
    } catch (e) { UI.toast("Ошибка: " + esc(e.message), "err"); }
  },
  async changePassword() {
    const which = $("secPwWhich").value, np = $("secPwNew").value, cur = $("secPwCur").value;
    if (!cur) return UI.toast("Введите текущий пароль администратора", "warn");
    try {
      await api("/api/auth/password", { which, new_password: np, current_password: cur });
      $("secPwNew").value = ""; $("secPwCur").value = "";
      UI.toast(`Пароль ${which === "admin" ? "администратора" : "пользователя"} ${np ? "изменён" : "убран"}`, "ok");
      await Auth.refreshStatus(); this.render();
    } catch (e) { UI.toast("Ошибка: " + esc(e.message), "err", 5000); }
  },
  async loadSessions() {
    try {
      const r = await api("/api/auth/sessions", {});
      $("secSessions").innerHTML = `<table class="tbl"><thead><tr><th>Роль</th><th>Имя</th><th>IP</th><th>Устройство</th><th>Вход</th><th>Активность</th><th>Истекает</th><th></th></tr></thead><tbody>${
        (r.sessions || []).map(s => `<tr><td><span class="badge ${s.role === "admin" ? "admin-badge" : ""}">${s.role}</span>${s.id === r.current ? ' <span class="small muted">(вы)</span>' : ""}</td><td>${esc(s.name || "—")}</td><td class="mono small">${esc(s.ip)}</td><td class="small ua">${esc(s.ua || "")}</td><td class="small">${fmtDate(s.created)}</td><td class="small">${fmtDate(s.last)}</td><td class="small">${s.expires_in_h} ч</td>
          <td>${s.id !== r.current ? `<button class="btn ghost sm danger" onclick="Security.revoke('${s.id}')">Отозвать</button>` : ""}</td></tr>`).join("") || '<tr><td colspan="8" class="muted small">нет активных сессий</td></tr>'}</tbody></table>`;
    } catch (e) { $("secSessions").innerHTML = `<div class="muted small" style="padding:10px">${esc(e.message)}</div>`; }
  },
  async revoke(id) { await api("/api/auth/revoke", { id }); UI.toast("Сессия отозвана", "ok"); this.loadSessions(); },
  revokeAll() { UI.confirm("Отозвать сессии", "Завершить все сессии, кроме текущей? Всем придётся войти заново.", async () => { await api("/api/auth/revoke", { all: true, keep_me: true }); UI.toast("Сессии отозваны", "ok"); this.loadSessions(); }, "Отозвать"); },
  async loadAudit() {
    try {
      const r = await api("/api/security/audit", { limit: 120 });
      const label = { login_ok: ["вход", "ok"], login_failed: ["неудачный вход", "err"], setup_complete: ["защита включена", "ok"], setup_denied: ["попытка настройки извне", "err"],
        logout: ["выход", ""], denied_admin_route: ["отказ: не администратор", "warn"], blocked_network: ["заблокирована сеть", "err"], session_revoked: ["сессия отозвана", ""],
        sessions_revoked_all: ["все сессии отозваны", ""], password_changed: ["смена пароля", "ok"], agent_secret_rotated: ["смена секрета кластера", "warn"],
        security_settings: ["изменены настройки", ""], tls_cert_issued: ["выпущен TLS-сертификат", "ok"], tls_settings: ["настройки TLS", ""], hub_start: ["запуск хаба", ""],
        installer_served: ["выдан установщик агента", ""], installer_denied: ["отказ в установщике", "warn"], auth_reset_cli: ["сброс паролей из консоли", "warn"] };
      $("secAudit").innerHTML = `<table class="tbl"><thead><tr><th>Время</th><th>Событие</th><th>IP</th><th>Роль</th><th>Детали</th></tr></thead><tbody>${
        (r.events || []).map(e => { const [txt, cls] = label[e.event] || [e.event, ""]; return `<tr><td class="mono small">${fmtDate(e.ts)}</td><td><span class="badge ${cls}">${esc(txt)}</span></td><td class="mono small">${esc(e.ip || "—")}</td><td class="small">${esc(e.role || "")}</td><td class="small muted">${esc((e.detail || "").slice(0, 90))}</td></tr>`; }).join("") || '<tr><td colspan="5" class="muted small">пусто</td></tr>'}</tbody></table>`;
    } catch (e) { $("secAudit").innerHTML = `<div class="muted small" style="padding:10px">${esc(e.message)}</div>`; }
  },
  async checkAgents() {
    $("secAgents").innerHTML = '<div class="active-banner"><div class="spinner"></div>Проверяю каналы управления…</div>';
    try {
      const r = await api("/api/security/agent_check", {});
      $("secAgents").innerHTML = Object.entries(r.nodes || {}).map(([k, v]) => {
        const n = State.nodes[k] || {};
        const bad = v.unsigned_accepted === true;
        return `<div class="sec-item ${v.ok && !bad ? "good" : bad ? "bad" : ""}"><span>${v.ok && !bad ? "✅" : bad ? "🚨" : "⚠️"}</span><b>${n.avatar || ""} ${esc(n.name || k)}</b><span class="small muted">${esc(v.detail)}</span></div>`;
      }).join("") + (r.secret_set ? "" : '<div class="tel-hint">Секрет кластера не задан — команды узлам не подписываются.</div>');
    } catch (e) { $("secAgents").innerHTML = `<div class="tel-hint">${esc(e.message)}</div>`; }
  },
  rotateSecret() {
    UI.confirm("Сменить секрет кластера", "Будет создан новый секрет. Агенты со старым секретом перестанут принимать команды, пока их не переустановят. Продолжить?", async () => {
      const r = await api("/api/security/agent_secret", {});
      UI.info("Новый секрет кластера", `<div class="small">Секрет показан один раз и уже сохранён на хабе. Переустановите агент на каждом удалённом узле — команда сама подставит новый секрет:</div>
        <div class="agent-cmds"><div class="agent-cmd"><span class="os">Windows</span><code id="rsw">${esc(r.install_win)}</code><button class="btn sm" onclick="Agent.copy('rsw',this)">📋</button></div>
        <div class="agent-cmd"><span class="os">macOS / Linux</span><code id="rsu">${esc(r.install_unix)}</code><button class="btn sm" onclick="Agent.copy('rsu',this)">📋</button></div></div>
        <div class="field" style="margin-top:8px"><label>Секрет (если ставите агент вручную: <code>--secret &lt;секрет&gt;</code>)</label><div class="agent-cmd"><code id="rss">${esc(r.secret)}</code><button class="btn sm" onclick="Agent.copy('rss',this)">📋</button></div></div>`, true);
      this.render();
    }, "Сменить");
  },
  async tlsCert() {
    UI.info("TLS", '<div class="active-banner"><div class="spinner"></div>Запрашиваю сертификат у Tailscale…</div>');
    try {
      const r = await api("/api/security/tls", { action: "tailscale_cert" });
      $("modalBody").innerHTML = `<div class="small">✅ Сертификат получен для <b class="mono">${esc(r.dns_name)}</b>.<br>Файл: <span class="mono">${esc(r.cert)}</span><br><br>Перезапустите хаб, после чего интерфейс будет доступен по адресу:<br><b class="mono">${esc(r.url)}</b></div>`;
      const st = await api("/api/settings"); State.settings = st.settings; this.render();
    } catch (e) {
      $("modalBody").innerHTML = `<div class="tel-hint">❌ ${esc(e.message)}<br><br>
        <b>Что сделать:</b> откройте <a href="https://login.tailscale.com/admin/dns" target="_blank" rel="noopener">login.tailscale.com/admin/dns</a> →
        раздел <b>HTTPS Certificates</b> → <b>Enable HTTPS</b>. Затем вернитесь и нажмите кнопку ① ещё раз — сертификат будет настоящим, без предупреждений браузера.<br><br>
        Не хотите включать — нажмите ② «Самоподписанный»: канал зашифруется сразу.</div>
        <div class="row" style="margin-top:8px"><button class="btn" onclick="Security.tlsSelfSigned()">② Создать самоподписанный</button></div>`;
    }
  },
  async tlsSelfSigned() {
    UI.info("TLS", '<div class="active-banner"><div class="spinner"></div>Создаю самоподписанный сертификат…</div>');
    try {
      const r = await api("/api/security/tls", { action: "self_signed" });
      const i = r.info || {};
      $("modalBody").innerHTML = `<div class="small">✅ Сертификат создан и включён. <b>Перезапустите хаб</b>, после чего интерфейс будет доступен по https:</div>
        <div class="agent-cmds">${(r.urls || []).map((u, n) => `<div class="agent-cmd"><span class="os">адрес ${n + 1}</span><code id="tu${n}">${esc(u)}</code><button class="btn sm" onclick="Agent.copy('tu${n}',this)">📋</button></div>`).join("")}</div>
        <div class="small muted" style="margin-top:8px">Сертификат выписан на: <span class="mono">${esc((i.dns || []).join(", "))}</span><br>и адреса: <span class="mono">${esc((i.ips || []).join(", "))}</span><br>Отпечаток SHA-256: <span class="mono" style="word-break:break-all">${esc(i.fingerprint || "")}</span></div>
        <div class="tel-hint" style="margin-top:10px"><b>Браузер всё равно предупредит</b> — сертификат подписан вами, а не публичным центром. Два способа убрать предупреждение:<br>
          • <b>Надёжно:</b> включить HTTPS Certificates в <a href="https://login.tailscale.com/admin/dns" target="_blank" rel="noopener">админке Tailscale</a> и нажать кнопку ① — сертификат станет настоящим.<br>
          • <b>Быстро:</b> скачать сертификат кнопкой ниже и добавить его в доверенные на каждом устройстве:<br>
          &nbsp;&nbsp;— Windows: двойной клик → «Установить сертификат» → «Локальный компьютер» → «Доверенные корневые центры сертификации»<br>
          &nbsp;&nbsp;— macOS: двойной клик → Связка ключей → найти сертификат → «Всегда доверять»<br>
          &nbsp;&nbsp;— Android/iOS: открыть файл и подтвердить установку профиля</div>
        <div class="row" style="margin-top:8px"><button class="btn" onclick="Security.downloadCert()">⬇ Скачать сертификат</button></div>`;
      const st = await api("/api/settings"); State.settings = st.settings; this.render();
    } catch (e) { $("modalBody").innerHTML = `<div class="tel-hint">❌ ${esc(e.message)}</div>`; }
  },
  async downloadCert() {
    try {
      const r = await api("/api/security/tls", { action: "download_cert" });
      const blob = new Blob([r.pem], { type: "application/x-pem-file" });
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = r.filename || "blackfox-hub.crt"; a.click();
      UI.toast("Сертификат скачан — установите его в доверенные на устройстве", "ok", 5000);
    } catch (e) { UI.toast("Ошибка: " + esc(e.message), "err"); }
  },
  async tlsOff() { await api("/api/security/tls", { action: "set", enabled: false, cert: "", key: "" }); UI.toast("TLS выключен — перезапустите хаб", "warn"); const st = await api("/api/settings"); State.settings = st.settings; this.render(); },
};
