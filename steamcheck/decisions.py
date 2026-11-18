"""能源指标、决策影响与"哪些影响已被解除"的记账。

产品的职责是分析复核建议，不控制生产设备。这里只计算：
- 各边界实测单耗与业务决策界限的差距；
- 若某个候选解释成立，单耗会变成多少；
- 阈值判断是否随解释改变、行动（归因）判断是否改变；
- 某个解释被现场结果确认/排除后，哪些指标随之更新。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .anomaly import DayRow, Episode, mean
from .sources import Sources

# 解释类别对应的"行动方向"：数值接近但行动不同时，才算决策分歧。
ACTION = {
    "transmitter-bias": "correct-measurement",
    "meter-bias": "correct-measurement",
    "reallocation": "operate-process",
    "flow-unknown": "investigate-loss",
    "operation": "operate-process",
    "replacement": "correct-measurement",
    "sample": "observe",
}

# 可以解释同一个"边界守恒变化"但不需要校验仪表的解释。共享偏移的独特
# 证据是"守恒量并没有同量变化"，因此这三类成立时不需要给仪表定罪。
NON_INSTRUMENT = {"reallocation", "flow-unknown", "operation"}

ACTION_LABEL = {
    "correct-measurement": "修正计量/校验仪表",
    "process-or-loss": "按运行变化操作或排查损失",
    "operate-process": "按运行变化调整操作",
    "investigate-loss": "排查未计量损失",
    "observe": "继续观察",
}


@dataclass
class BoundaryImpact:
    boundary: str
    policy_id: str
    limit: float
    service_output: float
    wrong_decision_cost: float
    supply_meter: str
    analysis_start: str
    analysis_end: str
    window_days: int
    window_basis: str  # evidence-segment / recent-regime
    measured: float
    corrected: float
    if_bias_low: float
    if_bias_high: float
    measured_verdict: str
    worst_case_verdict: str
    threshold_invariant: bool
    attribution: str  # resolved / open / none
    actions: list[str]
    action_labels: list[str]
    drivers: list[dict]
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "boundary": self.boundary,
            "policy_id": self.policy_id,
            "limit": self.limit,
            "service_output": self.service_output,
            "wrong_decision_cost": self.wrong_decision_cost,
            "supply_meter": self.supply_meter,
            "analysis_start": self.analysis_start,
            "analysis_end": self.analysis_end,
            "window_days": self.window_days,
            "window_basis": self.window_basis,
            "measured": round(self.measured, 4),
            "corrected": round(self.corrected, 4),
            "if_bias_low": round(self.if_bias_low, 4),
            "if_bias_high": round(self.if_bias_high, 4),
            "measured_verdict": self.measured_verdict,
            "worst_case_verdict": self.worst_case_verdict,
            "threshold_invariant": self.threshold_invariant,
            "attribution": self.attribution,
            "actions": self.actions,
            "action_labels": self.action_labels,
            "drivers": self.drivers,
            "notes": self.notes,
        }


def _supply_meter(sources: Sources, boundary: str) -> str | None:
    terms = [t for t in sources.flow_boundary if t.boundary == boundary]
    positives = [t for t in terms if t.coefficient > 0]
    return positives[0].meter_id if len(positives) == 1 else None


def _impact_window(
    days: list[DayRow], episodes: list[Episode], boundary: str
) -> tuple[list[DayRow], str]:
    """优先使用证据段；没有证据时用当前运行档最近 7 天。"""
    boundary_episodes = [e for e in episodes if e.boundary == boundary]
    if boundary_episodes:
        start = min(e.start for e in boundary_episodes)
        end = max(e.end for e in boundary_episodes)
        rows = [d for d in days if start <= d.day <= end]
        if rows:
            return rows, "evidence-segment"
    current = days[-1].regime
    same = [d for d in days if d.regime == current]
    return same[-7:], "recent-regime"


def boundary_impacts(
    sources: Sources,
    days: list[DayRow],
    episodes: list[Episode],
) -> list[BoundaryImpact]:
    """按边界计算实测/修正单耗和决策影响。"""
    impacts: list[BoundaryImpact] = []
    if not days:
        return impacts
    for policy in sources.service_policy:
        supply = _supply_meter(sources, policy.boundary)
        if supply is None:
            continue
        rows, basis = _impact_window(days, episodes, policy.boundary)
        supply_vals = [d.values[supply] for d in rows if supply in d.values]
        if not supply_vals:
            continue
        measured_flow = mean(supply_vals)
        measured = measured_flow / policy.service_output

        resolved = 0.0
        resolved_notes: list[str] = []
        for episode in episodes:
            if episode.boundary != policy.boundary or supply not in episode.meters:
                continue
            resolved_state = episode.resolved or {}
            if resolved_state.get("status") == "bias-confirmed":
                observed = float(resolved_state.get("shift_observed") or 0.0)
                resolved += observed
                resolved_notes.append(
                    f"{resolved_state['inspection_id']} 测得偏移 {observed:+.2f} t，"
                    f"用于修正供汽表 {supply} 的读数"
                )
        corrected = (measured_flow - resolved) / policy.service_output

        open_episodes = [
            e for e in episodes if e.boundary == policy.boundary and not e.resolved
        ]
        candidate_low = corrected
        actions: set[str] = set()
        drivers: list[dict] = []
        instrument_hypotheses = []
        process_hypotheses = []
        for episode in open_episodes:
            for hypothesis in episode.live:
                if hypothesis.fit not in {"possible", "supported", "insufficient"}:
                    continue
                if hypothesis.kind == "sample":
                    continue
                if supply not in hypothesis.covers and hypothesis.scope != f"boundary:{policy.boundary}":
                    continue
                action = ACTION.get(hypothesis.kind, "observe")
                if action in {"operate-process", "investigate-loss"}:
                    action = "process-or-loss"
                actions.add(action)
                if hypothesis.kind in {"transmitter-bias", "meter-bias"}:
                    instrument_hypotheses.append(hypothesis)
                elif hypothesis.kind in NON_INSTRUMENT:
                    process_hypotheses.append(hypothesis)
                if hypothesis.kind == "transmitter-bias" and supply in hypothesis.covers:
                    magnitude = abs(hypothesis.magnitude or 0.0)
                    candidate_low = min(
                        candidate_low,
                        (measured_flow - resolved - magnitude) / policy.service_output,
                    )
                if hypothesis.kind in {"transmitter-bias", "meter-bias", "reallocation", "flow-unknown", "operation"}:
                    drivers.append(
                        {
                            "episode": episode.episode_id,
                            "hypothesis": hypothesis.hypothesis_id,
                            "kind": hypothesis.kind,
                            "fit": hypothesis.fit,
                            "label": hypothesis.label,
                            "magnitude": hypothesis.magnitude,
                            "intensity_shift": round(
                                (abs(hypothesis.magnitude or 0.0) / policy.service_output)
                                if hypothesis.kind == "transmitter-bias"
                                else 0.0,
                                4,
                            ),
                        }
                    )

        measured_verdict = "over" if measured > policy.intensity_limit else "within"
        worst_verdict = "over" if candidate_low > policy.intensity_limit else "within"
        threshold_invariant = measured_verdict == worst_verdict
        if any(e.resolved and e.resolved.get("status") in {"bias-confirmed", "bias-cleared"} for e in episodes if e.boundary == policy.boundary):
            unresolved = [e for e in episodes if e.boundary == policy.boundary and not e.resolved]
            attribution = "open" if unresolved else "resolved"
        elif open_episodes:
            attribution = "open"
        else:
            attribution = "none"

        notes: list[str] = []
        notes.extend(resolved_notes)
        if attribution == "none":
            notes.append("该边界当前没有超过证据阈值的残差；这是观察结果，不是所有测点合格的证明")
        if attribution == "open" and threshold_invariant:
            notes.append(
                "单耗阈值判断不随候选解释改变；改变的是“读数异常还是运行变化”的归因"
            )
        if attribution == "open" and not threshold_invariant:
            notes.append("候选解释会把单耗推过/拉回决策界限，阈值判断本身依赖复核结果")
        if attribution == "resolved" and abs(corrected - measured) > 1e-9:
            notes.append(
                f"供汽读数已按现场结果修正，单耗由 {measured:.3f} 更新为 {corrected:.3f}"
            )
        impacts.append(
            BoundaryImpact(
                boundary=policy.boundary,
                policy_id=policy.policy_id,
                limit=policy.intensity_limit,
                service_output=policy.service_output,
                wrong_decision_cost=policy.wrong_decision_cost,
                supply_meter=supply,
                analysis_start=rows[0].day,
                analysis_end=rows[-1].day,
                window_days=len(rows),
                window_basis=basis,
                measured=measured,
                corrected=corrected,
                if_bias_low=min(candidate_low, measured),
                if_bias_high=measured,
                measured_verdict=measured_verdict,
                worst_case_verdict=worst_verdict,
                threshold_invariant=threshold_invariant,
                attribution=attribution,
                actions=sorted(actions),
                action_labels=[ACTION_LABEL.get(a, a) for a in sorted(actions)],
                drivers=drivers,
                notes=notes,
            )
        )
    return impacts


def decision_value(
    sources: Sources, episode: Episode, impacts: list[BoundaryImpact]
) -> tuple[float, list[str]]:
    """该证据单元若被澄清，能减少多少决策不确定性（元，仅用于排序）。

    只有在"可行解释导致不同行动"时才有价值；同一行动下数值变化不算。
    采用可行解释间等权假设（资料未给出概率），界面必须标注这一点。
    """
    actions = {ACTION.get(h.kind, "observe") for h in episode.live if h.fit == "possible"}
    if len(actions) <= 1:
        return 0.0, []
    reasons: list[str] = []
    value = 0.0
    for impact in impacts:
        if impact.boundary != episode.boundary:
            continue
        if impact.attribution != "open":
            continue
        value += impact.wrong_decision_cost * 0.5
        reasons.append(
            f"{impact.boundary} 的归因仍有 {len(actions)} 种可能行动，"
            f"错误决定代价 {impact.wrong_decision_cost:g} 元"
        )
    return value, reasons


def clarity_groups(
    episodes: list[Episode], impacts: list[BoundaryImpact]
) -> dict:
    """把证据单元分成"数值异常但不改变决定"和"可能改变决定但证据不足"。"""
    unchanged: list[dict] = []
    uncertain: list[dict] = []
    impact_by_boundary = {i.boundary: i for i in impacts}
    for episode in episodes:
        impact = impact_by_boundary.get(episode.boundary)
        if episode.resolved and episode.resolved.get("status") in {"bias-confirmed", "bias-cleared"}:
            continue
        live = [h for h in episode.live if h.fit in {"possible", "supported"}]
        actions = {ACTION.get(h.kind, "observe") for h in live}
        item = {
            "episode": episode.episode_id,
            "boundary": episode.boundary,
            "meters": list(episode.meters),
            "shape": episode.shape,
            "shift": round(episode.shift, 2),
            "actions": sorted(actions),
            "action_labels": [ACTION_LABEL.get(a, a) for a in sorted(actions)],
            "resolved": episode.resolved,
        }
        if len(actions) >= 2:
            item["why"] = "数值异常本身明确；但可行解释指向不同行动，所以会改变决定"
            item["wrong_decision_cost"] = impact.wrong_decision_cost if impact else 0
            uncertain.append(item)
        else:
            item["why"] = (
                "改动后阈值判断与行动都不变"
                if impact and impact.threshold_invariant
                else "只有单一可行解释"
            )
            unchanged.append(item)
    return {"unchanged": unchanged, "uncertain": uncertain}
