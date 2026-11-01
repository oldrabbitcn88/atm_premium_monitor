# -*- coding: utf-8 -*-
"""
Global X 备兑 ETF（3419 恒指备兑 / 3416 国指备兑）持仓归档 + 备兑覆盖比率统计 CLI

子命令
------
fetch            从官网抓当日持仓 → 存档 → 重算覆盖比率序列
local <file...>  解析手工下载的持仓文件（csv/xlsx/json/html）→ 存档 → 重算
report           只用已有存档重算覆盖比率序列（不联网）
wayback          用 Internet Archive CDX 找历史快照并回填（能回填多少取决于爬虫抓过多少）
monthly-urls     打印官方"备兑月报"PDF 的候选地址（月度覆盖率/行权价/权利金的权威来源）

存档布局（都在 --data-dir 下，默认 deploy/data/gx3419）
    raw/<as_of>.json     当日标准化持仓快照（含原始列，可复算）
    coverage.json        覆盖比率时间序列（数组，按日期升序）
    coverage.csv         同上，便于 Excel/pandas

示例
    python -m backend.tools.gx_coverage fetch --code 3419 --hsi 26000
    python -m backend.tools.gx_coverage local ~/Downloads/3419_holdings_20261031.csv --hsi 26013.5
    python -m backend.tools.gx_coverage report
    python -m backend.tools.gx_coverage wayback --code 3419 --since 20240228
"""
import argparse
import csv
import datetime
import json
import os
import re
import sys

# 允许 `python backend/tools/gx_coverage.py` 直接运行
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from backend.coverage import compute_coverage
    from backend.data import globalx_adapter as gx
else:
    from ..coverage import compute_coverage
    from ..data import globalx_adapter as gx

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DATA_DIR = os.path.join(REPO, "deploy", "data", "gx3419")

CSV_COLUMNS = [
    "as_of", "fund", "coverage_spot", "coverage_strike", "coverage_delta_est",
    "official_coverage_pct_nav", "nav_implied",
    "long_index_equiv", "equity_mv", "index_etf_mv", "futures_notional",
    "short_call_contracts", "short_call_spot_notional", "short_call_strike_notional",
    "short_call_mv", "index_level", "index_level_source",
    "wavg_strike", "wavg_moneyness", "wavg_dte", "n_call_legs", "n_future_legs",
    "cash_mv", "source_url",
]


def raw_dir(data_dir):
    return os.path.join(data_dir, "raw")


def save_snapshot(snapshot, data_dir, overwrite=False):
    """按 as_of 落盘；缺 as_of 时退化用抓取日期（并告警）"""
    as_of = snapshot.get("as_of")
    if not as_of:
        as_of = (snapshot.get("fetched_at") or "")[:10] or datetime.date.today().isoformat()
        snapshot["as_of"] = as_of
        snapshot.setdefault("warnings", []).append(
            "持仓文件未能解析出截至日期，已用抓取日期代替，请人工核对")
    os.makedirs(raw_dir(data_dir), exist_ok=True)
    path = os.path.join(raw_dir(data_dir), "%s.json" % as_of)
    if os.path.exists(path) and not overwrite:
        print("  已存在，跳过：%s" % path)
        return path, False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("  已存档：%s（%d 行）" % (path, snapshot.get("n_rows") or len(snapshot.get("rows") or [])))
    return path, True


