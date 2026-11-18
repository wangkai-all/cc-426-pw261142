"""三种去重视图与聚类策略。

视图：
  conservative  保守：仅自动强证据边（auto_conservative），纯机器基线
  lenient       宽松：自动弱证据边也接受（auto_lenient），纯机器基线
  adjudicated   裁决：保守基线 + 人工确认 - 人工驳回（系统实际使用的视图）

聚类：
  保守/裁决视图用【团约束】——一个记录要并入簇，必须与簇内所有成员都有
  已接受边。这从机制上防止链式相似误合并（A~B、B~C 但 A!~C 时，
  C 永远无法借 B 链入）。被阻止的合并进入 blocked 列表，在"未解决"页呈现。
  宽松视图用【连通分量】，故意展示链式合并的风险：直径 > 1 的簇被标记
  chain_risk，并列出其桥接边（断开即可还原）。

原始记录不可变，簇全部由日志重放派生 —— 拆回/撤销后自然还原原始记录。
"""
from __future__ import annotations


def accepted_edges(edges: list[dict], view: str, overrides: dict) -> list[dict]:
    """按视图筛选已接受边。overrides: {eid: (status, decision)}"""
    out = []
    for e in edges:
        status = overrides.get(e["eid"], (None, None))[0]
        if view == "conservative":
            ok = e["auto_conservative"]
        elif view == "lenient":
            ok = e["auto_lenient"]
        elif view == "adjudicated":
            ok = (e["auto_conservative"] or status == "confirmed") and status != "rejected"
        else:
            raise ValueError(view)
        if ok:
            out.append(e)
    return out


def cluster_clique(rids: list[str], edges: list[dict]) -> dict:
    """团约束聚类。返回 {clusters: [[rid...]], blocked: [{eid, reason}]}"""
    accepted = {}  # frozenset({u,v}) -> edge
    for e in edges:
        accepted[frozenset((e["a"], e["b"]))] = e
    membership: dict[str, int] = {}
    clusters: list[list[str]] = []
    blocked = []

    def linked(u: str, members: list[str]) -> bool:
        return all(frozenset((u, m)) in accepted for m in members)

    for e in sorted(edges, key=lambda x: (-x["score"], x["eid"])):
        u, v = e["a"], e["b"]
        cu, cv = membership.get(u), membership.get(v)
        if cu is not None and cv is not None and cu == cv:
            continue
        if cu is None and cv is None:
            membership[u] = membership[v] = len(clusters)
            clusters.append([u, v])
        elif cu is None or cv is None:
            cid, new = (cu, v) if cu is not None else (cv, u)
            if linked(new, clusters[cid]):
                clusters[cid].append(new)
                membership[new] = cid
            else:
                blocked.append({"eid": e["eid"], "reason":
                                f"链式防护：{new} 缺少与簇内部分成员的已接受边，"
                                f"不允许借链并入（防止链式相似误合并）"})
        else:
            if all(linked(m, clusters[cv]) for m in clusters[cu]):
                for m in clusters[cv]:
                    membership[m] = cu
                clusters[cu].extend(clusters[cv])
                clusters[cv] = []
            else:
                blocked.append({"eid": e["eid"], "reason":
                                "链式防护：两簇之间存在未接受的跨簇对，禁止整体合并"})
    clustered = set(membership)
    for rid in rids:  # 未被任何边触及的记录 = 单例簇
        if rid not in clustered:
            clusters.append([rid])
    return {"clusters": [sorted(c) for c in clusters if c], "blocked": blocked}


def cluster_components(rids: list[str], edges: list[dict]) -> dict:
    """连通分量聚类（宽松视图），并标记链式风险簇及其桥接边。"""
    parent = {r: r for r in rids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        parent[find(e["a"])] = find(e["b"])

    groups: dict[str, list[str]] = {}
    for r in rids:
        groups.setdefault(find(r), []).append(r)

    clusters, chain_risk = [], []
    for members in groups.values():
        members = sorted(members)
        clusters.append(members)
        if len(members) < 3:
            continue  # 二元簇不存在链式问题
        inner = [e for e in edges if e["a"] in members and e["b"] in members]
        adj = {m: set() for m in members}
        for e in inner:
            adj[e["a"]].add(e["b"])
            adj[e["b"]].add(e["a"])
        # 直径：BFS 最远距离的最大值
        diameter = 0
        for s in members:
            dist = {s: 0}
            queue = [s]
            while queue:
                x = queue.pop(0)
                for y in adj[x]:
                    if y not in dist:
                        dist[y] = dist[x] + 1
                        queue.append(y)
            diameter = max(diameter, max(dist.values()))
        if diameter > 1:
            # 桥接边：删掉它会使簇分裂的边（簇很小，逐个试即可）
            bridges = []
            for e in inner:
                adj2 = {m: set(adj[m]) for m in members}
                adj2[e["a"]].discard(e["b"])
                adj2[e["b"]].discard(e["a"])
                seen = {e["a"]}
                queue = [e["a"]]
                while queue:
                    x = queue.pop(0)
                    for y in adj2[x]:
                        if y not in seen:
                            seen.add(y)
                            queue.append(y)
                if len(seen) < len(members):
                    bridges.append(e["eid"])
            chain_risk.append({"members": members, "diameter": diameter,
                               "bridges": sorted(set(bridges))})
    return {"clusters": clusters, "chain_risk": chain_risk}


def build_views(records: list[dict], edges: list[dict], overrides: dict) -> dict:
    """计算三个视图的簇结构。"""
    rids = [r["rid"] for r in records]
    views = {}
    for view in ("conservative", "lenient", "adjudicated"):
        acc = accepted_edges(edges, view, overrides)
        if view == "lenient":
            res = cluster_components(rids, acc)
        else:
            res = cluster_clique(rids, acc)
        res["accepted_eids"] = sorted(e["eid"] for e in acc)
        views[view] = res
    return views
