# -*- coding: utf-8 -*-
"""SQLite 历史数据持久化"""
import sqlite3

from .config import DB_PATH


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS atm_history (
            ts TEXT NOT NULL,
            key TEXT NOT NULL,
            name TEXT,
            underlying REAL,
            strike REAL,
            option_price REAL,
            premium_yield REAL,
            expiry TEXT,
            contract TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_key_ts ON atm_history(key, ts)")
    return conn


def insert_snapshot(items):
    """items: [{key,name,underlying,strike,option_price,premium_yield,expiry,contract,ts?}]
    ts 缺省用当前时间；历史导入可显式传 ts（"YYYY-MM-DD HH:MM:SS"）"""
    if not items:
        return
    conn = _conn()
    default_ts = _now_str()
    conn.executemany(
        "INSERT INTO atm_history(ts,key,name,underlying,strike,option_price,premium_yield,expiry,contract) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        [(it.get("ts") or default_ts, it["key"], it.get("name"), it.get("underlying"), it.get("strike"),
          it.get("option_price"), it.get("premium_yield"), it.get("expiry"), it.get("contract"))
         for it in items],
    )
    conn.commit()
    conn.close()


def upsert_monthly(item):
    """按月去重写入：同 (key, YYYY-MM) 先删后插，保证每月每品种只有1个数据点（交割日口径）"""
    ts = item.get("ts") or _now_str()
    ym = ts[:7]
    conn = _conn()
    conn.execute(
        "DELETE FROM atm_history WHERE key=? AND substr(ts,1,7)=?", (item["key"], ym)
    )
    conn.execute(
        "INSERT INTO atm_history(ts,key,name,underlying,strike,option_price,premium_yield,expiry,contract) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (ts, item["key"], item.get("name"), item.get("underlying"), item.get("strike"),
         item.get("option_price"), item.get("premium_yield"), item.get("expiry"), item.get("contract")),
    )
    conn.commit()
    conn.close()


def has_month(key, ym):
    """某 key 某月（YYYY-MM）是否已有数据点"""
    conn = _conn()
    cur = conn.execute(
        "SELECT COUNT(*) FROM atm_history WHERE key=? AND substr(ts,1,7)=?", (key, ym)
    )
    n = cur.fetchone()[0]
    conn.close()
    return n > 0


def query_history(key, limit=1200):
    """月度序列：每 (key, 月) 仅保留最新一条（交割日点优先），升序返回"""
    conn = _conn()
    cur = conn.execute(
        "SELECT ts, underlying, strike, option_price, premium_yield, expiry, contract "
        "FROM atm_history WHERE key=? ORDER BY ts DESC LIMIT ?",
        (key, limit),
    )
    rows = cur.fetchall()
    conn.close()
    seen = set()
    out = []
    for r in rows:
        ym = r[0][:7]
        if ym in seen:
            continue
        seen.add(ym)
        out.append({"ts": r[0], "underlying": r[1], "strike": r[2], "option_price": r[3],
                    "premium_yield": r[4], "expiry": r[5], "contract": r[6]})
    out.reverse()
    return out


def history_stats(key):
    """某 key 历史 PY 统计（均值/中位数/样本数/统计区间），供前端参考线与对比"""
    conn = _conn()
    cur = conn.execute(
        "SELECT premium_yield, ts FROM atm_history WHERE key=? AND premium_yield IS NOT NULL", (key,)
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return {"count": 0, "mean": None, "median": None, "first_ts": None, "last_ts": None}
    vals = [r[0] for r in rows]
    s = sorted(vals)
    n = len(s)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    ts_sorted = sorted(r[1] for r in rows)
    return {
        "count": n, "mean": sum(s) / n, "median": median,
        "first_ts": ts_sorted[0], "last_ts": ts_sorted[-1],
    }


def latest_snapshot():
    conn = _conn()
    cur = conn.execute(
        "SELECT h.key, h.name, h.ts, h.underlying, h.strike, h.option_price, h.premium_yield, h.expiry, h.contract "
        "FROM atm_history h JOIN (SELECT key, MAX(ts) AS mts FROM atm_history GROUP BY key) m "
        "ON h.key = m.key AND h.ts = m.mts"
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {"key": r[0], "name": r[1], "ts": r[2], "underlying": r[3], "strike": r[4],
         "option_price": r[5], "premium_yield": r[6], "expiry": r[7], "contract": r[8]}
        for r in rows
    ]


def _now_str():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
