# -*- coding: utf-8 -*-
"""
ATM Premium Yield 月度监测站 — FastAPI 入口
启动：uvicorn main:app --host 0.0.0.0 --port 8000

数据口径：每月交割日（HSI=最后第二营业日 / IO=第三个周五 / ETF=第四个周三）记录1个数据点，
不做盘中/每日高频快照。后台线程每日 15:35（收盘后）检查当日是否交割日，是则落库。
"""
import os
import threading
import time
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import FRONTEND_DIR
from . import store
from .scheduler import check_and_record_monthly, VOL_ANNUAL

app = FastAPI(title="ATM Premium Yield Monthly Monitor")

# 静态前端
_frontend_abs = os.path.abspath(FRONTEND_DIR)
if os.path.isdir(_frontend_abs):
    app.mount("/static", StaticFiles(directory=_frontend_abs), name="static")


@app.on_event("startup")
def startup():
    """后台线程：每日 15:35 检查各品种月度交割日并落库（非交割日不写入）"""

    def worker():
        while True:
            try:
                now = datetime.now()
                target = now.replace(hour=15, minute=35, second=0, microsecond=0)
                if now >= target:
                    target += timedelta(days=1)
                wait = (target - now).total_seconds()
                if wait > 0:
                    time.sleep(wait)
                rep = check_and_record_monthly()
                for r in rep:
                    st = "已写入" if r.get("written") else f"跳过({r.get('reason','')})"
                    print(f"[daily] {r.get('key')}: {st}" + (f" @{r['ts']}" if r.get("written") else ""))
            except Exception as e:
                print(f"[daily] 检查失败: {e}")
                time.sleep(3600)

    t = threading.Thread(target=worker, daemon=True)
    t.start()


@app.get("/")
def index():
    fp = os.path.join(_frontend_abs, "index.html")
    if os.path.exists(fp):
        return FileResponse(fp)
    return JSONResponse({"error": "frontend not found"})


@app.get("/api/current")
def api_current():
    """最新月度数据（每品种最近一个交割日数据点，含历史均值/PY-Vol比率参考）"""
    items = store.latest_snapshot()
    out = []
    for it in items:
        k = it["key"]
        stats = store.history_stats(k)
        py = it.get("premium_yield")
        vol = VOL_ANNUAL.get(k)
        py_vol = (py / vol) if (py and vol) else None
        out.append({**it, "stats": stats, "vol_annual": vol, "py_vol_ratio": py_vol})
    return {"items": out, "server_time": _now(), "mode": "monthly"}


@app.get("/api/history")
def api_history(key: str = "HSI", limit: int = 1200):
    """月度历史序列（每 key 每月1个点，交割日口径）"""
    return {"key": key, "mode": "monthly", "items": store.query_history(key, limit)}


@app.get("/api/refresh")
def api_refresh():
    """手动触发：执行本月交割日检查（今日非交割日则不写库，仅返回报告与当前预览）"""
    try:
        report = check_and_record_monthly()
        items = store.latest_snapshot()
        return {"ok": True, "mode": "monthly", "report": report, "items": items}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
