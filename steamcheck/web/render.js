/* 页面渲染：从 /api/snapshot 取一次快照，全部视图同源。 */
let snapshot = null;

function fmt(value, digits = 2) {
  if (value == null || Number.isNaN(value)) return "—";
  return Number(value).toFixed(digits);
}

async function load(asOf) {
  const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
  const response = await fetch(`/api/snapshot${query}`);
  if (!response.ok) throw new Error(`快照加载失败：${response.status}`);
  return response.json();
}

function renderBanner(data) {
  const kinds = { explicit: "指定时点", latest_known: "资料最新可知" };
  document.getElementById("banner").innerHTML = `
    <strong>${esc(data.as_of)}</strong>（${kinds[data.as_of_kind] || data.as_of_kind}），
    数据截至 <strong>${esc(data.data_through)}</strong>。
    运行档：${data.regimes.map((r) => `${esc(r.regime)} ${esc(r.start)}~${esc(r.end)}（${r.days} 天）`).join("；")}。
    只使用该时点 <code>known_at</code> 之前可知的观测与记录。`;
}

function renderLoad(data) {
  const meters = Object.keys(data.baselines).sort();
  const days = Object.keys(data.load_ratio).sort();

  legend(document.getElementById("load-legend"),
    meters.map((m, i) => ({ name: `${m}（${data.meter_map[m].boundary}）`, color: color(i) })));

  // 左：单测点控制线（不扣负荷），用第一个测点示意 + 其余灰色。
  // 一个小多图：每个边界一组，最多 3 个测点，避免 6 条线互相干扰。
  const boundaries = [...new Set(meters.map((m) => data.meter_map[m].boundary))].sort();
  const groupOf = (boundary, rowsFor) => ({
    title: `${boundary} 边界`,
    series: meters
      .filter((m) => data.meter_map[m].boundary === boundary)
      .map((m) => ({
        name: m,
        color: color(meters.indexOf(m)),
        values: days.map((day) => rowsFor(day, m)),
      })),
  });
  stackedCharts(
    document.getElementById("chart-naive"),
    days,
    boundaries.map((b) => groupOf(b, (day, m) => {
      const row = data.naive[m].find((r) => r.day === day);
      return row ? row.z : null;
    })),
    { zero: true },
  );
  const cut = data.regimes.length > 1 ? data.regimes[1].start : null;
  stackedCharts(
    document.getElementById("chart-adjusted"),
    days,
    boundaries.map((b) => groupOf(b, (day, m) => {
      const row = data.observations.find((o) => o.day === day && o.meter_id === m);
      return row ? row.z : null;
    })),
    { zero: true, band: cut ? [cut, days[days.length - 1]] : null },
  );

  const naiveFlag = days.reduce((sum, day) => {
    const hits = meters.filter((m) => {
      const row = data.naive[m].find((r) => r.day === day);
      return row && Math.abs(row.z) > 3;
    }).length;
    return sum + (hits >= 2 ? 1 : 0);
  }, 0);
  const adjustedFlag = data.episodes.length;
  document.getElementById("load-note").innerHTML = `
    <strong>同一批读数：</strong>不扣负荷时，有 <strong>${naiveFlag}</strong> 天同时把多只表判为异常；
    按运行档 + 共同负荷调整后，只剩下 <strong>${adjustedFlag}</strong> 个持续证据单元。
    负荷切换（${esc(data.regimes.map((r) => r.regime).join(" → "))}）是运行变化，不应触发仪表告警。
    <br>共同负荷比例按各测点相对本档中心的比值取中位数；单个变送器对它的影响很小。`;

  const head = ["日期", "负荷比例", ...meters.map((m) => `${m} 残差 z`)];
  const rows = days.map((day) => {
    const parts = [`<td>${esc(day)}</td>`, `<td class="num">${fmt(data.load_ratio[day], 3)}</td>`];
    for (const m of meters) {
      const obs = data.observations.find((o) => o.day === day && o.meter_id === m);
      parts.push(`<td class="num">${obs ? fmt(obs.z, 2) : "—"}</td>`);
    }
    return parts;
  });
  table(document.getElementById("load-table"), head, rows);

  renderBalance(data, days);
}

function renderBalance(data, days) {
  const boundaries = Object.keys(data.balance.daily).sort();
  const series = boundaries.map((b, i) => ({
    name: b,
    color: color(i),
    values: days.map((day) => data.balance.daily[b][day] ?? null),
  }));
  legend(document.getElementById("balance-legend"), [
    ...series.map((s) => ({ name: `${s.name} 守恒量 S`, color: s.color })),
  ]);
  lineChart(document.getElementById("chart-balance"), days, series, { zero: true, height: 250 });

  const head = ["日期", ...boundaries.map((b) => `${b} S`)];
  const rows = days.map((day) => [
    `<td>${esc(day)}</td>`,
    ...boundaries.map((b) => `<td class="num">${fmt(data.balance.daily[b][day], 2)}</td>`),
  ]);
  table(document.getElementById("balance-table"), head, rows);
}

