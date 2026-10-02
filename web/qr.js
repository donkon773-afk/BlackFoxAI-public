/* BlackFox — компактный генератор QR-кодов (байтовый режим, уровень M, версии 1–10).
   Без внешних библиотек: адрес платформы показывается телефону как картинка.
   Реализация по ISO/IEC 18004; рендер — SVG. */
"use strict";

const QR = (() => {
  // --- параметры версий для уровня M: [всего кодовых слов, EC на блок, [[блоков, данных на блок], ...]]
  const V = {
    1: [26, 10, [[1, 16]]], 2: [44, 16, [[1, 28]]], 3: [70, 26, [[1, 44]]], 4: [100, 18, [[2, 32]]],
    5: [134, 24, [[2, 43]]], 6: [172, 16, [[4, 27]]], 7: [196, 18, [[4, 31]]],
    8: [242, 22, [[2, 38], [2, 39]]], 9: [292, 22, [[3, 36], [2, 37]]], 10: [346, 26, [[4, 43], [1, 44]]],
  };
  const ALIGN = { 1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 52] };

  // --- GF(256) --------------------------------------------------------------
  const EXP = new Uint8Array(512), LOG = new Uint8Array(256);
  (() => { let x = 1; for (let i = 0; i < 255; i++) { EXP[i] = x; LOG[x] = i; x <<= 1; if (x & 0x100) x ^= 0x11d; } for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255]; })();
  const mul = (a, b) => (a && b) ? EXP[LOG[a] + LOG[b]] : 0;
  function rsGenerator(n) { let g = [1]; for (let i = 0; i < n; i++) { const ng = new Array(g.length + 1).fill(0); for (let j = 0; j < g.length; j++) { ng[j] ^= g[j]; ng[j + 1] ^= mul(g[j], EXP[i]); } g = ng; } return g; }
  function rsEncode(data, n) {
    const g = rsGenerator(n), res = new Array(n).fill(0);
    for (const d of data) { const f = d ^ res.shift(); res.push(0); if (f) for (let j = 0; j < n; j++) res[j] ^= mul(g[j + 1], f); }
    return res;
  }

  // --- кодирование данных ---------------------------------------------------
  function encodeBytes(bytes) {
    let ver = 0;
    for (let v = 1; v <= 10; v++) { const dataCw = V[v][2].reduce((s, [n, k]) => s + n * k, 0); if ((dataCw * 8 - (v < 10 ? 12 : 20)) / 8 >= bytes.length) { ver = v; break; } }
    if (!ver) throw new Error("QR: слишком длинная строка (" + bytes.length + " байт)");
    const [total, ecLen, blocks] = V[ver];
    const dataCw = blocks.reduce((s, [n, k]) => s + n * k, 0);
    const bits = [];
    const put = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >> i) & 1); };
    put(4, 4); put(bytes.length, ver < 10 ? 8 : 16);
    for (const b of bytes) put(b, 8);
    put(0, Math.min(4, dataCw * 8 - bits.length));
    while (bits.length % 8) bits.push(0);
    for (let pad = 0xec; bits.length < dataCw * 8; pad ^= 0xec ^ 0x11) put(pad, 8);
    const cw = []; for (let i = 0; i < bits.length; i += 8) cw.push(parseInt(bits.slice(i, i + 8).join(""), 2));
    // разбиение на блоки + EC, затем чередование
    const dBlocks = [], eBlocks = []; let p = 0;
    for (const [n, k] of blocks) for (let i = 0; i < n; i++) { const d = cw.slice(p, p + k); p += k; dBlocks.push(d); eBlocks.push(rsEncode(d, ecLen)); }
    const out = [];
    const maxD = Math.max(...dBlocks.map(b => b.length));
    for (let i = 0; i < maxD; i++) for (const b of dBlocks) if (i < b.length) out.push(b[i]);
    for (let i = 0; i < ecLen; i++) for (const b of eBlocks) out.push(b[i]);
    if (out.length !== total) throw new Error("QR: внутренняя ошибка длины");
    return { ver, cw: out };
  }

  // --- матрица --------------------------------------------------------------
  function build(ver, cw) {
    const n = ver * 4 + 17;
    const m = Array.from({ length: n }, () => new Int8Array(n).fill(-1));   // -1 = свободно
    const fn = Array.from({ length: n }, () => new Uint8Array(n));            // 1 = служебный модуль
    const set = (x, y, v) => { m[y][x] = v; fn[y][x] = 1; };
    const finder = (cx, cy) => { for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) { const x = cx + dx, y = cy + dy; if (x < 0 || y < 0 || x >= n || y >= n) continue; const d = Math.max(Math.abs(dx), Math.abs(dy)); set(x, y, d === 2 || d === 4 ? 0 : 1); } };
    finder(3, 3); finder(n - 4, 3); finder(3, n - 4);
    for (let i = 8; i < n - 8; i++) { set(i, 6, i % 2 === 0 ? 1 : 0); set(6, i, i % 2 === 0 ? 1 : 0); }
    const al = ALIGN[ver], last = al.length - 1;
    for (let i = 0; i < al.length; i++) for (let j = 0; j < al.length; j++) {
      // выравнивающие метки не ставятся только в углах с искателями; на линиях синхронизации — ставятся
      if ((i === 0 && j === 0) || (i === 0 && j === last) || (i === last && j === 0)) continue;
      const cx = al[j], cy = al[i];
      for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) set(cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) === 1 ? 0 : 1);
    }
    // резерв под формат (и версию для v>=7)
    for (let i = 0; i < 8; i++) { set(8, i < 6 ? i : i + 1, 0); set(i < 6 ? i : i + 1, 8, 0); set(n - 1 - i, 8, 0); set(8, n - 1 - i, 0); }
    set(8, 8, 0); set(8, n - 8, 1);
    if (ver >= 7) for (let i = 0; i < 18; i++) { set(Math.floor(i / 3), n - 11 + i % 3, 0); set(n - 11 + i % 3, Math.floor(i / 3), 0); }
    // размещение данных
    const bits = []; for (const c of cw) for (let i = 7; i >= 0; i--) bits.push((c >> i) & 1);
    let bi = 0;
    for (let right = n - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let vert = 0; vert < n; vert++) {
        const y = ((right + 1) & 2) === 0 ? n - 1 - vert : vert;
        for (let j = 0; j < 2; j++) { const x = right - j; if (fn[y][x]) continue; m[y][x] = bi < bits.length ? bits[bi++] : 0; }
      }
    }
    return { n, m, fn };
  }

  const MASKS = [(x, y) => (x + y) % 2 === 0, (x, y) => y % 2 === 0, (x, y) => x % 3 === 0, (x, y) => (x + y) % 3 === 0,
    (x, y) => (Math.floor(y / 2) + Math.floor(x / 3)) % 2 === 0, (x, y) => (x * y) % 2 + (x * y) % 3 === 0,
    (x, y) => ((x * y) % 2 + (x * y) % 3) % 2 === 0, (x, y) => ((x + y) % 2 + (x * y) % 3) % 2 === 0];

  function applyMask(g, k, on) { const { n, m, fn } = g; for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) if (!fn[y][x] && MASKS[k](x, y)) m[y][x] ^= 1; }

  function writeFormat(g, mask) {
    const { n, m } = g; let d = (0 << 3) | mask;          // уровень M = 00
    let r = d; for (let i = 0; i < 10; i++) r = (r << 1) ^ ((r >> 9) * 0x537);
    const f = ((d << 10) | r) ^ 0x5412;
    const b = i => (f >> i) & 1;
    for (let i = 0; i <= 5; i++) m[i][8] = b(i);
    m[7][8] = b(6); m[8][8] = b(7); m[8][7] = b(8);
    for (let i = 9; i < 15; i++) m[8][14 - i] = b(i);
    for (let i = 0; i < 8; i++) m[8][n - 1 - i] = b(i);
    for (let i = 8; i < 15; i++) m[n - 15 + i][8] = b(i);
    m[n - 8][8] = 1;
  }
  function writeVersion(g, ver) {
    if (ver < 7) return; const { n, m } = g;
    let r = ver; for (let i = 0; i < 12; i++) r = (r << 1) ^ ((r >> 11) * 0x1f25);
    const v = (ver << 12) | r;
    for (let i = 0; i < 18; i++) { const bit = (v >> i) & 1, a = Math.floor(i / 3), b = n - 11 + i % 3; m[b][a] = bit; m[a][b] = bit; }
  }

  function penalty(g) {
    const { n, m } = g; let p = 0;
    const run = line => { let s = 0, prev = -1, cnt = 0; for (const v of line) { if (v === prev) { cnt++; if (cnt === 5) s += 3; else if (cnt > 5) s++; } else { prev = v; cnt = 1; } } return s; };
    for (let y = 0; y < n; y++) p += run(m[y]);
    for (let x = 0; x < n; x++) p += run(m.map(r => r[x]));
    for (let y = 0; y < n - 1; y++) for (let x = 0; x < n - 1; x++) { const v = m[y][x]; if (v === m[y][x + 1] && v === m[y + 1][x] && v === m[y + 1][x + 1]) p += 3; }
    const pat = [1, 0, 1, 1, 1, 0, 1], chk = (line, i, dir) => { for (let k = 0; k < 7; k++) if (line[i + k] !== pat[k]) return false; const before = dir ? line.slice(Math.max(0, i - 4), i) : line.slice(i + 7, i + 11); return before.length === 4 && before.every(v => v === 0); };
    const lines = []; for (let y = 0; y < n; y++) lines.push(m[y]); for (let x = 0; x < n; x++) lines.push(m.map(r => r[x]));
    for (const line of lines) for (let i = 0; i <= line.length - 7; i++) { if (chk(line, i, true)) p += 40; if (chk(line, i, false)) p += 40; }
    let dark = 0; for (const r of m) for (const v of r) dark += v;
    p += Math.floor(Math.abs(dark * 20 - n * n * 10) / (n * n)) * 10;
    return p;
  }

  function encode(text) {
    const bytes = Array.from(new TextEncoder().encode(text));
    const { ver, cw } = encodeBytes(bytes);
    const g = build(ver, cw);
    let best = 0, bestP = Infinity;
    for (let k = 0; k < 8; k++) { applyMask(g, k); writeFormat(g, k); writeVersion(g, ver); const p = penalty(g); if (p < bestP) { bestP = p; best = k; } applyMask(g, k); }
    applyMask(g, best); writeFormat(g, best); writeVersion(g, ver);
    return g.m.map(r => Array.from(r));
  }

  function svg(text, size = 220, dark = "#000", light = "#fff") {
    const m = encode(text), n = m.length, q = 4, cell = size / (n + 2 * q);
    let d = "";
    for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) if (m[y][x]) d += `M${((x + q) * cell).toFixed(2)} ${((y + q) * cell).toFixed(2)}h${cell.toFixed(2)}v${cell.toFixed(2)}h-${cell.toFixed(2)}z`;
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="${light}"/><path d="${d}" fill="${dark}"/></svg>`;
  }

  return { encode, svg };
})();
