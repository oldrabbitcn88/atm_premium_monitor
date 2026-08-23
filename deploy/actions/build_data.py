# -*- coding: utf-8 -*-
"""
GitHub Actions 计算脚本：A股两个品种（CSI300 / CSI500）
- 读 deploy/data/data.json（含历史）
- 腾讯行情接口取沪深300/510500 收盘价
- 中金所 quote_IO.txt 取 IO 期权；上交所 yunhq 取 510500 期权
- 若今日为交割日（IO=第三个周五 / ETF=第四个周三，含3天顺延窗口）且当月未更新，更新当月数据点
- 写回 data.json（generated_at=北京时间）

运行：python build_data.py（仅依赖 requests，Actions 环境无需富途）
"""
import calendar
import datetime
import json
import os
import re
import sys

import requests

# 允许从任意工作目录运行
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "data.json")
if not os.path.exists(DATA_FILE):
    DATA_FILE = os.path.join(BASE, "..", "data", "data.json")

VOL_ANNUAL = {"CSI300": 0.1772, "CSI500": 0.2293}
EXPIRY_WINDOW_DAYS = 3

CFFEX_IO_URL = "http://www.cffex.com.cn/quote_IO.txt"
QQ_QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
SSE_TSTYLE = "http://yunhq.sse.com.cn:32041/v1/sho/list/tstyle/{etf}_{mm}"
SSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://www.sse.com.cn/assortment/options/price/",
}


def now_cn():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def roundup_strike(price, interval):
    """HSICC 平值取法：roundup(价格/间距)*间距"""
    import math
    return math.ceil(price / interval) * interval


def io_interval(price):
    """中金所IO行权价间距（按现货点位区间）"""
    if price > 10000:
        return 200
    if price > 5000:
        return 100
    if price > 2500:
        return 50
    return 25


def etf_interval(price):
    """上交所ETF期权行权价间距（按现货价格区间）"""
    if price > 20:
        return 5.0
    if price > 10:
        return 1.0
    if price > 5:
        return 0.25
    if price > 3:
        return 0.1
    if price > 1:
        return 0.05
    return 0.05


def io_rule_day(year, month):
    """IO交割日规则日：每月第三个周五"""
    c = calendar.monthcalendar(year, month)
    fridays = [w[4] for w in c if w[4] != 0]
    return datetime.date(year, month, fridays[2]) if len(fridays) >= 3 else None


def etf_rule_day(year, month):
    """ETF交割日规则日：每月第四个周三"""
    c = calendar.monthcalendar(year, month)
    wed = [w[2] for w in c if w[2] != 0]
    return datetime.date(year, month, wed[3]) if len(wed) >= 4 else None