function meterBar(impact) {
  const max = Math.max(impact.measured, impact.limit, impact.if_bias_low) * 1.08;
  const pct = (v) => `${Math.max(0, Math.min(100, (v / max) * 100))}%`;
  const color = impact.measured_verdict === "over" ? "var(--critical)" : "var(--success-text)";
  return `<div class="meter-bar">
    <div class="fill" style="width:${pct(impact.measured)};background:${color};opacity:.35"></div>
    <div class="cand" style="left:${pct(impact.if_bias_low)}"></div>
    <div class="limit" style="left:${pct(impact.limit)}" title="决策上限 ${fmt(impact.limit, 3)}"></div>
  </div>
  <div class="axis-note">实心条=修正后单耗 ${fmt(impact.corrected, 3)}；
    虚线=若供汽表偏移成立的 ${fmt(impact.if_bias_low, 3)}；
    竖线=决策上限 ${fmt(impact.limit, 3)}。</div>`;
}

function renderImpacts(data) {
  const target = document.getElementById("impacts");
  target.innerHTML = data.impacts.map((impact) => {
    const verdict = impact.measured_verdict === "over"
      ? `<span class="chip over">高于上限</span>`
      : `<span class="chip within">低于上限</span>`;
    const attribution = impact.attribution === "open"
      ? `<span class="chip open">归因未决</span>`
      : impact.attribution === "resolved"
        ? `<span class="chip done">归因已澄清</span>`
        : `<span class="chip">无异常证据</span>`;
    const threshold = impact.threshold_invariant
      ? "阈值判断不随候选解释改变"
      : "<strong>阈值判断依赖复核结果</strong>";
    return `<div class="metric">
      <div class="metric-head">
        <span class="metric-name">${esc(impact.boundary)} 供汽单耗
          <small>（${esc(impact.supply_meter)} ÷ 服务产量 ${fmt(impact.service_output, 0)}，
          ${esc(impact.analysis_start)}~${esc(impact.analysis_end)}）</small></span>
        <span class="metric-value">${fmt(impact.measured, 3)}<small>上限 ${fmt(impact.limit, 2)}</small></span>
      </div>
      <div style="margin-top:8px">${verdict} ${attribution}</div>
      ${impact.notes.length ? `<ul class="tight">${impact.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
      ${meterBar(impact)}
      <div class="axis-note" style="margin-top:6px">
        实测 ${fmt(impact.measured, 3)} · 修正后 ${fmt(impact.corrected, 3)} ·
        若偏移成立 ${fmt(impact.if_bias_low, 3)} · ${threshold}
      </div>
      ${impact.action_labels.length
        ? `<div class="axis-note">候选行动：${impact.action_labels.map(esc).join("、")}</div>` : ""}
    </div>`;
  }).join("");
}

function renderClarity(data) {
  const { unchanged, uncertain } = data.clarity;
  const items = (list, empty) => list.length ? list.map((item) => `
    <li><strong>${esc(item.episode)}</strong> ${esc(item.boundary)} · ${esc(item.meters.join("/"))}
      · 台阶 ${fmt(item.shift)} t<br><span class="why">${esc(item.why)}</span></li>`).join("") : `<li>${empty}</li>`;
  document.getElementById("clarity").innerHTML = `
    <div class="split">
      <div class="panel"><h3>数值异常但不改变决定</h3><ul class="tight">${items(unchanged, "本时点没有此类证据单元")}</ul></div>
      <div class="panel"><h3>可能改变决定但证据不足</h3><ul class="tight">${items(uncertain, "本时点没有此类证据单元")}</ul></div>
    </div>`;
}

function renderEpisodes(data) {
  const target = document.getElementById("episodes");
  if (!data.episodes.length) {
    target.innerHTML = `<p class="note">当前可知窗口内没有超过证据阈值的残差。这不等于所有测点都合格。</p>`;
    return;
  }
  target.innerHTML = data.episodes.map((episode) => {
    const hyps = episode.hypotheses.map((h) => `
      <div class="hyp">
        <span class="fit ${esc(h.fit)}">${FIT[h.fit] || esc(h.fit)}</span>
        <span><strong>${esc(h.label)}</strong> — ${esc(h.evidence)}
          ${h.note ? `<br><span class="axis-note">${esc(h.note)}</span>` : ""}
          ${h.known_at ? `<br><span class="axis-note">该记录已知于 ${esc(h.known_at)}</span>` : ""}
        </span>
      </div>`).join("");
    const resolved = episode.resolved
      ? `<div class="result-banner"><strong>${esc(episode.resolved.inspection_id)} · ${esc(episode.resolved.outcome)}</strong>
         （窗口 ${esc(episode.resolved.window)}，${esc(episode.resolved.known_at)}）：${esc(episode.resolved.message)}</div>`
      : "";
    return `<div class="episode">
      <div class="episode-head">
        <span class="episode-title">${esc(episode.id)} · ${esc(episode.boundary)} · 变送器 ${esc(episode.transmitter)}</span>
        <span class="episode-meta">${esc(episode.start)}~${esc(episode.end)}（${episode.days} 天）
          · 读数台阶 ${fmt(episode.shift)} t · 边界守恒同期 ${fmt(episode.balance_delta)} t</span>
      </div>
      <div class="axis-note" style="margin:6px 0">
        ${episode.meters.map((m) => `${esc(m)} ${fmt(episode.per_meter_shift[m])} t（尺度 ${fmt(episode.per_meter_scale[m])}）`).join("；")}
      </div>
      ${hyps}${resolved}
    </div>`;
  }).join("");
}

function renderCandidates(data) {
  document.getElementById("rank-policy").textContent = data.ranking.policy;
  const target = document.getElementById("candidates");
  if (!data.candidates.length) {
    target.innerHTML = `<p class="note">当前窗口没有可执行且能减少决策不确定性的检查。
      这表示预算/名额无法区分关键解释，而不是未选中的告警正常。</p>`;
  } else {
    target.innerHTML = data.candidates.map((c, i) => `
      <div class="candidate ${i === 0 ? "primary" : ""}">
        <div class="candidate-head">
          <span class="candidate-title">${esc(c.inspection_id)} → 变送器 ${esc(c.transmitter)}</span>
          <span class="candidate-value">窗口 ${esc(c.window)} · 费用 ${fmt(c.cost, 0)} 元 ·
            精度 ${fmt(c.precision)} t · 边际价值 ${fmt(c.marginal_value, 0)} 元</span>
        </div>
        <ul class="tight">${c.reason.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      </div>`).join("");
  }
  const excluded = data.ranking.excluded || [];
  if (excluded.length) {
    target.insertAdjacentHTML("beforeend", `<div class="note"><strong>未选中（不等于正常）：</strong>
      ${excluded.map((c) => `${esc(c.inspection_id)}：${esc((c.blocked_by || []).join("；") || "价值较低")}`).join("<br>")}</div>`);
  }

  const windows = Object.values(data.usage);
  document.getElementById("windows").innerHTML = `<h3 style="font-size:12.5px;margin:14px 0 0">窗口占用</h3>` +
    windows.map((w) => `
      <div class="window">
        <div class="window-head">
          <span class="window-name">${esc(w.window)} · ${esc(w.at)}</span>
          <span>名额 ${w.slots_used}/${w.capacity} · 预算 ${fmt(w.cost_used, 0)}/${fmt(w.budget, 0)} 元</span>
        </div>
        ${w.inspections.length
          ? w.inspections.map((i) => `<div>· ${esc(i.inspection_id)}（${esc(i.transmitter)}）
              ${esc(i.outcome)}，${fmt(i.cost, 0)} 元，${esc(i.known_at)}</div>`).join("")
          : `<div class="axis-note">尚无已排检查</div>`}
      </div>`).join("");

  const coverage = data.coverage;
  const unresolved = Object.entries(coverage.cannot_safely_conclude || {});
  document.getElementById("coverage").innerHTML = `
    <strong>覆盖情况：</strong>已解除 ${coverage.resolved.length} 个证据单元；
    本轮建议覆盖 ${(coverage.covered_with_selected || []).length} 个；
    仍无人覆盖 ${(coverage.uncoverable || []).length} 个。
    ${unresolved.length
      ? `<br><strong>预算/名额无法区分的关键解释：</strong>${unresolved
          .map(([id, labels]) => `${esc(id)}（${esc(labels.join("、"))}）`).join("；")}`
      : ""}
    <br><span class="axis-note">${esc(coverage.note)}</span>`;
}

function renderCaveats(data) {
  document.getElementById("caveats").innerHTML = data.caveats
    .map((c) => `<li>${esc(c)}</li>`).join("");
}

function render(data) {
  snapshot = data;
  renderBanner(data);
  renderLoad(data);
  renderImpacts(data);
  renderClarity(data);
  renderEpisodes(data);
  renderCandidates(data);
  renderEvents(data);
  renderCaveats(data);
}

async function boot() {
  try {
    const data = await load(null);
    render(data);
    const options = data.as_of_choices || [];
    const select = document.getElementById("as-of");
    select.innerHTML = [
      `<option value="">资料最新可知（${esc(data.as_of)}）</option>`,
      ...options.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`),
    ].join("");
    select.addEventListener("change", async () => {
      const hint = document.getElementById("as-of-hint");
      hint.textContent = "计算中…";
      try {
        const next = await load(select.value || null);
        render(next);
        hint.textContent = "";
      } catch (error) {
        hint.textContent = "";
        document.getElementById("banner").innerHTML =
          `<span class="error">${esc(error.message)}</span>`;
      }
    });
    window.addEventListener("resize", () => render(snapshot));
  } catch (error) {
    document.getElementById("banner").innerHTML =
      `<span class="error">${esc(error.message)}</span>`;
  }
}

boot();

function renderEvents(data) {
  const target = document.getElementById("events");
  if (target) {
    eventStrip(target, data);
  }
}


