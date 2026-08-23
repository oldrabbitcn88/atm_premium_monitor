# -*- coding: utf-8 -*-
"""
一次性导入：将之前研究的真实历史 Premium Yield（v5 CSV）写入 SQLite 历史库
- CSI300（沪深300） ← v5 中 IO(沪深300股指期权) 的 79 期历史
- CSI500（中证500） ← v5 中 510500ETF期权 的 42 期历史
- HSI（恒生指数）   ← v5 未含恒指期权历史（此前研究未覆盖），从部署后实时积累

幂等策略：若目标 key 已存在"2026 年之前"的历史记录，则跳过（视为已导入）。
运行：python -m backend.import_history
"""
import csv
import os
import sqlite3

from .config import DB_PATH
from . import store

CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "outputs", "premium_yield_underlying_deliverable_v5.csv")

# 恒指历史 PY（用户提供：2005-01 ~ 2026-07 月度）
HSI_CSV_PATH = "D:/微云同步助手/philip's folder/工作/Hong Kong/项目/Mirae未来资产-Covered Call ETF/我的推介材料/第一创业证券资管部/附件1：恒生指数平值看涨期权费率统计表.csv"

# 品种 → (网站 key, 显示名)
MAP = {
    "IO(沪深300股指期权)": ("CSI300", "沪深300指数"),
    "510500ETF期权": ("CSI500", "中证500ETF"),
}


def _has_imported(key):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT COUNT(*) FROM atm_history WHERE key=? AND ts < '2026-01-01'", (key,))
    n = cur.fetchone()[0]
    conn.close()
    return n > 0


def import_history():
    if not os.path.exists(CSV_PATH):
        print("未找到 v5 CSV:", CSV_PATH)
        return 0
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    data = rows[1:]
    total = 0
    for r in data:
        name, cur_label, expiry_date, wind_code, underlying, settle, strike, interval, code, ocode, tcode, opt_price, py = r[:13]
        if name not in MAP:
            continue
        key, display = MAP[name]
        try:
            opt = float(opt_price)
            if opt <= 0:
                continue
            py_val = float(py)
        except (ValueError, TypeError):
            continue
        # 交割日 "2020/2/21" → "2020-02-21 15:00:00"
        d = expiry_date.replace("/", "-")
        parts = d.split("-")
        if len(parts) == 3:
            d = f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        ts = f"{d} 15:00:00"
        # 结算月 "202003" → "2020-03"
        exp = f"{settle[:4]}-{settle[4:6]}" if len(settle) == 6 else ""
        contract = ocode or code or ""
        store.insert_snapshot([{
            "key": key, "name": display, "underlying": float(underlying),
            "strike": float(strike), "option_price": opt, "premium_yield": py_val,
            "expiry": exp, "contract": contract, "ts": ts,
        }])
        total += 1
    return total


def import_hsi_history():
    """导入恒指历史月度 Premium Yield（用户提供的 CSV）
    CSV: Year, Month, PremiumYield（如 2005,1,1.00%）
    只取当年数据（<=当前年），时间戳取该月最后第二个营业日（真实交割日口径）
    """
    if not os.path.exists(HSI_CSV_PATH):
        print("未找到恒指 CSV:", HSI_CSV_PATH)
        return 0
    import datetime
    total = 0
    with open(HSI_CSV_PATH, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    for r in rows:
        if len(r) < 3 or not r[0].strip().isdigit():
            continue
        year = int(r[0].strip())
        month = int(r[1].strip())
        rate = r[2].strip().replace("%", "")
        try:
            py = float(rate) / 100.0
        except ValueError:
            continue
        if not (1 <= month <= 12) or year > datetime.date.today().year:
            continue
        # 恒指期权月度到期日 = 每月最后第二个营业日（与 futu_adapter._month_end_business_day 一致）
        d = _month_end_business_day(year, month)
        ts = f"{d.isoformat()} 15:00:00"
        store.insert_snapshot([{
            "key": "HSI", "name": "恒生指数", "underlying": None, "strike": None,
            "option_price": None, "premium_yield": py, "expiry": "",
            "contract": "", "ts": ts,
        }])
        total += 1
    return total


def _month_end_business_day(year, month, n=2):
    """某月最后第 n 个营业日（跳过周末；节假日近似处理）"""
    import datetime
    if month == 12:
        ny, nm = year + 1, 1
    else:
        ny, nm = year, month + 1
    d = datetime.date(ny, nm, 1) - datetime.timedelta(days=1)
    count = 0
    while count < n:
        if d.weekday() < 5:
            count += 1
        if count < n:
            d -= datetime.timedelta(days=1)
    return d


if __name__ == "__main__":
    print("CSV:", CSV_PATH)
    for key, _ in MAP.values():
        print(f"  {key}: 已导入历史={_has_imported(key)}")
    n = import_history()
    print("A股历史导入:", n, "条")
    print("HSI已导入历史:", _has_imported("HSI"))
    if not _has_imported("HSI"):
        h = import_hsi_history()
        print("恒指历史导入:", h, "条")
    else:
        print("恒指历史已存在，跳过")
    print("导入后各key历史条数:")
    for key in ["HSI", "CSI300", "CSI500"]:
        rows = store.query_history(key, limit=10000)
        print(f"  {key}: {len(rows)} 条")
