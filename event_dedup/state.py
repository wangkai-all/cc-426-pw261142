"""状态装配：重放全部日志，得到网页与模拟脚本所需的完整当前状态。"""
from __future__ import annotations

from . import common, decisions, metrics, policy

VIEW_NAMES = {"conservative": "保守", "lenient": "宽松", "adjudicated": "裁决"}


def load_state() -> dict:
    records = common.read_jsonl(common.RECORDS)
    edges = common.read_jsonl(common.CANDIDATES)
    decs = decisions.load()
    rep = decisions.replay(decs)
    views = policy.build_views(records, edges, rep["overrides"])
    by_rid = {r["rid"]: r for r in records}
    by_eid = {e["eid"]: e for e in edges}

    # 待人工裁决队列：宽松接受但保守不接受的"证据不足区间"，且尚无生效裁决
    queue = []
    for e in edges:
        if not e["auto_lenient"] or e["auto_conservative"]:
            continue
        if e["eid"] in rep["overrides"]:
            continue
        queue.append(e)
    queue.sort(key=lambda e: -e["score"])

    # 未解决部分汇总
    unresolved = {
        "pending_edges": queue,
        "chain_risk": views["lenient"].get("chain_risk", []),
        "blocked_merges": views["adjudicated"].get("blocked", []),
        "conflicts": rep["conflicts"],
        "no_candidate_records": sorted(
            r["rid"] for r in records
            if not any(r["rid"] in (e["a"], e["b"]) for e in edges)),
    }

    return {
        "records": records, "edges": edges, "decisions": decs,
        "by_rid": by_rid, "by_eid": by_eid,
        "overrides": rep["overrides"], "reverted": rep["reverted"],
        "views": views, "queue": queue, "unresolved": unresolved,
        "version": common.state_version(),
        "metrics_runs": common.read_jsonl(common.METRICS_RUNS),
        "current_metrics": metrics.current_run(),
    }


def edge_status(state: dict, eid: str) -> str:
    """边在裁决视图中的状态：confirmed / rejected / auto_cons / 待裁决 / auto_reject"""
    e = state["by_eid"][eid]
    if eid in state["overrides"]:
        return state["overrides"][eid][0]
    if e["auto_conservative"]:
        return "auto_conservative"
    if e["auto_lenient"]:
        return "pending"  # 宽松接受、保守不接受 -> 待人工裁决
    return "auto_reject"


def describe_edge(state: dict, eid: str) -> str:
    e = state["by_eid"][eid]
    ra, rb = state["by_rid"][e["a"]], state["by_rid"][e["b"]]
    return (f"{eid}：{ra['source']}:{ra['src_id']}「{ra['text']}」 vs "
            f"{rb['source']}:{rb['src_id']}「{rb['text']}」")
