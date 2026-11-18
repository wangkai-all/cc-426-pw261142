"""共享基础设施：路径、JSONL 读写、全局状态版本。"""
from __future__ import annotations

import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
RECORDS = os.path.join(DATA_DIR, "records.jsonl")        # ingest 输出：规范化记录（不可变）
CANDIDATES = os.path.join(DATA_DIR, "candidates.jsonl")  # matcher 输出：候选边 + 证据（只追加）
DECISIONS = os.path.join(DATA_DIR, "decisions.jsonl")    # 人工裁决日志（只追加，撤销=补偿条目）
METRICS_RUNS = os.path.join(DATA_DIR, "metrics_runs.jsonl")  # 指标计算器输出（含 stale 标记）
GROUND_TRUTH = os.path.join(DATA_DIR, "ground_truth.json")   # 仅模拟评估用，系统本身不读


def ensure_dirs() -> None:
    os.makedirs(RAW_DIR, exist_ok=True)


def read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def append_jsonl(path: str, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)


def _nlines(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def state_version() -> tuple[int, str]:
    """全局状态版本 = 三类日志行数。指标运行以此判定自己是否已过时。"""
    r, c, d = _nlines(RECORDS), _nlines(CANDIDATES), _nlines(DECISIONS)
    return r + c + d, f"r{r}·c{c}·d{d}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """简化本地投影（演示数据范围小，足够精确）。"""
    dy = (lat1 - lat2) * 111_000.0
    dx = (lon1 - lon2) * 111_000.0 * 0.72  # 当地纬度余弦近似
    return (dx * dx + dy * dy) ** 0.5
