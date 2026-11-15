# -*- coding: utf-8 -*-
"""
本机恒指同步脚本：本地 OpenD 抓恒指点位 + 恒指期权 ATM PY
- 读 deploy/data/hsi.json（含历史）
- 若今日为恒指期权月度交割日（每月最后第二个营业日，以富途到期日列表为准）且当月未更新，更新当月点
- 写回 hsi.json（供前端读取；由 Windows 计划任务每日 15:40 调用，随后推送到 GitHub 触发 Pages 重新发布）

用法：
  python sync_hsi.py                          # 仅处理当月（原行为）
  python sync_hsi.py --upload                 # 更新后 git commit + push 到 GitHub
  python sync_hsi.py --month 2026-09 --month 2026-10 --upload   # 补采指定的历史月份
  python sync_hsi.py --missing --upload       # 自动补采最近 4 个月里所有缺的月份（含中间空洞）

补采时效：补采 M 月要用 M+1 月到期的期权合约在 M 月交割日的日K。
富途只保留已退市合约约 1 个月的历史，即最晚到 M+1 月交割日后约 30 天；
超过就只能用 import_hsi_csv.py 从港交所费率表补 PY（无明细）。
"""
import datetime
import json
import socket
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(BASE, "..", ".."))
sys.path.insert(0, ROOT)

from backend.engine import premium_yield, atm_strike  # noqa: E402

# 富途 SDK 在 main() 里才导入：补采规划（plan_months）与单元测试不需要它
FUTU_HISTORY_DAYS = 30   # 富途保留已退市期权合约历史的大致天数

HSI_JSON = os.path.join(BASE, "..", "data", "hsi.json")
FUTU_HOST = os.environ.get("FUTU_HOST", "127.0.0.1")
FUTU_PORT = int(os.environ.get("FUTU_PORT", "11111"))


def monthly_expiry_of(year, month):
    """恒指月度交割日：该月最后第二个营业日（只跳周末，与 FutuAdapter.monthly_expiry_of 一致）"""
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    d = datetime.date(ny, nm, 1) - datetime.timedelta(days=1)
    count = 0
    while True:
        if d.weekday() < 5:
            count += 1
            if count == 2:
                return d
        d -= datetime.timedelta(days=1)


