"""接收日志服务（独立运行）：原始来源日志 -> 规范化记录，只追加、幂等。

  python3 -m event_dedup.ingest

规范化：类别词表归一、地址写法归一（Main Street -> main st）、文本分词。
原始记录不可变；同一 (source, src_id) 重复到达会被跳过。
"""
from __future__ import annotations

import os
import re

from . import common

CATEGORY_MAP = {
    "fire": "fire", "smoke": "fire", "#fire": "fire",
    "traffic": "traffic", "crash": "traffic", "collision": "traffic", "#accident": "traffic",
    "flood": "flood", "water": "flood", "#flood": "flood",
    "medical": "medical", "aed": "medical", "#emergency": "medical",
}

STREET_ABBR = {"street": "st", "avenue": "ave", "drive": "dr", "road": "rd"}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_text(text: str) -> list[str]:
    """小写、去标点、 hashtag 还原、简单词干（flooded->flood）。"""
    tokens = _TOKEN_RE.findall(text.lower())
    out = []
    for tok in tokens:
        tok = STREET_ABBR.get(tok, tok)
        for suffix in ("ing", "ed"):
            if len(tok) > 5 and tok.endswith(suffix):
                tok = tok[: -len(suffix)]
                break
        out.append(tok)
    return sorted(set(out))


def normalize_address(addr: str | None) -> tuple[str | None, str | None]:
    """返回 (门牌号, 规范化街道)。非 911 记录没有地址。"""
    if not addr:
        return None, None
    tokens = normalize_text(addr)
    num = tokens[0] if tokens and tokens[0].isdigit() else None
    street = " ".join(t for t in tokens if t != num) or None
    return num, street


def run() -> int:
    """摄取 raw/ 下全部来源日志，返回新写入的记录数。"""
    common.ensure_dirs()
    existing = common.read_jsonl(common.RECORDS)
    seen = {(r["source"], r["src_id"]) for r in existing}
    next_num = len(existing) + 1
    written = 0
    for fname in sorted(os.listdir(common.RAW_DIR)):
        if not fname.endswith(".jsonl"):
            continue
        for raw in common.read_jsonl(os.path.join(common.RAW_DIR, fname)):
            key = (raw["source"], raw["src_id"])
            if key in seen:
                continue  # 幂等：重复到达直接跳过
            seen.add(key)
            addr_num, addr_street = normalize_address(raw.get("address"))
            rec = {
                "rid": f"r{next_num:04d}",
                "source": raw["source"],
                "src_id": raw["src_id"],
                "ts": raw["ts"],
                "category": CATEGORY_MAP.get(str(raw.get("category_raw", "")).lower(), "other"),
                "lat": raw["lat"],
                "lon": raw["lon"],
                "addr_num": addr_num,
                "addr_street": addr_street,
                "phone": raw.get("phone"),
                "text": raw.get("text", ""),
                "tokens": normalize_text(raw.get("text", "")),
                "ingested_at": common.now_iso(),
            }
            common.append_jsonl(common.RECORDS, rec)
            next_num += 1
            written += 1
    return written


def main() -> None:
    n = run()
    total = len(common.read_jsonl(common.RECORDS))
    print(f"[ingest] 新写入 {n} 条，累计 {total} 条规范化记录")


if __name__ == "__main__":
    main()
