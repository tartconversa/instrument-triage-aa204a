"""共同工况基线、残差证据与候选解释集合。

分析分三层，刻意分开：

1. 基线层：按运行档分别取中位数；每个测点先扣除本档中心，再除以本档
   典型水平，得到"相对档位基准的比例"。跨测点的比例中位数即共同负荷，
   它由所有测点共享，单个变送器只能影响其中很小一份。直接对原始读数
   画控制线，会在负荷切换时反复报异常。
2. 证据层：同一变送器下的残差取平均，形成变送器合成分数；分别检测
   "持续台阶"（前后两段均值差）和"单日尖峰"。同变送器的测点共享一个
   证据单元，不拆成多张工单。
3. 解释层：每个证据单元保留多个候选解释（共享偏移、真实再分配、未计量
   流量、已确认运行事件、换表、抽样波动……），用边界守恒和当时已知事实
   逐条判断"可能/矛盾/证据不足"。矛盾的候选记录排除理由而不删除，方便
   回看当时的判断依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import median

from .sources import Sources

MIN_REFERENCE_DAYS = 6
Z_CAP = 9.0
STEP_T = 4.0
SPIKE_Z = 4.0
MIN_STEP_SIDE = 4
# 计量尺度下限：读数精度约 0.001 t，日内重复采样降低噪声；低于此值的
# 尺度只说明数据过于平滑，不能当成无限精度。
MIN_SCALE = 0.25
# 迭代剔除时最多重估几轮，以及判为"已解释"的变送器需要达到的 t 值。
MAX_EXCLUSION_ROUNDS = 3


def mad(values: list[float]) -> float:
    if not values:
        return 0.0
    center = median(values)
    return 1.4826 * median([abs(v - center) for v in values])


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    center = mean(values)
    return (sum((v - center) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def diff_scale(values: list[float]) -> float:
    """相邻观测差分的稳健尺度：对慢漂移稳健，衡量"日常读数波动"。"""
    diffs = [b - a for a, b in zip(values, values[1:])]
    if len(diffs) < 2:
        return MIN_SCALE
    return max(mad(diffs) / (2 ** 0.5), MIN_SCALE)


@dataclass
class DayRow:
    day: str
    regime: str
    values: dict[str, float]
    counts: dict[str, int]
    known_at: str


@dataclass
class MeterBaseline:
    meter_id: str
    transmitter: str
    boundary: str
    centers: dict[str, float]
    scale: float


@dataclass
class Observation:
    day: str
    meter_id: str
    transmitter: str
    boundary: str
    regime: str
    value: float
    expected: float
    residual: float
    z: float
    load_ratio: float
    in_reference: bool


@dataclass
class TransmitterScore:
    day: str
    regime: str
    score: float
    per_meter_z: dict[str, float]


@dataclass
class Hypothesis:
    hypothesis_id: str
    kind: str
    label: str
    fit: str  # possible / supported / contradicted / insufficient
    magnitude: float | None
    evidence: str
    scope: str
    known_at: str | None = None
    covers: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    resolves_with: tuple[str, ...] = ()
    note: str = ""

    @property
    def live(self) -> bool:
        return self.fit in {"possible", "supported", "insufficient"}

    def as_dict(self) -> dict:
        return {
            "id": self.hypothesis_id,
            "kind": self.kind,
            "label": self.label,
            "fit": self.fit,
            "magnitude": None if self.magnitude is None else round(self.magnitude, 2),
            "evidence": self.evidence,
            "scope": self.scope,
            "known_at": self.known_at,
            "covers": list(self.covers),
            "requires": list(self.requires),
            "resolves_with": list(self.resolves_with),
            "note": self.note,
        }


@dataclass
class Episode:
    episode_id: str
    shape: str  # step / spike
    boundary: str
    meters: tuple[str, ...]
    transmitter: str
    start: str
    end: str
    days: int
    regime: str
    score: float
    per_meter_shift: dict[str, float]
    per_meter_scale: dict[str, float]
    balance_delta: float | None
    balance_envelope: tuple[float, float] | None
    hypotheses: list[Hypothesis] = field(default_factory=list)
    resolved: dict | None = None

    @property
    def shift(self) -> float:
        return mean(list(self.per_meter_shift.values()))

    @property
    def strongest(self) -> float:
        return max((abs(v) for v in self.per_meter_shift.values()), default=0.0)

    @property
    def live(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.live and h.kind != "sample"]

    def as_dict(self) -> dict:
        return {
            "id": self.episode_id,
            "shape": self.shape,
            "boundary": self.boundary,
            "meters": list(self.meters),
            "transmitter": self.transmitter,
            "start": self.start,
            "end": self.end,
            "days": self.days,
            "regime": self.regime,
            "score": round(self.score, 2),
            "shift": round(self.shift, 2),
            "per_meter_shift": {k: round(v, 2) for k, v in sorted(self.per_meter_shift.items())},
            "per_meter_scale": {k: round(v, 2) for k, v in sorted(self.per_meter_scale.items())},
            "balance_delta": None if self.balance_delta is None else round(self.balance_delta, 2),
            "balance_envelope": list(self.balance_envelope) if self.balance_envelope else None,
            "hypotheses": [h.as_dict() for h in self.hypotheses],
            "resolved": self.resolved,
        }


def day_key(stamp: datetime) -> str:
    return stamp.date().isoformat()


def build_days(sources: Sources, as_of: datetime) -> list[DayRow]:
    """按生效日聚合读数；只使用 as_of 时点已经可知的观测。

    M1/M2 每日两次、其他每日一次——先求当日均值，再让每日等权，
    避免同一天因采样次数多而被重复计入。
    """
    buckets: dict[str, dict] = {}
    for reading in sources.readings:
        if reading.known_at > as_of:
            continue
        key = day_key(reading.effective_at)
        bucket = buckets.setdefault(
            key, {"values": {}, "counts": {}, "regime": reading.regime, "known": []}
        )
        bucket["regime"] = reading.regime
        bucket["values"].setdefault(reading.meter_id, []).append(reading.value)
        bucket["counts"][reading.meter_id] = bucket["counts"].get(reading.meter_id, 0) + 1
        bucket["known"].append(reading.known_at)
    rows: list[DayRow] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        rows.append(
            DayRow(
                day=key,
                regime=bucket["regime"],
                values={m: mean(v) for m, v in bucket["values"].items()},
                counts=dict(bucket["counts"]),
                known_at=max(bucket["known"]).isoformat(),
            )
        )
    return rows


def regime_runs(days: list[DayRow]) -> list[dict]:
    runs: list[dict] = []
    for row in days:
        if not runs or runs[-1]["regime"] != row.regime:
            runs.append({"regime": row.regime, "start": row.day, "end": row.day, "days": 0})
        runs[-1]["end"] = row.day
        runs[-1]["days"] += 1
    return runs


def reference_days(days: list[DayRow]) -> list[str]:
    """每个运行档取最早的 6 天作参考；慢漂移不会被后段观测重新吸收。"""
    ref: list[str] = []
    for run in regime_runs(days):
        group = [d.day for d in days if run["start"] <= d.day <= run["end"]]
        ref.extend(group[:MIN_REFERENCE_DAYS])
    return sorted(ref)


def build_baselines(
    sources: Sources, days: list[DayRow], ref: list[str]
) -> tuple[dict[str, MeterBaseline], list[str]]:
    ref_set = set(ref)
    by_regime: dict[str, list[DayRow]] = {}
    for row in days:
        if row.day in ref_set:
            by_regime.setdefault(row.regime, []).append(row)

    centers: dict[str, dict[str, float]] = {}
    for regime, rows in by_regime.items():
        for meter_row in sources.meters:
            vals = [r.values[meter_row.meter_id] for r in rows if meter_row.meter_id in r.values]
            if vals:
                centers.setdefault(meter_row.meter_id, {})[regime] = median(vals)

    baselines: dict[str, MeterBaseline] = {}
    for meter_row in sources.meters:
        meter = meter_row.meter_id
        if meter not in centers:
            continue
        # 尺度用全时段相邻差分：不会因异常段被抬高，也不依赖参考窗长度。
        series = [r.values[meter] for r in days if meter in r.values]
        baselines[meter] = MeterBaseline(
            meter_id=meter,
            transmitter=meter_row.transmitter,
            boundary=meter_row.boundary,
            centers=centers[meter],
            scale=diff_scale(series),
        )
    return baselines, sorted(ref_set)


def load_ratio(
    row: DayRow, baselines: dict[str, MeterBaseline], excluded: set[str]
) -> float:
    """共同负荷比例：先按变送器取中位数，再在变送器之间取中位数。

    同一个变送器下的测点共享通道，不能各投一票——否则一只变送器偏移
    （例如 T1 的 M1/M2 同时抬高）就会把"共同负荷"整体抬高，把自己的
    异常吸收掉。先按变送器聚合，使三台共享设备各占一票。
    """
    usable = [
        (meter, base)
        for meter, base in baselines.items()
        if meter in row.values
        and row.regime in base.centers
        and base.transmitter not in excluded
        and base.centers[row.regime]
    ]
    if not usable:
        return 1.0
    ratios = {meter: row.values[meter] / base.centers[row.regime] for meter, base in usable}
    # 一步截尾：先取中位数，再把偏离超过 2.5 倍尺度的测点剔除后重估。
    # 单个极端读数（尖峰、掉线）因此不会把共同负荷整体带偏。
    for _ in range(2):
        center = median(list(ratios.values()))
        if len(ratios) <= 3:
            break
        kept = {
            meter: ratio
            for meter, ratio in ratios.items()
            if abs(row.values[meter] - baselines[meter].centers[row.regime] * center)
            <= 2.5 * baselines[meter].scale
        }
        if len(kept) == len(ratios) or len(kept) < 3:
            break
        ratios = kept
    return median(list(ratios.values()))


def residual_series(
    days: list[DayRow],
    baselines: dict[str, MeterBaseline],
    load: dict[str, float],
    ref: set[str],
) -> list[Observation]:
    out: list[Observation] = []
    for row in days:
        ratio = load.get(row.day, 1.0)
        for meter, base in baselines.items():
            if meter not in row.values or row.regime not in base.centers:
                continue
            expected = base.centers[row.regime] * ratio
            residual = row.values[meter] - expected
            z = residual / base.scale
            out.append(
                Observation(
                    day=row.day,
                    meter_id=meter,
                    transmitter=base.transmitter,
                    boundary=base.boundary,
                    regime=row.regime,
                    value=row.values[meter],
                    expected=expected,
                    residual=residual,
                    z=z,
                    load_ratio=ratio,
                    in_reference=row.day in ref,
                )
            )
    return out


def transmitter_scores(
    days: list[DayRow], baselines: dict[str, MeterBaseline]
) -> dict[str, list[TransmitterScore]]:
    """把同一变送器下各测点的 z 取平均——共享证据单元。"""
    by_tx: dict[str, dict[str, TransmitterScore]] = {}
    for meter, base in baselines.items():
        by_tx.setdefault(base.transmitter, {})
    for row in days:
        for meter, base in baselines.items():
            if meter not in row.values or row.regime not in base.centers:
                continue
            expected = base.centers[row.regime] * load_ratio(row, baselines, excluded=set())
            z = (row.values[meter] - expected) / base.scale
            slot = by_tx[base.transmitter].setdefault(
                row.day, TransmitterScore(row.day, row.regime, 0.0, {})
            )
            slot.per_meter_z[meter] = z
    out: dict[str, list[TransmitterScore]] = {}
    for transmitter, per_day in by_tx.items():
        scores = []
        for day in sorted(per_day):
            slot = per_day[day]
            slot.score = mean(list(slot.per_meter_z.values()))
            scores.append(slot)
        out[transmitter] = scores
    return out


def best_step(scores: list[float]) -> tuple[float, int, float]:
    """返回 (t 值, 切分位置, 台阶幅度)。

    用两段的中位数而不是均值：单日尖峰不会把"台阶"造出来。
    """
    n = len(scores)
    if n < 2 * MIN_STEP_SIDE:
        return 0.0, 0, 0.0
    sigma = max(diff_scale(scores), 0.35)
    best = (0.0, 0, 0.0)
    for k in range(MIN_STEP_SIDE, n - MIN_STEP_SIDE + 1):
        before, after = scores[:k], scores[k:]
        se = sigma * (1 / len(before) + 1 / len(after)) ** 0.5
        if se <= 0:
            continue
        b_med, a_med = median(before), median(after)
        delta = a_med - b_med
        t = delta / se
        if abs(t) > abs(best[0]):
            best = (t, k, delta)
    return best


def _trim_step(values: list[float], split: int, delta: float) -> tuple[int, int]:
    """把台阶段的起止收回到越过半幅的观测；用于报告真实起点。"""
    half = abs(delta) / 2
    direction = 1 if delta >= 0 else -1
    start = split
    while start < len(values) - 1 and (values[start] - values[split - 1]) * direction < half * 0.5:
        start += 1
    end = len(values) - 1
    baseline = median(values[:split])
    while end > start and (values[end] - baseline) * direction < half * 0.5:
        end -= 1
    return start, end


def local_spikes(
    series: list[TransmitterScore], regimes: list[str] | None = None
) -> list[tuple[int, float]]:
    """与前后各两天比较的单日偏离；持续台阶解释不了的尖峰单独保留。

    除合成分数偏离外，还要求至少一个测点自身越过 3 倍尺度——否则只是
    两个测点小幅反向波动被平均成了尖峰。
    """
    scores = [s.score for s in series]
    sigma = max(diff_scale(scores), 0.35)
    found = []
    for index, row in enumerate(series):
        # 只和同一运行档内的相邻日比较；跨运行档的差异是负荷，不是尖峰。
        neighbours = [
            scores[j]
            for j in range(max(0, index - 2), min(len(series), index + 3))
            if j != index and (regimes is None or regimes[j] == regimes[index])
        ]
        if len(neighbours) < 2:
            continue
        deviation = (row.score - median(neighbours)) / (sigma * 1.25)
        meter_peak = max((abs(v) for v in row.per_meter_z.values()), default=0.0)
        if abs(deviation) >= SPIKE_Z and meter_peak >= 3.0:
            found.append((index, deviation))
    return found


def detect_episodes(
    sources: Sources,
    days: list[DayRow],
    scores: dict[str, list[TransmitterScore]],
    baselines: dict[str, MeterBaseline],
    balance: dict,
) -> list[Episode]:
    day_index = {d.day: d for d in days}
    episodes: list[Episode] = []
    for transmitter in sorted(scores):
        series = scores[transmitter]
        values = [s.score for s in series]
        keys = [s.day for s in series]
        t, split, delta = best_step(values)
        flagged_indices: set[int] = set()
        if abs(t) >= STEP_T:
            # 中位数切分可能把台阶前几天也划进后段（它们对中位数影响小），
            # 这里按"是否越过半幅"把边界收回到真实起点。
            start_index, end_index = _trim_step(values, split, delta)
            if end_index - start_index + 1 < MIN_STEP_SIDE:
                start_index, end_index = split, len(series) - 1
            flagged_indices.update(range(start_index, len(series)))
            episodes.append(
                _make_episode(
                    sources,
                    keys,
                    series,
                    baselines,
                    balance,
                    transmitter,
                    start=keys[start_index],
                    end=keys[end_index],
                    shape="step",
                    score=t,
                    day_index=day_index,
                )
            )
        regimes = [s.regime for s in series]
        for index, z in local_spikes(series, regimes):
            if index in flagged_indices:
                continue
            episodes.append(
                _make_episode(
                    sources,
                    keys,
                    series,
                    baselines,
                    balance,
                    transmitter,
                    start=keys[index],
                    end=keys[index],
                    shape="spike",
                    score=z,
                    day_index=day_index,
                )
            )
    episodes.sort(key=lambda e: (e.start, e.transmitter))
    for number, episode in enumerate(episodes, 1):
        episode.episode_id = f"E{number}-{episode.transmitter}-{episode.start}"
    return episodes


def _make_episode(
    sources: Sources,
    keys: list[str],
    series: list[TransmitterScore],
    baselines: dict[str, MeterBaseline],
    balance: dict,
    transmitter: str,
    start: str,
    end: str,
    shape: str,
    score: float,
    day_index: dict[str, DayRow],
) -> Episode:
    rows = [s for s in series if start <= s.day <= end]
    all_meters = sorted({m for s in rows for m in s.per_meter_z})
    shifts: dict[str, float] = {}
    scales: dict[str, float] = {}
    for meter in all_meters:
        base = baselines[meter]
        zs = [s.per_meter_z[meter] for s in rows if meter in s.per_meter_z]
        shifts[meter] = mean([z * base.scale for z in zs])
        scales[meter] = base.scale
    # 只有实际移动的测点算作本证据单元的成员；另一只表的稳定读数本身就是
    # "单表偏移"或"共享设备"的判断依据，不能当成覆盖对象。
    meters = [
        m for m in all_meters
        if abs(shifts[m]) >= 0.5 * scales[m]
    ] or all_meters
    regimes = sorted({day_index[s.day].regime for s in rows if s.day in day_index})
    boundaries = sorted({baselines[m].boundary for m in meters})
    boundary = "/".join(boundaries)
    balance_delta, envelope = _balance_shift(balance, boundary, start, end, shape)
    return Episode(
        episode_id="tmp",
        shape=shape,
        boundary=boundary,
        meters=tuple(meters),
        transmitter=transmitter,
        start=start,
        end=end,
        days=len(rows),
        regime="/".join(regimes),
        score=score,
        per_meter_shift=shifts,
        per_meter_scale=scales,
        balance_delta=balance_delta,
        balance_envelope=envelope,
    )


def _balance_shift(
    balance: dict, boundary: str, start: str, end: str, shape: str
) -> tuple[float | None, tuple[float, float] | None]:
    rows = balance.get("daily", {}).get(boundary, {})
    envelope = balance.get("envelope", {}).get(boundary)
    env = (envelope["low"], envelope["high"]) if envelope else None
    if not rows:
        return None, env
    if shape == "spike":
        normal = [v for d, v in rows.items() if d != start]
        during = [v for d, v in rows.items() if d == start]
    else:
        normal = [v for d, v in rows.items() if d < start]
        during = [v for d, v in rows.items() if d >= start]
    if not during or not normal:
        return None, env
    return mean(during) - mean(normal), env


def attach_hypotheses(
    sources: Sources,
    episodes: list[Episode],
    balance: dict,
    as_of: datetime,
) -> None:
    for episode in episodes:
        episode.hypotheses = _hypotheses(sources, episode, balance, as_of)


def _hypotheses(
    sources: Sources, episode: Episode, balance: dict, as_of: datetime
) -> list[Hypothesis]:
    boundary = episode.boundary
    tx = episode.transmitter
    meters = episode.meters
    boundaries = boundary.split("/") if boundary else []
    moving = [m for m, v in episode.per_meter_shift.items() if abs(v) > 1e-9]
    options: list[Hypothesis] = []

    # 0) 抽样波动
    persisted = episode.shape == "step" and episode.days >= 3
    options.append(
        Hypothesis(
            episode.episode_id + ":sample",
            "sample",
            "单日抽样波动",
            "contradicted" if persisted else "possible",
            episode.shift,
            "持续多日、两个测点同向的台阶不能由单日抽样解释"
            if persisted
            else "只出现一天或幅度接近日常波动",
            f"sample:{episode.episode_id}",
        )
    )

    # 1) 变送器共享偏移
    joint = len(moving) >= 2
    # 两个测点是否以一致的量移动：共享偏移应作用于同一变送器通道。
    consistency = None
    if joint:
        vals = [episode.per_meter_shift[m] for m in moving]
        spread = max(vals) - min(vals)
        pooled = sum(episode.per_meter_scale.get(m, 0.0) for m in moving) / len(moving)
        consistency = spread / max(pooled, 1e-9)
    options.append(
        Hypothesis(
            episode.episode_id + ":tx",
            "transmitter-bias",
            f"变送器 {tx} 共享加法偏移",
            "possible" if joint else "insufficient",
            episode.shift,
            (
                f"同变送器 {tx} 下 {len(moving)} 个测点同向移动约 "
                f"{episode.shift:+.2f} t（两测点差异 {spread:.2f} t，约 {consistency:.1f} 倍尺度）；"
                "单表故障不能同时解释"
                if joint
                else "只有一个测点移动，共享设备不是必要条件"
            ),
            f"transmitter:{tx}",
            covers=tuple(episode.meters),
            resolves_with=(f"inspection:{tx}",),
            note="同 transmitter 的一次现场校验可同时澄清这些测点，只占一个窗口名额",
        )
    )

    # 2) 单表偏移
    if len(moving) == 1 and episode.shape == "spike":
        only = moving[0]
        options.append(
            Hypothesis(
                episode.episode_id + f":single:{only}",
                "meter-bias",
                f"{only} 单日跳变",
                "possible",
                episode.per_meter_shift.get(only, 0.0),
                "只有该测点单日偏离，同变送器另一测点稳定；单表读数问题与瞬时工况都可能",
                f"meter:{only}",
                covers=(only,),
                resolves_with=(f"inspection:{tx}",),
                note="单日证据不足以安排现场；先观察下一次采样是否复现",
            )
        )
    spike_only = len(moving) == 1 and episode.shape == "spike"
    for meter in meters:
        if spike_only:
            break
        shift = episode.per_meter_shift.get(meter, 0.0)
        scale = episode.per_meter_scale.get(meter, 0.0) or 1.0
        others = [v for m, v in episode.per_meter_shift.items() if m != meter]
        if others and all(abs(v) > 0.3 * abs(shift) for v in others):
            fit = "contradicted"
            evidence = "同变送器另一测点同步移动，单表偏移无法解释全部证据"
        elif others and all(abs(v) < 0.5 * scale for v in others):
            fit = "possible"
            evidence = "同变送器另一测点保持稳定，单表偏移足以解释该测点移动"
        else:
            fit = "insufficient"
            evidence = "没有独立证据排除共享设备；单表假设保留但不优先"
        options.append(
            Hypothesis(
                episode.episode_id + f":meter:{meter}",
                "meter-bias",
                f"{meter} 单表偏移",
                fit,
                shift,
                evidence,
                f"meter:{meter}",
                covers=(meter,),
                resolves_with=(f"inspection:{baselines_tx(sources, meter)}",),
            )
        )

    # 3) 真实用量再分配：需要边界守恒同时变化，或成对反向变化。
    if episode.balance_delta is not None:
        if abs(episode.balance_delta) >= 0.5:
            fit = "possible"
            evidence = f"{boundary} 边界守恒量同期变化 {episode.balance_delta:+.2f} t"
        elif joint:
            fit = "possible"
            evidence = (
                f"{boundary} 边界守恒量基本未变（{episode.balance_delta:+.2f} t），"
                "供汽与用汽同步上升——真实再分配或共享偏移在读数上等价"
            )
        else:
            fit = "contradicted"
            evidence = "边界守恒量未变，且测点未成对抵消，再分配解释不成立"
        options.append(
            Hypothesis(
                episode.episode_id + ":reallocation",
                "reallocation",
                f"{boundary} 真实用量再分配",
                fit,
                episode.shift,
                evidence,
                f"boundary:{boundary}",
                note="若成立说明是运行变化；读数本身无法区分，需要校验或运行记录补充",
            )
        )
    else:
        options.append(
            Hypothesis(
                episode.episode_id + ":reallocation",
                "reallocation",
                f"{boundary} 真实用量再分配",
                "insufficient",
                episode.shift,
                "缺少可比较的边界守恒数据",
                f"boundary:{boundary}",
            )
        )

    # 4) 未计量流量/放空：受 flow_boundary 的合法区间约束。
    envelope = episode.balance_envelope
    required = episode.balance_delta if episode.balance_delta is not None else 0.0
    if envelope is not None:
        high = envelope[1]
        if abs(required) <= 1e-9:
            fit = "contradicted"
            evidence = "边界守恒量未变化，未计量流量不能解释同向的测点移动"
        elif required <= high + 1e-9:
            fit = "possible"
            evidence = (
                f"需解释的边界守恒变化 {required:+.2f} t 在资料允许的未计量净流量"
                f"上限 {high:g} t 之内"
            )
        else:
            fit = "contradicted"
            evidence = (
                f"需解释 {required:+.2f} t，超过资料允许的未计量净流量上限 {high:g} t"
            )
        options.append(
            Hypothesis(
                episode.episode_id + f":unknown:{boundary}",
                "flow-unknown",
                f"{boundary} 未计量净流量（放空/泄漏）",
                fit,
                required or None,
                evidence,
                f"boundary:{boundary}",
                note="只能解释边界守恒允许的份额，不能把测点偏移整体吞入未知损失",
            )
        )

    # 5) 已确认放空/检修：只用 as_of 时点已经知道的记录。
    for note in sources.operation_notes:
        if note.known_at > as_of:
            continue
        if note.boundary not in boundaries:
            continue
        flow = note.flow_t
        note_day = note.effective_at.date().isoformat()
        in_window = episode.start <= note_day <= episode.end
        if not in_window:
            fit = "contradicted"
            evidence = (
                f"该记录生效于 {note_day}，不在证据窗口 {episode.start}~{episode.end} 内；"
                f"单次 {flow:g} t 的{'放空' if note.kind == 'vent' else '检修'}"
                "无法解释持续多日的读数台阶"
            )
        elif note.kind == "vent":
            delta = episode.balance_delta or 0.0
            tol = 1.0 + 0.10 * abs(flow)
            fit = "possible" if abs(delta - flow) <= tol else "contradicted"
            evidence = (
                f"放空应使 {note.boundary} 守恒量增加约 {flow:g} t；"
                f"同期实测变化 {delta:+.2f} t"
            )
        else:
            fit = "insufficient"
            evidence = f"检修流量 {flow:g} t，停运范围未记录，方向不足以判定"
        options.append(
            Hypothesis(
                episode.episode_id + f":op:{note.note_id}",
                "operation",
                f"已确认{'放空' if note.kind == 'vent' else '检修'} {note.note_id}",
                fit,
                flow,
                evidence,
                f"operation:{note.note_id}",
                known_at=note.known_at.isoformat(),
                note="运行记录可解释真实流量，但不能证明仪表本身合格；事后确认不改变当时是否应告警",
            )
        )

    # 6) 换表：只在证据起始日之前已经知道时作为候选。
    for meter in meters:
        start_stamp = datetime.fromisoformat(episode.start + "T23:59:59+08:00")
        for rep in sources.known_replacements(meter, start_stamp):
            options.append(
                Hypothesis(
                    episode.episode_id + f":replace:{rep.replacement_id}",
                    "replacement",
                    f"{meter} 换表 {rep.replacement_id}（量程 {rep.range_max:g}，误差界 {rep.error_bound:g}）",
                    "possible",
                    episode.per_meter_shift.get(meter, 0.0),
                    f"换表于 {rep.effective_at.date().isoformat()} 生效、"
                    f"{rep.known_at.date().isoformat()} 可知；前后量程与误差界不同",
                    f"meter:{meter}",
                    known_at=rep.known_at.isoformat(),
                    covers=(meter,),
                    note="换表后的读数必须使用新量程和新误差界，旧契约不得沿用",
                )
            )
    return options


def baselines_tx(sources: Sources, meter: str) -> str:
    return sources.meter(meter).transmitter


def boundary_balance(
    sources: Sources, days: list[DayRow], as_of: datetime
) -> dict:
    """边界守恒量 S = Σ(系数×测点值) 与允许的未计量净流量区间。

    检修/放空记录只在 known_at <= as_of 时用于解释；不会用后来才确认的
    事项去评价当时的告警。
    """
    terms_by_boundary: dict[str, list] = {}
    for term in sources.flow_boundary:
        terms_by_boundary.setdefault(term.boundary, []).append(term)
    daily: dict[str, dict[str, float]] = {}
    envelope: dict[str, dict[str, float]] = {}
    for boundary, terms in terms_by_boundary.items():
        rows: dict[str, float] = {}
        for row in days:
            if all(t.meter_id in row.values for t in terms):
                rows[row.day] = sum(
                    t.coefficient * row.values[t.meter_id] for t in terms
                )
        daily[boundary] = rows
        low = -sum(t.unknown_max for t in terms)
        high = sum(t.unknown_max for t in terms)
        envelope[boundary] = {"low": low, "high": high}
    known_notes = [n for n in sources.operation_notes if n.known_at <= as_of]
    return {
        "daily": {k: dict(sorted(v.items())) for k, v in daily.items()},
        "envelope": envelope,
        "known_operations": [
            {
                "note_id": n.note_id,
                "boundary": n.boundary,
                "kind": n.kind,
                "flow_t": n.flow_t,
                "effective_at": n.effective_at.isoformat(),
                "known_at": n.known_at.isoformat(),
            }
            for n in sorted(known_notes, key=lambda n: n.known_at)
        ],
    }


def load_adjustment_notes(days: list[DayRow], load: dict[str, float]) -> list[dict]:
    notes = []
    for run in regime_runs(days):
        ratios = [load[d.day] for d in days if run["start"] <= d.day <= run["end"] and d.day in load]
        if ratios:
            notes.append(
                {
                    "regime": run["regime"],
                    "start": run["start"],
                    "end": run["end"],
                    "load_ratio": round(median(ratios), 3),
                }
            )
    return notes


def naive_series(
    days: list[DayRow], baselines: dict[str, MeterBaseline]
) -> dict[str, list[dict]]:
    """单测点控制线的对照：不按运行档、不扣共同负荷。

    直接拿最早 6 天的中心与尺度去衡量全部读数，负荷切换当天会被同时
    判为多只表异常——这个对照只用于说明"为什么不能那样画"。
    """
    if not days:
        return {}
    reference = days[:MIN_REFERENCE_DAYS]
    centers = {
        meter: median([d.values[meter] for d in reference if meter in d.values])
        for meter in baselines
        if any(meter in d.values for d in reference)
    }
    out: dict[str, list[dict]] = {meter: [] for meter in centers}
    for row in days:
        for meter, center in centers.items():
            if meter not in row.values:
                continue
            scale = baselines[meter].scale
            z = (row.values[meter] - center) / scale
            out[meter].append(
                {
                    "day": row.day,
                    "value": round(row.values[meter], 2),
                    "expected": round(center, 2),
                    "z": round(max(-12.0, min(12.0, z)), 2),
                }
            )
    return out
