# -*- coding: utf-8 -*-
"""
备兑覆盖比率（Covered Call Coverage Ratio）计算引擎

适用于 Global X 香港备兑系列（3419 恒指备兑 / 3416 国指备兑等）的每日持仓快照：
该类基金的组合由三块构成（招募说明书口径）：
  (i)  恒指成分股正股 + 恒指 ETF（如 2800 盈富基金）
  (ii) 恒指期货多头
  (iii) 卖出（写出）恒指认购期权

覆盖比率 = 卖出认购期权的标的名义 / 正股+ETF+期货多头的指数等价名义

三种口径（都算，各有用途）：
  coverage_spot   = Σ|空头张数| × 指数点位 × 乘数  / 多头指数等价名义
                    行业通称 overwrite ratio，衡量"组合有多少被备兑掉"
  coverage_strike = Σ|空头张数| × 行权价   × 乘数  / 多头指数等价名义
                    衡量"按行权价计的封顶敞口"，价外时 > spot 口径
  coverage_delta  = Σ|空头张数| × 指数点位 × 乘数 × delta / 多头指数等价名义
                    真实已让渡的上行参与度（delta 需假设波动率，属估算）

注意（易错点）：
  * 期货名义必须用 张数 × 期货价 × 乘数 自己算；持仓表里期货的 Market Value
    常常只是浮动盈亏（甚至为 0/负数），直接加总会把敞口算少。
  * 卖出期权的 Market Value 是负数（负债），绝不能计入分母多头名义。
  * 恒指期货/期权乘数 HK$50/点，小型恒指（MHI）HK$10/点；
    国指期货/期权（HHI）HK$50/点，小型国指（MCH）HK$10/点。
"""
import datetime
import math
import re

# ---------- 合约乘数（HKEX 官方合约条款，HKD/指数点） ----------

CONTRACT_MULTIPLIER = {
    "HSI": 50.0,    # 恒生指数期货/期权
    "MHI": 10.0,    # 小型恒生指数期货
    "HHI": 50.0,    # 恒生中国企业指数（国指）期货/期权
    "MCH": 10.0,    # 小型国指期货
    "HTI": 50.0,    # 恒生科技指数期货/期权
    "MHT": 10.0,    # 小型恒生科技指数期货
}
DEFAULT_MULTIPLIER = 50.0

# 持仓行分类
BUCKET_EQUITY = "EQUITY"          # 正股（指数成分股）
BUCKET_INDEX_ETF = "INDEX_ETF"    # 指数 ETF（2800 等），等同指数多头
BUCKET_FUT_LONG = "FUTURE_LONG"   # 指数期货多头
BUCKET_FUT_SHORT = "FUTURE_SHORT" # 指数期货空头（理论上不该有，出现要告警）
BUCKET_CALL_SHORT = "CALL_SHORT"  # 卖出认购期权（备兑腿）
BUCKET_CALL_LONG = "CALL_LONG"    # 买入认购（如展期/价差腿）
BUCKET_PUT = "PUT"                # 认沽（保护腿，若有）
BUCKET_CASH = "CASH"              # 现金/保证金/货币基金
BUCKET_OTHER = "OTHER"

# 指数 ETF 代码（港股），持有即视为指数多头敞口
INDEX_ETF_TICKERS = {"2800", "02800", "2828", "02828", "3033", "03033", "2833", "02833"}
INDEX_ETF_NAME_PAT = re.compile(
    r"TRACKER\s+FUND|盈富|TRACKER|HANG\s+SENG\s+INDEX\s+ETF|恒生指數\s*ETF|恒生指数\s*ETF",
    re.I,
)

_FUT_PAT = re.compile(r"\b(HSI|MHI|HHI|MCH|HTI|MHT)\b.{0,24}?(FUT|FUTURE|期貨|期货)|(FUT|FUTURE|期貨|期货).{0,24}?\b(HSI|MHI|HHI|MCH|HTI|MHT)\b", re.I)
_CALL_PAT = re.compile(r"\bC(ALL)?\b|認購|认购|CALL", re.I)
_PUT_PAT = re.compile(r"\bP(UT)?\b|認沽|认沽|PUT", re.I)
_OPT_PAT = re.compile(r"OPTION|期權|期权|\bOPT\b", re.I)
_CASH_PAT = re.compile(r"CASH|現金|现金|MARGIN|保證金|保证金|DEPOSIT|存款|MONEY\s*MARKET|RECEIVABLE|PAYABLE|應收|应收|應付|应付", re.I)

