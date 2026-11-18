"""把资料、检测、指标、复核与现场结果组装成一次可回溯的分析快照。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import anomaly, decisions, inspection
from .sources import Sources, load


@dataclass
class Snapshot:
    as_of: datetime
    as_of_kind: str  # explicit / latest_known
    data_through: str
    sources: Sources
    days: list[anomaly.DayRow]
    regimes: list[dict]
    reference: list[str]
    baselines: dict[str, anomaly.MeterBaseline]
    load: dict[str, float]
    observations: list[anomaly.Observation]
    balance: dict
    episodes: list[anomaly.Episode]
    impacts: list[decisions.BoundaryImpact]
    clarity: dict
    candidates: list[inspection.Candidate]
    usage: dict
    results: list[inspection.ResultRecord]
    applied: list[dict]
    coverage: dict
    histories: dict
    ranking: dict
    naive: dict
    scores: dict
    choices: list[dict]
    caveats: list[str]

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "as_of_kind": self.as_of_kind,
            "data_through": self.data_through,
            "regimes": self.regimes,
            "reference_days": self.reference,
            "baselines": {
                meter: {
                    "transmitter": base.transmitter,
                    "boundary": base.boundary,
                    "centers": {k: round(v, 2) for k, v in base.centers.items()},
                    "scale": round(base.scale, 2),
                }
                for meter, base in sorted(self.baselines.items())
            },
            "load_ratio": {k: round(v, 4) for k, v in sorted(self.load.items())},
            "observations": [
                {
                    "day": o.day,
                    "meter_id": o.meter_id,
                    "transmitter": o.transmitter,
                    "boundary": o.boundary,
                    "regime": o.regime,
                    "value": round(o.value, 2),
                    "expected": round(o.expected, 2),
                    "residual": round(o.residual, 2),
                    "z": round(max(-9.0, min(9.0, o.z)), 2),
                    "in_reference": o.in_reference,
                }
                for o in self.observations
            ],
            "balance": {
                "daily": {
                    b: {d: round(v, 2) for d, v in rows.items()}
                    for b, rows in self.balance.get("daily", {}).items()
                },
                "envelope": self.balance.get("envelope", {}),
                "known_operations": self.balance.get("known_operations", []),
            },
            "episodes": [e.as_dict() for e in self.episodes],
            "impacts": [i.as_dict() for i in self.impacts],
            "clarity": self.clarity,
            "candidates": [c.as_dict() for c in self.candidates],
            "ranking": self.ranking,
            "usage": self.usage,
            "results": [r.as_dict() for r in self.results],
            "applied_results": self.applied,
            "coverage": self.coverage,
            "histories": self.histories,
            "naive": self.naive,
            "scores": self.scores,
            "as_of_choices": self.choices,
            "events": self.sources.events_known(self.as_of),
            "caveats": self.caveats,
            "meter_map": {
                m.meter_id: {
                    "transmitter": m.transmitter,
                    "boundary": m.boundary,
                    "range_max": m.range_max,
                    "error_bound": m.error_bound,
                }
                for m in self.sources.meters
            },
            "boundary_terms": [
                {
                    "term_id": t.term_id,
                    "boundary": t.boundary,
                    "meter_id": t.meter_id,
                    "coefficient": t.coefficient,
                    "unknown_min": t.unknown_min,
                    "unknown_max": t.unknown_max,
                }
                for t in self.sources.flow_boundary
            ],
            "readings": [
                {
                    "reading_id": r.reading_id,
                    "meter_id": r.meter_id,
                    "effective_at": r.effective_at.isoformat(),
                    "value": r.value,
                    "regime": r.regime,
                    "known_at": r.known_at.isoformat(),
                }
                for r in self.sources.readings
            ],
        }


def analyse(root: Path, as_of: datetime | None = None) -> Snapshot:
    sources = load(root)
    if as_of is None:
        as_of = max(r.known_at for r in sources.readings)
        kind = "latest_known"
    else:
        kind = "explicit"
    days = anomaly.build_days(sources, as_of)
    if not days:
        raise ValueError("该时点尚无可知观测")
    data_through = days[-1].day
    regimes = anomaly.regime_runs(days)
    reference = anomaly.reference_days(days)
    baselines, reference = anomaly.build_baselines(sources, days, reference)
    load_ratio = {d.day: anomaly.load_ratio(d, baselines, excluded=set()) for d in days}
    observations = anomaly.residual_series(days, baselines, load_ratio, set(reference))
    balance = anomaly.boundary_balance(sources, days, as_of)

    scores = anomaly.transmitter_scores(days, baselines)
    episodes = anomaly.detect_episodes(sources, days, scores, baselines, balance)
    anomaly.attach_hypotheses(sources, episodes, balance, as_of)

    results = inspection.known_results(sources, as_of)
    applied = inspection.apply_results(sources, episodes, results)

    # 结果会改变解释集合：已有偏移证据的候选不再作为"可行解释"参与排序。
    impacts = decisions.boundary_impacts(sources, days, episodes)
    clarity = decisions.clarity_groups(episodes, impacts)

    def value_lookup(episode):
        return decisions.decision_value(sources, episode, impacts)

    candidates, usage, ranking = inspection.rank_candidates(
        sources, episodes, as_of, impacts, value_lookup
    )
    coverage = inspection.coverage_summary(sources, episodes, candidates, usage)
    histories = inspection.inspection_histories(sources, episodes, as_of)
    naive = anomaly.naive_series(days, baselines)
    scores = {
        tx: [
            {"day": s.day, "regime": s.regime, "score": round(s.score, 3),
             "per_meter_z": {k: round(v, 3) for k, v in s.per_meter_z.items()}}
            for s in series
        ]
        for tx, series in anomaly.transmitter_scores(days, baselines).items()
    }

    choices = as_of_choices(sources, kind, as_of)
    caveats = _caveats(sources, episodes, impacts, candidates, coverage, as_of, results)
    return Snapshot(
        as_of=as_of,
        as_of_kind=kind,
        data_through=data_through,
        sources=sources,
        days=days,
        regimes=regimes,
        reference=reference,
        baselines=baselines,
        load=load_ratio,
        observations=observations,
        balance=balance,
        episodes=episodes,
        impacts=impacts,
        clarity=clarity,
        candidates=candidates,
        usage=usage,
        results=results,
        applied=applied,
        ranking=ranking,
        coverage=coverage,
        histories=histories,
        naive=naive,
        scores=scores,
        choices=choices,
        caveats=caveats,
    )


def as_of_choices(sources: Sources, kind: str, as_of: datetime) -> list[dict]:
    """可回看的时点：换表、现场结果、放空/检修可知、运行档首日。

    只列出资料里明确登记过的"当时可知"节点，避免用户误以为可以任意插值。
    """
    stamps: dict[str, str] = {}
    for rep in sources.meter_replacements:
        stamps[rep.known_at.isoformat()] = f"换表 {rep.replacement_id} 可知"
    for result in sources.inspection_results:
        stamps[result.known_at.isoformat()] = f"现场结果 {result.result_id} 可知"
    for note in sources.operation_notes:
        stamps[note.known_at.isoformat()] = f"运行记录 {note.note_id} 可知"
    for row in _first_days(sources):
        # 首日读数在次日 08:00 才可知，因此选择该时刻才能看到完整首日。
        stamp = f"{row}T08:00:00+08:00"
        stamps.setdefault(stamp, f"运行档首日 {row} 可知后")
    out = [
        {"value": stamp, "label": f"{stamp} · {label}"}
        for stamp, label in sorted(stamps.items())
        if stamp != as_of.isoformat()
    ]
    return out


def _first_days(sources: Sources) -> list[str]:
    seen: list[str] = []
    for reading in sorted(sources.readings, key=lambda r: r.effective_at):
        if reading.regime not in {s.split(":")[-1] for s in seen}:
            seen.append(reading.effective_at.date().isoformat() + ":" + reading.regime)
    return [item.split(":")[0] for item in seen]


def _caveats(
    sources: Sources,
    episodes: list[anomaly.Episode],
    impacts: list[decisions.BoundaryImpact],
    candidates: list[inspection.Candidate],
    coverage: dict,
    as_of: datetime,
    results: list[inspection.ResultRecord],
) -> list[str]:
    notes: list[str] = []
    if not episodes:
        notes.append("当前可知窗口内没有超过证据阈值的残差；这不等于所有测点都合格。")
    unresolved = [e for e in episodes if not e.resolved]
    if unresolved:
        notes.append(
            f"{len(unresolved)} 个证据单元仍无现场结果；未选中复核的告警不得视为正常。"
        )
    if coverage.get("cannot_safely_conclude"):
        notes.append(
            "剩余窗口的名额/预算不足以区分关键解释："
            + "、".join(sorted(coverage["cannot_safely_conclude"]))
            + " 的相关结论仍不能安全形成。"
        )
    if not candidates:
        notes.append("现有窗口没有可执行且能减少决策不确定性的检查选项。")
    for impact in impacts:
        if impact.attribution == "open" and impact.threshold_invariant:
            notes.append(
                f"{impact.boundary} 单耗阈值结论（{impact.measured:.3f} "
                f"{'高于' if impact.measured_verdict == 'over' else '低于'}上限 {impact.limit:g}）"
                "不随仪表/负荷解释改变；仍需澄清的是归因而不是数值。"
            )
        if impact.attribution == "resolved":
            notes.append(
                f"{impact.boundary} 的供汽读数已按现场结果修正，"
                f"单耗由 {impact.measured:.3f} 更新为 {impact.corrected:.3f}。"
            )
    for result in results:
        if result.outcome == "undone":
            notes.append(
                f"{result.inspection_id}（变送器 {result.transmitter}）未完成，不提供合格证据，"
                f"但窗口 {result.window} 与费用已实际占用；该窗口内不能改派其他变送器。"
            )
    return notes
