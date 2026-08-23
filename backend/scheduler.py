# -*- coding: utf-8 -*-
"""
月度交割日数据采集：仅在每月交割日（或顺延窗口内首个交易日）记录一次，
其余时间不落库。每月每品种保留 1 个数据点（交割日口径）。

交割日规则：
- HSI    ：每月最后第二个营业日（以富途官方到期日列表为准）
- CSI300 ：每月第三个周五（中金所IO，节假日顺延窗口处理）
- CSI500 ：每月第四个周三（上交所ETF期权，节假日顺延窗口处理）
"""
import calendar
import datetime
import socket

from .config import FUTU_HOST, FUTU_PORT
from .engine import premium_yield
from .data.futu_adapter import FutuAdapter
from .data.cffex_adapter import CffexAdapter
from .data.sse_adapter import SseAdapter
from . import store

# 各标的年化实际波动率（Wind日收益×√252，2022-10~2026-08，用于PY/Vol比率）
VOL_ANNUAL = {"HSI": 0.2469, "CSI300": 0.1772, "CSI500": 0.2293}

# 交割日顺延窗口（规则日若休市，其后 EXPIRY_WINDOW_DAYS 天内首个能抓取成功的交易日补记）
EXPIRY_WINDOW_DAYS = 3


def _port_open(host, port, timeout=1.5):
    """快速探测端口是否可达（富途OpenD未运行时避免长阻塞）"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _io_rule_day(year, month):
    """中金所IO交割日规则日：每月第三个周五"""
    c = calendar.monthcalendar(year, month)
    fridays = [w[4] for w in c if w[4] != 0]
    if len(fridays) >= 3:
        return datetime.date(year, month, fridays[2])
    return None


def _etf_rule_day(year, month):
    """上交所ETF期权交割日规则日：每月第四个周三"""
    c = calendar.monthcalendar(year, month)
    wednesdays = [w[2] for w in c if w[2] != 0]
    if len(wednesdays) >= 4:
        return datetime.date(year, month, wednesdays[3])
    return None


class MonthlyRecorder:
    def __init__(self):
        self.futu = None
        self.cffex = CffexAdapter()
        self.sse = SseAdapter()

    def _ensure_futu(self):
        if self.futu is None:
            self.futu = FutuAdapter(host=FUTU_HOST, port=FUTU_PORT)
        return self.futu

    def is_hsi_expiry_today(self, futu, today):
        """今天是否为恒指期权月度到期日（每月最后第二个营业日，以富途官方到期日列表为准）"""
        try:
            for rd in futu._monthly_expiry_dates():
                rdt = datetime.date.fromisoformat(rd)
                if rdt == today and rdt == futu._month_end_business_day(rdt, n=2):
                    return True
        except Exception as e:
            print(f"[HSI] 到期日判断失败: {e}")
        return False

    def check_and_record(self, today=None):
        """检查今天是否各品种月度交割日；是则抓取并写库（同月覆盖）。返回逐品种报告"""
        today = today or datetime.date.today()
        report = []
        # 富途OpenD未运行时快速返回（避免40s+连接阻塞）
        if not _port_open(FUTU_HOST, FUTU_PORT):
            for k, nm in [("HSI", "HSI"), ("CSI300", "CSI300"), ("CSI500", "CSI500")]:
                report.append({"key": k, "written": False, "reason": "OpenD未连接"})
            return report
        try:
            futu = self._ensure_futu()

            # ---- HSI：以富途到期日列表判断 ----
            if self.is_hsi_expiry_today(futu, today):
                hsi_price = futu.get_price("HK.800000")
                item = self._fetch_hsi(futu, hsi_price, today)
                if item and item.get("option_price"):
                    store.upsert_monthly(item)
                    report.append({"key": "HSI", "written": True, "ts": item["ts"]})
                else:
                    report.append({"key": "HSI", "written": False, "reason": "抓取失败"})
            else:
                report.append({"key": "HSI", "written": False, "reason": "非交割日"})

            # ---- CSI300 / CSI500：规则日 + 顺延窗口 + 本月未记录 ----
            for key, rule_day, futu_code, fetch_fn in [
                ("CSI300", _io_rule_day(today.year, today.month), "SH.000300", self._fetch_io),
                ("CSI500", _etf_rule_day(today.year, today.month), "SH.510500", self._fetch_etf),
            ]:
                ym = today.strftime("%Y-%m")
                if rule_day is None:
                    report.append({"key": key, "written": False, "reason": "无规则日"})
                    continue
                delta = (today - rule_day).days
                if delta < 0 or delta > EXPIRY_WINDOW_DAYS:
                    report.append({"key": key, "written": False, "reason": "非交割日"})
                    continue
                if store.has_month(key, ym):
                    report.append({"key": key, "written": False, "reason": "本月已记录"})
                    continue
                underlying = futu.get_price(futu_code)
                item = fetch_fn(underlying, today)
                if item and item.get("option_price"):
                    store.upsert_monthly(item)
                    report.append({"key": key, "written": True, "ts": item["ts"]})
                else:
                    report.append({"key": key, "written": False, "reason": "抓取失败(窗口内重试)"})
        except Exception as e:
            report.append({"key": "_error", "written": False, "reason": str(e)})
        finally:
            if self.futu:
                self.futu.close()
                self.futu = None
        return report

    # ---------- 各品种抓取 ----------

    def _fetch_hsi(self, futu, underlying, today):
        if underlying is None:
            return None
        expiry = futu.get_hsi_monthly_expiry_next(asof=today)
        if expiry is None:
            return None
        strike, opt_price = futu.get_hsi_atm_call(underlying, expiry)
        if strike is None or opt_price is None:
            return None
        py = premium_yield(opt_price, underlying)
        return {
            "key": "HSI", "name": "恒生指数", "underlying": underlying,
            "strike": strike, "option_price": opt_price, "premium_yield": py,
            "expiry": expiry.strftime("%Y-%m-%d"), "contract": f"HSI ATM {strike}",
            "ts": today.strftime("%Y-%m-%d 15:30:00"),
        }

    def _fetch_io(self, underlying, today):
        if underlying is None:
            return None
        strike, opt_price, code = self.cffex.get_io_atm_call(underlying, today)
        if strike is None or opt_price is None:
            return None
        py = premium_yield(opt_price, underlying)
        return {
            "key": "CSI300", "name": "沪深300指数", "underlying": underlying,
            "strike": strike, "option_price": opt_price, "premium_yield": py,
            "expiry": "", "contract": code or f"IO ATM {strike}",
            "ts": today.strftime("%Y-%m-%d 15:30:00"),
        }

    def _fetch_etf(self, underlying, today):
        if underlying is None:
            return None
        strike, opt_price, cid, name, adj, tcode = self.sse.get_etf_atm_call(underlying, "510500", today)
        if strike is None or opt_price is None:
            return None
        py = premium_yield(opt_price, underlying)
        status = "标准合约" if adj == "" else f"除息调整({adj})合约"
        # 展示交易所交易代码（助记码），如 510500C2609M08000
        contract = tcode or (cid or f"510500 ATM {strike}")
        return {
            "key": "CSI500", "name": "中证500ETF", "underlying": underlying,
            "strike": strike, "option_price": opt_price, "premium_yield": py,
            "expiry": "", "contract": contract, "opt_status": status,
            "ts": today.strftime("%Y-%m-%d 15:30:00"),
        }


_recorder = MonthlyRecorder()


def check_and_record_monthly(today=None):
    """每日收盘后调用：仅交割日写库"""
    return _recorder.check_and_record(today)


def refresh_once():
    """兼容旧接口：立即执行交割日检查（非交割日不落库，仅返回报告）"""
    return _recorder.check_and_record()