def qq_price(code):
    """腾讯行情：sh000300 / sh510500 → (最新价, 行情日期YYYYMMDD)"""
    r = requests.get(QQ_QUOTE_URL.format(codes=code), headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.encoding = "gbk"
    m = re.search(r'="([^"]+)"', r.text)
    if not m:
        return None, None
    fields = m.group(1).split("~")
    if len(fields) < 4:
        return None, None
    price = float(fields[3])
    # 行情时间戳在索引30附近（YYYYMMDDHHMMSS）
    ts = fields[30] if len(fields) > 30 else ""
    return price, ts[:8]


def cffex_io_quotes():
    r = requests.get(CFFEX_IO_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.encoding = "utf-8"
    quotes = {}
    for line in r.text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 4:
            try:
                quotes[parts[0].strip()] = float(parts[3].strip())
            except ValueError:
                continue
    return quotes


def sse_etf_quotes(etf_code, month):
    """上交所 yunhq T型：返回 [(简称, 最新价)]"""
    url = SSE_TSTYLE.format(etf=etf_code, mm=f"{month:02d}")
    for _ in range(3):
        try:
            d = requests.get(url, headers=SSE_HEADERS, timeout=8).json()
            if d.get("total", 0) > 0:
                return [(it[1], float(it[2])) for it in d.get("list", [])]
        except Exception:
            continue
    return []


def parse_sse_name(name):
    """'500ETF购9月8000' / '500ETF购8月7859A' → (type, strike, adj)"""
    m = re.match(r".+?(购|沽)(\d+)月(\d+)([AB]?)$", name)
    if not m:
        return None
    t, _, strike_raw, adj = m.groups()
    return t, int(strike_raw) / 1000.0, adj


def next_month_ym(d):
    return (d.year, d.month + 1) if d.month < 12 else (d.year + 1, 1)


def fetch_io(underlying_price, today):
    """CSI300：IO 下月 ATM call 最新价 → (strike, price, code)"""
    quotes = cffex_io_quotes()
    if not quotes:
        return None
    strike = roundup_strike(underlying_price, io_interval(underlying_price))
    ny, nm = next_month_ym(today)
    prefix = f"IO{ny % 100:02d}{nm:02d}-C-{int(strike)}"
    if prefix in quotes:
        return strike, quotes[prefix], prefix
    pat = re.compile(rf"^IO{ny % 100:02d}{nm:02d}-C-(\d+)$")
    best = None
    for c, p in quotes.items():
        m = pat.match(c)
        if m:
            diff = abs(int(m.group(1)) - strike)
            if best is None or diff < best[0]:
                best = (diff, int(m.group(1)), p, c)
    if best:
        return best[1], best[2], best[3]
    return None


def fetch_etf(etf_price, today):
    """CSI500：510500 下月 ATM call 最新价 → (strike, price, tcode)"""
    strike = roundup_strike(etf_price, etf_interval(etf_price))
    ny, nm = next_month_ym(today)
    for off in range(4):
        yy, mm = ny + (nm - 1 + off) // 12, (nm - 1 + off) % 12 + 1
        quotes = sse_etf_quotes("510500", mm)
        calls = [q for q in quotes if parse_sse_name(q[0]) and parse_sse_name(q[0])[0] == "购"]
        for name, price in calls:
            t, s, adj = parse_sse_name(name)
            if adj == "" and abs(s - round(strike, 2)) < 1e-6:
                tcode = f"510500C{yy % 100:02d}{mm:02d}M{int(round(s * 1000)):05d}"
                return strike, price, tcode
        above = [q for q in calls if parse_sse_name(q[0])[1] >= etf_price - 1e-9]
        if above:
            best = min(above, key=lambda q: abs(parse_sse_name(q[0])[1] - etf_price))
            t, s, adj = parse_sse_name(best[0])
            tcode = f"510500C{yy % 100:02d}{mm:02d}{adj or 'M'}{int(round(s * 1000)):05d}"
            return s, best[1], tcode
    return None


def update_point(items, key, ts, underlying, strike, opt_price, py, expiry, contract):
    """交割日口径：当月已有数据点则跳过（不覆盖），否则追加"""
    ym = ts[:7]
    new = {
        "ts": ts, "underlying": underlying, "strike": strike,
        "option_price": opt_price, "premium_yield": py,
        "expiry": expiry, "contract": contract,
    }
    hist = items[key]["history"]
    for h in hist:
        if (h.get("ts") or "")[:7] == ym:
            return "skipped_当月已记录"
    hist.append(new)
    return "appended"


def main():
    today = now_cn().date()
    print(f"[build_data] 运行时间: {today}")
    with open(DATA_FILE, encoding="utf-8") as f:
        payload = json.load(f)
    items = payload["items"]
    report = []

    # ---- CSI300：IO ----
    rule = io_rule_day(today.year, today.month)
    if rule and 0 <= (today - rule).days <= EXPIRY_WINDOW_DAYS:
        price, _ = qq_price("sh000300")
        if price:
            got = fetch_io(price, today)
            if got:
                strike, opt, code = got
                py = opt / price
                act = update_point(items, "CSI300", today.strftime("%Y-%m-%d"), price,
                                   strike, opt, round(py, 6), f"{next_month_ym(today)[0]:04d}-{next_month_ym(today)[1]:02d}", code)
                report.append(f"CSI300: {act} | 收盘{price} ATM{strike} 权利金{opt} PY={py:.4%}")
            else:
                report.append("CSI300: IO行情获取失败")
        else:
            report.append("CSI300: 腾讯行情失败")
    else:
        report.append(f"CSI300: 非交割日(规则日{rule})")

    # ---- CSI500：510500 ----
    rule = etf_rule_day(today.year, today.month)
    if rule and 0 <= (today - rule).days <= EXPIRY_WINDOW_DAYS:
        price, _ = qq_price("sh510500")
        if price:
            got = fetch_etf(price, today)
            if got:
                strike, opt, tcode = got
                py = opt / price
                act = update_point(items, "CSI500", today.strftime("%Y-%m-%d"), price,
                                   strike, opt, round(py, 6), f"{next_month_ym(today)[0]:04d}-{next_month_ym(today)[1]:02d}", tcode)
                report.append(f"CSI500: {act} | 收盘{price} ATM{strike} 权利金{opt} PY={py:.4%}")
            else:
                report.append("CSI500: 期权行情获取失败")
        else:
            report.append("CSI500: 腾讯行情失败")
    else:
        report.append(f"CSI500: 非交割日(规则日{rule})")

    payload["generated_at"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print("\n".join(report))
    print("[build_data] 完成，data.json 已更新")


if __name__ == "__main__":
    main()
