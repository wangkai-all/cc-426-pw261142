"""虚构数据生成：同一事件被不同来源各自编号上报，无统一 ID。

埋入的测试情形：
  - 普通多来源事件（证据充分，保守策略即可自动合并）
  - 同一报警人重复来电（phone 强证据）
  - 证据不足的跨来源对（落入"待人工裁决"区间）
  - 链式陷阱 T1/T2/T3：三起独立追尾，两两相近但首尾不相干，
    宽松连通分量会链式误合并，团约束不会
  - 难分负样本 E13/E14：两起独立火灾，时间地点都近，宽松会误合并
ground_truth.json 仅供 simulate 评估演示，系统服务不读取它。
"""
from __future__ import annotations

import os

from . import common

BASE_EPOCH = 1_800_000_000  # 固定基准时刻，演示可复现

# (event_id, category, t_sec, x_m, y_m, addr_num, addr_street, reports)
# reports: (source, dt_sec, dx_m, dy_m, phone, text)
EVENTS = [
    ("E1", "fire", 12 * 60, 1000, 500, "412", "5th Ave", [
        ("911", 120, (20, 15), "ph-1001", "caller reports fire at 412 5th Ave smoke visible from windows"),
        ("sensor", 30, (8, -5), None, "fire smoke alarm zone 4 unit F2"),
        ("social", 300, (90, 80), None, "huge fire near 5th Ave omg #fire"),
    ]),
    ("E2", "traffic", 20 * 60, 3000, 800, "900", "Oak St", [
        ("911", 120, (30, -10), "ph-1002", "caller reports crash at 900 Oak St cyclist hurt"),
        ("social", 480, (-120, 110), None, "crash near Oak St #accident"),
    ]),
    ("E3", "flood", 65 * 60, 500, 3000, "12", "Riverside Dr", [
        ("sensor", 30, (5, 8), None, "flood water alert zone 3 unit W1"),
        ("social", 240, (-60, 70), None, "street flooded near Riverside Dr #flood"),
    ]),
    ("E4", "medical", 90 * 60, 2000, 2000, "77", "Main St", [
        ("911", 150, (10, 10), "ph-1003", "caller reports medical emergency at 77 Main St woman fainted"),
    ]),
    ("E5", "fire", 121 * 60, 4000, 4000, "300", "Hill Rd", [
        ("sensor", 40, (6, -6), None, "fire smoke alarm zone 8 unit F9"),
    ]),
    ("E6", "traffic", 140 * 60, 2100, 2100, "45", "Main St", [
        ("911", 120, (15, 20), "ph-1004", "caller reports crash at 45 Main St two cars"),
        ("911", 360, (-25, 25), "ph-1004", "second caller reports crash at 45 Main Street two cars"),
        ("social", 420, (100, -110), None, "crash near Main St #accident"),
    ]),
    ("E7", "fire", 160 * 60, 1200, 600, "510", "5th Ave", [
        ("911", 120, (20, 10), "ph-1005", "caller reports fire at 510 5th Ave flames on roof"),
        ("sensor", 30, (-8, 6), None, "fire smoke alarm zone 5 unit F7"),
    ]),
    ("E8", "flood", 180 * 60, 700, 3200, "88", "Riverside Dr", [
        ("social", 300, (80, 60), None, "basement flooded near Riverside Dr #flood"),
    ]),
    # 链式陷阱：三起独立追尾，间隔 8 分钟、260 米
    ("E9", "traffic", 240 * 60, 5000, 1000, "100", "5th Ave", [
        ("911", 120, (20, -15), "ph-1006", "caller reports crash at 100 5th Ave two cars"),
        ("social", 420, (90, 100), None, "crash near 5th Ave #accident"),
    ]),
    ("E10", "traffic", 248 * 60, 5260, 1000, "300", "5th Ave", [
        ("911", 120, (-20, 10), "ph-1010", "caller reports crash at 300 5th Ave two cars"),
        ("social", 360, (-80, -90), None, "crash near 5th Ave #accident"),
    ]),
    ("E11", "traffic", 256 * 60, 5520, 1000, "500", "5th Ave", [
        ("911", 120, (15, 15), "ph-1007", "caller reports crash at 500 5th Ave two cars"),
    ]),
    # 证据不足的跨来源对 -> 待人工裁决区间
    ("E12", "medical", 300 * 60, 3200, 900, "20", "Oak St", [
        ("911", 120, (25, -15), "ph-1008", "caller reports medical emergency at 20 Oak St man collapsed"),
        ("social", 660, (120, 110), None, "medical emergency near Oak St #emergency"),
    ]),
    # 难分负样本：两起独立火灾，相距 150 米、4 分钟
    ("E13", "fire", 360 * 60, 6000, 2000, "15", "Hill Rd", [
        ("911", 120, (20, 15), "ph-1009", "caller reports fire at 15 Hill Rd garage burning"),
        ("sensor", 60, (-8, 8), None, "fire smoke alarm zone 7 unit A3"),
    ]),
    ("E14", "fire", 364 * 60, 6150, 2000, "21", "Hill Rd", [
        ("sensor", 60, (8, -6), None, "fire smoke alarm zone 9 unit B1"),
        ("social", 300, (-100, 90), None, "fire near Hill Rd #fire"),
    ]),
]

SRC_SEQ = {"911": 7000, "sensor": 40000, "social": 9000}
SRC_PREFIX = {"911": "C", "sensor": "SNS", "social": "P"}


def _xy_to_latlon(x_m: float, y_m: float) -> tuple[float, float]:
    return 40.0 + y_m / 111_000.0, 116.0 + x_m / (111_000.0 * 0.72)


def main() -> None:
    common.ensure_dirs()
    for src in SRC_SEQ:
        path = os.path.join(common.RAW_DIR, f"{src}.jsonl")
        if os.path.exists(path):
            os.remove(path)
    truth = {}
    for eid, cat, t0, x, y, num, street, reports in EVENTS:
        truth[eid] = {"category": cat, "members": []}
        for source, dt, (dx, dy), phone, text in reports:
            SRC_SEQ[source] += 1
            src_id = f"{SRC_PREFIX[source]}-{SRC_SEQ[source]}"
            lat, lon = _xy_to_latlon(x + dx, y + dy)
            rec = {
                "source": source,
                "src_id": src_id,
                "ts": BASE_EPOCH + t0 + dt,
                "category_raw": {"911": cat, "sensor": {
                    "fire": "SMOKE", "traffic": "COLLISION", "flood": "WATER",
                    "medical": "AED"}[cat], "social": {
                    "fire": "#fire", "traffic": "#accident", "flood": "#flood",
                    "medical": "#emergency"}[cat]}[source],
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "address": f"{num} {street}" if source == "911" else None,
                "phone": phone,
                "text": text,
            }
            common.append_jsonl(os.path.join(common.RAW_DIR, f"{source}.jsonl"), rec)
            truth[eid]["members"].append({"source": source, "src_id": src_id})
    common.write_json(common.GROUND_TRUTH, truth)
    n = sum(len(v["members"]) for v in truth.values())
    print(f"生成 {len(truth)} 个真实事件、{n} 条来源记录 -> {common.RAW_DIR}")


if __name__ == "__main__":
    main()