# 月份代码：F=1月 G=2 H=3 J=4 K=5 M=6 N=7 Q=8 U=9 V=10 X=11 Z=12（期货/看涨期权通用月码）
_MONTH_CODE = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
               "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
_MONTH_ABBR = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

# 恒指/国指点位合理区间，用于从合约名里挑出"像行权价"的数字
_STRIKE_RANGE = (3000.0, 80000.0)


def _text(row):
    """合约标识文本（name/ticker/asset_class/sector）。
    刻意不含 raw 原始列：市值/价格那些数字会被误判成行权价。"""
    parts = []
    for k in ("name", "ticker", "asset_class", "sector"):
        v = row.get(k)
        if v:
            parts.append(str(v))
    return " ".join(parts)


def _text_all(row):
    """含原始列的全文本，仅用于分类关键词（WRITTEN/SHORT/现金/期货等）"""
    parts = [_text(row)]
    raw = row.get("raw")
    if isinstance(raw, dict):
        for k, v in raw.items():
            parts.append("%s %s" % (k, v))
    elif raw:
        parts.append(str(raw))
    return " ".join(parts)


def detect_index_code(text):
    """识别合约所属指数族（HSI/MHI/HHI/...），决定乘数。
    只要求左词边界：合约代码常与月码连写（MHIX6、HSIX6C26400），右边界会漏判。
    """
    up = (text or "").upper()
    mini = bool(re.search(r"\bMINI\b|小型|迷你", up))
    for code in ("MHI", "MCH", "MHT"):          # 小型合约代码优先
        if re.search(r"\b" + code, up):
            return code
    for code, mini_code in (("HSI", "MHI"), ("HHI", "MCH"), ("HTI", "MHT")):
        if re.search(r"\b" + code, up):
            return mini_code if mini else code
    if "恒生科技" in up or "HANG SENG TECH" in up:
        return "MHT" if mini else "HTI"
    if "國企" in up or "国企" in up or "HSCEI" in up or "H-SHARE" in up:
        return "MCH" if mini else "HHI"
    if "恒生指數" in up or "恒生指数" in up or "HANG SENG INDEX" in up:
        return "MHI" if mini else "HSI"
    return None


def multiplier_for(text, default=DEFAULT_MULTIPLIER):
    code = detect_index_code(text)
    return CONTRACT_MULTIPLIER.get(code, default)


def parse_strike(text):
    """从合约名里解析行权价。
    支持：'HSI 26000 CALL NOV26' / 'HSIX6C26000' / 'HSI C 26,000 11/27/26'
    策略：取落在恒指合理区间、且是 50/100/200 整数倍的最大候选数字
    """
    if not text:
        return None
    cands = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", str(text)):
        try:
            v = float(m.group(0).replace(",", ""))
        except ValueError:
            continue
        if _STRIKE_RANGE[0] <= v <= _STRIKE_RANGE[1] and v % 50 == 0:
            cands.append(v)
    if not cands:
        return None
    # 行权价通常是名字里最大的"整百"数字（年份 2026、张数等都更小或非整百）
    return max(cands)


def parse_expiry(text, as_of=None):
    """从合约名里解析到期（返回 date 或 None）。
    支持 'NOV26' / 'NOV 2026' / '11/27/26' / '2026-11-27' / 月码 'HSIX6'
    """
    if not text:
        return None
    s = str(text).upper()

    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yy = yy + 2000 if yy < 100 else yy
        try:
            return datetime.date(yy, mm, dd)
        except ValueError:
            pass

    m = re.search(r"\b(" + "|".join(_MONTH_ABBR) + r")[A-Z]*\s*'?(\d{2,4})\b", s)
    if m:
        mm = _MONTH_ABBR[m.group(1)]
        yy = int(m.group(2))
        yy = yy + 2000 if yy < 100 else yy
        return hsi_option_expiry(yy, mm)

    # 月码形式：HSIX6 / HSI X6 → X=11月, 6=2026
    m = re.search(r"\b(?:HSI|MHI|HHI|MCH|HTI|MHT)\s?([FGHJKMNQUVXZ])(\d)\b", s)
    if m:
        mm = _MONTH_CODE[m.group(1)]
        digit = int(m.group(2))
        base = (as_of.year if as_of else datetime.date.today().year)
        yy = base - base % 10 + digit
        if yy < base:          # 个位数年份跨十年时向前推
            yy += 10
        return hsi_option_expiry(yy, mm)
    return None


