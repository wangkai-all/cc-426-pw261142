"""端到端模拟：从零生成虚构数据，实际跑通 接收->匹配->指标->裁决 全流程。

  python3 -m event_dedup.simulate

演示并断言：
  1. 三个服务分别运行（子进程）：ingest / matcher / metrics
  2. 保守 vs 宽松去重对指标的影响（宽松链式误合并被标记）
  3. 两次裁决交错（乙基于旧视图驳回甲已确认的边）-> 冲突被显式标记
  4. 旧计算晚到：慢速指标运行写回时被标记 stale，不覆盖当前指标
  5. 链式防护：人工确认也不能把 T3 借 T2 链入 T1 的簇
  6. 撤销裁决后原始记录可完整还原（records.jsonl 全程不变）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from . import common, decisions, metrics, state as state_mod

PY = sys.executable


def svc(module: str, *args: str, **kw):
    return subprocess.run([PY, "-m", f"event_dedup.{module}", *args],
                          cwd=common.ROOT, capture_output=True, text=True, **kw)


def step(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def find_eid(st: dict, event_a: dict, event_b: dict) -> str:
    """按 (source, src_id) 找到两条记录之间的候选边 ID。"""
    rid_of = {(r["source"], r["src_id"]): r["rid"] for r in st["records"]}
    ra = rid_of[(event_a["source"], event_a["src_id"])]
    rb = rid_of[(event_b["source"], event_b["src_id"])]
    return "-".join(sorted([ra, rb]))


def cluster_of(st: dict, view: str, rid: str) -> list[str]:
    for c in st["views"][view]["clusters"]:
        if rid in c:
            return c
    return [rid]


def pair_eval(st: dict, truth: dict) -> None:
    """用虚构数据的真值评估各视图（仅演示验证，系统本身不依赖真值）。"""
    rid_of = {(r["source"], r["src_id"]): r["rid"] for r in st["records"]}
    pos = set()
    for ev in truth.values():
        rids = [rid_of[(m["source"], m["src_id"])] for m in ev["members"]]
        for i in range(len(rids)):
            for j in range(i + 1, len(rids)):
                pos.add(frozenset((rids[i], rids[j])))
    print(f"\n真值评估（{len(truth)} 个真实事件，{len(pos)} 个正例对）：")
    for view in ("conservative", "lenient", "adjudicated"):
        pred = set()
        for c in st["views"][view]["clusters"]:
            for i in range(len(c)):
                for j in range(i + 1, len(c)):
                    pred.add(frozenset((c[i], c[j])))
        tp = len(pos & pred)
        prec = tp / len(pred) if pred else 1.0
        rec = tp / len(pos) if pos else 1.0
        print(f"  {state_mod.VIEW_NAMES[view]:4s}视图  精确率 {prec:.2f}  召回率 {rec:.2f}"
              f"  （预测对 {len(pred)}，命中 {tp}）")


def main() -> None:
    step("0. 清空并从零生成虚构数据（三来源、各自编号、无统一事件 ID）")
    if os.path.exists(common.DATA_DIR):
        shutil.rmtree(common.DATA_DIR)
    common.ensure_dirs()
    print(svc("gen_data").stdout.strip())

    step("1. 接收日志服务 ingest（独立进程）")
    print(svc("ingest").stdout.strip())
    with open(common.RECORDS, "rb") as f:
        records_snapshot = f.read()  # 用于最后验证"原始记录全程不变"

    step("2. 候选匹配服务 matcher（独立进程，产出可解释证据）")
    print(svc("matcher").stdout.strip())

    step("3. 指标计算器 metrics：基线运行（独立进程）")
    print(svc("metrics", "--note", "基线").stdout.strip())

    st = state_mod.load_state()
    truth = common.read_json(common.GROUND_TRUTH)
    mem = {eid: v["members"] for eid, v in truth.items()}

    def member(eid, source, n=0):
        return [m for m in mem[eid] if m["source"] == source][n]

    e1_911_social = find_eid(st, member("E1", "911"), member("E1", "social"))
    e1_sensor_social = find_eid(st, member("E1", "sensor"), member("E1", "social"))
    t1_t2 = find_eid(st, member("E9", "911"), member("E10", "911"))
    t2_t3 = find_eid(st, member("E10", "911"), member("E11", "911"))
    e13_e14 = find_eid(st, member("E13", "sensor"), member("E14", "sensor"))

    print("\n待人工裁决队列（宽松接受但保守不接受的证据不足区间）：")
    for e in st["queue"]:
        print(f"  {e['eid']}  score={e['score']:.2f}  Δt={e['dt_sec']}s  Δd={e['dist_m']:.0f}m")
    print("\n宽松视图的链式风险簇（直径>1，连通分量误合并风险）：")
    for cr in st["views"]["lenient"]["chain_risk"]:
        print(f"  成员 {cr['members']}  直径 {cr['diameter']}  桥接边 {cr['bridges']}")

    step("4. 启动一次慢速指标计算（4 秒后写回，模拟旧计算晚到）")
    slow = subprocess.Popen([PY, "-m", "event_dedup.metrics",
                             "--note", "慢速旧计算", "--delay", "4"],
                            cwd=common.ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    print("慢速计算已启动（基于当前版本快照），期间将有新裁决到达……")

    step("5. 两次裁决交错：甲确认、乙基于旧视图驳回同一条边")
    base0 = len(decisions.load())  # 甲和乙都基于 0 条裁决的视图
    da = decisions.submit("confirm_edge", "甲", "E1：电话报警与网帖时间地点吻合，是同一事件",
                          eid=e1_911_social, base_dseq=base0)
    print(f"甲确认 {e1_911_social} -> {da['did']}")
    db = decisions.submit("reject_edge", "乙", "乙的页面还是旧队列，未看到甲的确认，误判驳回",
                          eid=e1_911_social, base_dseq=base0)
    print(f"乙驳回 {e1_911_social} -> {db['did']}（基于日志位置 {db['base_dseq']}）")
    st = state_mod.load_state()
    for c in st["unresolved"]["conflicts"]:
        print(f"  【冲突】{c['detail']}")
    assert any(c["kind"] == "stale_based" for c in st["unresolved"]["conflicts"]), "应检测到交错裁决"

    step("6. 乙撤销自己的驳回（撤销 = 追加补偿条目，不改历史）")
    dr = decisions.submit("revert", "乙", "看到甲的确认与证据，撤销我的驳回", target_did=db["did"])
    print(f"撤销 {db['did']} -> {dr['did']}，甲的确认重新生效")

    step("7. 甲补确认 E1 的 sensor-social 边：团约束满足，三方合并生效")
    st = state_mod.load_state()
    rid_social = [r for r in st["records"] if r["source"] == "social" and r["src_id"] == member("E1", "social")["src_id"]][0]["rid"]
    print(f"合并前 E1 网帖所在簇（裁决视图）：{cluster_of(st, 'adjudicated', rid_social)}")
    blocked_before = [b["eid"] for b in st["views"]["adjudicated"]["blocked"]]
    print(f"此前被链式防护阻止的合并：{blocked_before}")
    assert e1_911_social in blocked_before, "甲的确认此前应被链式防护阻止（缺 sensor-social 边）"
    decisions.submit("confirm_edge", "甲", "补确认传感器与网帖，三方证据闭环", eid=e1_sensor_social)
    st = state_mod.load_state()
    clu = cluster_of(st, "adjudicated", rid_social)
    print(f"合并后 E1 网帖所在簇（裁决视图）：{clu}")
    assert len(clu) == 3, "E1 三条记录应合并为一簇"

    step("8. 链式防护：人工确认也不能把 T3 借 T2 链入 T1 的簇")
    dt1 = decisions.submit("confirm_edge", "甲", "T1 与 T2 时间地点相近，暂认为是同一起追尾", eid=t1_t2)
    dt2 = decisions.submit("confirm_edge", "甲", "试图把 T3 也并入（应被链式防护阻止）", eid=t2_t3)
    st = state_mod.load_state()
    blocked = {b["eid"]: b["reason"] for b in st["views"]["adjudicated"]["blocked"]}
    print(f"被阻止：{t2_t3} -> {blocked.get(t2_t3)}")
    assert t2_t3 in blocked, "T3 借 T2 链入应被阻止"
    rid_t3 = [r for r in st["records"] if r["src_id"] == member("E11", "911")["src_id"]][0]["rid"]
    assert cluster_of(st, "adjudicated", rid_t3) == [rid_t3], "T3 应保持独立"
    decisions.submit("revert", "甲", "实地核实：T1/T2/T3 是三起独立追尾，撤销先前对 T1-T2 的确认",
                     target_did=dt1["did"])
    decisions.submit("revert", "甲", "一并撤销对 T2-T3 的确认（否则撤销 T1-T2 后它会悄然生效）",
                     target_did=dt2["did"])
    st = state_mod.load_state()
    rid_t1 = [r for r in st["records"] if r["src_id"] == member("E9", "911")["src_id"]][0]["rid"]
    assert cluster_of(st, "adjudicated", rid_t1) == [rid_t1], "撤销后 T1 应还原为独立记录"
    assert cluster_of(st, "adjudicated", rid_t3) == [rid_t3], "撤销后 T3 应还原为独立记录"
    print("甲撤销对 T1-T2、T2-T3 的误确认，T1/T2/T3 全部还原为独立事件 ✓")

    step("9. 乙驳回 E13/E14 之间的误合并候选边（两起独立火灾）")
    decisions.submit("reject_edge", "乙", "两处门牌不同、报警人称互不知情，是两起火灾", eid=e13_e14)
    print(f"已驳回 {e13_e14}，从待裁决队列移除")

    step("10. 慢速旧计算此刻写回 —— 应被标记 stale，不覆盖当前指标")
    out, _ = slow.communicate()
    print(out.strip())
    runs = common.read_jsonl(common.METRICS_RUNS)
    slow_run = [r for r in runs if r["note"] == "慢速旧计算"][0]
    assert slow_run["stale"], "旧计算晚到必须标记 stale"
    assert metrics.current_run()["note"] == "基线", "当前指标不应被旧计算覆盖"
    print(f"旧计算基准版本 {slow_run['base_version']}，写回时版本 {slow_run['finish_version']} -> stale ✓")

    step("11. 重新计算指标（当前有效），对比裁决前后")
    print(svc("metrics", "--note", "裁决后").stdout.strip())
    cur = metrics.current_run()["metrics"]
    base = [r for r in runs if r["note"] == "基线"][0]["metrics"]
    for view in ("conservative", "lenient", "adjudicated"):
        b, c = base[view], cur[view]
        print(f"  {state_mod.VIEW_NAMES[view]:4s}视图  事件数 {b['events']} -> {c['events']}  "
              f"跨来源事件 {b['cross_source_events']} -> {c['cross_source_events']}")

    step("12. 撤销甲对 E1 的确认：拆回后原始记录完整还原")
    decisions.submit("revert", "甲", "演示撤销：拆回 E1 三方合并", target_did=da["did"])
    st = state_mod.load_state()
    clu = cluster_of(st, "adjudicated", rid_social)
    print(f"拆回后 E1 网帖所在簇：{clu}（还原为独立记录）")
    assert clu == [rid_social]
    with open(common.RECORDS, "rb") as f:
        assert f.read() == records_snapshot, "原始记录全程不变"
    print("records.jsonl 与 ingest 刚完成时逐字节一致 ✓（原始记录不可变，簇由日志重放派生）")
    decisions.submit("confirm_edge", "甲", "重新确认 E1 三方合并（演示完毕，恢复）", eid=e1_911_social)
    print(svc("metrics", "--note", "最终").stdout.strip())

    step("13. 真值评估与总结")
    st = state_mod.load_state()
    pair_eval(st, truth)
    print(f"\n数据目录：{common.DATA_DIR}")
    print("启动审阅网页：python3 -m event_dedup.web  （默认 http://127.0.0.1:8077）")


if __name__ == "__main__":
    main()
