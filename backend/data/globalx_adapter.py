# -*- coding: utf-8 -*-
"""
Global X 香港（Mirae Asset HK）ETF 每日持仓适配器

用途：抓 3419（恒指备兑）/3416（国指备兑）等的"全部持仓"文件并标准化，
      供 backend/coverage.py 计算备兑覆盖比率。

设计要点：
1) 官网持仓的下载地址不保证稳定（会随站点改版变化），所以本模块不写死 URL，
   而是"先发现、再下载"：抓基金详情页 → 从 HTML/内嵌 JSON 里找出
   含 holdings/portfolio/positions 的 csv/xlsx/json 链接 → 逐个尝试解析。
   发现到的地址会随快照一起记录（source_url），便于复现。
2) 解析层对格式不挑：CSV / XLSX / JSON / HTML 表格都能吃，
   列名用别名表做模糊映射（中英文、简繁都覆盖）。
3) 无网络环境（或官网需要人工点验证）时，用 load_local_file() 直接解析
   手工下载到本地的持仓文件，后续流程完全一致。

依赖：requests（必需）；openpyxl（仅解析 .xlsx 时需要）
"""
import csv
import io
import json
import os
import re

try:
    import requests
except ImportError:  # 允许在纯解析场景下不装 requests
    requests = None

SITE = "https://www.globalxetfs.com.hk"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 基金详情页候选（站点有多语言前缀，逐个试）
FUND_PAGE_PATTERNS = [
    "/zh-hant/funds/{code}/",
    "/en/funds/{code}/",
    "/funds/{code}/",
    "/zh-hant/funds/covered-call-etf/",
    "/en/funds/covered-call-etf/",
]

# 从页面里挑链接用的关键词
_HOLDINGS_HINT = re.compile(r"holding|portfolio|position|constituent|持倉|持仓|成分|組合|组合", re.I)
_DATA_EXT = re.compile(r"\.(csv|xlsx|xls|json)(\?|$)", re.I)
_URL_PAT = re.compile(r"""["'(]((?:https?:)?//[^"'()\s]+|/[^"'()\s]+)["')]""")

# ---------- 列名别名（小写去空格后匹配） ----------
COLUMN_ALIASES = {
    "name": ["name", "securityname", "holdingname", "description", "securitydescription",
             "security", "holding", "stockname", "instrument", "instrumentname",
             "名稱", "名称", "證券名稱", "证券名称", "持倉名稱", "持仓名称", "股票名稱", "股票名称",
             "資產名稱", "资产名称"],
    "ticker": ["ticker", "symbol", "code", "stockcode", "securityid", "identifier", "isin",
               "bloombergticker", "bbgticker", "sedol", "ric",
               "代號", "代号", "股票代號", "股票代码", "證券代號", "证券代码", "編號", "编号"],
    "quantity": ["quantity", "shares", "sharesheld", "noofshares", "numberofshares", "units",
                 "nominal", "nominalvalue", "parvalue", "position", "contracts",
                 "noofcontracts", "numberofcontracts", "holdingquantity", "qty",
                 "數量", "数量", "持股數", "持股数", "股數", "股数", "張數", "张数", "合約張數",
                 "合约张数", "持倉量", "持仓量", "單位", "单位"],
    "price": ["price", "marketprice", "lastprice", "closeprice", "localprice", "px",
              "價格", "价格", "市價", "市价", "收市價", "收盘价"],
    "market_value": ["marketvalue", "marketvaluehkd", "value", "valuehkd", "mv",
                     "marketvalueinhkd", "notionalvalue", "basemarketvalue",
                     "市值", "市場價值", "市场价值", "持倉市值", "持仓市值", "金額", "金额"],
    "weight": ["weight", "weighting", "weightings", "ofnetassets", "netassets", "pctofnav",
               "percentofnav", "weightpct", "allocation",
               "權重", "权重", "比重", "佔比", "占比", "佔基金資產淨值", "占基金资产净值"],
    "asset_class": ["assetclass", "type", "securitytype", "instrumenttype", "assettype",
                    "category", "sector", "gicssector",
                    "資產類別", "资产类别", "類別", "类别", "類型", "类型", "行業", "行业"],
    "currency": ["currency", "ccy", "localcurrency", "貨幣", "货币", "幣種", "币种"],
    "strike": ["strike", "strikeprice", "exerciseprice", "行權價", "行权价", "行使價", "行使价"],
    "expiry": ["expiry", "expirydate", "maturity", "maturitydate", "expiration",
               "到期日", "到期", "屆滿日", "届满日"],
}
_ALIAS_LOOKUP = {}
for _field, _names in COLUMN_ALIASES.items():
    for _n in _names:
        _ALIAS_LOOKUP[_n] = _field


