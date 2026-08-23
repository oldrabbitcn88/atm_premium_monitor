# -*- coding: utf-8 -*-
"""
本机恒指同步脚本：本地 OpenD 抓恒指点位 + 恒指期权 ATM PY
- 读 deploy/data/hsi.json（含历史）
- 若今日为恒指期权月度交割日（每月最后第二个营业日，以富途到期日列表为准）且当月未更新，更新当月点
- 写回 hsi.json（供前端读取；由 Windows 计划任务每日 15:40 调用，随后推送到 GitHub 触发 Pages 重新发布）

用法：
  python sync_hsi.py                 # 仅更新 hsi.json
  python sync_hsi.py --upload        # 更新后 git commit + push 到 GitHub（触发 Pages 重新发布）
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

from backend.data.futu_adapter import FutuAdapter  # noqa: E402
from backend.engine import premium_yield            # noqa: E402

HSI_JSON = os.path.join(BASE, "..", "data", "hsi.json")
FUTU_HOST = os.environ.get("FUTU_HOST", "127.0.0.1")
FUTU_PORT = int(os.environ.get("FUTU_PORT", "11111"))


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


def main():
    upload = "--upload" in sys.argv
    today = datetime.date.today()
    print(f"[sync_hsi] {today} 连接OpenD({FUTU_HOST}:{FUTU_PORT})...")

    with open(HSI_JSON, encoding="utf-8") as f:
        payload = json.load(f)
    items = payload["items"]
    hist = items["HSI"]["history"]
    ym = today.strftime("%Y-%m")
    if any((h.get("ts") or "")[:7] == ym for h in hist):
        print("[sync_hsi] 本月已记录，跳过")
        return

    if not opend_reachable(FUTU_HOST, FUTU_PORT):
        print(f"[sync_hsi] OpenD 未运行（{FUTU_HOST}:{FUTU_PORT} 连不上），本次跳过。"
              "请先启动富途 OpenD 并登录后重试。")
        sys.exit(1)

    futu = FutuAdapter(host=FUTU_HOST, port=FUTU_PORT)
    try:
        hsi_price = futu.get_price("HK.800000")
        if not is_hsi_expiry_today(futu, today):
            print(f"[sync_hsi] 今日非恒指期权交割日（恒指收盘 {hsi_price}），不写入")
            return
        expiry = futu.get_hsi_monthly_expiry_next(asof=today)
        strike, opt_price = futu.get_hsi_atm_call(hsi_price, expiry)
        if not strike or not opt_price:
            print("[sync_hsi] 恒指期权行情获取失败")
            return
        py = premium_yield(opt_price, hsi_price)
        new = {
            "ts": today.strftime("%Y-%m-%d"),
            "underlying": hsi_price, "strike": strike, "option_price": opt_price,
            "premium_yield": round(py, 6),
            "expiry": f"{expiry.year:04d}-{expiry.month:02d}",
            "contract": f"HK.HSI{expiry.strftime('%y%m%d')}C{int(strike) * 1000}",
        }
        for i, h in enumerate(hist):
            if (h.get("ts") or "")[:7] == ym:
                hist[i] = new
                break
        else:
            hist.append(new)
        payload["generated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(HSI_JSON, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        print(f"[sync_hsi] 已写入: {today} 收盘{hsi_price} ATM{strike} 权利金{opt_price} PY={py:.4%}")
    finally:
        futu.close()

    if upload:
        rel = os.path.relpath(os.path.normpath(HSI_JSON), ROOT)
        for cmd in [
            ["git", "add", rel],
            ["git", "commit", "-m", f"chore(data): 恒指 {today} 交割日数据"],
            ["git", "pull", "--rebase", "--autostash", "origin", "main"],
            ["git", "push", "origin", "main"],
        ]:
            print("[sync_hsi] ", " ".join(cmd))
            r = subprocess.run(cmd, cwd=ROOT)
            if r.returncode != 0:
                print(f"[sync_hsi] 失败(退出码{r.returncode})，已中止推送；hsi.json 已写入本地，可稍后手动 git push")
                break
        else:
            print("[sync_hsi] 已推送到 GitHub，Pages 将自动重新发布")


if __name__ == "__main__":
    main()
