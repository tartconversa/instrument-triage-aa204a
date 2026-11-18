"""读取并校验合成资料，不在此层做业务推断。"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sourcepack.catalog import read_table

TZ_OFFSET = "+08:00"


@dataclass(frozen=True)
class Meter:
    meter_id: str
    transmitter: str
    boundary: str
    range_max: float
    calibration_from: date
    calibration_to: date
    error_bound: float


@dataclass(frozen=True)
class Reading:
    reading_id: str
    meter_id: str
    effective_at: datetime
    value: float
    regime: str
    known_at: datetime


@dataclass(frozen=True)
class BoundaryTerm:
    term_id: str
    boundary: str
    meter_id: str
    coefficient: float
    unknown_min: float
    unknown_max: float


@dataclass(frozen=True)
class OperationNote:
    note_id: str
    boundary: str
    kind: str
    flow_t: float
    effective_at: datetime
    known_at: datetime


@dataclass(frozen=True)
class ServicePolicy:
    policy_id: str
    boundary: str
    intensity_limit: float
    service_output: float
    wrong_decision_cost: float


@dataclass(frozen=True)
class InspectionOption:
    inspection_id: str
    transmitter: str
    window: str
    cost: float
    slot_usage: int
    precision: float
    status: str


@dataclass(frozen=True)
class Window:
    window: str
    capacity: int
    budget: float
    at: datetime


@dataclass(frozen=True)
class InspectionResult:
    result_id: str
    inspection_id: str
    outcome: str
    shift_observed: float | None
    known_at: datetime


@dataclass(frozen=True)
class CheckHistory:
    history_id: str
    transmitter: str
    observed_shift: float
    error_bound: float
    outcome: str
    effective_at: datetime
    known_at: datetime


@dataclass(frozen=True)
class MeterReplacement:
    replacement_id: str
    meter_id: str
    range_max: float
    error_bound: float
    effective_at: datetime
    known_at: datetime


@dataclass(frozen=True)
class Sources:
    root: Path
    meters: tuple[Meter, ...]
    readings: tuple[Reading, ...]
    flow_boundary: tuple[BoundaryTerm, ...]
    operation_notes: tuple[OperationNote, ...]
    service_policy: tuple[ServicePolicy, ...]
    inspection_options: tuple[InspectionOption, ...]
    windows: tuple[Window, ...]
    inspection_results: tuple[InspectionResult, ...]
    check_history: tuple[CheckHistory, ...]
    meter_replacements: tuple[MeterReplacement, ...]

    def meter(self, meter_id: str) -> Meter:
        return self._by_id(self.meters, "meter_id", meter_id)

    @staticmethod
    def _by_id(rows, field, value):
        for row in rows:
            if getattr(row, field) == value:
                return row
        raise KeyError(f"{field}={value}")

    def window(self, window_id: str) -> Window:
        return self._by_id(self.windows, "window", window_id)

    def inspection(self, inspection_id: str) -> InspectionOption:
        return self._by_id(self.inspection_options, "inspection_id", inspection_id)

    def policy(self, boundary: str) -> ServicePolicy:
        return self._by_id(self.service_policy, "boundary", boundary)

    def events_known(self, as_of: datetime) -> list[dict]:
        """按 known_at 进入时间线的登记事实：换表、放空/检修、现场结果。"""
        events: list[dict] = []
        for rep in self.meter_replacements:
            if rep.known_at <= as_of:
                events.append(
                    {
                        "at": rep.known_at.isoformat(),
                        "kind": "replacement",
                        "label": f"换表 {rep.replacement_id} 生效：{rep.meter_id} 量程 {rep.range_max:g}、误差界 {rep.error_bound:g}",
                        "meters": [rep.meter_id],
                        "boundary": self.meter(rep.meter_id).boundary,
                        "effective_at": rep.effective_at.isoformat(),
                    }
                )
        for note in self.operation_notes:
            if note.known_at <= as_of:
                events.append(
                    {
                        "at": note.known_at.isoformat(),
                        "kind": "operation",
                        "label": f"运行记录 {note.note_id}（{'放空' if note.kind == 'vent' else '检修'}，{note.flow_t:g} t）",
                        "meters": [],
                        "boundary": note.boundary,
                        "effective_at": note.effective_at.isoformat(),
                    }
                )
        for result in self.inspection_results:
            if result.known_at <= as_of:
                option = self.inspection(result.inspection_id)
                events.append(
                    {
                        "at": result.known_at.isoformat(),
                        "kind": "result",
                        "label": f"现场结果 {result.result_id}（{result.outcome}）覆盖变送器 {option.transmitter}",
                        "meters": self.meters_of(option.transmitter),
                        "boundary": "/".join(
                            sorted({self.meter(m).boundary for m in self.meters_of(option.transmitter)})
                        ),
                        "effective_at": None,
                    }
                )
        return sorted(events, key=lambda e: (e["at"], e["kind"]))

    @property
    def boundary_names(self) -> list[str]:
        return sorted({m.boundary for m in self.meters})

    def transmitter_of(self, meter_id: str) -> str:
        return self.meter(meter_id).transmitter

    def meters_of(self, transmitter: str) -> list[str]:
        return [m.meter_id for m in self.meters if m.transmitter == transmitter]

    def contracts_at(self, meter_id: str, when: datetime) -> tuple[float, float]:
        """返回 (量程, 误差界)：换表生效后使用新契约。"""
        meter = self.meter(meter_id)
        best = (meter.range_max, meter.error_bound, None)
        for rep in self.meter_replacements:
            if rep.meter_id == meter_id and rep.effective_at <= when:
                if best[2] is None or rep.effective_at >= best[2]:
                    best = (rep.range_max, rep.error_bound, rep.effective_at)
        return best[0], best[1]

    def known_replacements(self, meter_id: str, as_of: datetime) -> list[MeterReplacement]:
        return sorted(
            (
                rep
                for rep in self.meter_replacements
                if rep.meter_id == meter_id and rep.known_at <= as_of
            ),
            key=lambda r: r.known_at,
        )


def _dt(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("时刻必须带时区")
    return stamp


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("数值必须是有限数")
    return number


def load(root: Path) -> Sources:
    """按资料目录读取全部表，并做跨表一致性检查。"""
    root = Path(root).resolve()
    catalog = json.loads((root / "data/catalog.json").read_text(encoding="utf-8"))
    tables = {t["name"]: t for t in catalog["tables"]}
    raw = {name: read_table(root / t["path"], t) for name, t in tables.items()}

    meters = tuple(
        Meter(
            meter_id=r["meter_id"],
            transmitter=r["transmitter"],
            boundary=r["boundary"],
            range_max=_float(r["range_max"]),
            calibration_from=date.fromisoformat(r["calibration_from"]),
            calibration_to=date.fromisoformat(r["calibration_to"]),
            error_bound=_float(r["error_bound"]),
        )
        for r in raw["meters"]
    )
    readings = tuple(
        Reading(
            reading_id=r["reading_id"],
            meter_id=r["meter_id"],
            effective_at=_dt(r["effective_at"]),
            value=_float(r["value"]),
            regime=r["regime"],
            known_at=_dt(r["known_at"]),
        )
        for r in raw["readings"]
    )
    flow_boundary = tuple(
        BoundaryTerm(
            term_id=r["term_id"],
            boundary=r["boundary"],
            meter_id=r["meter_id"],
            coefficient=_float(r["coefficient"]),
            unknown_min=_float(r["unknown_min"]),
            unknown_max=_float(r["unknown_max"]),
        )
        for r in raw["flow_boundary"]
    )
    operation_notes = tuple(
        OperationNote(
            note_id=r["note_id"],
            boundary=r["boundary"],
            kind=r["kind"],
            flow_t=_float(r["flow_t"]),
            effective_at=_dt(r["effective_at"]),
            known_at=_dt(r["known_at"]),
        )
        for r in raw["operation_notes"]
    )
    service_policy = tuple(
        ServicePolicy(
            policy_id=r["policy_id"],
            boundary=r["boundary"],
            intensity_limit=_float(r["intensity_limit"]),
            service_output=_float(r["service_output"]),
            wrong_decision_cost=_float(r["wrong_decision_cost"]),
        )
        for r in raw["service_policy"]
    )
    inspection_options = tuple(
        InspectionOption(
            inspection_id=r["inspection_id"],
            transmitter=r["transmitter"],
            window=r["window"],
            cost=_float(r["cost"]),
            slot_usage=int(r["slot_usage"]),
            precision=_float(r["precision"]),
            status=r["status"],
        )
        for r in raw["inspection_options"]
    )
    windows = tuple(
        Window(
            window=r["window"],
            capacity=int(r["capacity"]),
            budget=_float(r["budget"]),
            at=_dt(r["at"]),
        )
        for r in raw["windows"]
    )
    inspection_results = tuple(
        InspectionResult(
            result_id=r["result_id"],
            inspection_id=r["inspection_id"],
            outcome=r["outcome"],
            shift_observed=(None if r["shift_observed"] == "" else _float(r["shift_observed"])),
            known_at=_dt(r["known_at"]),
        )
        for r in raw["inspection_results"]
    )
    check_history = tuple(
        CheckHistory(
            history_id=r["history_id"],
            transmitter=r["transmitter"],
            observed_shift=_float(r["observed_shift"]),
            error_bound=_float(r["error_bound"]),
            outcome=r["outcome"],
            effective_at=_dt(r["effective_at"]),
            known_at=_dt(r["known_at"]),
        )
        for r in raw["check_history"]
    )
    meter_replacements = tuple(
        MeterReplacement(
            replacement_id=r["replacement_id"],
            meter_id=r["meter_id"],
            range_max=_float(r["range_max"]),
            error_bound=_float(r["error_bound"]),
            effective_at=_dt(r["effective_at"]),
            known_at=_dt(r["known_at"]),
        )
        for r in raw["meter_replacements"]
    )

    sources = Sources(
        root=root,
        meters=meters,
        readings=readings,
        flow_boundary=flow_boundary,
        operation_notes=operation_notes,
        service_policy=service_policy,
        inspection_options=inspection_options,
        windows=windows,
        inspection_results=inspection_results,
        check_history=check_history,
        meter_replacements=meter_replacements,
    )
    _validate(sources)
    return sources


def _validate(s: Sources) -> None:
    """跨表口径检查：边界、变送器归属、换表与窗口约束。"""
    boundaries = set(s.boundary_names)
    for term in s.flow_boundary:
        if term.boundary not in boundaries:
            raise ValueError(f"边界不存在：{term.term_id}")
        if term.unknown_min > term.unknown_max:
            raise ValueError(f"未知流量区间倒置：{term.term_id}")
    for term_rows in _group_terms(s):
        _check_conservation_shape(s, term_rows)
    for note in s.operation_notes:
        if note.boundary not in boundaries:
            raise ValueError(f"边界不存在：{note.note_id}")
        if note.kind not in {"vent", "maintenance"}:
            raise ValueError(f"未知运行记录类型：{note.note_id}")
        if note.flow_t < 0:
            raise ValueError(f"流量为负：{note.note_id}")
    for policy in s.service_policy:
        if policy.boundary not in boundaries:
            raise ValueError(f"边界不存在：{policy.policy_id}")
        if policy.service_output <= 0:
            raise ValueError(f"服务产量必须为正：{policy.policy_id}")
    for opt in s.inspection_options:
        if opt.window not in {w.window for w in s.windows}:
            raise ValueError(f"窗口不存在：{opt.inspection_id}")
        if opt.status not in {"planned", "completed"}:
            raise ValueError(f"检查状态未知：{opt.inspection_id}")
        known = {m.transmitter for m in s.meters}
        if opt.transmitter not in known:
            raise ValueError(f"变送器不存在：{opt.inspection_id}")
    for res in s.inspection_results:
        if res.outcome not in {"passed", "shift", "undone"}:
            raise ValueError(f"校验结果未知：{res.result_id}")
        if res.outcome == "undone" and res.shift_observed is not None:
            raise ValueError(f"未完成的检查不应带偏移：{res.result_id}")
        if res.outcome == "shift" and res.shift_observed is None:
            raise ValueError(f"偏移结果缺少偏移值：{res.result_id}")
    for hist in s.check_history:
        if hist.outcome not in {"completed", "undone"}:
            raise ValueError(f"既往校验状态未知：{hist.history_id}")
    for rep in s.meter_replacements:
        meter = next((m for m in s.meters if m.meter_id == rep.meter_id), None)
        if meter is None:
            raise ValueError(f"换表对应仪表不存在：{rep.replacement_id}")
        if rep.effective_at.date() < meter.calibration_from:
            raise ValueError(f"换表早于校准期：{rep.replacement_id}")
        if rep.error_bound <= 0 or rep.range_max <= 0:
            raise ValueError(f"换表契约非法：{rep.replacement_id}")
    for meter in s.meters:
        if meter.error_bound <= 0 or meter.range_max <= 0:
            raise ValueError(f"仪表契约非法：{meter.meter_id}")
        if meter.calibration_from > meter.calibration_to:
            raise ValueError(f"校准期倒置：{meter.meter_id}")


def _group_terms(s: Sources) -> list[list[BoundaryTerm]]:
    groups: dict[str, list[BoundaryTerm]] = {}
    for term in s.flow_boundary:
        groups.setdefault(term.boundary, []).append(term)
    return list(groups.values())


def _check_conservation_shape(s: Sources, terms: list[BoundaryTerm]) -> None:
    """同一边界必须有恰好一正一负两类项，否则签名守恒无法解释。"""
    if not terms:
        return
    positive = [t for t in terms if t.coefficient > 0]
    negative = [t for t in terms if t.coefficient < 0]
    if not positive or not negative:
        raise ValueError(f"边界缺少流入或流出项：{terms[0].boundary}")
    seen = [t.meter_id for t in terms]
    if len(set(seen)) != len(seen):
        raise ValueError(f"边界重复计量同一只表：{terms[0].boundary}")
    for term in terms:
        if s.meter(term.meter_id).boundary != term.boundary:
            raise ValueError(f"边界与仪表归属不一致：{term.term_id}")
