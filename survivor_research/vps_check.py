# -*- coding: utf-8 -*-
"""VPS environment check for the operator lab.

Kept ASCII-only on purpose: this file gets scp'd, and scp does not preserve
the UTF-8 BOM that Windows PowerShell needs, so Chinese text would come out
mangled. Files that need Chinese go through git instead.
"""
import sys


def main():
    ok = True
    print("python", sys.version.split()[0])
    for mod in ("requests", "sqlite3"):
        try:
            __import__(mod)
            print(f"  {mod}: OK")
        except ImportError as e:
            print(f"  {mod}: MISSING ({e})")
            ok = False
    try:
        import lab_forensics as fx
        print(f"  lab_forensics: OK  helius={fx.HAS_HELIUS}  endpoints={len(fx.RPCS)}")
        r = fx.rpc("getSlot", [])
        print(f"  rpc getSlot -> {r}")
        if not r:
            ok = False
    except Exception as e:
        print(f"  lab_forensics: FAIL {type(e).__name__}: {e}")
        ok = False
    try:
        import cg_client as cg
        d = cg.get("networks/solana/new_pools", {"page": 1})
        n = len((d or {}).get("data", []))
        print(f"  cg_client: OK  new_pools returned {n}")
        if not n:
            ok = False
    except Exception as e:
        print(f"  cg_client: FAIL {type(e).__name__}: {e}")
        ok = False
    try:
        import lab_db as db
        db.init()
        print(f"  lab_db: OK  {db.counts()}")
    except Exception as e:
        print(f"  lab_db: FAIL {type(e).__name__}: {e}")
        ok = False
    print("RESULT:", "READY" if ok else "NOT_READY")


if __name__ == "__main__":
    main()
