# -*- coding: utf-8 -*-
"""
一次性导出：从 SQLite 历史库生成部署基线 JSON
- deploy/data/data.json  ← CSI300 + CSI500（GitHub Actions 维护）
- deploy/data/hsi.json   ← HSI（本机 OpenD 同步脚本维护）
运行：python deploy/export_initial_json.py
"""
import json
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "..", "backend", "atm_history.db")
DATA_DIR = os.path.join(BASE, "data")

VOL_ANNUAL = {"HSI": 0.2469, "CSI300": 0.1772, "CSI500": 0.2293}
META = {
    "HSI": {"name": "恒生指数", "unit": "点"},
    "CSI300": {"name": "沪深300指数", "unit": "点"},
    "CSI500": {"name": "中证500ETF", "unit": "元"},
}


def load_key(conn, key):
    rows = conn.execute(
        "SELECT ts, underlying, strike, option_price, premium_yield, expiry, contract "
        "FROM atm_history WHERE key=? ORDER BY ts", (key,)
    ).fetchall()
    hist = []
    for r in rows:
        hist.append({
            "ts": r[0][:10],
            "underlying": r[1],
            "strike": r[2],
            "option_price": r[3],
            "premium_yield": r[4],
            "expiry": r[5],
            "contract": r[6],
        })
    return hist


def make_file(keys, path):
    conn = sqlite3.connect(DB)
    items = {}
    for k in keys:
        items[k] = {**META[k], "history": load_key(conn, k)}
    conn.close()
    payload = {
        "generated_at": "",
        "vol_annual": {k: VOL_ANNUAL[k] for k in keys},
        "items": items,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    total = sum(len(items[k]["history"]) for k in keys)
    print(f"已生成 {path}: {keys} 共 {total} 期")


if __name__ == "__main__":
    make_file(["CSI300", "CSI500"], os.path.join(DATA_DIR, "data.json"))
    make_file(["HSI"], os.path.join(DATA_DIR, "hsi.json"))