def hsi_option_expiry(year, month):
    """恒指期货/期权到期日：该月最后营业日的前一个营业日（倒数第二个营业日）。
    此处只扣周末（不含香港公众假期），用于近似/校验，精确到期日应以交易所列表为准。
    """
    last = datetime.date(year, month, _days_in_month(year, month))
    biz = []
    d = last
    while len(biz) < 2:
        if d.weekday() < 5:
            biz.append(d)
        d -= datetime.timedelta(days=1)
    return biz[1]


def _days_in_month(year, month):
    if month == 12:
        return 31
    return (datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)).day


def classify(row):
    """把一行持仓归入 bucket。依赖 name/ticker/asset_class 文本 + 数量/市值符号。"""
    text = _text(row)
    up = _text_all(row).upper()
    qty = row.get("quantity")
    mv = row.get("market_value")

    if _CASH_PAT.search(up) and not _OPT_PAT.search(up):
        return BUCKET_CASH

    is_fut = bool(_FUT_PAT.search(up)) or (
        detect_index_code(up) and re.search(r"\bFUT|期貨|期货", up))
    is_opt = bool(_OPT_PAT.search(up)) or bool(
        re.search(r"\b(?:HSI|MHI|HHI|MCH|HTI|MHT)\b.*\b[CP]\s?\d{4,6}\b", up))

    if is_fut and not is_opt:
        short = (qty is not None and qty < 0)
        return BUCKET_FUT_SHORT if short else BUCKET_FUT_LONG

    if is_opt or _CALL_PAT.search(up) or _PUT_PAT.search(up):
        if _PUT_PAT.search(up) and not _CALL_PAT.search(up):
            return BUCKET_PUT
        if _CALL_PAT.search(up) or is_opt:
            # 空头判定：数量为负、或市值为负、或文本标注 WRITTEN/SHORT/賣出
            short = (qty is not None and qty < 0) or \
                    (qty is None and mv is not None and mv < 0) or \
                    bool(re.search(r"WRITTEN|SHORT|SOLD|賣出|卖出|空頭|空头", up))
            return BUCKET_CALL_SHORT if short else BUCKET_CALL_LONG

    tk = str(row.get("ticker") or "").strip().upper()
    tk_num = re.sub(r"^0+", "", re.sub(r"[^0-9]", "", tk)) or None
    if tk_num and tk_num in {t.lstrip("0") for t in INDEX_ETF_TICKERS}:
        return BUCKET_INDEX_ETF
    if INDEX_ETF_NAME_PAT.search(text):
        return BUCKET_INDEX_ETF

    if mv is not None or qty is not None:
        return BUCKET_EQUITY
    return BUCKET_OTHER


# ---------- Black-Scholes 看涨 delta（仅用于 delta 调整口径的估算） ----------

def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_delta(spot, strike, dte_days, vol_annual, rate=0.0):
    """欧式看涨 delta。dte_days<=0 返回 0/1 的边界值。"""
    if not spot or not strike or spot <= 0 or strike <= 0 or vol_annual is None or vol_annual <= 0:
        return None
    if dte_days is None:
        return None
    t = max(float(dte_days), 0.0) / 365.0
    if t <= 0:
        return 1.0 if spot > strike else 0.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol_annual ** 2) * t) / (vol_annual * math.sqrt(t))
    return _norm_cdf(d1)


# ---------- 主计算 ----------

