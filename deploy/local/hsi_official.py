# -*- coding: utf-8 -*-
"""恒指月度 Premium Yield —— 以港交所恒指期权日报的官方结算价（O.Q.P.）为准

口径：交割日（每月倒数第二个港股交易日）"下月 ATM 认购"的 O.Q.P. ÷ 当日恒指收盘。
港交所日报：https://www.hkex.com.hk/eng/stat/dmstat/dayrpt/hsio{YYMMDD}.htm
  - 当晚才发布，交割日 16:30 的作业通常取不到，次日运行即可补上
  - 只保留约 11 个月，更早的月份取不到（会被跳过，原值不动）

对窗口内"缺失"或"尚非官方口径"的月份逐一补取，已是官方口径的不再请求。
OpenD 在交割日当天写入的临时值（premium_yield_provisional）会在此被官方值覆盖，
OpenD 抓到的价格保留在 option_price_market 存档。

用法：
  python hsi_official.py                    # 默认处理最近 330 天内的交割日
  python hsi_official.py --since 2025-10    # 指定起始月份（一次性重建用）
  python hsi_official.py --upload           # 有变化则 git commit + push
"""
import argparse
import calendar
import datetime
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(BASE, "..", ".."))
HSI_JSON = os.path.join(BASE, "..", "data", "hsi.json")
sys.path.insert(0, ROOT)
from backend.engine import atm_strike  # noqa: E402

DMR_URL = "https://www.hkex.com.hk/eng/stat/dmstat/dayrpt/hsio{d:%y%m%d}.htm"
KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
             "?param=hkHSI,day,{start},{end},640,")
MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
UA = {"User-Agent": "Mozilla/5.0"}
SOURCE = "hkex_oqp"


def _get(url, timeout=30, tries=3):
    """GET 文本；404 返回 None；连接中断/超时最多重试 tries 次"""
    import http.client
    import time
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == tries - 1:
                raise
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, ConnectionError):
            if i == tries - 1:
                raise
        time.sleep(3 * (i + 1))


def fetch_dmr(day):
    """取 day 当天的恒指期权日报（纯文本）；未发布/已过保留期/非交易日返回 None"""
    raw = _get(DMR_URL.format(d=day))
    if not raw or "O.Q.P" not in raw:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", "", raw))
    # 防止取到别的日期：报告表头含"29 OCT 2026"这样的交易日
    if f"{day.day} {MON[day.month - 1]} {day.year}" not in text:
        return None
    return text


def settle_price(text, year, month, strike):
    """日报里 year-month 到期、行权价 strike 的认购期权 → (O.Q.P., IV%)；找不到返回 None"""
    tag = f"{MON[month - 1]}-{year % 100:02d}"
    for line in text.splitlines():
        if line.startswith(tag) and re.search(rf"\b{int(strike)}\s+C\b", line):
            day_session = line.split("|")[1].split()   # 开 高 低 O.Q.P. 变动 IV% 成交量
            return float(day_session[3]), day_session[5]
    return None


def hsi_closes(start, end):
    """恒指日收盘 {date: close}（腾讯日线）"""
    raw = json.loads(_get(KLINE_URL.format(start=start.isoformat(), end=end.isoformat())))
    node = raw["data"]["hkHSI"]
    return {datetime.date.fromisoformat(r[0]): float(r[2])
            for r in (node.get("day") or node.get("qfqday") or [])}


def expiry_day(year, month, closes, today=None):
    """恒指期权月度交割日 = 该月倒数第二个港股交易日。
    今天之前的日子以实际交易日（日线）为准——没有日线的工作日就是休市（如月末的耶稣受难节）；
    今天及以后尚未发生的日子，只能按工作日假定为交易日。"""
    today = today or datetime.date.today()
    last = datetime.date(year, month, calendar.monthrange(year, month)[1])
    traded = {d for d in closes if d.year == year and d.month == month and d < today}
    d = max(today, datetime.date(year, month, 1))
    while d <= last:
        if d.weekday() < 5:
            traded.add(d)
        d += datetime.timedelta(days=1)
    days = sorted(traded)
    return days[-2] if len(days) >= 2 else None


