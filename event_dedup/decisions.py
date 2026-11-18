"""人工裁决日志：只追加；撤销 = 追加一条 revert 补偿条目；原始记录永不改动。

裁决类型：
  confirm_edge  人工确认某条候选边（覆盖自动策略的"不接受"）
  reject_edge   人工驳回某条候选边（覆盖自动策略的"接受"）
  revert        撤销某条既往裁决（target_did），使被撤销者失效

交错检测：每条裁决记录提交人看到的日志长度 base_dseq。若针对同一条边，
在 (base_dseq, 本人提交) 之间已有他人裁决，则标记 stale_based ——
即"两次裁决交错"：后写者基于的是旧视图。同一条边多次生效裁决时
后写覆盖先写（last-writer-wins），并在冲突列表中显式呈现，绝不静默。
"""
from __future__ import annotations

from . import common

DECISION_TYPES = ("confirm_edge", "reject_edge", "revert")


def load() -> list[dict]:
    return common.read_jsonl(common.DECISIONS)


def submit(dtype: str, reviewer: str, note: str = "", eid: str | None = None,
           target_did: str | None = None, base_dseq: int | None = None) -> dict:
    """追加一条裁决。base_dseq 记录提交人所见日志长度，用于交错检测；
    网页表单携带的是页面渲染时的日志长度，可能已过期（这正是交错来源）。"""
    assert dtype in DECISION_TYPES, f"未知裁决类型 {dtype}"
    decisions = load()
    d = {
        "did": f"d{len(decisions) + 1:04d}",
        "type": dtype,
        "eid": eid,
        "target_did": target_did,
        "reviewer": reviewer,
        "note": note,
        "base_dseq": len(decisions) if base_dseq is None else base_dseq,
        "ts": common.now_iso(),
    }
    common.append_jsonl(common.DECISIONS, d)
    return d


def replay(decisions: list[dict]) -> dict:
    """重放裁决日志 -> 边的有效覆盖、冲突列表、被撤销集合。

    返回 {overrides: {eid: (status, decision)}, conflicts: [...], reverted: {did}}
    status ∈ confirmed / rejected
    """
    reverted = {d["target_did"] for d in decisions if d["type"] == "revert"}
    effective = [d for d in decisions
                 if d["type"] in ("confirm_edge", "reject_edge") and d["did"] not in reverted]

    by_eid: dict[str, list[dict]] = {}
    for d in effective:
        by_eid.setdefault(d["eid"], []).append(d)

    overrides: dict[str, tuple[str, dict]] = {}
    conflicts: list[dict] = []
    seq = {d["did"]: i + 1 for i, d in enumerate(decisions)}
    for eid, ds in by_eid.items():
        ds.sort(key=lambda d: seq[d["did"]])
        winner = ds[-1]
        status = "confirmed" if winner["type"] == "confirm_edge" else "rejected"
        overrides[eid] = (status, winner)
        if len(ds) > 1:
            conflicts.append({
                "kind": "last_writer_wins", "eid": eid,
                "detail": f"边 {eid} 有 {len(ds)} 条生效裁决，最后生效："
                          f"{winner['did']}（{winner['reviewer']} {winner['type']}）",
                "dids": [d["did"] for d in ds],
            })
        for d in ds:
            others = [o for o in ds if seq[o["did"]] < seq[d["did"]] and seq[o["did"]] > d["base_dseq"]]
            if others:
                conflicts.append({
                    "kind": "stale_based", "eid": eid, "did": d["did"],
                    "detail": f"裁决 {d['did']}（{d['reviewer']}）基于日志位置 {d['base_dseq']}，"
                              f"但 {', '.join(o['did'] for o in others)} 在那之后、"
                              f"该裁决之前已改动同一条边 —— 两次裁决交错",
                    "dids": [d["did"]] + [o["did"] for o in others],
                })
    return {"overrides": overrides, "conflicts": conflicts, "reverted": reverted}
