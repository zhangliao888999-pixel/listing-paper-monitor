# -*- coding: utf-8 -*-
"""补 watchlist.tier 列。写成文件而不是内联 python -c: SSH->PowerShell->python
三层引号嵌套会把 SQL 里的引号吃掉(这个坑今天踩过三次)。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import lab_db as db
c = db.conn()
try:
    c.execute("ALTER TABLE watchlist ADD COLUMN tier TEXT DEFAULT 'newborn'")
    c.commit()
    print("tier 列已添加")
except Exception as e:
    print("已存在或失败:", e)
cols = [r[1] for r in c.execute("PRAGMA table_info(watchlist)")]
print("watchlist 列:", cols)
