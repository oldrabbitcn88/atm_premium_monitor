# -*- coding: utf-8 -*-
"""
富途 OpenD 适配器：
- 标的实时价格（恒指 HK.800000、沪深300 SH.000300、510300/510500 SH.510xxx）
- 恒指期权（HSI Options）实时行情：get_option_expiration_date + get_option_chain + get_market_snapshot
恒指期权规则（HKEX）：合约乘数 $50/点；20000点以上行权价间距200点；
月度合约到期日 = 每月最后第二个营业日；欧式、现金交收。
"""
import datetime
import futu as ft

from ..engine import atm_strike


class FutuAdapter:
    def __init__(self, host="127.0.0.1", port=11111):
        self.quote = ft.OpenQuoteContext(host=host, port=port)

    def get_price(self, code):
        """获取标的最新价（指数/ETF）"""
        try:
            ret, data = self.quote.get_market_snapshot([code])
            if ret == ft.RET_OK and len(data) > 0:
                return float(data.iloc[0]["last_price"])
        except Exception as e:
            print(f"[Futu] get_price {code} 失败: {e}")
        return None

    def _monthly_expiry_dates(self):
        """恒指期权月度到期日列表（最近若干个月度到期日）"""
        ret, data = self.quote.get_option_expiration_date(
            code="HK.800000", index_option_type=ft.IndexOptionType.NORMAL
        )
        if ret != ft.RET_OK:
            print(f"[Futu] 恒指期权到期日失败: {data}")
            return []
        dates = [d[:10] for d in data["strike_time"].tolist()]
        return dates

    @staticmethod
    def _month_end_business_day(date, n=2):
        """某月最后第 n 个营业日（跳过周末；节假日近似处理）"""
        y, m = date.year, date.month
        if m == 12:
            nm = 1
            ny = y + 1
        else:
            nm = m + 1
            ny = y
        # 从下月第一天往回数，跳过周六周日
        d = datetime.date(ny, nm, 1) - datetime.timedelta(days=1)
        count = 0
        while count < n:
            if d.weekday() < 5:  # 周一~周五
                count += 1
            if count < n:
                d -= datetime.timedelta(days=1)
        return d

    def get_hsi_monthly_expiry_next(self, asof=None):
        """下一个恒指期权【月度】到期日（每月最后第二个营业日）
        富途返回的到期日含每周期权（每周五），此处仅保留月度到期日
        :return: date 对象
        """
        asof = asof or datetime.date.today()
        real = self._monthly_expiry_dates()
        future_monthly = []
        for rd in real:
            rdt = datetime.date.fromisoformat(rd)
            # 判断是否该月最后第二个营业日
            if rdt == self._month_end_business_day(rdt, n=2):
                future_monthly.append(rdt)
        future_monthly = sorted(d for d in future_monthly if d > asof and (d - asof).days >= 10)
        if future_monthly:
            return future_monthly[0]
        # 兜底：理论计算
        for offset in range(0, 6):
            m = asof.month + offset
            y = asof.year + (m - 1) // 12
            m = (m - 1) % 12 + 1
            d = self._month_end_business_day(datetime.date(y, m, 1))
            if d > asof:
                return d
        return None

    def get_hsi_atm_call(self, underlying_price, expiry_date):
        """获取指定到期日（如2026-09-29）的恒指期权 ATM call 最新价
        :return: (strike, option_price) 或 (None, None)
        """
        strike, interval = atm_strike(underlying_price, "HSI")
        d = expiry_date.strftime("%Y-%m-%d")
        ret, chain = self.quote.get_option_chain(
            code="HK.800000", index_option_type=ft.IndexOptionType.NORMAL,
            start=d, end=d
        )
        if ret != ft.RET_OK:
            print(f"[Futu] 恒指期权链失败: {chain}")
            return None, None
        calls = chain[chain["option_type"] == ft.OptionType.CALL]
        # 找行权价 == ATM strike 的合约
        target = calls[calls["strike_price"].round(6) == round(strike, 6)]
        if len(target) == 0:
            # 若无正好平值，取最接近的
            calls = calls.copy()
            calls["diff"] = (calls["strike_price"] - strike).abs()
            target = calls.sort_values("diff").head(1)
        code = target["code"].iloc[0]
        ret2, snap = self.quote.get_market_snapshot([code])
        if ret2 == ft.RET_OK and len(snap) > 0:
            price = float(snap.iloc[0]["last_price"])
            return strike, price
        print(f"[Futu] 恒指期权行情失败: {snap}")
        return None, None

    @classmethod
    def monthly_expiry_of(cls, year, month):
        """某月的恒指月度交割日（最后第二个营业日，理论算法）
        富途的到期日列表只给未来合约，补采过去的月份查不到，故用规则推算。
        """
        return cls._month_end_business_day(datetime.date(year, month, 1), n=2)

    def get_close_on(self, code, day, lookback=7):
        """取 code 在 day 当日的日K收盘价。
        该日休市则回退到之前最近的交易日（lookback 天内）。
        :return: (实际日期字符串, 收盘价) 或 (None, None)
        """
        start = (day - datetime.timedelta(days=lookback)).isoformat()
        try:
            ret, data, _ = self.quote.request_history_kline(
                code, start=start, end=day.isoformat(),
                ktype=ft.KLType.K_DAY, autype=None, max_count=None,
            )
        except Exception as e:
            print(f"[Futu] {code} 历史K线异常: {e}")
            return None, None
        if ret != ft.RET_OK:
            print(f"[Futu] {code} 历史K线失败: {data}")
            return None, None
        if len(data) == 0:
            print(f"[Futu] {code} 在 {start}~{day} 无K线数据")
            return None, None
        row = data.iloc[-1]
        return str(row["time_key"])[:10], float(row["close"])

    def close(self):
        try:
            self.quote.close()
        except Exception:
            pass