def compute_coverage(snapshot, index_level=None, vol_annual=0.20, rate=0.0):
    """由一份标准化持仓快照算覆盖比率。

    :param snapshot: {"as_of": "YYYY-MM-DD", "rows": [ {...}, ... ], ...}
        row 字段（缺失容忍）：name, ticker, asset_class, quantity, price,
        market_value, weight, currency
    :param index_level: 指数点位（恒指收盘）。缺省时按顺序回退：
        快照里的 index_level 字段 → 期货多头的价格 → None（现货名义口径不可算）
    :param vol_annual: delta 调整口径假设的年化波动率（估算，非真实隐含波动率）
    :return: dict，见文件头注释
    """
    rows = snapshot.get("rows") or []
    as_of = snapshot.get("as_of")
    as_of_date = None
    if as_of:
        try:
            as_of_date = datetime.date.fromisoformat(str(as_of)[:10])
        except ValueError:
            as_of_date = None

    equity_mv = 0.0
    etf_mv = 0.0
    cash_mv = 0.0
    other_mv = 0.0
    fut_notional = 0.0
    fut_legs = []
    call_legs = []
    put_legs = []
    warnings = []
    buckets = {}

    for row in rows:
        bucket = row.get("bucket") or classify(row)
        row["bucket"] = bucket
        buckets[bucket] = buckets.get(bucket, 0) + 1
        mv = row.get("market_value")
        qty = row.get("quantity")
        price = row.get("price")
        text = _text(row)

        if bucket == BUCKET_EQUITY:
            equity_mv += _mv_or_calc(mv, qty, price)
        elif bucket == BUCKET_INDEX_ETF:
            etf_mv += _mv_or_calc(mv, qty, price)
        elif bucket == BUCKET_CASH:
            cash_mv += mv or 0.0
        elif bucket in (BUCKET_FUT_LONG, BUCKET_FUT_SHORT):
            mult = multiplier_for(text)
            # 期货名义必须自己算：持仓表的 MV 常只是浮动盈亏
            n = None
            if qty is not None and price:
                n = qty * price * mult
            leg = {
                "name": row.get("name"), "ticker": row.get("ticker"),
                "contracts": qty, "price": price, "multiplier": mult,
                "notional": n, "expiry": _iso(parse_expiry(text, as_of_date)),
                "market_value": mv,
            }
            fut_legs.append(leg)
            if n is None:
                warnings.append("期货缺张数或价格，名义无法计算：%s" % (row.get("name") or row.get("ticker")))
            else:
                fut_notional += n
            if bucket == BUCKET_FUT_SHORT:
                warnings.append("出现指数期货空头腿（与备兑策略不符，请核对符号）：%s"
                                % (row.get("name") or row.get("ticker")))
        elif bucket in (BUCKET_CALL_SHORT, BUCKET_CALL_LONG):
            mult = multiplier_for(text)
            strike = row.get("strike") or parse_strike(text)
            expiry = row.get("expiry") or _iso(parse_expiry(text, as_of_date))
            contracts = qty
            leg = {
                "name": row.get("name"), "ticker": row.get("ticker"),
                "contracts": contracts, "strike": strike, "expiry": expiry,
                "multiplier": mult, "market_value": mv,
                "side": "short" if bucket == BUCKET_CALL_SHORT else "long",
            }
            if contracts is None:
                warnings.append("期权腿缺张数，无法计入覆盖：%s" % (row.get("name") or row.get("ticker")))
            if strike is None:
                warnings.append("期权腿行权价解析失败，strike 口径将缺失：%s"
                                % (row.get("name") or row.get("ticker")))
            call_legs.append(leg)
        elif bucket == BUCKET_PUT:
            put_legs.append({
                "name": row.get("name"), "contracts": qty,
                "strike": row.get("strike") or parse_strike(text),
                "expiry": row.get("expiry") or _iso(parse_expiry(text, as_of_date)),
                "market_value": mv,
            })
        else:
            other_mv += mv or 0.0

    # 指数点位解析顺序：显式参数 → 快照字段 → 期货价格
    idx = index_level or snapshot.get("index_level")
    idx_source = "param" if index_level else ("snapshot" if snapshot.get("index_level") else None)
    if not idx:
        fut_prices = [l["price"] for l in fut_legs if l.get("price")]
        if fut_prices:
            idx = sum(fut_prices) / len(fut_prices)
            idx_source = "futures_price_proxy"
            warnings.append("未提供指数点位，已用期货价格近似（含基差，现货名义口径略有偏差）")

    long_index_equiv = equity_mv + etf_mv + fut_notional

    short_spot_notional = 0.0
    short_strike_notional = 0.0
    short_delta_notional = 0.0
    short_contracts = 0.0
    short_mv = 0.0
    strike_num = 0.0
    strike_den = 0.0
    dte_num = 0.0
    dte_den = 0.0
    has_strike_all = True
    has_delta_all = True

    for leg in call_legs:
        c = leg.get("contracts")
        if c is None:
            continue
        sign = -1.0 if leg["side"] == "short" else 1.0  # 买入认购抵减卖出覆盖
        n = abs(c) if leg["side"] == "short" else -abs(c)
        short_contracts += n
        short_mv += leg.get("market_value") or 0.0
        mult = leg["multiplier"]
        if idx:
            short_spot_notional += n * idx * mult
        if leg.get("strike"):
            short_strike_notional += n * leg["strike"] * mult
            w = abs(n) * mult
            strike_num += leg["strike"] * w
            strike_den += w
        else:
            has_strike_all = False
        dte = None
        if leg.get("expiry") and as_of_date:
            try:
                dte = (datetime.date.fromisoformat(leg["expiry"]) - as_of_date).days
            except ValueError:
                dte = None
        if dte is not None:
            w = abs(n) * mult
            dte_num += dte * w
            dte_den += w
        d = bs_call_delta(idx, leg.get("strike"), dte, vol_annual, rate)
        leg["delta_est"] = d
        leg["dte"] = dte
        if d is None or not idx:
            has_delta_all = False
        else:
            short_delta_notional += n * idx * mult * d

    wavg_strike = (strike_num / strike_den) if strike_den else None
    wavg_dte = (dte_num / dte_den) if dte_den else None

    def _ratio(num, ok=True):
        if not ok or not long_index_equiv:
            return None
        return num / long_index_equiv

    # 官网"期權持倉"表自带的官方口径（名義敞口佔資產淨值比例），可直接对照
    official_pct_nav = None
    nav_implied = None
    ot = snapshot.get("option_table") or []
    if ot:
        pcts = [o.get("notional_pct_nav") for o in ot if o.get("notional_pct_nav") is not None]
        nots = [o.get("notional") for o in ot if o.get("notional") is not None]
        if pcts:
            official_pct_nav = sum(abs(x) for x in pcts)
        if pcts and nots and official_pct_nav:
            nav_implied = sum(abs(x) for x in nots) / official_pct_nav

    result = {
        "as_of": as_of,
        "fund": snapshot.get("fund"),
        # 官方口径：分母是资产净值 NAV（含现金、扣期权负债）
        "official_coverage_pct_nav": (round(official_pct_nav, 6)
                                      if official_pct_nav is not None else None),
        "nav_implied": round(nav_implied, 2) if nav_implied else None,
        "source_url": snapshot.get("source_url"),
        "index_level": idx,
        "index_level_source": idx_source,
        # 分母：多头指数等价敞口
        "equity_mv": round(equity_mv, 2),
        "index_etf_mv": round(etf_mv, 2),
        "futures_notional": round(fut_notional, 2),
        "long_index_equiv": round(long_index_equiv, 2),
        "cash_mv": round(cash_mv, 2),
        "other_mv": round(other_mv, 2),
        # 分子：卖出认购
        "short_call_contracts": short_contracts,
        "short_call_mv": round(short_mv, 2),
        "short_call_spot_notional": round(short_spot_notional, 2) if idx else None,
        "short_call_strike_notional": round(short_strike_notional, 2) if has_strike_all else None,
        # 三种覆盖比率
        "coverage_spot": _ratio(short_spot_notional, bool(idx)),
        "coverage_strike": _ratio(short_strike_notional, has_strike_all),
        "coverage_delta_est": _ratio(short_delta_notional, has_delta_all and bool(idx)),
        "delta_vol_assumed": vol_annual,
        # 期权结构特征
        "wavg_strike": round(wavg_strike, 2) if wavg_strike else None,
        "wavg_moneyness": (round(wavg_strike / idx - 1.0, 6) if (wavg_strike and idx) else None),
        "wavg_dte": round(wavg_dte, 2) if wavg_dte is not None else None,
        "n_call_legs": len(call_legs),
        "n_future_legs": len(fut_legs),
        "put_legs": put_legs,
        "call_legs": call_legs,
        "future_legs": fut_legs,
        "bucket_counts": buckets,
        "warnings": warnings,
    }
    for k in ("coverage_spot", "coverage_strike", "coverage_delta_est"):
        if result[k] is not None:
            result[k] = round(result[k], 6)

    # 完整性自检：持仓文件只抓到前十大、或漏了期货/ETF 腿时，分母会明显偏小
    if nav_implied and long_index_equiv:
        ratio = long_index_equiv / nav_implied
        result["long_vs_nav"] = round(ratio, 6)
        if ratio < 0.8:
            warnings.append(
                "多头名义仅为资产净值的 %.1f%%，持仓多半不完整（例如只拿到前十大持股），"
                "此时 coverage_spot/strike 会虚高，请以 official_coverage_pct_nav 为准"
                % (ratio * 100))
    cs = result.get("coverage_spot")
    if cs is not None and (cs > 1.5 or cs < 0.5):
        warnings.append("覆盖比率 %.1f%% 偏离正常区间（满额备兑应在 100%% 附近），"
                        "请先核对分母是否完整、期货乘数是否取对" % (cs * 100))
    return result


def _mv_or_calc(mv, qty, price):
    if mv is not None:
        return mv
    if qty is not None and price is not None:
        return qty * price
    return 0.0


def _iso(d):
    return d.isoformat() if isinstance(d, datetime.date) else None
