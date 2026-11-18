/* 只读视图：所有数字来自 /api/snapshot，前端不重算业务结论。 */
const M = { left: 44, right: 16, top: 18, bottom: 28 };
const SVG = "http://www.w3.org/2000/svg";
const SERIES = [
  ["--series-1", "--series-1"],
  ["--series-2", "--series-2"],
  ["--series-3", "--series-3"],
  ["--series-4", "--series-4"],
  ["--series-5", "--series-5"],
  ["--series-6", "--series-6"],
];
const FIT = { possible: "可能", supported: "支持", contradicted: "矛盾", insufficient: "证据不足" };

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function node(name, attrs = {}, text = null) {
  const n = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text != null) n.textContent = text;
  return n;
}
function color(index) {
  const name = SERIES[index % SERIES.length][0];
  return getComputedStyle(document.querySelector(".viz-root")).getPropertyValue(name).trim();
}
function extent(values, pad = 0.12) {
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = Math.max(hi - lo, 1e-6);
  return [lo - span * pad, hi + span * pad];
}

function frame(container, height) {
  const width = Math.max(container.clientWidth || 320, 280);
  const h = height || 230;
  const svg = node("svg", { viewBox: `0 0 ${width} ${h}`, width: "100%", height: h });
  const innerW = width - M.left - M.right;
  const innerH = h - M.top - M.bottom;
  container.replaceChildren(svg);
  return {
    svg, width, h, innerW, innerH,
    x: (i, n) => M.left + (n <= 1 ? 0 : (innerW * i) / (n - 1)),
    y: (v, lo, hi) => M.top + innerH * (1 - (v - lo) / (hi - lo)),
  };
}

function axes(f, lo, hi, days, ticks = 5) {
  const g = node("g");
  for (let t = 0; t <= ticks; t += 1) {
    const v = lo + ((hi - lo) * t) / ticks;
    const y = f.y(v, lo, hi);
    g.appendChild(node("line", { x1: M.left, x2: M.left + f.innerW, y1: y, y2: y,
      stroke: "var(--grid)", "stroke-width": 1 }));
    g.appendChild(node("text", { x: M.left - 6, y: y + 4, "text-anchor": "end",
      "font-size": 10, fill: "var(--muted)" }, v.toFixed(1)));
  }
  g.appendChild(node("line", { x1: M.left, x2: M.left + f.innerW, y1: M.top + f.innerH,
    y2: M.top + f.innerH, stroke: "var(--axis)", "stroke-width": 1 }));
  const count = Math.min(days.length, 8);
  for (let i = 0; i < count; i += 1) {
    const idx = Math.round((i * (days.length - 1)) / (count - 1));
    const x = f.x(idx, days.length);
    g.appendChild(node("text", { x, y: f.h - 8, "text-anchor": "middle",
      "font-size": 10, fill: "var(--muted)" }, days[idx].slice(5)));
  }
  f.svg.appendChild(g);
}

function tooltipLayer(f, days, series) {
  const line = node("line", { y1: M.top, y2: M.top + f.innerH, stroke: "var(--axis)",
    "stroke-width": 1, visibility: "hidden" });
  f.svg.appendChild(line);
  const box = document.createElement("div");
  box.className = "tip";
  box.style.display = "none";
  f.svg.parentElement.appendChild(box);
  const hit = node("rect", { x: M.left, y: M.top, width: f.innerW, height: f.innerH,
    fill: "transparent" });
  f.svg.appendChild(hit);
  function show(clientX) {
    const rect = f.svg.getBoundingClientRect();
    const scale = f.width / rect.width;
    const px = (clientX - rect.left) * scale;
    const ratio = (px - M.left) / Math.max(f.innerW, 1);
    let idx = Math.max(0, Math.min(days.length - 1, Math.round(ratio * (days.length - 1))));
    const x = f.x(idx, days.length);
    line.setAttribute("x1", x); line.setAttribute("x2", x);
    line.setAttribute("visibility", "visible");
    box.innerHTML = `<strong>${esc(days[idx])}</strong>` + series.map((s) =>
      `<div><i style="background:${s.color}"></i>${esc(s.name)}
       <b>${s.values[idx] == null ? "—" : s.values[idx].toFixed(2)}</b></div>`).join("");
    box.style.display = "block";
    const pxLeft = rect.left - f.svg.parentElement.getBoundingClientRect().left + (x / scale);
    box.style.left = `${Math.min(pxLeft + 12, rect.width - 150)}px`;
    box.style.top = "8px";
  }
  hit.addEventListener("pointermove", (event) => show(event.clientX));
  hit.setAttribute("tabindex", "0");
  hit.addEventListener("focus", () => show(f.svg.getBoundingClientRect().left + M.left * (f.width / f.svg.getBoundingClientRect().width)));
  hit.addEventListener("blur", () => {
    line.setAttribute("visibility", "hidden");
    box.style.display = "none";
  });
  hit.addEventListener("pointerleave", () => {
    line.setAttribute("visibility", "hidden");
    box.style.display = "none";
  });
}

