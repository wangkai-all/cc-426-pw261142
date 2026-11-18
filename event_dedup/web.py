"""审阅网页（独立运行）：证据审阅、撤销匹配、指标变化、未解决部分。

  python3 -m event_dedup.web [--port 8077]

页面：
  /           仪表盘：三视图指标对比、指标运行历史（含 stale 标记）
  /review     待裁决候选边：完整证据明细，确认/驳回
  /clusters   裁决视图簇 + 宽松视图链式风险簇（可断开桥接边拆分）
  /decisions  裁决日志：交错冲突标记、撤销
  /unresolved 未解决部分：待裁决边、链式风险、被阻止合并、冲突、无候选记录
所有提交都通过追加裁决日志生效；表单携带页面渲染时的日志位置，
过期提交会被交错检测显式标记，绝不静默覆盖。
"""
from __future__ import annotations

import argparse
import html
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import common, decisions, metrics, state as state_mod

LOCK = threading.Lock()

VIEW_ORDER = ("conservative", "lenient", "adjudicated")
VIEW_SERIES = {"conservative": "var(--series-1)", "lenient": "var(--series-2)",
               "adjudicated": "var(--series-3)"}

CSS = """
.viz-root {
  color-scheme: light;
  --surface-1: #fcfcfb; --page: #f9f9f7;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a;
  --gridline: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
  --good: #0ca30c; --warning: #fab219; --critical: #d03b3b; --success-text: #006300;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19; --page: #0d0d0d;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --muted: #898781;
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
    --gridline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
    --success-text: #0ca30c;
  }
}
body { margin: 0; }
.viz-root {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--page); color: var(--text-primary);
  max-width: 1080px; margin: 0 auto; padding: 16px 24px 64px;
}
nav { display: flex; gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--baseline);
      margin-bottom: 20px; flex-wrap: wrap; }
nav a { color: var(--series-1); text-decoration: none; font-size: 14px; }
nav a:hover { text-decoration: underline; }
h1 { font-size: 20px; } h2 { font-size: 16px; margin-top: 32px; }
.card { background: var(--surface-1); border: 1px solid var(--border);
        border-radius: 8px; padding: 16px; margin-bottom: 16px; }
.tiles { display: flex; gap: 12px; flex-wrap: wrap; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
        padding: 12px 18px; min-width: 130px; }
.tile .v { font-size: 26px; } .tile .k { color: var(--text-secondary); font-size: 12px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; background: var(--surface-1); }
th, td { border: 1px solid var(--gridline); padding: 5px 8px; text-align: left;
         font-variant-numeric: tabular-nums; }
th { color: var(--text-secondary); font-weight: 600; }
.ev-ok { color: var(--success-text); font-weight: 600; }
.ev-no { color: var(--muted); }
.stale { color: var(--critical); font-weight: 600; }
.current { color: var(--success-text); font-weight: 600; }
.warn { color: var(--text-secondary); background: var(--surface-1); }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px;
         border: 1px solid var(--border); }
.legend { display: flex; gap: 16px; font-size: 13px; color: var(--text-secondary);
          margin: 8px 0; flex-wrap: wrap; }
.legend .sw { display: inline-block; width: 12px; height: 12px; border-radius: 3px;
              margin-right: 5px; vertical-align: -1px; }
form.inline { display: inline; }
input[type=text] { padding: 4px 6px; border: 1px solid var(--baseline); border-radius: 4px;
                   background: var(--surface-1); color: var(--text-primary); }
button { padding: 4px 12px; border-radius: 4px; border: 1px solid var(--baseline);
         background: var(--surface-1); color: var(--text-primary); cursor: pointer; }
button:hover { filter: brightness(0.95); }
button.ok { border-color: var(--series-3); } button.no { border-color: var(--series-2); }
.rec { font-size: 13px; color: var(--text-secondary); }
.small { font-size: 12px; color: var(--muted); }
"""


def esc(s) -> str:
    return html.escape(str(s))


def fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


# ---------- SVG 分组柱状图 ----------

