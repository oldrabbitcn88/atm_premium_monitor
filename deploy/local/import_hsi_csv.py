# -*- coding: utf-8 -*-
"""从港交所月度费率 CSV 补齐 hsi.json 中缺失的月份（兜底路径）

用途：交割日当天机器没开、OpenD 没抓到时，等港交所更新费率表后用它补上。
注意：CSV 只有 PremiumYield 一列，**没有**收盘点位/行权价/权利金明细，
补进来的月份在前端明细表里这几列会是空的——这是数据源本身的限制，不是 bug。
完整明细只有交割日当天用 OpenD 实时抓才拿得到。

CSV 格式（首行标题行会被跳过）：
    HSI Covered Call,,
    Year,Month,PremiumYield
    2005,1,1.00%

用法：
  python import_hsi_csv.py                    # 用默认路径，仅补缺失月份
  python import_hsi_csv.py --csv 别的路径.csv    # 指定 CSV
  python import_hsi_csv.py --upload           # 补完 git commit + push
"""
import argparse
import csv
import datetime
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(BASE, "..", ".."))
HSI_JSON = os.path.join(BASE, "..", "data", "hsi.json")

DEFAULT_CSV = os.path.join(
    "D:", os.sep, "微云同步助手", "philip's folder", "工作", "Hong Kong", "项目",
    "Mirae未来资产-Covered Call ETF", "我的推介材料", "第一创业证券资管部",
    "附件1：恒生指数平值看涨期权费率统计表.csv",
)


def month_end_business_day(year, month, n=2):
    """某月最后第 n 个营业日（跳过周末；不含港股假期，与采集器口径一致）"""
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    d = datetime.date(ny, nm, 1) - datetime.timedelta(days=1)
    count = 0
    while count < n:
        if d.weekday() < 5:
            count += 1
        if count < n:
            d -= datetime.timedelta(days=1)
    return d


def parse_rows(path):
    """返回 [(year, month, premium_yield_float)]"""
    with open(path, "rb") as f:
        text = f.read().decode("utf-8-sig")
    out = []
    for row in csv.reader(text.splitlines()):
        if len(row) < 3:
            continue
        y, m, py = (c.strip() for c in row[:3])
        if not y.isdigit() or not m.isdigit() or not py.endswith("%"):
            continue          # 标题行、表头、空行
        try:
            out.append((int(y), int(m), float(py[:-1]) / 100.0))
        except ValueError:
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--upload", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        print(f"[import_csv] 找不到 CSV: {args.csv}")
        sys.exit(1)

    rows = parse_rows(args.csv)
    if not rows:
        print("[import_csv] CSV 里没解析出任何数据行，检查格式")
        sys.exit(1)
    print(f"[import_csv] CSV 解析到 {len(rows)} 行：{rows[0][0]}-{rows[0][1]:02d} ~ "
          f"{rows[-1][0]}-{rows[-1][1]:02d}")

    with open(HSI_JSON, encoding="utf-8") as f:
        payload = json.load(f)
    hist = payload["items"]["HSI"]["history"]
    have = {(h.get("ts") or "")[:7] for h in hist}

    added = []
    for year, month, py in rows:
        ym = f"{year:04d}-{month:02d}"
        if ym in have:
            continue          # 已有则不覆盖，保护实时采集到的明细
        exp = month_end_business_day(year, month)
        ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
        hist.append({
            "ts": exp.strftime("%Y-%m-%d"),
            "underlying": None, "strike": None, "option_price": None,
            "premium_yield": round(py, 6),
            "expiry": f"{ny:04d}-{nm:02d}",
            "contract": None,
        })
        added.append(ym)

    if not added:
        print(f"[import_csv] 无缺口，hsi.json 已有 {len(hist)} 期，无需补")
        return

    hist.sort(key=lambda h: h.get("ts") or "")
    payload["generated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(HSI_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"[import_csv] 已补 {len(added)} 期: {', '.join(added)}（共 {len(hist)} 期）")
    print("[import_csv] 注意：补进来的月份没有点位/行权价/权利金明细，前端明细表这几列会显示空")

    if args.upload:
        rel = os.path.relpath(os.path.normpath(HSI_JSON), ROOT)
        for cmd in [
            ["git", "add", rel],
            ["git", "commit", "-m", f"chore(data): 从港交所费率表补恒指 {', '.join(added)}"],
            ["git", "pull", "--rebase", "--autostash", "origin", "main"],
            ["git", "push", "origin", "main"],
        ]:
            print("[import_csv] ", " ".join(cmd))
            r = subprocess.run(cmd, cwd=ROOT)
            if r.returncode != 0:
                print(f"[import_csv] 失败(退出码{r.returncode})，已中止推送；"
                      "hsi.json 已写入本地，可稍后手动 git push")
                break
        else:
            print("[import_csv] 已推送，Actions 会把 hsi.json 传上 COS")


if __name__ == "__main__":
    main()
