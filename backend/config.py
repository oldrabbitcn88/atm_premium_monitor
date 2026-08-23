# -*- coding: utf-8 -*-
"""配置：数据源、监测品种、刷新频率"""
import os

# 富途 OpenD 网关
FUTU_HOST = os.environ.get("FUTU_HOST", "127.0.0.1")
FUTU_PORT = int(os.environ.get("FUTU_PORT", "11111"))

# 刷新间隔（秒）
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "300"))  # 默认5分钟

# SQLite 历史数据文件
DB_PATH = os.path.join(os.path.dirname(__file__), "atm_history.db")

# 前端静态目录
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

# 监测品种定义
# kind: HSI=恒指期权 IO=沪深300股指期权 ETF=上交所ETF期权
# futu_code: 富途行情代码（标的价格源）
# opt_underlying: 期权标的（决定ATM strike间距用哪个价格）
TARGETS = [
    {
        "key": "HSI",
        "name": "恒生指数",
        "kind": "HSI",
        "futu_code": "HK.800000",          # 恒指点位（富途）
        "opt_source": "futu",               # 期权数据源：富途（恒指期权）
        "opt_month_rule": "hsi",            # 到期日规则：每月最后第二个营业日
        "display_unit": "点",
        "note": "恒指期权（HKEX），间距200点（20000点以上）",
    },
    {
        "key": "CSI300",
        "name": "沪深300指数",
        "kind": "IO",
        "futu_code": "SH.000300",           # 沪深300指数点位（富途）
        "opt_source": "cffex",              # 期权数据源：中金所 quote_IO.txt（沪深300股指期权）
        "opt_month_rule": "io",             # 到期日规则：每月第三个周五
        "display_unit": "点",
        "note": "沪深300股指期权（中金所），间距50/100点",
    },
    {
        "key": "CSI500",
        "name": "中证500ETF",
        "kind": "ETF",
        "futu_code": "SH.510500",           # 510500 ETF价格（富途）
        "opt_source": "sse",                # 期权数据源：上交所 yunhq（510500 ETF期权）
        "opt_month_rule": "etf",            # 到期日规则：每月第四个周三
        "display_unit": "元",
        "note": "中证500ETF期权（上交所），间距0.25元（5-10元区间）",
    },
]
