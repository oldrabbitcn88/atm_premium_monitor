# -*- coding: utf-8 -*-
"""备兑覆盖比率引擎与持仓解析的单元测试

测试数据为合成样本（tests/fixtures/sample_holdings_3419.csv），
按 Global X 香港持仓文件的常见列名/格式构造，不是真实基金持仓。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.coverage import (  # noqa: E402
    BUCKET_CALL_SHORT, BUCKET_CASH, BUCKET_EQUITY, BUCKET_FUT_LONG, BUCKET_INDEX_ETF,
    bs_call_delta, classify, compute_coverage, hsi_option_expiry, multiplier_for,
    parse_expiry, parse_strike,
)
from backend.data.globalx_adapter import (  # noqa: E402
    load_local_file, map_column, parse_csv_text, parse_number,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_holdings_3419.csv")

# 合成样本的预期分母构成
EQUITY_MV = 6205000 + 8856000 + 4000000      # 19,061,000
ETF_MV = 5200000
FUT_NOTIONAL = 3 * 26100.0 * 50              # 3,915,000（张数×期货价×乘数，不是表里的 MV）
LONG_EQUIV = EQUITY_MV + ETF_MV + FUT_NOTIONAL
IDX = 26000.0


class TestColumnMapping(unittest.TestCase):
    def test_aliases_en_and_zh(self):
        self.assertEqual(map_column("Market Value (HKD)"), "market_value")
        self.assertEqual(map_column("No. of Shares"), "quantity")
        self.assertEqual(map_column("Weight (%)"), "weight")
        self.assertEqual(map_column("持倉量"), "quantity")
        self.assertEqual(map_column("證券名稱"), "name")
        self.assertEqual(map_column("市值（港元）"), "market_value")
        self.assertIsNone(map_column("Totally Unknown Column"))

    def test_parse_number(self):
        self.assertEqual(parse_number("1,234.5"), 1234.5)
        self.assertEqual(parse_number("(1,102,500)"), -1102500.0)
        self.assertAlmostEqual(parse_number("22.02%"), 0.2202)
        self.assertIsNone(parse_number("—"))
        self.assertIsNone(parse_number(""))
        self.assertEqual(parse_number("HK$ 98.40"), 98.40)


class TestContractParsing(unittest.TestCase):
    def test_strike(self):
        self.assertEqual(parse_strike("HANG SENG INDEX CALL OPTION NOV26 C26400"), 26400)
        self.assertEqual(parse_strike("HSI 11/27/26 C26,000"), 26000)
        self.assertEqual(parse_strike("HSIX6C25800"), 25800)
        self.assertIsNone(parse_strike("TENCENT HOLDINGS LTD"))

    def test_expiry(self):
        # 2026-11 最后营业日 11/30(一)，倒数第二个营业日 11/27(五)
        self.assertEqual(hsi_option_expiry(2026, 11).isoformat(), "2026-11-27")
        self.assertEqual(parse_expiry("HSI CALL NOV26 C26400").isoformat(), "2026-11-27")
        self.assertEqual(parse_expiry("HSI 11/27/26 C26000").isoformat(), "2026-11-27")
        self.assertEqual(parse_expiry("option expiring 2026-11-27").isoformat(), "2026-11-27")

    def test_multiplier(self):
        self.assertEqual(multiplier_for("HANG SENG INDEX FUTURE NOV26"), 50.0)
        self.assertEqual(multiplier_for("MINI HANG SENG INDEX FUT MHIX6"), 10.0)
        self.assertEqual(multiplier_for("HSCEI FUTURE HHIX6"), 50.0)


class TestClassify(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(classify({"name": "TENCENT HOLDINGS LTD", "ticker": "700",
                                   "market_value": 1.0}), BUCKET_EQUITY)
        self.assertEqual(classify({"name": "TRACKER FUND OF HONG KONG", "ticker": "2800",
                                   "market_value": 1.0}), BUCKET_INDEX_ETF)
        self.assertEqual(classify({"name": "HANG SENG INDEX FUTURE NOV26",
                                   "asset_class": "Futures", "quantity": 3}), BUCKET_FUT_LONG)
        self.assertEqual(classify({"name": "HANG SENG INDEX CALL OPTION NOV26 C26400",
                                   "asset_class": "Options", "quantity": -21}), BUCKET_CALL_SHORT)
        self.assertEqual(classify({"name": "CASH AND CASH EQUIVALENTS",
                                   "market_value": 1.0}), BUCKET_CASH)

    def test_written_flag_without_negative_qty(self):
        """有些持仓表不给负号，而是在类别里写 Written/賣出"""
        self.assertEqual(classify({"name": "HSI CALL NOV26 C26400",
                                   "asset_class": "Written Options", "quantity": 21}),
                         BUCKET_CALL_SHORT)


class TestCoverage(unittest.TestCase):
    def setUp(self):
        self.snap = load_local_file(FIXTURE, code="3419")

    def test_fixture_parses(self):
        self.assertEqual(self.snap["as_of"], "2026-10-31")   # 文件里的 As of 31/10/2026
        self.assertEqual(self.snap["n_rows"], 7)
        self.assertEqual(self.snap["fund"], "3419.HK")

    def test_denominator_uses_futures_notional_not_market_value(self):
        r = compute_coverage(self.snap, index_level=IDX)
        self.assertAlmostEqual(r["equity_mv"], EQUITY_MV, places=2)
        self.assertAlmostEqual(r["index_etf_mv"], ETF_MV, places=2)
        # 表里期货 MV 只有 15,000（浮动盈亏），名义必须是 3,915,000
        self.assertAlmostEqual(r["futures_notional"], FUT_NOTIONAL, places=2)
        self.assertAlmostEqual(r["long_index_equiv"], LONG_EQUIV, places=2)

    def test_short_call_not_in_denominator(self):
        r = compute_coverage(self.snap, index_level=IDX)
        self.assertLess(r["short_call_mv"], 0)              # 卖出期权是负债
        self.assertNotIn("short", str(r["long_index_equiv"]))
        self.assertAlmostEqual(r["long_index_equiv"], LONG_EQUIV, places=2)

    def test_coverage_ratios(self):
        r = compute_coverage(self.snap, index_level=IDX)
        self.assertEqual(r["short_call_contracts"], 21)
        self.assertAlmostEqual(r["short_call_spot_notional"], 21 * IDX * 50, places=2)
        self.assertAlmostEqual(r["coverage_spot"], 21 * IDX * 50 / LONG_EQUIV, places=6)
        self.assertAlmostEqual(r["coverage_strike"], 21 * 26400 * 50 / LONG_EQUIV, places=6)
        # 价外，故行权价口径 > 现货口径 > delta 口径
        self.assertGreater(r["coverage_strike"], r["coverage_spot"])
        self.assertGreater(r["coverage_spot"], r["coverage_delta_est"])
        self.assertGreater(r["coverage_delta_est"], 0)

    def test_option_structure_fields(self):
        r = compute_coverage(self.snap, index_level=IDX)
        self.assertAlmostEqual(r["wavg_strike"], 26400.0, places=2)
        self.assertAlmostEqual(r["wavg_moneyness"], 26400.0 / IDX - 1.0, places=6)
        self.assertAlmostEqual(r["wavg_dte"], 27.0, places=2)   # 10-31 → 11-27
        self.assertEqual(r["n_call_legs"], 1)
        self.assertEqual(r["n_future_legs"], 1)

    def test_index_level_fallback_to_futures_price(self):
        """不给点位时退化用期货价近似，并留告警"""
        r = compute_coverage(self.snap)
        self.assertEqual(r["index_level_source"], "futures_price_proxy")
        self.assertAlmostEqual(r["index_level"], 26100.0, places=2)
        self.assertTrue(any("期货价格近似" in w for w in r["warnings"]))

    def test_strike_coverage_degrades_when_strike_unknown(self):
        snap = {"as_of": "2026-10-31", "rows": [
            {"name": "TENCENT", "ticker": "700", "market_value": 1000000.0},
            {"name": "HSI CALL OPTION (OTC)", "asset_class": "Options", "quantity": -1},
        ]}
        r = compute_coverage(snap, index_level=IDX)
        self.assertIsNone(r["coverage_strike"])              # 行权价解析不出 → 该口径缺失
        self.assertIsNotNone(r["coverage_spot"])             # 现货口径仍可算
        self.assertTrue(any("行权价解析失败" in w for w in r["warnings"]))

    def test_long_call_offsets_short(self):
        """买入认购（展期/价差腿）应抵减覆盖"""
        snap = {"as_of": "2026-10-31", "rows": [
            {"name": "TENCENT", "ticker": "700", "market_value": 1300000.0},
            {"name": "HSI CALL NOV26 C26400", "asset_class": "Options", "quantity": -2},
            {"name": "HSI CALL NOV26 C27000", "asset_class": "Options", "quantity": 1},
        ]}
        r = compute_coverage(snap, index_level=IDX)
        self.assertEqual(r["short_call_contracts"], 1)
        self.assertAlmostEqual(r["coverage_spot"], 1 * IDX * 50 / 1300000.0, places=6)

    def test_mini_future_multiplier(self):
        snap = {"as_of": "2026-10-31", "rows": [
            {"name": "MINI HANG SENG INDEX FUTURE MHIX6", "asset_class": "Futures",
             "quantity": 10, "price": 26100.0},
        ]}
        r = compute_coverage(snap, index_level=IDX)
        self.assertAlmostEqual(r["futures_notional"], 10 * 26100.0 * 10, places=2)

    def test_short_future_warns(self):
        snap = {"as_of": "2026-10-31", "rows": [
            {"name": "HANG SENG INDEX FUTURE NOV26", "asset_class": "Futures",
             "quantity": -3, "price": 26100.0},
        ]}
        r = compute_coverage(snap, index_level=IDX)
        self.assertTrue(any("期货空头" in w for w in r["warnings"]))
        self.assertAlmostEqual(r["futures_notional"], -3 * 26100.0 * 50, places=2)


class TestDelta(unittest.TestCase):
    def test_bs_delta_monotonic_and_bounded(self):
        atm = bs_call_delta(26000, 26000, 30, 0.20)
        otm = bs_call_delta(26000, 27000, 30, 0.20)
        itm = bs_call_delta(26000, 25000, 30, 0.20)
        self.assertTrue(0 < otm < atm < itm < 1)
        self.assertAlmostEqual(atm, 0.52, places=1)

    def test_expired(self):
        self.assertEqual(bs_call_delta(26000, 25000, 0, 0.2), 1.0)
        self.assertEqual(bs_call_delta(26000, 27000, 0, 0.2), 0.0)


class TestCsvRobustness(unittest.TestCase):
    def test_header_not_on_first_row(self):
        text = ("Some Fund Title\nAs of 31/10/2026\n\n"
                "Security Name,Ticker,Quantity,Market Value\n"
                "TENCENT,700,100,62050\n")
        rows = parse_csv_text(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "TENCENT")
        self.assertEqual(rows[0]["market_value"], 62050.0)

    def test_semicolon_delimiter(self):
        text = "Name;Ticker;Quantity;Market Value\nHSBC;5;100;9840\nTENCENT;700;10;6205\n"
        rows = parse_csv_text(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["ticker"], "700")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestOtherFormats(unittest.TestCase):
    """官网真实接口可能返回 JSON / HTML 表格 / XLSX，解析路径都要通"""

    def test_json_payload(self):
        from backend.data.globalx_adapter import parse_payload
        payload = ('{"data":{"asOfDate":"2026-10-31","holdings":['
                   '{"securityName":"TENCENT HOLDINGS LTD","ticker":"700","quantity":10000,'
                   '"marketValue":6205000,"weight":22.02,"assetClass":"Equity"},'
                   '{"securityName":"HANG SENG INDEX FUTURE NOV26","ticker":"HSIX6","quantity":3,'
                   '"price":26100,"marketValue":15000,"assetClass":"Futures"},'
                   '{"securityName":"HSI CALL NOV26 C26400","ticker":"HSIX6C26400","quantity":-21,'
                   '"marketValue":-1102500,"assetClass":"Options"}]}}')
        rows = parse_payload(payload.encode("utf-8"), "application/json", "x.json")
        self.assertEqual(len(rows), 3)
        r = compute_coverage({"as_of": "2026-10-31", "rows": rows}, index_level=IDX)
        self.assertAlmostEqual(r["futures_notional"], 3 * 26100 * 50, places=2)
        self.assertEqual(r["short_call_contracts"], 21)
        self.assertIsNotNone(r["coverage_spot"])

    def test_html_table_payload(self):
        from backend.data.globalx_adapter import parse_payload
        html = """<html><body><p>As of 31/10/2026</p><table>
        <tr><th>Security Name</th><th>Ticker</th><th>No. of Shares</th><th>Market Value (HKD)</th></tr>
        <tr><td>TENCENT HOLDINGS LTD</td><td>700</td><td>10,000</td><td>6,205,000</td></tr>
        <tr><td>HSBC HOLDINGS PLC</td><td>5</td><td>90,000</td><td>8,856,000</td></tr>
        <tr><td>HSI CALL OPTION NOV26 C26400</td><td>HSIX6C26400</td><td>-21</td><td>(1,102,500)</td></tr>
        </table></body></html>"""
        rows = parse_payload(html, "text/html", "fund.html")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["market_value"], 6205000.0)
        self.assertEqual(rows[2]["quantity"], -21.0)

    def test_as_of_from_html(self):
        from backend.data.globalx_adapter import guess_as_of
        self.assertEqual(guess_as_of("<p>Holdings as of 31/10/2026</p>"), "2026-10-31")
        self.assertEqual(guess_as_of("持倉截至 2026-10-31"), "2026-10-31")
        self.assertEqual(guess_as_of("數據日期：2026/10/31"), "2026-10-31")

    @unittest.skipUnless(__import__("importlib").util.find_spec("openpyxl"), "需要 openpyxl")
    def test_xlsx_payload(self):
        import io
        from openpyxl import Workbook
        from backend.data.globalx_adapter import parse_payload, xlsx_text_preview
        wb = Workbook()
        ws = wb.active
        ws.append(["Global X HSI Covered Call Active ETF"])
        ws.append(["As of 31/10/2026"])
        ws.append([])
        ws.append(["Security Name", "Ticker", "Quantity", "Price", "Market Value (HKD)"])
        ws.append(["TENCENT HOLDINGS LTD", "700", 10000, 620.5, 6205000])
        ws.append(["HANG SENG INDEX FUTURE NOV26", "HSIX6", 3, 26100, 15000])
        ws.append(["HSI CALL NOV26 C26400", "HSIX6C26400", -21, 1050, -1102500])
        buf = io.BytesIO()
        wb.save(buf)
        data = buf.getvalue()
        rows = parse_payload(data, "", "h.xlsx")
        self.assertEqual(len(rows), 3)
        self.assertEqual(guess_as_of_safe(xlsx_text_preview(data)), "2026-10-31")
        r = compute_coverage({"as_of": "2026-10-31", "rows": rows}, index_level=IDX)
        self.assertAlmostEqual(r["futures_notional"], 3 * 26100 * 50, places=2)


def guess_as_of_safe(text):
    from backend.data.globalx_adapter import guess_as_of
    return guess_as_of(text)
