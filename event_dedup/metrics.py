"""指标计算器（独立运行）：对三个视图分别计算指标，基于快照、迟到标记 stale。

  python3 -m event_dedup.metrics [--note 说明] [--delay 秒]

迟到防护：启动时读取快照与版本 base_version；计算完成后、写回前重新读取
当前版本。若不一致，说明计算期间状态已变（例如来了新裁决），本次结果
标记 stale=True —— 保留在历史中可查，但不会被当作"当前指标"。
--delay 用于模拟一次慢计算，演示"旧计算晚到"。
"""
from __future__ import annotations

import argparse
import time
from collections import Counter, defaultdict

from . import common, decisions, policy


def compute_view_metrics(records: list[dict], clusters: list[list[str]]) -> dict:
    by_rid = {r["rid"]: r for r in records}
    member_of = {}
    for i, c in enumerate(clusters):
        for rid in c:
            member_of[rid] = i

    per_source_records = Counter(r["source"] for r in records)
    per_source_events = defaultdict(set)
    per_category_events = Counter()
    cross_source = 0
    exclusive = Counter()  # 只被单一来源覆盖的事件
    for i, c in enumerate(clusters):
        sources = {by_rid[rid]["source"] for rid in c}
        cats = {by_rid[rid]["category"] for rid in c}
        per_category_events["/".join(sorted(cats))] += 1
        for s in sources:
            per_source_events[s].add(i)
        if len(sources) > 1:
            cross_source += 1
        elif len(sources) == 1:
            exclusive[next(iter(sources))] += 1

    n_events = len(clusters)
    n_records = len(records)
    return {
        "records": n_records,
        "events": n_events,
        "duplicates_collapsed": n_records - n_events,
        "singletons": sum(1 for c in clusters if len(c) == 1),
        "cross_source_events": cross_source,
        "per_category_events": dict(sorted(per_category_events.items())),
        "per_source": {
            s: {
                "records": per_source_records.get(s, 0),
                "events_covered": len(per_source_events.get(s, set())),
                "coverage": round(len(per_source_events.get(s, set())) / n_events, 3) if n_events else 0,
                "exclusive_events": exclusive.get(s, 0),
            } for s in sorted({r["source"] for r in records})
        },
    }


def compute_all(records: list[dict], edges: list[dict], decs: list[dict]) -> dict:
    rep = decisions.replay(decs)
    views = policy.build_views(records, edges, rep["overrides"])
    out = {}
    for view, res in views.items():
        m = compute_view_metrics(records, res["clusters"])
        m["accepted_edges"] = len(res["accepted_eids"])
        if view == "lenient":
            m["chain_risk_clusters"] = len(res.get("chain_risk", []))
        else:
            m["blocked_merges"] = len(res.get("blocked", []))
        out[view] = m
    return out


def run(note: str = "", delay: float = 0.0) -> dict:
    """执行一次指标计算。delay 模拟慢计算（旧计算晚到）。"""
    base_version, base_str = common.state_version()
    records = common.read_jsonl(common.RECORDS)      # 快照：之后的状态变化
    edges = common.read_jsonl(common.CANDIDATES)     # 不影响本次计算内容
    decs = common.read_jsonl(common.DECISIONS)
    started = common.now_iso()
    metrics = compute_all(records, edges, decs)
    if delay:
        time.sleep(delay)
    finish_version, finish_str = common.state_version()
    stale = finish_version != base_version
    rec = {
        "run_id": f"m{len(common.read_jsonl(common.METRICS_RUNS)) + 1:04d}",
        "note": note,
        "base_version": base_str,
        "finish_version": finish_str,
        "started_at": started,
        "finished_at": common.now_iso(),
        "stale": stale,
        "metrics": metrics,
    }
    common.append_jsonl(common.METRICS_RUNS, rec)
    return rec


def current_run() -> dict | None:
    """最近一次非 stale 的运行（"当前指标"）。"""
    for r in reversed(common.read_jsonl(common.METRICS_RUNS)):
        if not r["stale"]:
            return r
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--note", default="")
    ap.add_argument("--delay", type=float, default=0.0)
    args = ap.parse_args()
    rec = run(note=args.note, delay=args.delay)
    flag = "【已过时 stale：计算期间状态已变化，结果仅存档】" if rec["stale"] else "【当前有效】"
    print(f"[metrics] 运行 {rec['run_id']} {flag} 基准版本 {rec['base_version']} "
          f"-> 完成时版本 {rec['finish_version']}")
    for view, m in rec["metrics"].items():
        print(f"  {view:13s} 事件数 {m['events']:3d}  折叠重复 {m['duplicates_collapsed']:2d}  "
              f"跨来源事件 {m['cross_source_events']:2d}")


if __name__ == "__main__":
    main()
