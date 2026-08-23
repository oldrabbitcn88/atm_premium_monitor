# -*- coding: utf-8 -*-
"""
上交所适配器：ETF期权（510300/510500）实时行情
数据源：上交所行情推送服务 yunhq.sse.com.cn:32041（T型报价，官方免费）
接口：http://yunhq.sse.com.cn:32041/v1/sho/list/tstyle/{etf}_{yyMM}
返回：{"date":20260821,"time":162902,"total":52,
      "list":[["10011887","500ETF购8月7859A",0.1080], ...]}
list 每项 = [合约ID, 合约简称, 最新价]
简称格式："300ETF购9月4600"（标准） / "500ETF购8月7859A"（A=除息调整）
行权价 = 简称中数字 ÷ 1000（如 4600 → 4.6 元）
"""
import datetime
import re
import time

import requests

from ..engine import atm_strike


class SseAdapter:
    def __init__(self, timeout=8, retries=3):
        self.timeout = timeout
        self.retries = retries
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "http://www.sse.com.cn/assortment/options/price/",
        }

    def get_month_quotes(self, etf_code, year_month):
        """获取某到期月全部期权行情（带重试）
        :param etf_code: '510300' / '510500'
        :param year_month: (year, month)
        :return: [{contract_id, name, price, type, strike, adj, trade_code}]
        """
        y, m = year_month
        ym = f"{m:02d}"  # yunhq 接口用"月"两位（如 09=9月），跨年时取当年合约
        url = f"http://yunhq.sse.com.cn:32041/v1/sho/list/tstyle/{etf_code}_{ym}"
        for attempt in range(self.retries):
            try:
                r = requests.get(url, headers=self.headers, timeout=self.timeout)
                data = r.json()
                if data.get("total", 0) > 0 or attempt == self.retries - 1:
                    result = []
                    for item in data.get("list", []):
                        cid, name, last = item[0], item[1], item[2]
                        parsed = self._parse_name(name)
                        if parsed:
                            parsed["contract_id"] = cid
                            parsed["name"] = name
                            parsed["price"] = float(last)
                            parsed["trade_code"] = self._build_trade_code(etf_code, parsed, y, m)
                            result.append(parsed)
                    return result
            except Exception as e:
                print(f"[SSE] {etf_code}_{ym} 请求失败(第{attempt+1}次): {e}")
            time.sleep(1.5)
        return []

    @staticmethod
    def _build_trade_code(etf_code, parsed, y, m):
        """生成交易所交易代码（助记码）：510500C2608M07750
        规则：标的代码 + C/P + 到期年月(YYMM) + 调整标识(M标准/A/B除息调整) + 行权价×1000 补5位
        """
        cp = "C" if parsed["type"] == "购" else "P"
        adj = parsed["adj"] if parsed["adj"] else "M"
        strike_int = int(round(parsed["strike"] * 1000))
        return f"{etf_code}{cp}{y % 100:02d}{m:02d}{adj}{strike_int:05d}"

    @staticmethod
    def _parse_name(name):
        """解析合约简称：'300ETF购9月4600' / '500ETF沽8月7859A'"""
        m = re.match(r"(.+?)(购|沽)(\d+)月(\d+)([AB]?)$", name)
        if not m:
            return None
        etf, opt_type, month, strike_raw, adj = m.groups()
        return {
            "etf": etf,
            "type": opt_type,           # 购/沽
            "month": int(month),        # 到期月（1-12）
            "strike": int(strike_raw) / 1000.0,  # 行权价（元）
            "adj": adj,                 # ''=标准, A/B=除息调整
        }

    @staticmethod
    def next_expiry(asof, month_rule="etf"):
        """下一个月度到期日（距今天>=10天的最近一个），返回 (到期日, 月份)
        ETF期权：每月第四个周三"""
        def nth_weekday(y, m, weekday, n):
            import calendar
            c = calendar.monthcalendar(y, m)
            # 第 n 个 weekday 的日期
            days = [d for week in c for d in [week[weekday]] if d != 0]
            if len(days) >= n:
                return datetime.date(y, m, days[n - 1])
            return None

        for offset in range(0, 6):
            m = asof.month + offset
            y = asof.year + (m - 1) // 12
            m = (m - 1) % 12 + 1
            d = nth_weekday(y, m, 2, 4)  # 周三=2, 第四个
            if d and d > asof and (d - asof).days >= 10:
                return d, (y, m)
        return None, None

    def get_etf_atm_call(self, etf_price, etf_code, asof):
        """获取下月 ETF 期权 ATM call 最新价
        优先：标准合约（adj==''）行权价==ATM strike
        兼容：当月/下月合约因 ETF 除息而被调整（A/B）时，
              取行权价 >= 现货且最接近的 call（实际可作为备兑卖出的近月ATM）
        :return: (strike, option_price, contract_id, contract_name, adj, trade_code)
        """
        strike, interval = atm_strike(etf_price, "ETF")
        expiry, ym = self.next_expiry(asof)
        if expiry is None:
            return None, None, None, None, None, None
        # 从目标月向后试最多4个月（跳过无合约的月份）
        for off in range(0, 4):
            yy, mm = ym[0] + (ym[1] - 1 + off) // 12, (ym[1] - 1 + off) % 12 + 1
            quotes = self.get_month_quotes(etf_code, (yy, mm))
            if not quotes:
                continue
            calls = [q for q in quotes if q["type"] == "购"]
            # 1) 标准合约精确匹配
            for q in calls:
                if q["adj"] == "" and abs(q["strike"] - round(strike, 2)) < 1e-6:
                    return (strike, q["price"], q["contract_id"], q["name"], q["adj"], q.get("trade_code", ""))
            # 2) 无标准ATM：取行权价 >= 现货且最接近的 call（含除息调整合约）
            above = [q for q in calls if q["strike"] >= etf_price - 1e-9]
            if above:
                best = min(above, key=lambda q: abs(q["strike"] - etf_price))
                return (best["strike"], best["price"], best["contract_id"], best["name"], best["adj"], best.get("trade_code", ""))
            # 3) 该月无合适合约，尝试下一月
        return None, None, None, None, None, None
