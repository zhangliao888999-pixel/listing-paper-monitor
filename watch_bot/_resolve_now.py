# -*- coding: utf-8 -*-
"""一次性: 调用git_lock.resolve_stuck_merge()收拾当前卡住的合并冲突。
journal.jsonl是append-only账本,union合并保留两边全部记录,不丢任何成交。"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from git_lock import resolve_stuck_merge

repo = HERE.parent
print("resolve_stuck_merge ->", resolve_stuck_merge(repo))

import json
p = HERE / "journal.jsonl"
n = bad = live = 0
for line in p.open(encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    n += 1
    try:
        r = json.loads(line)
        if r.get("dry_run") is False:
            live += 1
    except json.JSONDecodeError:
        bad += 1
print(f"journal: {n}行, 坏行{bad}, 实盘记录{live}条")