def rebuild_report(data_dir, vol_annual=0.20, index_levels=None):
    """遍历存档 → 算覆盖比率 → 写 coverage.json / coverage.csv"""
    index_levels = index_levels or {}
    rd = raw_dir(data_dir)
    if not os.path.isdir(rd):
        print("没有任何存档：%s" % rd)
        return []
    series = []
    for fn in sorted(os.listdir(rd)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(rd, fn), encoding="utf-8") as f:
            snap = json.load(f)
        lvl = index_levels.get(snap.get("as_of")) or snap.get("index_level")
        res = compute_coverage(snap, index_level=lvl, vol_annual=vol_annual)
        series.append(res)
    series.sort(key=lambda r: r.get("as_of") or "")

    os.makedirs(data_dir, exist_ok=True)
    tz8 = datetime.timezone(datetime.timedelta(hours=8))
    out = {
        "generated_at": datetime.datetime.now(tz8).isoformat(timespec="seconds"),
        "note": "备兑覆盖比率 = 卖出认购期权标的名义 / (正股市值+指数ETF市值+指数期货多头名义)",
        "series": series,
    }
    with open(os.path.join(data_dir, "coverage.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(os.path.join(data_dir, "coverage.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in series:
            w.writerow(r)
    print("覆盖比率序列已更新：%d 个交易日 → %s" % (len(series), data_dir))
    _print_table(series[-10:])
    allw = [(r["as_of"], w) for r in series for w in (r.get("warnings") or [])]
    if allw:
        print("\n告警（最近 10 条）：")
        for d, w in allw[-10:]:
            print("  %s  %s" % (d, w))
    return series


def _fmt_pct(x):
    return "%.2f%%" % (x * 100) if isinstance(x, (int, float)) else "n/a"


def _print_table(series):
    if not series:
        return
    print("\n%-12s %10s %10s %10s %10s %12s %8s %7s" % (
        "日期", "官方/NAV", "现货口径", "行权价口径", "delta口径",
        "多头名义(亿)", "空头张数", "剩余天"))
    for r in series:
        le = r.get("long_index_equiv")
        print("%-12s %10s %10s %10s %10s %12s %8s %7s" % (
            r.get("as_of") or "?", _fmt_pct(r.get("official_coverage_pct_nav")),
            _fmt_pct(r.get("coverage_spot")), _fmt_pct(r.get("coverage_strike")),
            _fmt_pct(r.get("coverage_delta_est")),
            ("%.2f" % (le / 1e8)) if le else "n/a",
            r.get("short_call_contracts") if r.get("short_call_contracts") is not None else "n/a",
            r.get("wavg_dte") if r.get("wavg_dte") is not None else "n/a"))


# ---------- Wayback 回填 ----------

CDX_API = "https://web.archive.org/cdx/search/cdx"


def wayback_backfill(code, data_dir, since="20240228", until=None, limit=400,
                     extra_urls=(), overwrite=False):
    """用 Internet Archive 回填历史持仓。

    注意：官网持仓多由前端 JS 异步加载，而爬虫一般不执行 JS，
    所以能回填到的日期通常很稀疏——这是方法的上限，不是脚本的问题。
    """
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": gx.USER_AGENT})
    patterns = list(extra_urls) + [
        "globalxetfs.com.hk/*funds/%s*" % code,
        "globalxetfs.com.hk/*%s*holding*" % code,
        "globalxetfs.com.hk/*holding*%s*" % code,
        "investments.miraeasset.com.hk/docs/ETF/%s*" % code,
    ]
    seen, ingested = set(), 0
    for pat in patterns:
        params = {"url": pat, "output": "json", "collapse": "digest",
                  "filter": "statuscode:200", "from": since, "limit": str(limit)}
        if until:
            params["to"] = until
        print("查询 CDX：%s" % pat)
        try:
            r = s.get(CDX_API, params=params, timeout=60)
            rows = r.json()
        except Exception as e:
            print("  CDX 查询失败：%s" % e)
            continue
        if not rows or len(rows) < 2:
            print("  无快照")
            continue
        head = rows[0]
        for rec in rows[1:]:
            d = dict(zip(head, rec))
            ts, orig = d.get("timestamp"), d.get("original")
            if not ts or not orig:
                continue
            key = (ts, orig)
            if key in seen:
                continue
            seen.add(key)
            # id_ 后缀 = 取原始字节，不加 Wayback 的工具栏
            url = "https://web.archive.org/web/%sid_/%s" % (ts, orig)
            try:
                rr = s.get(url, timeout=60)
                rows2 = gx.parse_payload(rr.content, rr.headers.get("Content-Type", ""), orig)
            except Exception as e:
                print("  %s %s -> 取/解析失败 %s" % (ts, orig[:70], e))
                continue
            if len(rows2) < 5:
                continue
            as_of = gx.guess_as_of(rr.text if "text" in (rr.headers.get("Content-Type") or "")
                                   else "") or "%s-%s-%s" % (ts[0:4], ts[4:6], ts[6:8])
            snap = gx.make_snapshot(code, rows2, source_url=url, as_of=as_of,
                                    tried=["wayback %s" % ts])
            snap["wayback_timestamp"] = ts
            print("  %s %s -> %d 行" % (ts, orig[:70], len(rows2)))
            _p, written = save_snapshot(snap, data_dir, overwrite=overwrite)
            ingested += 1 if written else 0
    print("\nWayback 回填完成：新增 %d 个日期" % ingested)
    return ingested


# ---------- 按日期枚举回填历史持仓 ----------

def backfill_by_url(code, data_dir, url_template=None, from_url=None,
                    since="20240229", until=None, sleep_sec=0.4, overwrite=False,
                    skip_weekends=True):
    """持仓文件名自带日期（3419_full_holdings_20261029.csv），
    所以历史往往可以直接按日期枚举下载——这是拿到真·逐日序列最省事的路。

    :param from_url: 任意一个已知的持仓下载地址（chrome://downloads 里复制），
                     自动推导出日期模板
    :param url_template: 直接给模板，{date}=YYYYMMDD、{date_dash}=YYYY-MM-DD、{code}=代码
    """
    if not url_template:
        if from_url:
            url_template, d = gx.derive_url_template(from_url)
            if not url_template:
                raise SystemExit("给的地址里没找到 YYYYMMDD 日期，无法推导模板：%s" % from_url)
            print("由 %s 推导出模板：\n  %s" % (d, url_template))
        else:
            print("未给地址，尝试从基金页自动发现持仓文件地址……")
            ad = gx.GlobalXAdapter()
            _page, _html, cands = ad.discover_holdings_urls(code)
            for u in cands:
                tpl, d = gx.derive_url_template(u)
                if tpl:
                    url_template, from_url = tpl, u
                    print("发现：%s\n模板：%s" % (u, tpl))
                    break
            if not url_template:
                raise SystemExit(
                    "没能自动发现带日期的持仓地址。\n"
                    "请在浏览器点一次「每日持股 (.CSV)」，到 chrome://downloads 复制该\n"
                    "文件的来源地址，然后：--from-url '<那个地址>'")

    ad = gx.GlobalXAdapter()
    start = datetime.datetime.strptime(since, "%Y%m%d").date()
    end = (datetime.datetime.strptime(until, "%Y%m%d").date() if until
           else datetime.date.today())
    existing = set()
    if os.path.isdir(raw_dir(data_dir)):
        existing = {fn[:-5] for fn in os.listdir(raw_dir(data_dir)) if fn.endswith(".json")}

    import time
    day = start
    hit = miss = skipped = 0
    while day <= end:
        if skip_weekends and day.weekday() >= 5:
            day += datetime.timedelta(days=1)
            continue
        iso = day.isoformat()
        if iso in existing and not overwrite:
            skipped += 1
            day += datetime.timedelta(days=1)
            continue
        url = gx.expand_template(url_template, day, code)
        try:
            snap = gx.fetch_one_url(ad, url, code=code)
        except Exception:
            miss += 1                     # 404/节假日都走这里，正常
            snap = None
        if snap:
            snap["as_of"] = snap.get("as_of") or iso
            save_snapshot(snap, data_dir, overwrite=overwrite)
            hit += 1
            print("  %s  OK（%d 行）" % (iso, snap.get("n_rows") or 0))
        time.sleep(sleep_sec)
        day += datetime.timedelta(days=1)

    total = hit + miss
    print("\n回填完成：命中 %d / 尝试 %d（%.1f%%），已存在跳过 %d"
          % (hit, total, (hit / total * 100) if total else 0.0, skipped))
    if hit == 0 and total:
        print("一个都没命中 → 服务器很可能只保留当日文件（历史被删或改名）。\n"
              "改走：官方备兑月报（monthly-urls）+ 中期/年度报告 + wayback。")
    return hit


# ---------- 官方月报（月度覆盖率的权威来源） ----------

MONTHLY_BASE = "https://investments.miraeasset.com.hk/docs/ETF"
# 已见过的真实命名（大小写/后缀都变过，所以要穷举候选）：
#   24-08-Covered-call-monthly_EN_Final.pdf
#   26-01-Covered-Call-monthly_ENG.pdf
#   26-03-Covered-Call-monthly_ENG_Final.pdf
MONTHLY_NAME_PATTERNS = [
    "{yy}-{mm}-Covered-Call-monthly_ENG.pdf",
    "{yy}-{mm}-Covered-Call-monthly_ENG_Final.pdf",
    "{yy}-{mm}-Covered-call-monthly_EN_Final.pdf",
    "{yy}-{mm}-Covered-call-monthly_EN.pdf",
    "{yy}-{mm}-Covered-Call-monthly_TC.pdf",
    "{yy}-{mm}-Covered-Call-monthly_CHI.pdf",
]


def monthly_urls(start="2024-03", end=None, check=False):
    """生成（可选：联网验证）官方备兑月报候选地址"""
    if end is None:
        end = datetime.date.today().strftime("%Y-%m")
    y, m = [int(x) for x in start.split("-")]
    ey, em = [int(x) for x in end.split("-")]
    out = []
    sess = None
    if check:
        import requests
        sess = requests.Session()
        sess.headers.update({"User-Agent": gx.USER_AGENT})
    while (y, m) <= (ey, em):
        for pat in MONTHLY_NAME_PATTERNS:
            url = "%s/%s" % (MONTHLY_BASE, pat.format(yy="%02d" % (y % 100), mm="%02d" % m))
            ok = None
            if sess:
                try:
                    ok = sess.head(url, timeout=20, allow_redirects=True).status_code == 200
                    if not ok:   # 有些站点 HEAD 不支持，再试 GET
                        ok = sess.get(url, timeout=30, stream=True).status_code == 200
                except Exception:
                    ok = False
            out.append((url, ok))
            if ok:
                break
        m += 1
        if m > 12:
            m, y = 1, y + 1
    for url, ok in out:
        flag = "" if ok is None else ("  [OK]" if ok else "  [404]")
        if ok is None or ok:
            print(url + flag)
    return out


# ---------- 指数点位（现货名义口径的分子需要） ----------

def fetch_index_close(symbol="hkHSI"):
    """取指数最新/收盘点位。腾讯行情（免费、无需凭证、大陆可直连）：
        https://qt.gtimg.cn/q=hkHSI   → v_hkHSI="...~恒生指数~HSI~26013.5~..."
    取不到就返回 None——此时覆盖比率会退化用期货价近似，并在 warnings 里标注。
    """
    try:
        import requests
        r = requests.get("https://qt.gtimg.cn/q=%s" % symbol, timeout=10,
                         headers={"User-Agent": gx.USER_AGENT})
        r.encoding = "gbk"
        m = re.search(r'="([^"]+)"', r.text)
        if not m:
            return None
        fields = m.group(1).split("~")
        for i in (3, 4, 5):          # 不同品种字段位略有差异，取第一个像点位的
            if i < len(fields):
                try:
                    v = float(fields[i])
                except ValueError:
                    continue
                if 3000 < v < 80000:
                    return v
    except Exception as e:
        print("取指数点位失败（将退化用期货价近似）：%s" % e)
    return None


# ---------- 入口 ----------

def _index_levels_arg(arg):
    """--index-levels 支持 'YYYY-MM-DD=26000,YYYY-MM-DD=25800' 或 json 文件路径"""
    if not arg:
        return {}
    if os.path.exists(arg):
        with open(arg, encoding="utf-8") as f:
            return {k: float(v) for k, v in json.load(f).items()}
    out = {}
    for part in arg.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = float(v)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="Global X 备兑 ETF 持仓归档与覆盖比率统计")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--code", default="3419")
    p.add_argument("--vol", type=float, default=0.20, help="delta 口径假设的年化波动率")
    p.add_argument("--hsi", default=None,
                   help="当日指数点位；填 auto 则自动取腾讯行情 hkHSI（现货名义口径用）")
    p.add_argument("--index-levels", default=None,
                   help="历史指数点位：'2026-10-31=26000,...' 或 json 文件路径")
    p.add_argument("--overwrite", action="store_true", help="同一 as_of 已存档时覆盖")
    sub = p.add_subparsers(dest="cmd", required=True)

    sf = sub.add_parser("fetch", help="联网抓当日持仓")
    sf.add_argument("--url", action="append", default=[],
                    help="显式指定持仓地址（F12 → Network → Fetch/XHR 里抓到的那个），可重复")
    sp = sub.add_parser("local", help="解析本地持仓文件")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--as-of", default=None, help="覆盖文件里/文件名推断的日期")
    sub.add_parser("report", help="仅重算覆盖比率序列")
    sw = sub.add_parser("wayback", help="Internet Archive 回填")
    sw.add_argument("--since", default="20240228")
    sw.add_argument("--until", default=None)
    sw.add_argument("--url", action="append", default=[], help="额外的 CDX url 模式")
    sb = sub.add_parser("backfill-csv", help="按日期枚举回填历史持仓文件")
    sb.add_argument("--from-url", default=None,
                    help="任意一个已知持仓下载地址（chrome://downloads 复制），自动推模板")
    sb.add_argument("--url-template", default=None,
                    help="日期模板，如 'https://.../{code}_full_holdings_{date}.csv'")
    sb.add_argument("--since", default="20240229")
    sb.add_argument("--until", default=None)
    sb.add_argument("--sleep", type=float, default=0.4, help="每次请求间隔秒数")
    sm = sub.add_parser("monthly-urls", help="官方备兑月报候选地址")
    sm.add_argument("--start", default="2024-03")
    sm.add_argument("--end", default=None)
    sm.add_argument("--check", action="store_true", help="联网验证哪些真实存在")

    a = p.parse_args(argv)
    levels = _index_levels_arg(a.index_levels)
    if a.hsi == "auto":
        a.hsi = fetch_index_close("hkHSI")
        print("自动取到指数点位：%s" % a.hsi)
    elif a.hsi is not None:
        a.hsi = float(a.hsi)

    if a.cmd == "fetch":
        ad = gx.GlobalXAdapter()
        snap = ad.fetch_holdings(a.code, urls=a.url)
        if a.hsi:
            snap["index_level"] = a.hsi
        print("抓取成功：%s as_of=%s 来源=%s" % (snap["fund"], snap["as_of"], snap["source_url"]))
        save_snapshot(snap, a.data_dir, overwrite=a.overwrite)
        rebuild_report(a.data_dir, a.vol, levels)
    elif a.cmd == "local":
        for path in a.files:
            snap = gx.load_local_file(path, code=a.code, as_of=a.as_of, index_level=a.hsi)
            print("解析 %s：as_of=%s，%d 行" % (path, snap["as_of"], snap["n_rows"]))
            save_snapshot(snap, a.data_dir, overwrite=a.overwrite)
        rebuild_report(a.data_dir, a.vol, levels)
    elif a.cmd == "report":
        rebuild_report(a.data_dir, a.vol, levels)
    elif a.cmd == "wayback":
        wayback_backfill(a.code, a.data_dir, since=a.since, until=a.until,
                         extra_urls=a.url, overwrite=a.overwrite)
        rebuild_report(a.data_dir, a.vol, levels)
    elif a.cmd == "backfill-csv":
        backfill_by_url(a.code, a.data_dir, url_template=a.url_template,
                        from_url=a.from_url, since=a.since, until=a.until,
                        sleep_sec=a.sleep, overwrite=a.overwrite)
        rebuild_report(a.data_dir, a.vol, levels)
    elif a.cmd == "monthly-urls":
        monthly_urls(a.start, a.end, a.check)
    return 0


if __name__ == "__main__":
    sys.exit(main())