def _bar_path(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """顶部 4px 圆角、底部锚定基线的柱形路径。"""
    r = min(r, w / 2, h)
    return (f"M{x},{y + h} L{x},{y + r} Q{x},{y} {x + r},{y} "
            f"L{x + w - r},{y} Q{x + w},{y} {x + w},{y + r} L{x + w},{y + h} Z")


def grouped_bars(groups: list[str], series: list[str], data: dict,
                 colors: dict, width: int = 720, height: int = 260) -> str:
    """data[(group, series)] = value。每组并排便签柱，2px 间隔，网格线内凹。"""
    ml, mr, mt, mb = 44, 12, 16, 46
    pw, ph = width - ml - mr, height - mt - mb
    vmax = max(max(data.values()), 1)
    step = max(1, int(vmax / 4) + (1 if vmax % 4 else 0))
    ymax = step * 4
    n_g, n_s = len(groups), len(series)
    slot = pw / n_g
    bw = min(26.0, (slot - 24) / n_s - 2)
    out = [f'<svg width="{width}" height="{height}" role="img" '
           f'style="font-family:inherit" aria-label="分组柱状图">']
    for i in range(5):  # 网格线与轴标签（弱化）
        y = mt + ph - ph * i / 4
        out.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + pw}" y2="{y:.1f}" '
                   f'stroke="var(--gridline)" stroke-width="1"/>')
        out.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
                   f'fill="var(--muted)" style="font-variant-numeric:tabular-nums">{step * i}</text>')
    out.append(f'<line x1="{ml}" y1="{mt + ph}" x2="{ml + pw}" y2="{mt + ph}" '
               f'stroke="var(--baseline)" stroke-width="1.5"/>')
    for gi, g in enumerate(groups):
        gx = ml + gi * slot
        total_w = n_s * bw + (n_s - 1) * 2
        x0 = gx + (slot - total_w) / 2
        for si, s in enumerate(series):
            v = data.get((g, s), 0)
            h = ph * v / ymax
            x = x0 + si * (bw + 2)
            y = mt + ph - h
            out.append(f'<path d="{_bar_path(x, y, bw, h)}" fill="{colors[s]}">'
                       f'<title>{esc(g)} · {esc(s)}：{v}</title></path>')
            # 缓解规则：浅色系列对比度不足 -> 每柱直接标数值
            out.append(f'<text x="{x + bw / 2:.1f}" y="{y - 4:.1f}" text-anchor="middle" '
                       f'font-size="11" fill="var(--text-secondary)" '
                       f'style="font-variant-numeric:tabular-nums">{v}</text>')
        out.append(f'<text x="{gx + slot / 2:.1f}" y="{mt + ph + 18}" text-anchor="middle" '
                   f'font-size="12" fill="var(--text-primary)">{esc(g)}</text>')
    out.append('</svg>')
    return "".join(out)


def legend() -> str:
    items = "".join(
        f'<span><span class="sw" style="background:{VIEW_SERIES[v]}"></span>'
        f'{state_mod.VIEW_NAMES[v]}视图</span>' for v in VIEW_ORDER)
    return f'<div class="legend">{items}</div>'


# ---------- 页面片段 ----------