def _norm_key(s):
    """列名归一化：去空格/标点/单位括号，转小写"""
    s = str(s or "")
    s = re.sub(r"\(.*?\)|（.*?）", "", s)          # 去掉 (HKD)、（港元）之类
    s = re.sub(r"[%\s_\-\./:#'\"]+", "", s)
    s = s.replace("％", "")
    return s.strip().lower()


def map_column(header):
    return _ALIAS_LOOKUP.get(_norm_key(header))


def parse_number(v):
    """宽松数字解析：千分位、括号负数、百分号、空值占位符"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "" or s in {"-", "--", "—", "–", "N/A", "n/a", "NA", "null", "None"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    pct = s.endswith("%") or s.endswith("％")
    s = s.strip("()").replace(",", "").replace("%", "").replace("％", "")
    s = s.replace("HK$", "").replace("$", "").replace("港元", "").strip()
    try:
        x = float(s)
    except ValueError:
        return None
    if neg:
        x = -x
    if pct:
        x = x / 100.0
    return x


# ---------- 解析各种格式 → 标准行 ----------

def rows_from_records(records):
    """records: [{列名: 值}] → 标准化行列表"""
    out = []
    for rec in records:
        row = {"raw": {}}
        for k, v in rec.items():
            field = map_column(k)
            row["raw"][str(k)] = v
            if not field:
                continue
            if field in ("quantity", "price", "market_value", "weight", "strike"):
                val = parse_number(v)
            else:
                val = (str(v).strip() if v is not None else None)
            if val is not None and (field not in row or row.get(field) in (None, "")):
                row[field] = val
        if any(row.get(f) is not None for f in ("name", "ticker", "quantity", "market_value")):
            out.append(row)
    return out


def parse_csv_text(text):
    """CSV/TSV：自动找表头行（前 30 行里映射到字段最多的一行）"""
    if text.startswith("﻿"):
        text = text[1:]
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        delim = dialect.delimiter
    except Exception:
        delim = ","
    lines = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not lines:
        return []
    best_i, best_score = None, 0
    for i, line in enumerate(lines[:30]):
        score = sum(1 for c in line if map_column(c))
        if score > best_score:
            best_i, best_score = i, score
    if best_i is None or best_score < 2:
        return []
    header = lines[best_i]
    records = []
    for line in lines[best_i + 1:]:
        if not any(str(c).strip() for c in line):
            continue
        records.append({header[j] if j < len(header) else "col%d" % j: line[j]
                        for j in range(len(line))})
    return rows_from_records(records)


def parse_xlsx_bytes(data):
    """XLSX：需要 openpyxl。逐工作表找表头，取映射字段最多的工作表"""
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise RuntimeError("解析 .xlsx 需要 openpyxl：pip install openpyxl")
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    best = []
    for ws in wb.worksheets:
        grid = [list(r) for r in ws.iter_rows(max_row=400, values_only=True)]
        if not grid:
            continue
        best_i, best_score = None, 0
        for i, line in enumerate(grid[:30]):
            score = sum(1 for c in line if map_column(c))
            if score > best_score:
                best_i, best_score = i, score
        if best_i is None or best_score < 2:
            continue
        header = grid[best_i]
        records = []
        for line in grid[best_i + 1:]:
            if not any(c is not None and str(c).strip() for c in line):
                continue
            records.append({(header[j] if j < len(header) and header[j] else "col%d" % j): line[j]
                            for j in range(len(line))})
        rows = rows_from_records(records)
        if len(rows) > len(best):
            best = rows
    return best


def parse_json_obj(obj):
    """JSON：递归找"字典列表"，取其中能映射出最多字段的那个列表"""
    best = []

    def walk(o):
        nonlocal best
        if isinstance(o, list):
            dicts = [x for x in o if isinstance(x, dict)]
            if len(dicts) >= 3:
                rows = rows_from_records(dicts)
                if len(rows) > len(best):
                    best = rows
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)

    walk(obj)
    return best


def parse_html_table(html):
    """服务端渲染的持仓表格：粗解析 <table> → 行"""
    best = []
    for tbl in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S | re.I):
        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S | re.I)
        grid = []
        for tr in trs:
            cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S | re.I)
            grid.append([re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip() for c in cells])
        if len(grid) < 3:
            continue
        best_i, best_score = None, 0
        for i, line in enumerate(grid[:10]):
            score = sum(1 for c in line if map_column(c))
            if score > best_score:
                best_i, best_score = i, score
        if best_i is None or best_score < 2:
            continue
        header = grid[best_i]
        records = [{(header[j] if j < len(header) else "col%d" % j): line[j]
                    for j in range(len(line))} for line in grid[best_i + 1:] if any(line)]
        rows = rows_from_records(records)
        if len(rows) > len(best):
            best = rows
    return best


def decode_bytes(data):
    """按常见编码解码成文本（xlsx 等二进制返回 None）"""
    if isinstance(data, str):
        return data
    if not data or data[:2] == b"PK":
        return None
    for enc in ("utf-8-sig", "utf-8", "big5", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def xlsx_text_preview(data, max_rows=30):
    """取 xlsx 各表前若干行的文本，用于猜"截至日期"（表头上方那几行）"""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return ""
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        return ""
    parts = []
    for ws in wb.worksheets:
        for i, row in enumerate(ws.iter_rows(max_row=max_rows, values_only=True)):
            if i >= max_rows:
                break
            parts.extend(str(c) for c in row if c is not None)
    return " ".join(parts)


def parse_payload(content, content_type="", url=""):
    """按内容类型/扩展名选解析器，失败则逐个兜底尝试"""
    ct = (content_type or "").lower()
    lower_url = (url or "").lower()
    if isinstance(content, bytes) and content[:2] == b"PK":   # xlsx 是 zip
        return parse_xlsx_bytes(content)
    text = decode_bytes(content)
    if text is None:
        return []

    if "json" in ct or lower_url.endswith(".json"):
        try:
            return parse_json_obj(json.loads(text))
        except (ValueError, TypeError):
            pass
    if "csv" in ct or lower_url.endswith(".csv"):
        rows = parse_csv_text(text)
        if rows:
            return rows
    if "html" in ct or "<table" in text.lower():
        rows = parse_html_table(text)
        if rows:
            return rows
    # 兜底：按 JSON → CSV → HTML 依次试
    stripped = text.lstrip()
    if stripped[:1] in "[{":
        try:
            rows = parse_json_obj(json.loads(text))
            if rows:
                return rows
        except (ValueError, TypeError):
            pass
    rows = parse_csv_text(text)
    if rows:
        return rows
    return parse_html_table(text)


# ---------- 网络层：发现 + 下载 ----------

class GlobalXAdapter:
    def __init__(self, timeout=20, retries=3, session=None):
        if requests is None and session is None:
            raise RuntimeError("需要 requests：pip install requests")
        self.timeout = timeout
        self.retries = retries
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT,
                               "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8"})

    def _get(self, url, **kw):
        last = None
        for i in range(self.retries):
            try:
                r = self.s.get(url, timeout=self.timeout, **kw)
                if r.status_code == 200:
                    return r
                last = "HTTP %d" % r.status_code
            except Exception as e:      # 网络异常重试
                last = str(e)
            import time
            time.sleep(2 ** i)
        raise RuntimeError("取 %s 失败：%s" % (url, last))

    def fund_page(self, code):
        """返回 (url, html)：逐个候选路径试，第一个 200 且页面提到该代码的就用"""
        errors = []
        for pat in FUND_PAGE_PATTERNS:
            url = SITE + pat.format(code=code)
            try:
                r = self._get(url)
            except RuntimeError as e:
                errors.append(str(e))
                continue
            if str(code) in r.text:
                return url, r.text
            errors.append("%s 不含 %s" % (url, code))
        raise RuntimeError("找不到 %s 的基金页：%s" % (code, "; ".join(errors[:4])))

    def discover_holdings_urls(self, code):
        """从基金页里找出可能的持仓数据地址（按可能性排序）"""
        page_url, html = self.fund_page(code)
        found, seen = [], set()
        for m in _URL_PAT.finditer(html):
            u = m.group(1)
            if u.startswith("//"):
                u = "https:" + u
            elif u.startswith("/"):
                u = SITE + u
            if u in seen:
                continue
            seen.add(u)
            is_data = bool(_DATA_EXT.search(u))
            is_hint = bool(_HOLDINGS_HINT.search(u))
            is_api = "/api/" in u.lower() or "/graphql" in u.lower()
            if not (is_hint or (is_data and str(code) in u) or (is_api and is_hint)):
                continue
            # 排序分：带 holdings 关键词 + 数据扩展名 + 含基金代码 的排前面
            score = (2 if is_hint else 0) + (2 if is_data else 0) + (1 if str(code) in u else 0)
            found.append((score, u))
        found.sort(key=lambda x: -x[0])
        return page_url, html, [u for _, u in found]

    def fetch_holdings(self, code="3419", urls=None):
        """抓并标准化一份持仓快照。
        :param urls: 显式指定的持仓地址（F12 里抓到的真实 XHR），优先于自动发现
        :return: {"fund","as_of","fetched_at","source_url","rows",...}
        """
        html = ""
        page_url = None
        cands = list(urls or [])
        if cands:
            # 显式给了地址就不必依赖页面发现；页面仍尽量取一份，用于兜底猜 as_of
            try:
                page_url, html = self.fund_page(code)
            except RuntimeError as e:
                print("（基金页取不到，仅用显式地址）%s" % e)
        else:
            page_url, html, cands = self.discover_holdings_urls(code)
        tried = []
        for u in cands:
            try:
                r = self._get(u)
                rows = parse_payload(r.content, r.headers.get("Content-Type", ""), u)
            except Exception as e:
                tried.append("%s -> %s" % (u, e))
                continue
            tried.append("%s -> %d 行" % (u, len(rows)))
            if len(rows) >= 5:
                body_text = decode_bytes(r.content) or xlsx_text_preview(r.content)
                return make_snapshot(code, rows, source_url=u,
                                     as_of=guess_as_of(body_text, html), tried=tried)
        # 兜底：基金页自己可能就把持仓渲染在 HTML 里
        rows = parse_html_table(html) if html else []
        if len(rows) >= 5:
            return make_snapshot(code, rows, source_url=page_url,
                                 as_of=guess_as_of(html), tried=tried)
        raise RuntimeError(
            "未能从官网解析出持仓。候选地址尝试记录：\n  " + "\n  ".join(tried or ["（没找到候选地址）"])
            + "\n提示：官网持仓多为前端 JS 异步加载，请用浏览器 F12 → Network 找到真实 "
              "XHR 地址，或手工下载持仓文件后用 load_local_file() 解析。")


def _is_texty(r):
    ct = (r.headers.get("Content-Type") or "").lower()
    return "text" in ct or "json" in ct or "csv" in ct


_AS_OF_PATS = [
    r"(20\d{2})[-/\.](\d{1,2})[-/\.](\d{1,2})",
    r"(\d{1,2})[-/\.](\d{1,2})[-/\.](20\d{2})",
]


def guess_as_of(*texts):
    """从文件/页面里猜"截至日期"（as of / 截至 / 數據日期 附近的日期）"""
    import datetime
    for text in texts:
        if not text:
            continue
        window = None
        m = re.search(r"(as\s*of|as\s*at|截至|數據日期|数据日期|持倉日期|持仓日期)[^0-9]{0,20}"
                      r"([0-9][0-9\-/\.\s年月日]{6,24})", text, re.I)
        if m:
            window = m.group(2)
        for pat in _AS_OF_PATS:
            mm = re.search(pat, window or text[:6000])
            if not mm:
                continue
            g = [int(x) for x in mm.groups()]
            y, mo, d = (g if g[0] > 1000 else [g[2], g[1], g[0]])
            try:
                return datetime.date(y, mo, d).isoformat()
            except ValueError:
                continue
    return None


def make_snapshot(code, rows, source_url=None, as_of=None, tried=None, index_level=None):
    import datetime
    from ..coverage import classify
    for row in rows:
        row["bucket"] = classify(row)
    tz8 = datetime.timezone(datetime.timedelta(hours=8))
    return {
        "fund": "%s.HK" % str(code).zfill(4),
        "as_of": as_of,
        "fetched_at": datetime.datetime.now(tz8).isoformat(timespec="seconds"),
        "source_url": source_url,
        "index_level": index_level,
        "n_rows": len(rows),
        "rows": rows,
        "discovery_log": tried or [],
    }


def load_local_file(path, code="3419", as_of=None, index_level=None):
    """解析手工下载到本地的持仓文件（csv/xlsx/json/html），流程与线上一致"""
    with open(path, "rb") as f:
        data = f.read()
    rows = parse_payload(data, "", path)
    if not rows:
        raise RuntimeError("未能从 %s 解析出持仓行（表头无法识别？）" % path)
    if as_of is None:
        # 先看文件内容里的 "As of / 截至" 行，再退回文件名里的日期
        text = decode_bytes(data) or xlsx_text_preview(data)
        as_of = guess_as_of(text)
    if as_of is None:
        # 文件名里常带日期，如 3419_holdings_20261031.csv
        m = re.search(r"(20\d{2})[-_\.]?(\d{2})[-_\.]?(\d{2})", os.path.basename(path))
        if m:
            as_of = "%s-%s-%s" % m.groups()
    return make_snapshot(code, rows, source_url="file://" + os.path.abspath(path),
                         as_of=as_of, index_level=index_level)
