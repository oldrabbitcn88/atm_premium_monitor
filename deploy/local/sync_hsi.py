# -*- coding: utf-8 -*-
"""
本机恒指同步脚本：本地 OpenD 抓恒指点位 + 恒指期权 ATM PY
- 读 deploy/data/hsi.json（含历史）
- 若今日为恒指期权月度交割日（每月最后第二个营业日，以富途到期日列表为准）且当月未更新，更新当月点
- 写回 hsi.json（供前端读取；由 Windows 计划任务每日 15:40 调用，随后上传 COS）

用法：
  python sync_hsi.py                 # 仅更新 hsi.json
  python sync_hsi.py --upload        # 更新后调用 coscmd 上传（需已配置 coscmd）
"""
import datetime
import json
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
        for cmd in [
            ["coscmd", "upload", os.path.normpath(HSI_JSON), "/hsi.json"],
        ]:
            print("[sync_hsi] 上传COS:", " ".join(cmd))
            subprocess.run(cmd, check=False)


if __name__ == "__main__":
    main()
