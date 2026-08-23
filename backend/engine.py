# -*- coding: utf-8 -*-
"""
ATM Premium Yield 计算引擎
规则参考港交所恒生指数备兑期权指数（HSICC）编制方法：
  ATM strike = roundup(标的收盘 / 行权价间距) * 行权价间距
  行权价间距按 A股/港股各品种合约规则，依据现货所在区间动态选取
  Premium Yield = ATM call 权利金 / 标的收盘价
"""
import math

# ---------- 行权价间距规则（官方合约条款） ----------

def strike_interval_hsi(price):
    """恒指期权（HKEX）：20000点以上间距200，5000-20000点间距100"""
    if price >= 20000:
        return 200
    return 100

def strike_interval_io(price):
    """中金所沪深300股指期权（当月/下2月合约）：
    ≤2500点25；2500-5000点50；5000-10000点100；>10000点200"""
    if price <= 2500:
        return 25
    if price <= 5000:
        return 50
    if price <= 10000:
        return 100
    return 200

def strike_interval_etf(price):
    """上交所ETF期权：≤3元0.05；3-5元0.10；5-10元0.25；10-20元0.50；
    20-50元1.0；50-100元2.50；>100元5.0"""
    if price <= 3:
        return 0.05
    if price <= 5:
        return 0.10
    if price <= 10:
        return 0.25
    if price <= 20:
        return 0.50
    if price <= 50:
        return 1.0
    if price <= 100:
        return 2.50
    return 5.0

def atm_strike(price, kind):
    """按 HSICC 规则取 ATM 行权价（roundup，取最接近且不低于现货）
    :param kind: HSI / IO / ETF
    :return: (strike, interval)
    """
    if kind == "HSI":
        interval = strike_interval_hsi(price)
    elif kind == "IO":
        interval = strike_interval_io(price)
    else:
        interval = strike_interval_etf(price)
    strike = math.ceil(price / interval) * interval
    # 浮点取整误差修正（0.25 档会出现 0.25000000000006 之类）
    strike = round(strike, 6)
    return strike, interval

def premium_yield(option_price, underlying_price):
    """Premium Yield = ATM call 权利金 / 标的收盘价（单月，不年化）"""
    if underlying_price is None or underlying_price <= 0 or option_price is None:
        return None
    return option_price / underlying_price