def _next_ym(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


MISSING_LOOKBACK_MONTHS = 4   # --missing 回看的月数（更早的早已超出富途历史窗口）


def plan_months(hist, today, months=None, missing=False, force=False,
                lookback=MISSING_LOOKBACK_MONTHS):
    """决定本次要采哪些月份。
    :param months: 显式指定 ["2026-09", ...]；None 表示不指定
    :param missing: True 时扫最近 lookback 个月（含当月）里所有没记录的月份——
        包括夹在中间的空洞（如有 08、10 而缺 09），不只是最后一个点之后的
    :return: [(ym, exp_day, action, note)]，action ∈ realtime / backfill / skip
    """
    recorded = {(h.get("ts") or "")[:7] for h in hist}
    if months:
        targets = sorted(set(months))
    elif missing:
        y, m = today.year, today.month
        targets = []
        for _ in range(lookback):
            targets.append(f"{y:04d}-{m:02d}")
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        targets.sort()
    else:
        targets = [today.strftime("%Y-%m")]

    plan = []
    for ym in targets:
        y, m = map(int, ym.split("-"))
        exp_day = monthly_expiry_of(y, m)
        if ym in recorded and not force:
            plan.append((ym, exp_day, "skip", "已记录"))
        elif today < exp_day:
            plan.append((ym, exp_day, "skip", f"交割日 {exp_day} 未到"))
        elif today == exp_day:
            plan.append((ym, exp_day, "realtime", "今日为交割日"))
        else:
            # 补采 M 月要用 M+1 月到期的合约；该合约到期后富途只留约 30 天历史
            ny, nm = _next_ym(y, m)
            contract_exp = monthly_expiry_of(ny, nm)
            late = (today - contract_exp).days
            note = "补采交割日日K"
            if late > FUTU_HISTORY_DAYS:
                hint = (f"（所需合约已于 {contract_exp} 到期 {late} 天，"
                        "很可能超出富途历史窗口，请改用 import_hsi_csv.py 补费率）")
                if missing and not months:
                    # 自动补缺模式下不再徒劳重试，否则每天的计划任务都会报失败
                    plan.append((ym, exp_day, "skip", "超出富途历史窗口" + hint))
                    continue
                note += hint
            elif late > 0:
                note += f"（所需合约已于 {contract_exp} 到期，约剩 {FUTU_HISTORY_DAYS - late} 天窗口，尽快）"
            plan.append((ym, exp_day, "backfill", note))
    return plan


def opend_reachable(host, port, timeout=3):
    """OpenD 未启动时富途 SDK 会无限重连，先探一下端口，探不通就快速失败"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_hsi_expiry_today(futu, today):
    for rd in futu._monthly_expiry_dates():
        rdt = datetime.date.fromisoformat(rd)
        if rdt == today and rdt == futu._month_end_business_day(rdt, n=2):
            return True
    return False


def collect_realtime(futu, day):
    """交割日当天：用实时快照"""
    hsi_price = futu.get_price("HK.800000")
    if not hsi_price:
        return None
    expiry = futu.get_hsi_monthly_expiry_next(asof=day)
    strike, opt_price = futu.get_hsi_atm_call(hsi_price, expiry)
    if not strike or not opt_price:
        return None
    return day, hsi_price, strike, opt_price, expiry


def collect_backfill(futu, exp_day):
    """交割日已过：用交割日当天的日K补采

    注意：富途只保留退市期权合约近 1 个月的历史，超过窗口就补不回来了。
    """
    ts, hsi_price = futu.get_close_on("HK.800000", exp_day)
    if not hsi_price:
        return None
    actual_day = datetime.date.fromisoformat(ts)
    strike, _ = atm_strike(hsi_price, "HSI")

    ny, nm = _next_ym(exp_day.year, exp_day.month)
    expiry = monthly_expiry_of(ny, nm)
    contract = f"HK.HSI{expiry.strftime('%y%m%d')}C{int(strike) * 1000}"

    _, opt_price = futu.get_close_on(contract, actual_day)
    if not opt_price:
        print(f"[sync_hsi] 期权 {contract} 在 {actual_day} 无历史K线；"
              "富途仅保留退市合约近1个月历史，超窗口需手动补")
        return None
    return actual_day, hsi_price, strike, opt_price, expiry


def build_record(day, hsi_price, strike, opt_price, expiry):
    py = premium_yield(opt_price, hsi_price)
    # 口径：主序列一律以港交所费率表为准（259 期历史都是这个口径）。
    # OpenD 的 last 价系统性高出港交所约 20%（港交所用的应是卖方可成交价），
    # 直接拿它写 premium_yield 会让序列出现结构性断层、历史均值对比失真。
    # 故此处只记为临时值，等港交所发布当月费率后由 import_hsi_csv.py 覆盖。
    return {
        "ts": day.strftime("%Y-%m-%d"),
        "underlying": hsi_price, "strike": strike,
        "option_price": opt_price,
        "option_price_market": opt_price,
        "premium_yield": round(py, 6),
        "premium_yield_provisional": True,
        "expiry": f"{expiry.year:04d}-{expiry.month:02d}",
        "contract": f"HK.HSI{expiry.strftime('%y%m%d')}C{int(strike) * 1000}",
    }


def upsert(hist, rec):
    ym = rec["ts"][:7]
    for i, h in enumerate(hist):
        if (h.get("ts") or "")[:7] == ym:
            hist[i] = rec
            break
    else:
        hist.append(rec)
    hist.sort(key=lambda h: h.get("ts") or "")


def parse_args(argv):
    import argparse
    p = argparse.ArgumentParser(description="恒指月度交割日 ATM PY 同步（本机 OpenD）")
    p.add_argument("--upload", action="store_true", help="写入后 git commit + push")
    p.add_argument("--month", action="append", default=[],
                   help="补采指定月份 YYYY-MM，可重复")
    p.add_argument("--missing", action="store_true",
                   help="补采最近 4 个月里所有缺的月份（含中间空洞）")
    p.add_argument("--force", action="store_true", help="已记录的月份也重新采集覆盖")
    a = p.parse_args(argv)
    for ym in a.month:
        try:
            datetime.datetime.strptime(ym, "%Y-%m")
        except ValueError:
            p.error(f"--month 格式应为 YYYY-MM：{ym}")
    return a


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    today = datetime.date.today()
    print(f"[sync_hsi] {today} 连接OpenD({FUTU_HOST}:{FUTU_PORT})...")

    with open(HSI_JSON, encoding="utf-8") as f:
        payload = json.load(f)
    hist = payload["items"]["HSI"]["history"]

    plan = plan_months(hist, today, months=args.month or None,
                       missing=args.missing, force=args.force)
    for ym, exp_day, action, note in plan:
        print(f"[sync_hsi] {ym} 交割日 {exp_day}: {action} —— {note}")
    todo = [x for x in plan if x[2] != "skip"]
    if not todo:
        print("[sync_hsi] 无需采集，退出")
        return

    if not opend_reachable(FUTU_HOST, FUTU_PORT):
        print(f"[sync_hsi] OpenD 未运行（{FUTU_HOST}:{FUTU_PORT} 连不上），本次跳过。"
              "请先启动富途 OpenD 并登录后重试。")
        sys.exit(1)

    from backend.data.futu_adapter import FutuAdapter
    futu = FutuAdapter(host=FUTU_HOST, port=FUTU_PORT)
    written, failed = [], []
    try:
        for ym, exp_day, action, _note in todo:
            if action == "realtime" and is_hsi_expiry_today(futu, today):
                print(f"[sync_hsi] {ym}: 今日为交割日，取实时行情")
                got = collect_realtime(futu, today)
            else:
                print(f"[sync_hsi] {ym}: 补采 {exp_day} 历史行情")
                got = collect_backfill(futu, exp_day)
            if not got:
                print(f"[sync_hsi] {ym}: 恒指期权行情获取失败，未写入")
                failed.append(ym)
                continue
            day, hsi_price, strike, opt_price, expiry = got
            rec = build_record(day, hsi_price, strike, opt_price, expiry)
            upsert(hist, rec)
            written.append(ym)
            print(f"[sync_hsi] {ym}: 已写入(临时口径) {day} 收盘{hsi_price} ATM{strike} "
                  f"权利金{opt_price} PY={rec['premium_yield']:.4%} —— 待港交所费率发布后覆盖")
    finally:
        futu.close()

    if failed:
        print(f"[sync_hsi] 失败月份：{', '.join(failed)}。若已超出富途历史窗口，"
              "请用 import_hsi_csv.py 从港交所费率表补 PY。")
    if not written:
        if failed:
            sys.exit(2)       # 让计划任务/日志能看出这次是失败而不是"无事可做"
        return

    payload["generated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(HSI_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    if args.upload:
        rel = os.path.relpath(os.path.normpath(HSI_JSON), ROOT)
        label = "、".join(written)
        for cmd in [
            ["git", "add", rel],
            ["git", "commit", "-m", f"chore(data): 恒指 {label} 交割日数据"],
            ["git", "pull", "--rebase", "--autostash", "origin", "main"],
            ["git", "push", "origin", "main"],
        ]:
            print("[sync_hsi] ", " ".join(cmd))
            r = subprocess.run(cmd, cwd=ROOT)
            if r.returncode != 0:
                print(f"[sync_hsi] 失败(退出码{r.returncode})，已中止推送；"
                      "hsi.json 已写入本地，可稍后手动 git push")
                break
        else:
            print("[sync_hsi] 已推送到 GitHub，Actions 将把 hsi.json 传上 COS")


if __name__ == "__main__":
    main()
