# -*- coding: utf-8 -*-
"""
HSI 历史明细补全（v2）：
- underlying: Wind HSI.HI 交割日收盘（全历史可得）
- strike: HKEX行权价间距规则 roundup（2018-01-22前后规则不同，确定性）
- expiry: 下月到期日（最后第二营业日）
- contract: HKEX标准合约代码 HK.HSI{YYMMDD}C{strike*1000}
- option_price: 仅当到期合约距今天<80天（富途保留K线）时尝试查实际值；更早月份保持None
  （富途历史K线只保留近1-2月退市合约，2005-2026更早月份需用户从Wind终端导出）
"""
import argparse
import datetime
import json
import os
import sqlite3
import time

import futu as ft

DB = os.path.join(os.path.dirname(__file__), "atm_history.db")
WIND_FILE = r"C:\Users\Philip Z\.workbuddy\projects\e-Users-Philip Z-Documents-WorkBuddy-2026-08-22-17-30-32\5b1db8e6-e16f-4749-9701-d7112e20d128\tool-results\mcp-connector-proxy-wind-finance_get_index_kline-1787470440102-fb1dc3.txt"

RULE_SWITCH = datetime.date(2018, 1, 22)
# (门槛点位, 间距)：2018-01-22前  / 2018-01-22后（HKEX官方短期期权规则）
TABLES = {
    "old": [(8000, 200), (2000, 100), (0, 50)],
    "new": [(20000, 200), (5000, 100), (0, 50)],
}

FUTU_RETENTION_DAYS = 80  # 富途保留已退市期权K线的近似窗口


def load_wind_hsi():
    with open(WIND_FILE, encoding="utf-8") as f:
        content = f.read()
    try:
        d = json.loads(content)
    except Exception:
        d = json.loads(content[content.find("{"):])
    return {r[0][:10]: float(r[2]) for r in d["data"]["rows"]}


def strike_interval(price, date):
    table = TABLES["old"] if date < RULE_SWITCH else TABLES["new"]
    for lo, iv in sorted(table, reverse=True):
        if price >= lo:
            return iv
    return 50


def month_end_business_day(y, m, n=2):
    if m == 12:
        ny, nm = y + 1, 1
    else:
        ny, nm = y, m + 1
    d = datetime.date(ny, nm, 1) - datetime.timedelta(days=1)
    count = 0
    while count < n:
        if d.weekday() < 5:
            count += 1
        if count < n:
            d -= datetime.timedelta(days=1)
    return d


def opt_code(expiry_date, strike):
    return f"HK.HSI{expiry_date.strftime('%y%m%d')}C{int(strike) * 1000}"


def query_close(q, code, target_date, window=6):
    start = (target_date - datetime.timedelta(days=window)).strftime("%Y-%m-%d")
    end = (target_date + datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    ret, data, _ = q.request_history_kline(code, start=start, end=end, ktype=ft.KLType.K_DAY)
    if ret != ft.RET_OK or len(data) == 0:
        return None
    rows = sorted((r["time_key"][:10], float(r["close"])) for _, r in data.iterrows())
    tstr = target_date.strftime("%Y-%m-%d")
    for dt, close in rows:
        if dt == tstr:
            return close
    before = [x for x in rows if x[0] <= tstr]
    return before[-1][1] if before else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理最近N期")
    ap.add_argument("--no-futu", action="store_true", help="不查富途（只填确定性字段）")
    args = ap.parse_args()

    hsi_close = load_wind_hsi()
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT rowid, ts FROM atm_history WHERE key='HSI' ORDER BY ts").fetchall()
    if args.limit:
        rows = rows[-args.limit:]
    print(f"待处理: {len(rows)} 期（{rows[0][1][:10]} ~ {rows[-1][1][:10]}）")

    q = None
    if not args.no_futu:
        q = ft.OpenQuoteContext(host="127.0.0.1", port=11111)

    ok, no_close, no_opt = 0, 0, 0
    for i, (rid, ts) in enumerate(rows, 1):
        d = datetime.date.fromisoformat(ts[:10])
        # 1) 恒指收盘（Wind，缺失取前最近交易日）
        close = None
        probe = d
        for _ in range(7):
            if probe.isoformat() in hsi_close:
                close = hsi_close[probe.isoformat()]
                break
            probe -= datetime.timedelta(days=1)
        if close is None:
            no_close += 1
            print(f"[{i}] {d} 无Wind收盘")
            continue
        # 2) 下月到期日 + 行权价
        ny, nm = (d.year, d.month + 1) if d.month < 12 else (d.year + 1, 1)
        expiry = month_end_business_day(ny, nm)
        iv = strike_interval(close, d)
        strike = -(-int(close) // iv) * iv
        contract = opt_code(expiry, strike)

        # 3) 权利金：仅近月尝试富途（退市久远的合约富途无K线）
        opt_price = None
        if q is not None and (expiry - datetime.date.today()).days < FUTU_RETENTION_DAYS:
            for off in range(-1, 2):
                s = strike + off * iv
                if s <= 0:
                    continue
                p = query_close(q, opt_code(expiry, s), d)
                if p is not None:
                    opt_price = p
                    contract = opt_code(expiry, s)
                    strike = s
                    break
                time.sleep(0.2)

        conn.execute(
            "UPDATE atm_history SET underlying=?, strike=?, option_price=?, expiry=?, contract=? WHERE rowid=?",
            (close, strike, opt_price, f"{expiry.year:04d}-{expiry.month:02d}", contract, rid),
        )
        conn.commit()
        ok += 1
        if opt_price is None:
            no_opt += 1
        if i <= 5 or i % 40 == 0:
            tag = f"权利金={opt_price}" if opt_price is not None else "权利金=--"
            print(f"[{i}/{len(rows)}] {d} 收盘={close:.1f} 间距={iv} ATM={strike} {tag} {contract}")
        time.sleep(0.15)

    if q:
        q.close()
    conn.close()
    print(f"完成: 成功 {ok}（其中权利金未补 {no_opt}，无Wind收盘 {no_close}）")


if __name__ == "__main__":
    main()
