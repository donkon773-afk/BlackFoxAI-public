/* BlackFox AI Workstation — deep sensor rendering (temperatures, clocks, fans, power) */
"use strict";

Object.assign(Telemetry, {
  val(v, unit, digits) {
    if (v === null || v === undefined || v === "") return '<span class="na">н/д</span>';
    const num = digits != null ? (+v).toFixed(digits) : v;
    return num + (unit ? '<span class="u">' + unit + "</span>" : "");
  },
  tempCls(t, hot) { return t == null ? "" : t >= hot ? "hot" : t >= hot - 15 ? "warm" : "cool"; },

  sensorsHtml(s, key) {
    if (!s || !s.ts) return "";
    const c = s.cpu || {}, r = s.ram || {}, p = s.power || {}, gpus = s.gpus || [], fans = s.fans || [];
    const cores = (c.core_clocks || []).filter(x => x.mhz);
    const coreTemps = c.core_temps || [];
    const srcName = { LibreHardwareMonitor: "LibreHardwareMonitor", OpenHardwareMonitor: "OpenHardwareMonitor",
      windows: "WMI + счётчики производительности", hwmon: "hwmon / RAPL", macos: "macOS", nvml: "NVML", none: "нет" }[s.source] || s.source;
    const gpuSrc = gpus.length && gpus[0].source === "nvml" && s.source !== "nvml" ? " · GPU: NVML" : "";
    const t = (v, hot) => this.tempCls(v, hot);

    const cpuCard = `<div class="sens-card"><div class="sens-h">🧠 Процессор <span class="muted">${esc((c.name || "").replace(/\s+/g, " ").trim())}</span></div>
      <div class="sens-rows">
        <div class="sens-row"><span>Температура</span><b class="${t(c.temp_c, 85)}">${this.val(c.temp_c, "°C", 0)}</b><span>${esc(c.temp_label || "")}</span></div>
        <div class="sens-row"><span>Частота (макс. ядро)</span><b>${this.val(c.clock_mhz, " МГц", 0)}</b><span>база ${c.base_mhz ? Math.round(c.base_mhz) : "—"} МГц</span></div>
        <div class="sens-row"><span>Эффективная частота</span><b>${this.val(c.effective_mhz, " МГц", 0)}</b><span>${esc(c.effective_source || "")}</span></div>
        <div class="sens-row"><span>Средняя по ядрам</span><b>${this.val(c.clock_avg_mhz, " МГц", 0)}</b><span>${c.cores ? c.cores + " ядер / " + (c.threads || "?") + " потоков" : ""}</span></div>
        ${c.bus_mhz ? `<div class="sens-row"><span>Частота шины</span><b>${this.val(c.bus_mhz, " МГц", 1)}</b><span></span></div>` : ""}
        <div class="sens-row"><span>Потребление</span><b>${this.val(c.power_w, " Вт", 1)}</b><span>${c.power_measured ? "измерено" : "оценка" + (c.tdp_w ? ", TDP " + c.tdp_w + " Вт" : "")}</span></div>
      </div>
      ${cores.length ? `<div class="cores">${cores.map((x, i) => {
        const ct = (coreTemps[i] || {}).c;
        const label = esc(String(x.name).replace(/^Core\s*/i, "").replace("#", ""));
        return `<div class="core" title="${esc(x.name)}${ct ? " · " + ct + "°C" : ""}"><span class="cn">${label}</span><b>${Math.round(x.mhz)}</b>${ct ? `<i class="${t(ct, 85)}">${Math.round(ct)}°</i>` : ""}</div>`;
      }).join("")}</div><div class="muted" style="font-size:10px;margin-top:4px">частоты ядер, МГц</div>` : ""}
    </div>`;

    const ramCard = `<div class="sens-card"><div class="sens-h">💾 Оперативная память</div>
      <div class="sens-rows">
        <div class="sens-row"><span>Частота</span><b>${this.val(r.speed_mhz, " МГц", 0)}</b><span>${r.rated_mhz && r.rated_mhz !== r.speed_mhz ? "паспорт " + Math.round(r.rated_mhz) + " МГц" : esc(r.part || "")}</span></div>
        <div class="sens-row"><span>Объём</span><b>${this.val(r.total_gb, " ГБ", 0)}</b><span>${r.modules ? r.modules + " модул." : ""}</span></div>
        <div class="sens-row"><span>Потребление</span><b>${this.val(r.power_w_est, " Вт", 1)}</b><span>оценка</span></div>
        ${s.motherboard_temp_c ? `<div class="sens-row"><span>Плата</span><b class="${t(s.motherboard_temp_c, 70)}">${this.val(s.motherboard_temp_c, "°C", 0)}</b><span></span></div>` : ""}
      </div>
      <div class="sens-h" style="margin-top:8px">🌀 Кулеры</div>
      <div class="sens-rows">${fans.length ? fans.map(f => `<div class="sens-row"><span>${esc(f.name)}</span><b>${this.val(f.rpm, " об/мин", 0)}</b><span></span></div>`).join("")
        : `<div class="sens-row"><span>Системные</span><b><span class="na">н/д</span></b><span>нужен LibreHardwareMonitor</span></div>`}
        ${gpus.filter(g => g.fan_percent != null).map(g => `<div class="sens-row"><span>${esc(g.name.replace("NVIDIA GeForce ", ""))}</span><b>${this.val(g.fan_percent, " %", 0)}</b><span>${g.fan_rpm ? g.fan_rpm + " об/мин" : ""}</span></div>`).join("")}
      </div></div>`;

    const gpuCards = gpus.map(g => `<div class="sens-card"><div class="sens-h">🎮 ${esc(g.name)} <span class="muted">${esc(g.pstate || "")}</span></div>
      <div class="sens-rows">
        <div class="sens-row"><span>Температура ядра</span><b class="${t(g.temp_c, 83)}">${this.val(g.temp_c, "°C", 0)}</b><span>${g.mem_temp_c ? "память " + g.mem_temp_c + "°C" : "датчик памяти н/д"}</span></div>
        <div class="sens-row"><span>Частота ядра</span><b>${this.val(g.core_clock_mhz, " МГц", 0)}</b><span>${g.video_clock_mhz ? "видеодвижок " + Math.round(g.video_clock_mhz) + " МГц" : ""}</span></div>
        <div class="sens-row"><span>Частота видеопамяти</span><b>${this.val(g.mem_clock_mhz, " МГц", 0)}</b><span>${g.mem_used_mb != null ? Math.round(g.mem_used_mb) + " / " + Math.round(g.mem_total_mb) + " МБ" : ""}</span></div>
        <div class="sens-row"><span>Кулер</span><b>${this.val(g.fan_percent, " %", 0)}</b><span>${g.fan_rpm ? g.fan_rpm + " об/мин" : ""}</span></div>
        <div class="sens-row"><span>Потребление</span><b>${this.val(g.power_w, " Вт", 1)}</b><span>${g.power_limit_w ? "лимит " + Math.round(g.power_limit_w) + " Вт" : ""}</span></div>
        <div class="sens-row"><span>Загрузка</span><b>${this.val(g.util_percent, " %", 0)}</b><span>${g.mem_util_percent != null ? "шина памяти " + g.mem_util_percent + "%" : ""}</span></div>
      </div>
      ${g.power_limit_w && g.power_w != null ? `<div class="bar ${g.power_w / g.power_limit_w > 0.9 ? "warn" : "ok"}" style="margin-top:6px" title="мощность к лимиту"><i style="width:${Math.min(100, g.power_w / g.power_limit_w * 100)}%"></i></div>` : ""}
    </div>`).join("");

    const powerCard = `<div class="sens-card power"><div class="sens-h">⚡ Энергопотребление</div>
      <div class="sens-rows">
        <div class="sens-row"><span>Процессор</span><b>${this.val(p.cpu_w, " Вт", 1)}</b><span>${p.cpu_measured ? "измерено" : "оценка по загрузке"}</span></div>
        <div class="sens-row"><span>Видеочип${gpus.length > 1 ? "ы" : ""}</span><b>${this.val(p.gpu_w, " Вт", 1)}</b><span>измерено</span></div>
        <div class="sens-row"><span>Память</span><b>${this.val(p.ram_w_est, " Вт", 1)}</b><span>оценка</span></div>
        <div class="sens-row"><span>Плата, накопители</span><b>${this.val(p.platform_w_est, " Вт", 0)}</b><span>оценка</span></div>
        <div class="sens-row total"><span>Итого по системе</span><b>${this.val(p.total_w_est, " Вт", 0)}</b><span>${p.shared_host ? "с GPU этого узла" : ""}</span></div>
        ${s.battery ? `<div class="sens-row"><span>Батарея</span><b>${this.val(s.battery.percent, " %", 0)}</b><span></span></div>` : ""}
      </div></div>`;

    return `<details class="sensors" data-k="sens_${key}">
      <summary>🌡 Датчики: температуры, частоты, кулеры, ватты <span class="muted small">— источник: ${esc(srcName)}${gpuSrc}${p.estimated ? ", мощность CPU оценочная" : ""}</span></summary>
      <div class="sens-grid">${cpuCard}${ramCard}${gpuCards}${powerCard}</div>
      ${(s.notes || []).length ? `<div class="tel-hint" style="margin-top:8px">${s.notes.map(esc).join("<br>")}</div>` : ""}
    </details>`;
  },
});
