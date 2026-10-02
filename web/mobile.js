/* BlackFox AI Workstation — мобильный слой.

   Никакой отдельной мобильной версии: это тот же интерфейс и те же объекты
   (UI / Chat / Threads / Telemetry / …), перестроенные под телефон. Контролы
   композера физически переезжают в шторку, а не дублируются, поэтому любая
   функция десктопа доступна и с телефона — и остаётся одна точка правды.   */
"use strict";

const Mobile = {
  mq: null,
  wakeLock: null,
  busyWas: false,
  recog: null,
  installEvent: null,

  on() { return !!(this.mq && this.mq.matches); },

  // -------------------------------------------------------------------------
  init() {
    // тот же порог, что в mobile.css: узкий экран ИЛИ телефон в альбомной ориентации
    this.mq = window.matchMedia("(max-width: 760px), (max-width: 950px) and (max-height: 500px) and (orientation: landscape)");
    this.buildChrome();
    this.hookApp();
    this.gestures();
    this.keyboard();
    this.mq.addEventListener("change", () => this.apply());
    document.addEventListener("visibilitychange", () => { if (!document.hidden) this.wakeIfBusy(); });
    window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); this.installEvent = e; });
    this.apply();
  },

  NAV: [
    { tab: "chat", icon: "💬", label: "Чат" },
    { tab: "telemetry", icon: "📊", label: "Узлы" },
    { tab: "prompts", icon: "🧠", label: "Роли" },
    { tab: "models", icon: "🧮", label: "Модели" },
  ],
  TAB_TITLE: { chat: "Чат", telemetry: "Телеметрия", prompts: "Промпт-роли", models: "Модели",
               users: "Пользователи", help: "Справка", settings: "Настройки", admin: "Админ" },
  MODE: { single: "🎯 Один узел", all: "⚡ Все узлы", pipeline: "🔄 Конвейер", hybrid: "🧩 Гибрид", council: "🤝 Консилиум" },

  // ---- разметка, которой нет в десктопной версии ---------------------------
  buildChrome() {
    const app = $("app");

    const scrim = document.createElement("div");
    scrim.className = "drawer-scrim";
    scrim.addEventListener("click", () => this.closeDrawer());
    app.appendChild(scrim);

    const title = document.createElement("div");
    title.className = "m-title"; title.id = "mTitle";
    const bar = document.querySelector(".topbar");
    bar.insertBefore(title, bar.querySelector(".topbar-right"));

    const nav = document.createElement("nav");
    nav.className = "mobile-nav"; nav.id = "mobileNav";
    nav.innerHTML = this.NAV.map(n =>
      `<button data-tab="${n.tab}" onclick="Mobile.go('${n.tab}')"><i>${n.icon}</i>${n.label}<span class="nav-dot"></span></button>`).join("") +
      `<button data-more onclick="Mobile.more()"><i>☰</i>Ещё</button>`;
    document.body.appendChild(nav);

    const sheet = document.createElement("div");
    sheet.className = "sheet-wrap"; sheet.id = "sheetWrap";
    sheet.innerHTML = `<div class="sheet" id="sheet"><div class="sheet-grip"></div><div id="sheetBody"></div></div>`;
    sheet.addEventListener("click", (e) => { if (e.target === sheet) this.closeSheet(); });
    document.body.appendChild(sheet);

    // строка чипов композера + кнопка голосового ввода
    const composer = document.querySelector(".composer");
    const chips = document.createElement("div");
    chips.className = "m-chips"; chips.id = "mChips";
    composer.insertBefore(chips, composer.querySelector(".send-row"));
    if (this.speechSupported()) {
      const mic = document.createElement("button");
      mic.className = "btn btn-mic"; mic.id = "btnMic"; mic.title = "Голосовой ввод";
      mic.textContent = "🎤";
      mic.onclick = () => this.dictate();
      composer.querySelector(".send-row").insertBefore(mic, $("btnSend"));
    }

    // кнопка «назад» для двухколоночных вкладок живёт вне перерисовываемых блоков
    const pw = document.querySelector(".prompts-wrap");
    if (pw) {
      const b = document.createElement("button");
      b.className = "btn m-back"; b.innerHTML = "◀ К списку узлов";
      b.onclick = () => this.page("prompts", "list");
      pw.insertBefore(b, pw.firstChild);
    }
  },

  // ---- перехват базовых функций приложения --------------------------------
  hookApp() {
    const wrap = (obj, name, after) => {
      if (!obj || typeof obj[name] !== "function") return;
      const orig = obj[name];
      obj[name] = function (...a) { const r = orig.apply(this, a); try { after.apply(Mobile, [r, ...a]); } catch (e) { console.warn("Mobile hook " + name, e); } return r; };
    };

    wrap(Boot, "start", () => setTimeout(() => this.apply(), 0));
    wrap(UI, "showTab", (r, name) => { this.syncNav(name); this.closeDrawer(); this.closeSheet(); this.resetPage(name); });
    wrap(UI, "applyAdmin", () => this.syncNav(UI.tab));
    wrap(Chat, "refreshModeUi", () => this.syncChips());
    wrap(Chat, "applyThreadToComposer", () => this.syncChips());
    wrap(Chat, "renderMessages", (r, t, active) => this.onRender(active));
    wrap(Chat, "submit", () => { this.askNotify(); this.buzz(12); this.closeSheet(); });
    wrap(Threads, "select", () => this.closeDrawer());
    wrap(Threads, "create", () => this.closeDrawer());
    // Редактор ролей: на телефоне это две страницы, а не две колонки.
    if (typeof Prompts !== "undefined") wrap(Prompts, "select", () => this.page("prompts", "detail"));
  },

  // ---- включение/выключение мобильного режима ------------------------------
  apply() {
    const on = this.on();
    document.body.classList.toggle("is-mobile", on);
    const ti = $("taskInput");
    if (ti) {
      if (!this._hint) this._hint = ti.placeholder;
      ti.placeholder = on ? "Опишите задачу…" : this._hint;
    }
    if (on) {
      $("app").classList.add("sidebar-collapsed");
      this.syncNav(UI.tab); this.syncChips(); this.resetPage(UI.tab);
    } else {
      this.closeSheet();
      $("app").classList.remove("sidebar-collapsed");
      document.querySelectorAll(".view").forEach(v => v.removeAttribute("data-page"));
    }
  },

  /** Контролы композера существуют в одном экземпляре и переезжают в шторку. */
  composerParts() {
    return [document.querySelector(".ctl-row"), $("panel-plan"), $("panel-council")].filter(Boolean);
  },
  toSheet() {
    const host = $("sheetComposer"); if (!host) return;
    this.composerParts().forEach(p => host.appendChild(p));
  },
  toComposer() {
    const composer = document.querySelector(".composer");
    const anchor = $("mChips") || composer.querySelector(".send-row");
    this.composerParts().forEach(p => { if (p.parentElement !== composer) composer.insertBefore(p, anchor); });
  },

  // ---- нижняя навигация ----------------------------------------------------
  go(tab) { UI.showTab(tab); this.buzz(8); },
  syncNav(tab) {
    const nav = $("mobileNav"); if (!nav) return;
    const primary = this.NAV.map(n => n.tab);
    nav.querySelectorAll("button").forEach(b => {
      const t = b.dataset.tab;
      b.classList.toggle("active", t ? t === tab : !primary.includes(tab));
    });
    const mt = $("mTitle"); if (mt) mt.textContent = this.TAB_TITLE[tab] || "";
  },

  more() {
    const items = [
      { tab: "users", icon: "👥", label: "Пользователи", sub: "кто подключён, скорость канала" },
      { tab: "settings", icon: "⚙️", label: "Настройки", sub: "интерфейс, узлы, LLM, безопасность" },
      { tab: "help", icon: "📖", label: "Справка", sub: "как устроен кластер" },
    ];
    if (State.isAdmin) items.push({ tab: "admin", icon: "🛡", label: "Админ", sub: "карта сети, команды узлам" });
    const extra = [
      { act: "theme", icon: document.documentElement.dataset.theme === "dark" ? "☀️" : "🌙", label: "Сменить тему" },
      { act: "tunnel", icon: "🌐", label: "Скопировать адрес хаба", sub: `${State.tunnel}:${State.port}` },
    ];
    if (this.installEvent) extra.unshift({ act: "install", icon: "📲", label: "Установить приложение", sub: "ярлык на домашний экран" });
    if (State.you && State.you.auth_enabled) extra.push({ act: "logout", icon: "⎋", label: "Выйти" });

    this.sheet("Ещё", `<div class="sheet-list">
      ${items.map(i => `<button class="${UI.tab === i.tab ? "active" : ""}" onclick="Mobile.go('${i.tab}')"><i>${i.icon}</i><span>${i.label}<span class="sub">${esc(i.sub)}</span></span></button>`).join("")}
      <div style="height:1px;background:var(--line);margin:6px 0"></div>
      ${extra.map(i => `<button onclick="Mobile.act('${i.act}')"><i>${i.icon}</i><span>${i.label}${i.sub ? `<span class="sub">${esc(i.sub)}</span>` : ""}</span></button>`).join("")}
    </div>`);
  },

  act(what) {
    this.closeSheet();
    if (what === "theme") UI.toggleTheme();
    else if (what === "tunnel") UI.copyTunnel();
    else if (what === "logout") Auth.logout();
    else if (what === "install" && this.installEvent) { this.installEvent.prompt(); this.installEvent = null; }
  },

  // ---- чипы композера ------------------------------------------------------
  syncChips() {
    const box = $("mChips"); if (!box || !this.on()) return;
    const nodeSel = $("targetNode"), skillSel = $("skillSelect");
    const nodeTxt = nodeSel && nodeSel.selectedIndex >= 0 ? nodeSel.options[nodeSel.selectedIndex].text : "";
    const skillTxt = skillSel && skillSel.selectedIndex >= 0 ? skillSel.options[skillSel.selectedIndex].text : "";
    const single = Chat.mode === "single" || Chat.mode === "pipeline";
    const chips = [`<button class="m-chip on" onclick="Mobile.sheetComposer()">${esc(this.MODE[Chat.mode] || Chat.mode)}</button>`];
    if (single && nodeTxt) chips.push(`<button class="m-chip" onclick="Mobile.sheetComposer()">🖥 ${esc(nodeTxt)}</button>`);
    if (Chat.mode === "hybrid") chips.push(`<button class="m-chip" onclick="Mobile.sheetComposer('plan')">🧩 План</button>`);
    if (Chat.mode === "council") chips.push(`<button class="m-chip" onclick="Mobile.sheetComposer('council')">🤝 Участники</button>`);
    if (skillTxt) chips.push(`<button class="m-chip" onclick="Mobile.sheetComposer()">${esc(skillTxt)}</button>`);
    if ($("noRoles") && $("noRoles").checked) chips.push(`<button class="m-chip on" onclick="Mobile.sheetComposer()">🚫 без ролей</button>`);
    if ($("toolsOn") && $("toolsOn").checked && State.isAdmin) chips.push(`<button class="m-chip on" onclick="Mobile.sheetComposer()">🖥 Агент ПК</button>`);
    chips.push(`<button class="m-chip" onclick="Mobile.sheetComposer()">🎛 Параметры</button>`);
    const html = chips.join("");
    if (box.innerHTML !== html) box.innerHTML = html;
  },

  /** Шторка с настоящими контролами композера (они сюда переезжают целиком). */
  sheetComposer(panel) {
    this.sheet("Как выполнять запрос", `<div id="sheetComposer"></div>`);
    this.toSheet();
    if (panel && Chat.panel !== panel) Chat.togglePanel(panel, true);
    const host = $("sheetComposer");
    host.addEventListener("change", () => this.syncChips());
    host.addEventListener("click", () => setTimeout(() => this.syncChips(), 0));
  },

  // ---- шторка --------------------------------------------------------------
  sheet(title, html) {
    this.toComposer();                       // не дать уничтожить контролы при перерисовке
    $("sheetBody").innerHTML = (title ? `<h4>${esc(title)}</h4>` : "") + html;
    $("sheetWrap").classList.add("open");
  },
  closeSheet() {
    const w = $("sheetWrap"); if (!w || !w.classList.contains("open")) return;
    this.toComposer();
    w.classList.remove("open");
    $("sheetBody").innerHTML = "";
    this.syncChips();
  },
  closeDrawer() { if (this.on()) $("app").classList.add("sidebar-collapsed"); },
  openDrawer() { $("app").classList.remove("sidebar-collapsed"); },

  // ---- страницы «список → карточка» ---------------------------------------
  page(tab, which) {
    if (!this.on()) return;
    const v = $("view-" + tab); if (v) v.dataset.page = which;
  },
  resetPage(tab) {
    document.querySelectorAll(".view").forEach(v => v.removeAttribute("data-page"));
    if (this.on() && tab === "prompts") $("view-prompts").dataset.page = "list";
  },

  // ---- жесты ---------------------------------------------------------------
  gestures() {
    let x0 = 0, y0 = 0, y1 = 0, edge = false, pulling = null;

    document.addEventListener("touchstart", (e) => {
      if (!this.on() || e.touches.length !== 1) return;
      const t = e.touches[0];
      x0 = t.clientX; y0 = t.clientY; y1 = y0;
      edge = x0 < 22 && $("app").classList.contains("sidebar-collapsed") && UI.tab === "chat";
      const sc = e.target.closest(".scroll, .messages");
      pulling = (sc && sc.scrollTop <= 0) ? sc : null;
    }, { passive: true });

    document.addEventListener("touchmove", (e) => {
      if (!this.on() || e.touches.length !== 1) return;
      const t = e.touches[0], dx = t.clientX - x0, dy = t.clientY - y0;
      if (edge && dx > 55 && Math.abs(dy) < 45) { this.openDrawer(); edge = false; return; }
      if (pulling && dy > 0 && Math.abs(dx) < 40) {
        y1 = t.clientY;
        const hint = this.pullHint(pulling);
        hint.classList.toggle("on", dy > 30);
        hint.textContent = dy > 78 ? "Отпустите — обновлю" : "Потяните вниз, чтобы обновить";
      }
    }, { passive: true });

    document.addEventListener("touchend", () => {
      if (!this.on()) return;
      if (pulling) {
        this.pullHint(pulling).classList.remove("on");
        if (y1 - y0 > 78) this.refresh();
      }
      pulling = null; edge = false;
    }, { passive: true });
  },

  pullHint(sc) {
    let h = sc.querySelector(":scope > .pull-hint");
    if (!h) { h = document.createElement("div"); h.className = "pull-hint"; sc.insertBefore(h, sc.firstChild); }
    return h;
  },

  refresh() {
    this.buzz(10);
    const hooks = { telemetry: () => Telemetry.refresh(true), users: () => Users.refresh(),
                    models: () => Models.open(), admin: () => Admin.open(), chat: () => Chat.loadThread() };
    try { Poll.tick(); } catch (e) {}
    try { if (hooks[UI.tab]) hooks[UI.tab](); } catch (e) {}
    UI.toast("Обновлено", "ok", 1200);
  },

  // ---- клавиатура телефона -------------------------------------------------
  keyboard() {
    const vv = window.visualViewport; if (!vv) return;
    const onResize = () => {
      if (!this.on()) return;
      const open = Math.max(0, window.innerHeight - vv.height - vv.offsetTop) > 120;
      document.body.classList.toggle("kbd-open", open);
      const nav = $("mobileNav"); if (nav) nav.style.display = open ? "none" : "";
      const main = document.querySelector(".main"); if (main) main.style.paddingBottom = open ? "0px" : "";
    };
    vv.addEventListener("resize", onResize);
    vv.addEventListener("scroll", onResize);
  },

  // ---- работа кластера в фоне ---------------------------------------------
  onRender(active) {
    const busy = !!(active && active.length);
    const nav = $("mobileNav");
    if (nav) { const b = nav.querySelector('[data-tab="chat"]'); if (b) b.classList.toggle("busy", busy); }
    if (!this.on()) { this.busyWas = busy; return; }
    if (busy && !this.busyWas) this.wake(true);
    if (!busy && this.busyWas) { this.wake(false); this.notifyDone(); }
    this.busyWas = busy;
  },

  /** Экран не должен гаснуть, пока узлы считают: иначе вкладка засыпает. */
  async wake(want) {
    try {
      if (want && !this.wakeLock && navigator.wakeLock && !document.hidden) {
        this.wakeLock = await navigator.wakeLock.request("screen");
        this.wakeLock.addEventListener("release", () => { this.wakeLock = null; });
      } else if (!want && this.wakeLock) { await this.wakeLock.release(); this.wakeLock = null; }
    } catch (e) { /* политика браузера или экономия батареи — не критично */ }
  },
  wakeIfBusy() { if (this.busyWas) this.wake(true); },

  askNotify() {
    if (!this.on() || !("Notification" in window) || Notification.permission !== "default") return;
    try { Notification.requestPermission(); } catch (e) {}
  },
  notifyDone() {
    this.buzz([20, 60, 20]);
    if (!document.hidden || !("Notification" in window) || Notification.permission !== "granted") return;
    const t = State.thread;
    const last = t && t.messages ? t.messages[t.messages.length - 1] : null;
    try {
      new Notification("BlackFox: ответ готов", {
        body: (last && (last.content || "").slice(0, 140)) || (t ? t.title : ""),
        icon: "/icons/icon-192.png", tag: "bf-done",
      });
    } catch (e) {}
  },
  buzz(p) { try { if (this.on() && navigator.vibrate) navigator.vibrate(p); } catch (e) {} },

  // ---- голосовой ввод ------------------------------------------------------
  speechSupported() { return !!(window.SpeechRecognition || window.webkitSpeechRecognition); },
  dictate() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const btn = $("btnMic"), input = $("taskInput");
    if (this.recog) { this.recog.stop(); return; }
    const r = new SR();
    r.lang = navigator.language && navigator.language.startsWith("en") ? "en-US" : "ru-RU";
    r.interimResults = true; r.continuous = true;
    const base = input.value ? input.value + " " : "";
    r.onstart = () => { this.recog = r; btn.classList.add("rec"); this.buzz(15); };
    r.onresult = (e) => {
      let text = "";
      for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript;
      input.value = base + text;
      input.dispatchEvent(new Event("input"));
    };
    r.onerror = (e) => { UI.toast("Микрофон: " + (e.error === "not-allowed" ? "нет доступа" : esc(e.error)), "err"); };
    r.onend = () => { this.recog = null; btn.classList.remove("rec"); input.focus(); };
    try { r.start(); } catch (e) { UI.toast("Не удалось включить микрофон", "err"); }
  },
};

window.addEventListener("DOMContentLoaded", () => { try { Mobile.init(); } catch (e) { console.error("Mobile", e); } });