def next_ym(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def official_record(exp, old, closes):
    """按官方结算价生成 exp 交割日的数据点；日报暂不可得返回 None"""
    text = fetch_dmr(exp)
    if not text:
        return None
    underlying = (old or {}).get("underlying") or closes.get(exp)
    if not underlying:
        return None
    strike = (old or {}).get("strike") or atm_strike(underlying, "HSI")[0]
    ny, nm = next_ym(exp.year, exp.month)
    got = settle_price(text, ny, nm, strike)
    if not got:
        print(f"[hsi_official] {exp} 日报里找不到 {MON[nm - 1]}-{ny % 100:02d} {int(strike)} C")
        return None
    oqp, iv = got
    contract = (old or {}).get("contract")
    if not contract:
        nexp = expiry_day(ny, nm, closes)
        contract = f"HK.HSI{nexp:%y%m%d}C{int(strike) * 1000}"
    rec = {
        "ts": exp.isoformat(),
        "underlying": underlying, "strike": strike,
        "option_price": oqp,
        "premium_yield": round(oqp / underlying, 6),
        "expiry": f"{ny:04d}-{nm:02d}",
        "contract": contract,
        "source": SOURCE,
        "iv": float(iv) / 100 if iv.replace(".", "", 1).isdigit() else None,
    }
    market = (old or {}).get("option_price_market")
    if market is not None:
        rec["option_price_market"] = market      # OpenD 当日抓到的价格，存档
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="起始月份 YYYY-MM（默认最近 330 天）")
    ap.add_argument("--upload", action="store_true")
    args = ap.parse_args()

    today = datetime.date.today()
    if args.since:
        y0, m0 = map(int, args.since.split("-"))
    else:
        d0 = today - datetime.timedelta(days=330)
        y0, m0 = d0.year, d0.month

    with open(HSI_JSON, encoding="utf-8") as f:
        payload = json.load(f)
    hist = payload["items"]["HSI"]["history"]
    by_ym = {(h.get("ts") or "")[:7]: h for h in hist}

    closes = hsi_closes(datetime.date(y0, m0, 1) - datetime.timedelta(days=5),
                        today + datetime.timedelta(days=1))

    changed = []
    y, m = y0, m0
    while (y, m) <= (today.year, today.month):
        ym = f"{y:04d}-{m:02d}"
        old = by_ym.get(ym)
        y, m = next_ym(y, m)
        if old and old.get("source") == SOURCE:
            continue                                   # 已是官方口径
        exp = (datetime.date.fromisoformat(old["ts"]) if old and old.get("ts")
               else expiry_day(int(ym[:4]), int(ym[5:]), closes))
        if not exp or exp > today:
            continue
        try:
            rec = official_record(exp, old, closes)
        except Exception as e:                         # 单月失败不拖累其他月份
            print(f"[hsi_official] {ym}（{exp}）取数出错，跳过本次: {e!r}")
            continue
        if not rec:
            print(f"[hsi_official] {ym}（{exp}）官方日报暂不可得，保持原值")
            continue
        before = f"{old['premium_yield']:.4%}" if old else "缺"
        if old:
            hist[hist.index(old)] = rec
        else:
            hist.append(rec)
        changed.append(ym)
        print(f"[hsi_official] {ym} {exp}  收盘{rec['underlying']}  ATM{int(rec['strike'])}  "
              f"结算价{rec['option_price']:.0f}  PY {before} → {rec['premium_yield']:.4%}")

    if not changed:
        print("[hsi_official] 窗口内均已是官方口径，无需更新")
        return

    hist.sort(key=lambda h: h.get("ts") or "")
    payload["generated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(HSI_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"[hsi_official] 已更新 {len(changed)} 期: {', '.join(changed)}（共 {len(hist)} 期）")

    if args.upload:
        rel = os.path.relpath(os.path.normpath(HSI_JSON), ROOT)
        for cmd in [
            ["git", "add", rel],
            ["git", "commit", "-m", f"chore(data): 恒指按港交所结算价更新 {', '.join(changed)}"],
            ["git", "pull", "--rebase", "--autostash", "origin", "main"],
            ["git", "push", "origin", "main"],
        ]:
            print("[hsi_official] ", " ".join(cmd))
            if subprocess.run(cmd, cwd=ROOT).returncode != 0:
                print("[hsi_official] 推送失败，已中止；hsi.json 已写入本地，作业下次会自动重推")
                break


if __name__ == "__main__":
    main()
