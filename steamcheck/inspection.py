"""现场复核选项、时间线状态机与"何时能澄清什么"的计算。

规则来自资料口径：
- 窗口容量与预算同时约束；completed 与 undone 都消耗窗口和成本，不释放；
- 同一 transmitter 的现场校验可同时提供多个仪表信息，只占一个名额；
- 结果按 known_at 进入时间线，不能用未来结果评价当时；
- 一个结果只澄清它覆盖的解释，其余候选解释保留；
- 若预算无法区分关键解释，必须明确说明仍不能安全形成的结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .anomaly import Episode
from .sources import Sources


@dataclass
class ResultRecord:
    result_id: str
    inspection_id: str
    transmitter: str
    window: str
    outcome: str
    shift_observed: float | None
    known_at: datetime
    covers: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "result_id": self.result_id,
            "inspection_id": self.inspection_id,
            "transmitter": self.transmitter,
            "window": self.window,
            "outcome": self.outcome,
            "shift_observed": self.shift_observed,
            "known_at": self.known_at.isoformat(),
            "meters": list(self.covers),
        }


def known_results(sources: Sources, as_of: datetime) -> list[ResultRecord]:
    out: list[ResultRecord] = []
    for result in sources.inspection_results:
        if result.known_at > as_of:
            continue
        option = sources.inspection(result.inspection_id)
        out.append(
            ResultRecord(
                result_id=result.result_id,
                inspection_id=result.inspection_id,
                transmitter=option.transmitter,
                window=option.window,
                outcome=result.outcome,
                shift_observed=result.shift_observed,
                known_at=result.known_at,
                covers=tuple(sources.meters_of(option.transmitter)),
            )
        )
    return sorted(out, key=lambda r: r.known_at)


def _reservations(
    sources: Sources, as_of: datetime
) -> list[tuple[str, str, str]]:
    """窗口占用的唯一依据是"当时已经知道的现场结果"。

    资料口径：已完成会消耗窗口与成本，undone 也消耗已实际使用的窗口与费用。
    `inspection_options.status = planned` 只是候选清单状态，不等于已排产；
    更不能因为资料后来登记了结果，就倒推当时那个名额已经被占用——那会
    用未来事实评价当时的决策。
    """
    out: list[tuple[str, str, str]] = []
    for result in sources.inspection_results:
        if result.known_at > as_of:
            continue
        option = sources.inspection(result.inspection_id)
        out.append((option.inspection_id, result.outcome, result.known_at.isoformat()))
    return out


def window_usage(sources: Sources, as_of: datetime) -> dict[str, dict]:
    """每个窗口的实际占用：计划中、已完成、未完成都计入。"""
    usage: dict[str, dict] = {}
    for window in sorted(sources.windows, key=lambda w: w.at):
        usage[window.window] = {
            "window": window.window,
            "at": window.at.isoformat(),
            "capacity": window.capacity,
            "budget": window.budget,
            "slots_used": 0,
            "cost_used": 0.0,
            "inspections": [],
        }
    used_inspections: set[str] = set()
    reservations = _reservations(sources, as_of)
    for inspection_id, outcome, known_at in reservations:
        option = sources.inspection(inspection_id)
        row = usage.setdefault(
            option.window,
            {
                "window": option.window,
                "at": None,
                "capacity": 0,
                "budget": 0.0,
                "slots_used": 0,
                "cost_used": 0.0,
                "inspections": [],
            },
        )
        row["slots_used"] += option.slot_usage
        row["cost_used"] += option.cost
        if option.inspection_id not in used_inspections:
            used_inspections.add(option.inspection_id)
            row["inspections"].append(
                {
                    "inspection_id": option.inspection_id,
                    "transmitter": option.transmitter,
                    "cost": option.cost,
                    "outcome": outcome,
                    "known_at": known_at,
                }
            )
    for window in sources.windows:
        status = "open"
        if window.at <= as_of:
            status = "elapsed"
        usage[window.window]["status"] = status
        usage[window.window]["remaining_slots"] = usage[window.window]["capacity"] - usage[window.window]["slots_used"]
        usage[window.window]["remaining_budget"] = usage[window.window]["budget"] - usage[window.window]["cost_used"]
    return usage


def clarify_episode(episode: Episode, result: ResultRecord, option_precision: float) -> dict:
    """一个现场结果对某个证据单元的影响。

    - shift：若偏移量与读数台阶同量级，支持共享偏移，并给出修正量；
      若偏移接近零，则把读数移动推回"运行/再分配"候选。
    - passed：支持"不是仪表偏移"，保留运行解释。
    - undone：不提供证据，窗口与费用仍已消耗。
    """
    covered = bool(set(result.covers) & set(episode.meters))
    if result.outcome == "undone":
        return {
            "status": "undone",
            "covered": covered,
            "message": "现场未完成，不提供合格证据；该窗口与费用已占用，不得释放",
        }
    if not covered:
        return {"status": "not-covered", "covered": False, "message": "该结果不覆盖此证据单元"}
    shift = episode.shift
    observed = float(result.shift_observed or 0.0)
    if result.outcome == "shift":
        if abs(observed) >= max(1.5 * option_precision, 1.0) and (observed > 0) == (shift > 0):
            return {
                "status": "bias-confirmed",
                "covered": True,
                "message": (
                    f"校验测得 {observed:+.2f} t，与读数台阶 {shift:+.2f} t 同向同量级；"
                    "共享偏移解释获得支持，供汽表读数需按测得偏移修正"
                ),
                "correction": observed,
            }
        return {
            "status": "bias-unconfirmed",
            "covered": True,
            "message": (
                f"校验测得 {observed:+.2f} t，与读数台阶 {shift:+.2f} t 不匹配；"
                "共享偏移解释被削弱，运行/分配解释重新占优"
            ),
        }
    # passed
    return {
        "status": "bias-cleared",
        "covered": True,
        "message": (
            f"校验通过（精度 {option_precision:g} t），读数移动不能归因于仪表偏移；"
            "真实用量再分配/未记录工况成为主要候选，但其自身仍未被证实"
        ),
    }


def apply_results(
    sources: Sources,
    episodes: list[Episode],
    results: list[ResultRecord],
) -> list[dict]:
    """把结果写回证据单元；已完成检查不可撤销。

    同一证据单元先被某个结果澄清时，后续结果不能把它改回去——例如先确认
    偏移、之后另一窗口 undone，已确认的结论仍然成立。
    """
    applied: list[dict] = []
    for result in results:
        option = sources.inspection(result.inspection_id)
        for episode in episodes:
            if episode.resolved is not None:
                continue
            impact = clarify_episode(episode, result, option.precision)
            if impact["covered"] and impact["status"] != "undone":
                episode.resolved = {
                    **impact,
                    "result_id": result.result_id,
                    "inspection_id": result.inspection_id,
                    "window": result.window,
                    "outcome": result.outcome,
                    "shift_observed": result.shift_observed,
                    "known_at": result.known_at.isoformat(),
                }
                applied.append(
                    {
                        "result_id": result.result_id,
                        "episode": episode.episode_id,
                        **episode.resolved,
                    }
                )
    return applied


@dataclass
class Candidate:
    inspection_id: str
    transmitter: str
    window: str
    cost: float
    slot_usage: int
    precision: float
    capacity: int
    budget: float
    covers: tuple[str, ...]
    episodes: tuple[str, ...]
    expected_value: float
    cost_penalty: float
    reason: list[str] = field(default_factory=list)
    marginal_value: float = 0.0
    blocked_by: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "inspection_id": self.inspection_id,
            "transmitter": self.transmitter,
            "window": self.window,
            "cost": self.cost,
            "slot_usage": self.slot_usage,
            "precision": self.precision,
            "capacity": self.capacity,
            "budget": self.budget,
            "covers": list(self.covers),
            "episodes": list(self.episodes),
            "expected_value": round(self.expected_value, 2),
            "cost_penalty": round(self.cost_penalty, 2),
            "marginal_value": round(self.marginal_value, 2),
            "reason": self.reason,
            "blocked_by": self.blocked_by,
        }


def rank_candidates(
    sources: Sources,
    episodes: list[Episode],
    as_of: datetime,
    impacts: list,
    value_lookup,
) -> tuple[list[Candidate], dict, dict]:
    """在当前仍可安排的窗口里比较候选组合。

    规则：
    - 已排入窗口的检查（含已排未到点）先占名额与预算，不释放；
    - 同一变送器的价值按覆盖证据单元的并集计一次，不重复计价；
    - 每个窗口分别枚举可行组合（容量、预算），取价值减现场代价最高者；
    - 同 transmitter 在两份资料中重复出现时，只保留一个，避免把同一台设备
      当成两个独立任务。
    """
    usage = window_usage(sources, as_of)
    open_episodes = [e for e in episodes if not e.resolved]
    per_episode_value: dict[str, float] = {}
    per_episode_reason: dict[str, list[str]] = {}
    for episode in open_episodes:
        value, why = value_lookup(episode)
        per_episode_value[episode.episode_id] = value
        per_episode_reason[episode.episode_id] = why

    # 一个 transmitter 只安排一次：同一变送器在不同窗口的选项互为替代，
    # 不能因为有两个窗口就把它当成两个独立任务。
    dedup: dict[str, Candidate] = {}
    for option in sorted(sources.inspection_options, key=lambda o: (o.window, o.inspection_id)):
        covers = tuple(sources.meters_of(option.transmitter))
        targets = [e for e in open_episodes if set(covers) & set(e.meters)]
        if not targets:
            continue
        expected = sum(per_episode_value[e.episode_id] for e in targets)
        cost_penalty = option.cost
        window = usage.get(option.window, {})
        reason = [
            (
                f"覆盖 {', '.join(covers)}，一次现场校验可同时澄清 {len(targets)} 个证据单元"
                if len(targets) > 1
                else f"覆盖 {', '.join(covers)}"
            )
        ]
        if option.precision:
            reason.append(
                f"校验精度 {option.precision:g} t，小于待区分台阶 {min(abs(e.shift) for e in targets):.2f} t"
            )
        reasons: list[str] = []
        for episode in targets:
            reasons.extend(per_episode_reason[episode.episode_id])
        reason.extend(dict.fromkeys(reasons))
        candidate = Candidate(
            inspection_id=option.inspection_id,
            transmitter=option.transmitter,
            window=option.window,
            cost=option.cost,
            slot_usage=option.slot_usage,
            precision=option.precision,
            capacity=window.get("capacity", 0),
            budget=window.get("budget", 0.0),
            covers=covers,
            episodes=tuple(e.episode_id for e in targets),
            expected_value=expected,
            cost_penalty=cost_penalty,
            reason=reason,
        )
        key = option.transmitter
        if key not in dedup:
            dedup[key] = candidate
        else:
            existing = dedup[key]
            # 覆盖相同解释时，选精度更高、费用更低的那个窗口。
            if (candidate.precision, candidate.cost) < (existing.precision, existing.cost):
                candidate.reason.append(
                    f"与 {existing.inspection_id}（{existing.window}）覆盖同一变送器，"
                    f"按精度 {candidate.precision:g} t / 费用 {candidate.cost:g} 元取此班次"
                )
                dedup[key] = candidate
            else:
                existing.reason.append(
                    f"与 {candidate.inspection_id}（{candidate.window}）覆盖同一变送器，"
                    f"按精度 {existing.precision:g} t / 费用 {existing.cost:g} 元取此班次"
                )
    candidates = list(dedup.values())

    selected: list[Candidate] = []
    by_window: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_window.setdefault(candidate.window, []).append(candidate)
    for window_id, group in sorted(by_window.items()):
        window = usage.get(window_id, {})
        if window.get("remaining_slots", 0) <= 0 or window.get("remaining_budget", 0) <= 0:
            for candidate in group:
                candidate.blocked_by.append(
                    f"窗口 {window_id} 已无名额或预算（已排检查不释放）"
                )
            continue
        if _combo_feasible(group, window):
            chosen = _best_combo(group, window)
            selected.extend(chosen)
            chosen_ids = {c.inspection_id for c in chosen}
            for candidate in group:
                if candidate.inspection_id not in chosen_ids:
                    candidate.blocked_by.append(
                        "同一窗口名额已用于更高价值的变送器检查"
                    )
        else:
            for candidate in group:
                candidate.blocked_by.append(
                    "该窗口内任何可安排组合都无法区分关键解释"
                )
    selected_ids = {c.inspection_id for c in selected}
    for candidate in candidates:
        if candidate.inspection_id not in selected_ids and not candidate.blocked_by:
            candidate.blocked_by.append("同窗口内被更高价值组合占用名额或预算")
    selected.sort(key=lambda c: (-c.marginal_value, c.inspection_id))
    return selected, usage, {
        "policy": (
            "价值 = 有分歧的可行解释所占的错误决定代价 × 0.5（等权假设，资料未给概率）"
            " − 现场代价；同一变送器的覆盖证据单元只计一次"
        ),
        "excluded": [
            c.as_dict() for c in candidates if c.inspection_id not in selected_ids
        ],
    }


def _combo_feasible(group: list[Candidate], window: dict) -> bool:
    capacity = window.get("remaining_slots", 0)
    budget = window.get("remaining_budget", 0.0)
    return any(c.slot_usage <= capacity and c.cost <= budget for c in group)


def _best_combo(group: list[Candidate], window: dict) -> list[Candidate]:
    capacity = window.get("remaining_slots", 0)
    budget = window.get("remaining_budget", 0.0)
    best: tuple[float, list[Candidate]] = (0.0, [])
    n = len(group)

    def score(combo: list[Candidate]) -> float:
        # 价值取覆盖证据单元的并集，避免同一变送器的重复计数。
        covered: dict[str, float] = {}
        for candidate in combo:
            share = candidate.expected_value / max(1, len(candidate.episodes))
            for episode in candidate.episodes:
                covered[episode] = max(covered.get(episode, 0.0), share)
        return sum(covered.values()) - sum(c.cost_penalty for c in combo)

    for mask in range(1, 1 << n):
        combo = [group[i] for i in range(n) if mask >> i & 1]
        if sum(c.slot_usage for c in combo) > capacity:
            continue
        if sum(c.cost for c in combo) > budget:
            continue
        value = score(combo)
        if value > best[0] + 1e-9:
            best = (value, combo)
    if not best[1]:
        return []
    for candidate in best[1]:
        candidate.marginal_value = round(candidate.expected_value - candidate.cost_penalty, 2)
    return best[1]


def inspection_histories(
    sources: Sources, episodes: list[Episode], as_of: datetime
) -> dict[str, dict]:
    """每个变送器的既往校验：用于说明精度与"共同设备"证据，不当作故障答案。"""
    out: dict[str, dict] = {}
    for transmitter in sorted({m.transmitter for m in sources.meters}):
        rows = [
            h
            for h in sources.check_history
            if h.transmitter == transmitter and h.known_at <= as_of
        ]
        completed = [r for r in rows if r.outcome == "completed"]
        out[transmitter] = {
            "checks": len(rows),
            "completed": len(completed),
            "undone": len(rows) - len(completed),
            "mean_shift": round(sum(r.observed_shift for r in completed) / len(completed), 2)
            if completed
            else None,
            "max_abs_shift": round(max((abs(r.observed_shift) for r in completed), default=0.0), 2),
            "error_bound": rows[0].error_bound if rows else None,
        }
    return out


def coverage_summary(
    sources: Sources,
    episodes: list[Episode],
    selected: list[Candidate],
    usage: dict,
) -> dict:
    """哪些影响已解除、哪些仍保留、以及预算无法区分的关键解释。"""
    solved = [e.episode_id for e in episodes if e.resolved]
    open_items = [e for e in episodes if not e.resolved]
    selected_tx = {c.transmitter for c in selected}
    coverable = [
        e.episode_id
        for e in open_items
        if any(set(e.meters) & set(sources.meters_of(tx)) for tx in selected_tx)
    ]
    uncoverable = [e.episode_id for e in open_items if e.episode_id not in coverable]
    unresolved: dict[str, list[str]] = {}
    for episode in open_items:
        if episode.episode_id in coverable:
            continue
        live = [h for h in episode.live if h.fit == "possible"]
        if len({ACTION_BY_KIND.get(h.kind) for h in live if h.kind in ACTION_BY_KIND}) >= 2:
            unresolved[episode.episode_id] = [h.label for h in live]
    windows_full = [
        w
        for w, row in usage.items()
        if row.get("remaining_slots", 0) <= 0 or row.get("remaining_budget", 0) <= 0
    ]
    return {
        "resolved": solved,
        "covered_with_selected": coverable,
        "uncoverable": uncoverable,
        "cannot_safely_conclude": unresolved,
        "windows_exhausted": windows_full,
        "note": (
            "未被选中的检查不等于告警正常；窗口名额/预算无法区分关键解释时，"
            "相关结论应保留为未决"
        ),
    }


ACTION_BY_KIND = {
    "transmitter-bias": "correct-measurement",
    "meter-bias": "correct-measurement",
    "reallocation": "operate-process",
    "flow-unknown": "investigate-loss",
    "operation": "operate-process",
    "replacement": "correct-measurement",
}