def page(title: str, body: str) -> bytes:
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{esc(title)} · 事件去重审阅</title><style>{CSS}</style></head>
<body><div class="viz-root">
<nav><b>事件去重审阅</b><a href="/">仪表盘</a><a href="/review">证据审阅</a>
<a href="/clusters">簇与链式风险</a><a href="/decisions">裁决日志</a>
<a href="/unresolved">未解决</a></nav>
{body}</div></body></html>""".encode()


def action_form(action: str, label: str, st: dict, cls: str = "", **fields) -> str:
    """提交表单。base_dseq 携带页面渲染时的日志位置 —— 过期提交会被交错检测标记。"""
    hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(v)}">'
                     for k, v in fields.items())
    return (f'<form class="inline" method="post" action="/action">'
            f'<input type="hidden" name="action" value="{action}">'
            f'<input type="hidden" name="base_dseq" value="{len(st["decisions"])}">'
            f'{hidden}<button class="{cls}" type="submit">{esc(label)}</button></form>')


def evidence_table(e: dict) -> str:
    rows = "".join(
        f'<tr><td class="{"ev-ok" if ev["ok"] else "ev-no"}">{"✓" if ev["ok"] else "✗"}</td>'
        f'<td>{esc(ev["key"])}</td><td>{ev["weight"]:.2f}</td><td>{esc(ev["detail"])}</td></tr>'
        for ev in e["evidence"])
    return (f'<table><tr><th></th><th>证据</th><th>权重</th><th>明细</th></tr>{rows}</table>'
            f'<p class="rec">证据权重合计（仅用于排序，不替代依据）：<b>{e["score"]:.2f}</b>'
            f'　强证据：{"是" if e["strong"] else "否"}　保守阈值 3.0 / 宽松阈值 1.6</p>')


def rec_line(st: dict, rid: str) -> str:
    r = st["by_rid"][rid]
    return (f'<div class="rec">[{esc(rid)}] {esc(r["source"])}:{esc(r["src_id"])} '
            f'{fmt_ts(r["ts"])} 「{esc(r["text"])}」'
            f'{" 电话 " + esc(r["phone"]) if r.get("phone") else ""}</div>')


# ---------- 路由 ----------

def dashboard(st: dict) -> bytes:
    cur = st["current_metrics"]
    if cur is None:
        return page("仪表盘", "<p>尚无指标运行。先运行 python3 -m event_dedup.metrics。</p>")
    m = cur["metrics"]
    tiles = "".join([
        f'<div class="tile"><div class="v">{len(st["records"])}</div><div class="k">规范化记录</div></div>',
        f'<div class="tile"><div class="v">{len(st["queue"])}</div><div class="k">待裁决候选边</div></div>',
        f'<div class="tile"><div class="v">{len(st["unresolved"]["chain_risk"])}</div><div class="k">链式风险簇（宽松）</div></div>',
        f'<div class="tile"><div class="v">{len(st["unresolved"]["blocked_merges"])}</div><div class="k">被链式防护阻止</div></div>',
        f'<div class="tile"><div class="v">{len(st["unresolved"]["conflicts"])}</div><div class="k">裁决冲突</div></div>',
        f'<div class="tile"><div class="v">{esc(st["version"][1])}</div><div class="k">当前状态版本</div></div>',
    ])
    groups = ["事件数", "折叠重复", "跨来源事件"]
    keys = ["events", "duplicates_collapsed", "cross_source_events"]
    data = {(g, state_mod.VIEW_NAMES[v]): m[v][k] for g, k in zip(groups, keys) for v in VIEW_ORDER}
    chart = grouped_bars(groups, [state_mod.VIEW_NAMES[v] for v in VIEW_ORDER], data,
                         {state_mod.VIEW_NAMES[v]: VIEW_SERIES[v] for v in VIEW_ORDER})
    # 数据表（图表的表格视图，可访问性必需）；差值 = 相对上一有效运行
    prev = None
    for r in reversed(st["metrics_runs"]):
        if not r["stale"] and r["run_id"] != cur["run_id"]:
            prev = r
            break

    def delta(view: str, key: str) -> str:
        if prev is None:
            return ""
        d = m[view][key] - prev["metrics"][view][key]
        if d == 0:
            return '<span class="small">（±0）</span>'
        cls = "current" if d < 0 else "stale"  # 事件数下降=去重更充分
        return f'<span class="{cls}">（{"+" if d > 0 else ""}{d}）</span>'

    trows = "".join(
        f'<tr><td>{state_mod.VIEW_NAMES[v]}</td>'
        f'<td>{m[v]["events"]}{delta(v, "events")}</td>'
        f'<td>{m[v]["duplicates_collapsed"]}{delta(v, "duplicates_collapsed")}</td>'
        f'<td>{m[v]["cross_source_events"]}{delta(v, "cross_source_events")}</td>'
        f'<td>{m[v]["singletons"]}</td><td>{m[v]["accepted_edges"]}</td></tr>'
        for v in VIEW_ORDER)
    delta_note = (f'<p class="small">括号内为相对上一有效运行（{esc(prev["run_id"])} '
                  f'{esc(prev["note"])}）的变化 —— 人工裁决对指标的影响。</p>' if prev else "")
    table = (f'<table><tr><th>视图</th><th>事件数</th><th>折叠重复</th><th>跨来源事件</th>'
             f'<th>单例</th><th>已接受边</th></tr>{trows}</table>')
    # 分来源贡献（裁决视图）
    src_rows = ""
    for s, sm_ in m["adjudicated"]["per_source"].items():
        src_rows += (f'<tr><td>{esc(s)}</td><td>{sm_["records"]}</td>'
                     f'<td>{sm_["events_covered"]}</td><td>{sm_["coverage"]:.0%}</td>'
                     f'<td>{sm_["exclusive_events"]}</td></tr>')
    src_table = (f'<table><tr><th>来源</th><th>记录数</th><th>覆盖事件</th>'
                 f'<th>覆盖率</th><th>独占事件</th></tr>{src_rows}</table>')
    # 指标运行历史（含 stale）
    runs = "".join(
        f'<tr><td>{esc(r["run_id"])}</td><td>{esc(r["note"])}</td>'
        f'<td>{esc(r["base_version"])}</td><td>{esc(r["finish_version"])}</td>'
        f'<td class="{"stale" if r["stale"] else "current"}">'
        f'{"⚠ 已过时（旧计算晚到，仅存档）" if r["stale"] else "✓ 有效"}</td>'
        f'<td>{esc(r["finished_at"])}</td></tr>'
        for r in reversed(st["metrics_runs"]))
    runs_table = (f'<table><tr><th>运行</th><th>说明</th><th>基准版本</th><th>完成时版本</th>'
                  f'<th>状态</th><th>完成时间</th></tr>{runs}</table>')
    body = f"""