function lineChart(container, days, series, opts = {}) {
  const f = frame(container, opts.height || 230);
  const values = series.flatMap((s) => s.values.filter((v) => v != null));
  let [lo, hi] = extent(values);
  if (opts.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  axes(f, lo, hi, days);
  if (lo < 0 && hi > 0) {
    f.svg.insertBefore(node("line", { x1: M.left, x2: M.left + f.innerW,
      y1: f.y(0, lo, hi), y2: f.y(0, lo, hi), stroke: "var(--axis)",
      "stroke-width": 1, opacity: 0.7 }), f.svg.firstChild);
  }
  if (opts.band) {
    const [a, b] = opts.band;
    const x1 = f.x(days.indexOf(a) < 0 ? 0 : days.indexOf(a), days.length);
    const x2 = f.x(days.indexOf(b) < 0 ? days.length - 1 : days.indexOf(b), days.length);
    f.svg.insertBefore(node("rect", { x: x1, y: M.top, width: Math.max(x2 - x1, 2),
      height: f.innerH, fill: "var(--band)" }), f.svg.firstChild);
  }
  series.forEach((s, index) => {
    const stroke = s.color || color(index);
    let d = "";
    s.values.forEach((v, i) => {
      if (v == null) return;
      d += `${d ? "L" : "M"}${f.x(i, days.length).toFixed(1)},${f.y(v, lo, hi).toFixed(1)}`;
    });
    f.svg.appendChild(node("path", { d, fill: "none", stroke, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round" }));
  });
  tooltipLayer(f, days, series.map((s, i) => ({ ...s, color: s.color || color(i) })));
}

function legend(target, items) {
  target.innerHTML = items.map((item, i) =>
    `<span><i class="swatch" style="background:${item.color || color(i)}"></i>${esc(item.name)}</span>`
  ).join("");
}

function table(target, head, rows) {
  target.innerHTML = `<table><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function eventStrip(container, data) {
  const containerEl = typeof container === "string" ? document.getElementById(container) : container;
  const events = data.events || [];
  if (!events.length) {
    containerEl.innerHTML = `<span class="axis-note">该时点还没有已知的运行/换表/现场记录。</span>`;
    return;
  }
  containerEl.innerHTML = events.map((event) => `
    <div class="ev ${esc(event.kind)}">
      <div class="ev-at">${esc(event.at.slice(0, 10))}</div>
      <div class="ev-label">${esc(event.label)}</div>
      ${event.effective_at ? `<div class="ev-eff">生效 ${esc(event.effective_at.slice(0, 10))}</div>` : ""}
    </div>`).join("");
}

function stackedCharts(container, days, groups, opts = {}) {
  container.replaceChildren();
  groups.forEach((group) => {
    const wrap = document.createElement("div");
    wrap.className = "subchart";
    const title = document.createElement("div");
    title.className = "subchart-title";
    title.textContent = group.title;
    const plot = document.createElement("div");
    plot.className = "chart";
    wrap.append(title, plot);
    container.appendChild(wrap);
    lineChart(plot, days, group.series, { ...opts, height: opts.height || 150 });
  });
}
