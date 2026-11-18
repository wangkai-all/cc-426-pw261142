"""候选匹配服务（独立运行）：分块 -> 成对可解释证据 -> 候选图（只追加、幂等）。

  python3 -m event_dedup.matcher

设计选择：持久化候选图，而不是只保存分块供每次重建。
人工裁决锚定在稳定的边 ID 上；重跑 matcher 只会发现新边，绝不删除
已有边（否则裁决会失去锚点）。分块索引每次临时重建，仅用于发现候选对。

所有匹配依据都是可解释的证据条目；score 只是证据权重之和，便于排序，
绝不单独作为合并依据——裁决界面展示的永远是证据本身。
"""
from __future__ import annotations

from . import common

# ---- 候选对生成边界（比任何证据阈值都宽，只用于"要不要比较这对"）----
MAX_DT_SEC = 900      # 15 分钟
MAX_DIST_M = 400.0

# ---- 证据权重（全部公开、可调，score = 通过证据的权重之和）----
W_PHONE = 3.0
W_ADDR = 2.0
TIME_TIERS = [(180, 1.5), (360, 1.0), (600, 0.75), (900, 0.4)]      # 秒
GEO_TIERS = [(60, 1.5), (120, 1.0), (250, 0.6), (400, 0.3)]          # 米
TEXT_TIERS = [(0.60, 1.5), (0.45, 1.0), (0.30, 0.5)]                 # Jaccard

CONSERVATIVE_MIN = 3.0   # 保守策略：自动接受阈值
LENIENT_MIN = 1.6        # 宽松策略：自动接受阈值（两者之间 = 待人工裁决区间）


def _tier(value: float, tiers: list[tuple[float, float]]) -> tuple[float, str]:
    for limit, w in tiers:
        if value <= limit:
            return w, f"≤{limit}"
    return 0.0, ""


def compare(ra: dict, rb: dict) -> dict:
    """对一对记录产出全部证据条目（含未通过的，便于审阅时看到全貌）。"""
    dt = abs(ra["ts"] - rb["ts"])
    dist = common.haversine_m(ra["lat"], ra["lon"], rb["lat"], rb["lon"])
    ev = []

    phone_ok = bool(ra.get("phone")) and ra.get("phone") == rb.get("phone")
    ev.append({"key": "phone_exact", "ok": phone_ok, "weight": W_PHONE if phone_ok else 0.0,
               "detail": f"联系电话 {ra.get('phone') or '无'} vs {rb.get('phone') or '无'}"
                         + ("，完全一致" if phone_ok else "")})

    addr_ok = bool(ra.get("addr_num")) and ra.get("addr_num") == rb.get("addr_num") \
        and ra.get("addr_street") and ra.get("addr_street") == rb.get("addr_street")
    ev.append({"key": "addr_match", "ok": addr_ok, "weight": W_ADDR if addr_ok else 0.0,
               "detail": f"地址 {ra.get('addr_street') or '无'} {ra.get('addr_num') or ''} vs "
                         f"{rb.get('addr_street') or '无'} {rb.get('addr_num') or ''}"
                         + ("，门牌与街道一致" if addr_ok else "")})

    tw, tlabel = _tier(dt, TIME_TIERS)
    ev.append({"key": "time_prox", "ok": tw > 0, "weight": tw,
               "detail": f"时间差 {int(dt)} 秒" + (f"（{tlabel} 秒档）" if tlabel else "（超出 900 秒）")})

    gw, glabel = _tier(dist, GEO_TIERS)
    ev.append({"key": "geo_prox", "ok": gw > 0, "weight": gw,
               "detail": f"距离 {dist:.0f} 米" + (f"（{glabel} 米档）" if glabel else "（超出 400 米）")})

    sa, sb = set(ra["tokens"]), set(rb["tokens"])
    jac = len(sa & sb) / len(sa | sb) if sa and sb else 0.0
    xw = 0.0
    for limit, w in TEXT_TIERS:  # 阈值从高到低取第一档
        if jac >= limit:
            xw = w
            break
    ev.append({"key": "text_sim", "ok": xw > 0, "weight": xw,
               "detail": f"文本词集 Jaccard {jac:.2f}（共有词：{', '.join(sorted(sa & sb)) or '无'}）"})

    score = round(sum(e["weight"] for e in ev), 3)
    strong = phone_ok or (addr_ok and dt <= 360) or (dt <= 180 and dist <= 60 and jac >= 0.45)
    return {
        "eid": "-".join(sorted([ra["rid"], rb["rid"]])),
        "a": min(ra["rid"], rb["rid"]), "b": max(ra["rid"], rb["rid"]),
        "category": ra["category"],
        "dt_sec": int(dt), "dist_m": round(dist, 1),
        "score": score, "strong": strong,
        "auto_conservative": strong or score >= CONSERVATIVE_MIN,
        "auto_lenient": strong or score >= LENIENT_MIN,
        "evidence": ev,
        "matched_at": common.now_iso(),
    }


def candidate_pairs(records: list[dict]):
    """分块：同类别 + 时间窗 + 地理网格。索引每次重建，只用于发现新对。"""
    recs = [r for r in records if r["category"] != "other"]
    for i in range(len(recs)):
        for j in range(i + 1, len(recs)):
            ra, rb = recs[i], recs[j]
            if ra["category"] != rb["category"]:
                continue
            if abs(ra["ts"] - rb["ts"]) > MAX_DT_SEC:
                continue
            if common.haversine_m(ra["lat"], ra["lon"], rb["lat"], rb["lon"]) > MAX_DIST_M:
                continue
            yield ra, rb


def run() -> int:
    records = common.read_jsonl(common.RECORDS)
    existing = {e["eid"] for e in common.read_jsonl(common.CANDIDATES)}
    written = 0
    for ra, rb in candidate_pairs(records):
        eid = "-".join(sorted([ra["rid"], rb["rid"]]))
        if eid in existing:
            continue  # 幂等：已有边永不覆盖，人工裁决锚点稳定
        common.append_jsonl(common.CANDIDATES, compare(ra, rb))
        existing.add(eid)
        written += 1
    return written


def main() -> None:
    n = run()
    edges = common.read_jsonl(common.CANDIDATES)
    n_con = sum(1 for e in edges if e["auto_conservative"])
    n_len = sum(1 for e in edges if e["auto_lenient"])
    print(f"[matcher] 新增候选边 {n} 条，累计 {len(edges)} 条"
          f"（保守自动接受 {n_con}，宽松自动接受 {n_len}，待裁决区间 {n_len - n_con}）")


if __name__ == "__main__":
    main()