<h1>仪表盘 <span class="small">当前指标：{esc(cur["run_id"])}（{esc(cur["note"])}，基准版本 {esc(cur["base_version"])}）</span></h1>
<div class="tiles">{tiles}</div>
<h2>三种去重策略的指标对比</h2>
<div class="card">{legend()}{chart}</div>
{table}{delta_note}
<h2>裁决视图的分来源贡献</h2>{src_table}
<h2>指标运行历史</h2>{runs_table}
<p>{action_form("run_metrics", "立即重算指标", st)}
   {action_form("run_metrics_slow", "模拟慢速计算（4 秒后写回）", st)}</p>
"""
    return page("仪表盘", body)


def review(st: dict) -> bytes:
    parts = ["<h1>证据审阅 <span class='small'>宽松接受但保守不接受的候选边，逐条人工裁决</span></h1>"]
    if not st["queue"]:
        parts.append("<p>待裁决队列为空。</p>")
    for e in st["queue"]:
        forms = (action_form("confirm", "✓ 确认同一事件", st, "ok", eid=e["eid"], reviewer="甲")
                 + " " + action_form("reject", "✗ 驳回（不同事件）", st, "no", eid=e["eid"], reviewer="乙"))
        parts.append(f"""<div class="card">
<b>{esc(e["eid"])}</b>（类别 {esc(e["category"])}，Δt {e["dt_sec"]}s，Δd {e["dist_m"]:.0f}m）
{rec_line(st, e["a"])}{rec_line(st, e["b"])}
{evidence_table(e)}
<p>{forms}</p></div>""")
    return page("证据审阅", "".join(parts))


def clusters(st: dict) -> bytes:
    parts = ["<h1>簇与链式风险</h1><h2>裁决视图的簇（团约束：任意两成员间都有已接受边）</h2>"]
    for c in st["views"]["adjudicated"]["clusters"]:
        if len(c) < 2:
            continue
        inner = [eid for eid in st["views"]["adjudicated"]["accepted_eids"]
                 if st["by_eid"][eid]["a"] in c and st["by_eid"][eid]["b"] in c]
        edges = "、".join(f"{eid}（{state_mod.edge_status(st, eid)}）" for eid in inner)
        recs = "".join(rec_line(st, rid) for rid in c)
        parts.append(f'<div class="card"><b>簇（{len(c)} 条记录）</b>{recs}'
                     f'<p class="small">已接受边：{esc(edges)}</p></div>')
    parts.append("<h2>宽松视图的链式风险簇（直径&gt;1，连通分量可能误合并）</h2>")
    for cr in st["unresolved"]["chain_risk"]:
        recs = "".join(rec_line(st, rid) for rid in cr["members"])
        bridges = " ".join(
            f'{esc(b)} {action_form("reject", "断开此边", st, "no", eid=b, reviewer="甲")}'
            for b in cr["bridges"])
        parts.append(f"""<div class="card"><b>链式风险簇（{len(cr["members"])} 条记录，直径 {cr["diameter"]}）</b>
{recs}<p class="small">桥接边（断开即拆分，还原原始记录）：{bridges}</p></div>""")
    if not st["unresolved"]["chain_risk"]:
        parts.append("<p>当前没有链式风险簇。</p>")
    return page("簇与链式风险", "".join(parts))


def decisions_page(st: dict) -> bytes:
    rows = []
    for d in reversed(st["decisions"]):
        reverted = d["did"] in st["reverted"]
        undo = ""
        if d["type"] != "revert" and not reverted:
            undo = action_form("revert", "撤销", st, target_did=d["did"], reviewer="甲")
        rows.append(
            f'<tr><td>{esc(d["did"])}</td><td>{esc(d["type"])}</td>'
            f'<td>{esc(d.get("eid") or d.get("target_did") or "")}</td>'
            f'<td>{esc(d["reviewer"])}</td><td>{esc(d["note"])}</td>'
            f'<td>{d["base_dseq"]}</td>'
            f'<td>{"<span class=stale>已撤销</span>" if reverted else ""} {undo}</td></tr>')
    table = ("<table><tr><th>ID</th><th>类型</th><th>对象</th><th>裁决人</th><th>说明</th>"
             "<th>基于日志位置</th><th>操作</th></tr>" + "".join(rows) + "</table>")
    conflicts = "".join(f'<li class="stale">{esc(c["detail"])}</li>'
                        for c in st["unresolved"]["conflicts"])
    body = f"<h1>裁决日志</h1>"
    if conflicts:
        body += f'<div class="card"><b>交错/冲突</b><ul>{conflicts}</ul></div>'
    return page("裁决日志", body + table)


def unresolved(st: dict) -> bytes:
    u = st["unresolved"]
    parts = ["<h1>未解决部分</h1>"]
    parts.append(f'<div class="card"><b>待裁决候选边：{len(u["pending_edges"])} 条</b> '
                 f'<a href="/review">前往证据审阅</a><ul>' +
                 "".join(f'<li>{esc(e["eid"])}（score {e["score"]:.2f}）</li>'
                         for e in u["pending_edges"]) + "</ul></div>")
    parts.append(f'<div class="card"><b>链式风险簇（宽松视图）：{len(u["chain_risk"])} 个</b> '
                 f'<a href="/clusters">前往簇页面</a><ul>' +
                 "".join(f'<li>成员 {esc("、".join(cr["members"]))}，直径 {cr["diameter"]}</li>'
                         for cr in u["chain_risk"]) + "</ul></div>")
    parts.append(f'<div class="card"><b>被链式防护阻止的合并：{len(u["blocked_merges"])} 个</b><ul>' +
                 "".join(f'<li>{esc(b["eid"])}：{esc(b["reason"])}</li>'
                         for b in u["blocked_merges"]) + "</ul></div>")
    parts.append(f'<div class="card"><b>裁决冲突：{len(u["conflicts"])} 个</b><ul>' +
                 "".join(f'<li>{esc(c["detail"])}</li>' for c in u["conflicts"]) + "</ul></div>")
    parts.append(f'<div class="card"><b>从未产生候选边的记录：{len(u["no_candidate_records"])} 条</b>'
                 f'<p class="small">{esc("、".join(u["no_candidate_records"]) or "无")}'
                 f'（可能真是孤立事件，也可能是证据完全缺失的漏配）</p></div>')
    return page("未解决", "".join(parts))


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        st = state_mod.load_state()
        if path == "/":
            self._send(dashboard(st))
        elif path == "/review":
            self._send(review(st))
        elif path == "/clusters":
            self._send(clusters(st))
        elif path == "/decisions":
            self._send(decisions_page(st))
        elif path == "/unresolved":
            self._send(unresolved(st))
        else:
            self._send(page("404", "<h1>404</h1>"), 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        get = lambda k, d="": form.get(k, [d])[0]
        action = get("action")
        with LOCK:
            if action in ("confirm", "reject"):
                decisions.submit("confirm_edge" if action == "confirm" else "reject_edge",
                                 get("reviewer", "匿名"), note=get("note", "网页裁决"),
                                 eid=get("eid"), base_dseq=int(get("base_dseq", "0")))
            elif action == "revert":
                decisions.submit("revert", get("reviewer", "匿名"), note="网页撤销",
                                 target_did=get("target_did"),
                                 base_dseq=int(get("base_dseq", "0")))
            elif action == "run_metrics":
                metrics.run(note="网页触发")
            elif action == "run_metrics_slow":
                threading.Thread(target=metrics.run,
                                 kwargs={"note": "网页慢速计算", "delay": 4},
                                 daemon=True).start()
        self._redirect("/")

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8077)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"审阅网页：http://127.0.0.1:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
