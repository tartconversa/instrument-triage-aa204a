"""CLI: 计算并输出一次分析快照（JSON），或写入状态文件。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .engine import analyse


def parse_when(value: str | None) -> datetime | None:
    if value is None:
        return None
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise argparse.ArgumentTypeError("--as-of 必须带 +08:00 时区")
    return stamp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="steamcheck",
        description="共同工况下的仪表复核优先级分析（只给建议，不控制设备）",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="资料包根目录（含 data/ 与 docs/）",
    )
    parser.add_argument(
        "--as-of",
        type=parse_when,
        default=None,
        help="当时可知时点（带 +08:00）；缺省取资料中最新的 known_at",
    )
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 快照")
    parser.add_argument("--summary", action="store_true", help="输出便于阅读的摘要")
    parser.add_argument(
        "--write-state",
        type=Path,
        default=None,
        help="把快照写入该路径（用于网页视图）",
    )
    args = parser.parse_args(argv)

    snapshot = analyse(args.root, args.as_of)
    payload = snapshot.as_dict()
    if args.write_state:
        args.write_state.parent.mkdir(parents=True, exist_ok=True)
        args.write_state.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=1)
        print()
    if args.summary or not (args.json or args.write_state):
        _print_summary(payload)
    return 0


def _print_summary(payload: dict) -> None:
    print(f"分析时点 {payload['as_of']}（{payload['as_of_kind']}），数据截至 {payload['data_through']}")
    print("运行档：", "；".join(
        f"{r['regime']} {r['start']}~{r['end']}（{r['days']} 天）" for r in payload["regimes"]
    ))
    print()
    if not payload["episodes"]:
        print("没有超过证据阈值的残差。")
    for episode in payload["episodes"]:
        print(
            f"[{episode['id']}] {episode['start']}~{episode['end']} "
            f"{episode['transmitter']}（{', '.join(episode['meters'])}）"
            f" 读数台阶 {episode['shift']:+.2f} t，边界守恒同期变化 "
            f"{episode['balance_delta']:+.2f} t"
            if episode["balance_delta"] is not None
            else f"[{episode['id']}] {episode['start']}~{episode['end']}"
        )
        for hypothesis in episode["hypotheses"]:
            mark = {"possible": "可能", "contradicted": "矛盾", "insufficient": "证据不足"}.get(
                hypothesis["fit"], hypothesis["fit"]
            )
            print(f"    - {mark}：{hypothesis['label']}｜{hypothesis['evidence']}")
        if episode.get("resolved"):
            print(f"    → 现场结果：{episode['resolved']['message']}")
    print()
    for impact in payload["impacts"]:
        verdict = "高于" if impact["measured_verdict"] == "over" else "低于"
        print(
            f"{impact['boundary']} 单耗 {impact['measured']:.3f}，{verdict}上限 {impact['limit']:g}"
            f"（{impact['analysis_start']}~{impact['analysis_end']}，{impact['window_days']} 天）"
        )
        if impact["attribution"] == "open":
            print(f"    归因未决，候选行动：{'、'.join(impact['action_labels'])}")
            print(
                f"    若供汽表偏移成立，单耗约为 {impact['if_bias_low']:.3f}；"
                f"阈值判断{'不变' if impact['threshold_invariant'] else '会改变'}"
            )
        elif impact["corrected"] != impact["measured"]:
            print(f"    已按现场结果修正为 {impact['corrected']:.3f}")
    print()
    print("复核建议（按减少的决策不确定性 / 现场代价排序）：")
    if not payload["candidates"]:
        print("    当前窗口没有可执行且能减少决策不确定性的检查。")
    for candidate in payload["candidates"]:
        print(
            f"    {candidate['inspection_id']} → 变送器 {candidate['transmitter']}"
            f"（窗口 {candidate['window']}，费用 {candidate['cost']:g} 元，"
            f"精度 {candidate['precision']:g} t）"
        )
        for reason in candidate["reason"]:
            print(f"        · {reason}")
    excluded = payload["ranking"].get("excluded", [])
    if excluded:
        print("    未选中的检查：")
        for item in excluded:
            print(f"        · {item['inspection_id']}：{'；'.join(item['blocked_by']) or '价值较低'}")
    print()
    print("仍不能安全形成的结论：")
    unresolved = payload["coverage"].get("cannot_safely_conclude") or {}
    if unresolved:
        for episode, labels in unresolved.items():
            print(f"    · {episode}：{'、'.join(labels)} 无法区分")
    else:
        print("    · 无（本时点没有因预算/名额被迫保留的关键解释）")
    for caveat in payload["caveats"]:
        print(f"    · {caveat}")


if __name__ == "__main__":
    raise SystemExit(main())
