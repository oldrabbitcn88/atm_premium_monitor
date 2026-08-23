# -*- coding: utf-8 -*-
"""
中金所适配器：沪深300股指期权（IO）实时行情
数据源：http://www.cffex.com.cn/quote_IO.txt （中金所官方实时行情文本）
格式：instrument,position,volume,lastprice,updown,bprice,bamount,sprice,samount
如：IO2609-C-4650,0,123,45.60,1.20,44.80,1,47.00,2
"""
import re
import requests

from ..engine import atm_strike

CFFEX_IO_URL = "http://www.cffex.com.cn/quote_IO.txt"


class CffexAdapter:
    def __init__(self, url=CFFEX_IO_URL, timeout=8):
        self.url = url
        self.timeout = timeout

    def get_io_quotes(self):
        """获取IO全合约行情，返回 {合约代码: 最新价}"""
        try:
            r = requests.get(self.url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout)
            r.encoding = "utf-8"
            lines = r.text.strip().splitlines()
        except Exception as e:
            print(f"[Cffex] 请求失败: {e}")
            return {}
        quotes = {}
        for line in lines[1:]:  # 跳过表头
            parts = line.split(",")
            if len(parts) >= 4:
                instrument = parts[0].strip()
                try:
                    last = float(parts[3].strip())
                except ValueError:
                    continue
                quotes[instrument] = last
        return quotes

    @staticmethod
    def _next_io_month(asof):
        """IO合约月份：当月、下2个月及随后3个季月。取 asof 之后最近的一个到期月（次月）"""
        y, m = asof.year, asof.month
        nm = m + 1
        ny = y + (nm - 1) // 12
        nm = (nm - 1) % 12 + 1
        return ny, nm

    def get_io_atm_call(self, underlying_price, asof):
        """获取 IO 下月 ATM call 最新价
        :return: (strike, option_price, contract_code) 或 (None, None, None)
        """
        quotes = self.get_io_quotes()
        if not quotes:
            return None, None, None
        strike, interval = atm_strike(underlying_price, "IO")
        ny, nm = self._next_io_month(asof)
        prefix = f"IO{ny % 100:02d}{nm:02d}-C-{int(strike)}"
        # 精确匹配
        code = prefix
        if code not in quotes:
            # 容错：匹配同月份所有call，取行权价最接近
            pat = re.compile(rf"^IO{ny % 100:02d}{nm:02d}-C-(\d+)$")
            best_code, best_diff, best_price = None, 1e18, None
            for c, p in quotes.items():
                m = pat.match(c)
                if m:
                    diff = abs(int(m.group(1)) - strike)
                    if diff < best_diff:
                        best_diff, best_code, best_price = diff, c, p
            if best_code is None:
                return None, None, None
            code = best_code
            price = best_price
            # 若没找到正好平值，返回实际匹配的行权价
            m = pat.match(code)
            if m:
                strike = int(m.group(1))
        else:
            price = quotes[code]
        return strike, price, code
