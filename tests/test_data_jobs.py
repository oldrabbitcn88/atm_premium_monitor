# -*- coding: utf-8 -*-
"""数据采集作业的回归测试：A股交割日落点（build_data）与恒指补采规划（sync_hsi）

用例直接复现两次真实事故：
- 2026-08-26 北京时间 00:35，push 触发的运行把 08-25 收盘价记成了 08-26 交割日的点
- 计划任务常拖到北京时间次日凌晨才跑（实测最晚 01:47），交割日数据会被记成第二天
- 恒指 09/10 月漏采后，sync_hsi 只看当月，永远补不回来
"""
import datetime as dt
import importlib.util
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bd = _load("build_data", "deploy/actions/build_data.py")
sh = _load("sync_hsi", "deploy/local/sync_hsi.py")

CN = dt.timezone(dt.timedelta(hours=8))


def cn(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=CN)


class TestResolveDataDay(unittest.TestCase):
    def test_incident_0826_push_before_open(self):
        """08-26 00:35 运行，行情还是 08-25 的 → 08-25 不是交割日，不落点"""
        day, note = bd.resolve_data_day("20260825", cn(2026, 8, 26, 0, 35), bd.etf_rule_day)
        self.assertIsNone(day)
        self.assertIn("非交割日", note)

    def test_run_after_midnight_keeps_expiry_date(self):
        """交割日 11-20(周五) 的任务拖到周六 01:47 才跑：仍记为 11-20"""
        day, note = bd.resolve_data_day("20261120", cn(2026, 11, 21, 1, 47), bd.io_rule_day)
        self.assertEqual(day, dt.date(2026, 11, 20))
        self.assertEqual(note, "")

    def test_intraday_run_on_expiry_day_skipped(self):
        day, note = bd.resolve_data_day("20261125", cn(2026, 11, 25, 11, 0), bd.etf_rule_day)
        self.assertIsNone(day)
        self.assertIn("尚未收盘", note)

    def test_evening_run_on_expiry_day(self):
        day, _ = bd.resolve_data_day("20261125", cn(2026, 11, 25, 22, 30), bd.etf_rule_day)
        self.assertEqual(day, dt.date(2026, 11, 25))

    def test_holiday_shift_window(self):
        """2026-06 第三个周五 06-19 为端午休市，顺延到 06-22(周一) 仍在窗口内"""
        day, _ = bd.resolve_data_day("20260622", cn(2026, 6, 22, 21, 0), bd.io_rule_day)
        self.assertEqual(day, dt.date(2026, 6, 22))

    def test_future_quote_date_rejected(self):
        day, note = bd.resolve_data_day("20261121", cn(2026, 11, 20, 22, 0), bd.io_rule_day)
        self.assertIsNone(day)
        self.assertIn("可疑", note)

    def test_missing_date_fallbacks(self):
        # 收盘后：当天
        day, note = bd.resolve_data_day(None, cn(2026, 11, 20, 22, 0), bd.io_rule_day)
        self.assertEqual(day, dt.date(2026, 11, 20))
        self.assertIn("警告", note)
        # 次日凌晨（周六）：上一交易日（周五）
        day, _ = bd.resolve_data_day(None, cn(2026, 11, 21, 1, 47), bd.io_rule_day)
        self.assertEqual(day, dt.date(2026, 11, 20))
        # 盘中：不落点
        day, note = bd.resolve_data_day("", cn(2026, 11, 20, 10, 0), bd.io_rule_day)
        self.assertIsNone(day)


class TestQuoteDateParsing(unittest.TestCase):
    def test_field_30(self):
        f = ["1", "沪深300", "000300", "4170.61"] + ["x"] * 26 + ["20261016150300"] + ["y"] * 5
        self.assertEqual(bd.quote_date_from_fields(f), "20261016")

    def test_scan_fallback_when_position_shifts(self):
        f = ["1", "沪深300", "000300", "4170.61", "20261016150300"] + ["x"] * 40
        self.assertEqual(bd.quote_date_from_fields(f), "20261016")

    def test_no_timestamp(self):
        self.assertIsNone(bd.quote_date_from_fields(["1", "x", "4170.61"]))


class TestPreScreen(unittest.TestCase):
    def test_in_window_recently(self):
        self.assertTrue(bd.in_window_recently(dt.date(2026, 11, 21), bd.io_rule_day))   # 规则日次日
        self.assertFalse(bd.in_window_recently(dt.date(2026, 11, 14), bd.io_rule_day))  # 离 11-20 还早
        self.assertTrue(bd.in_window_recently(dt.date(2026, 11, 25), bd.etf_rule_day))


class TestSseDateCrossCheck(unittest.TestCase):
    def test_mismatch_raises(self):
        quotes = [("500ETF购12月6750", 0.15)]
        with mock.patch.object(bd, "sse_etf_quotes", return_value=(quotes, "20261126")):
            with self.assertRaises(bd.QuoteDateMismatch):
                bd.fetch_etf(6.672, dt.date(2026, 11, 25))

    def test_match_ok(self):
        quotes = [("500ETF购12月6750", 0.15)]
        with mock.patch.object(bd, "sse_etf_quotes", return_value=(quotes, "20261125")):
            strike, price, tcode = bd.fetch_etf(6.672, dt.date(2026, 11, 25))
        self.assertEqual((strike, price), (6.75, 0.15))
        self.assertEqual(tcode, "510500C2612M06750")


class TestHsiPlan(unittest.TestCase):
    HIST = [{"ts": "2026-07-30"}, {"ts": "2026-08-28"}]

    def test_expiry_rule(self):
        self.assertEqual(sh.monthly_expiry_of(2026, 9), dt.date(2026, 9, 29))
        self.assertEqual(sh.monthly_expiry_of(2026, 10), dt.date(2026, 10, 29))
        self.assertEqual(sh.monthly_expiry_of(2026, 11), dt.date(2026, 11, 27))
        self.assertEqual(sh.monthly_expiry_of(2026, 12), dt.date(2026, 12, 30))

    def test_missing_finds_sep_oct(self):
        plan = sh.plan_months(self.HIST, dt.date(2026, 11, 15), missing=True)
        got = {ym: (act, note) for ym, _e, act, note in plan}
        self.assertEqual(got["2026-09"][0], "backfill")
        self.assertIn("约剩 13 天窗口", got["2026-09"][1])     # 合约 10-29 到期，已过 17 天
        self.assertEqual(got["2026-10"][0], "backfill")
        self.assertEqual(got["2026-11"][0], "skip")             # 11-27 未到

    def test_default_is_current_month_only(self):
        plan = sh.plan_months(self.HIST, dt.date(2026, 11, 15))
        self.assertEqual([p[0] for p in plan], ["2026-11"])

    def test_explicit_month_recorded_skips_unless_force(self):
        self.assertEqual(sh.plan_months(self.HIST, dt.date(2026, 11, 15), months=["2026-08"])[0][2], "skip")
        self.assertEqual(sh.plan_months(self.HIST, dt.date(2026, 11, 15), months=["2026-08"],
                                        force=True)[0][2], "backfill")

    def test_realtime_on_expiry_day(self):
        plan = sh.plan_months(self.HIST, dt.date(2026, 11, 27))
        self.assertEqual(plan[0][2], "realtime")

    def test_too_late_points_to_hkex_csv(self):
        plan = sh.plan_months(self.HIST, dt.date(2027, 3, 1), months=["2026-09"])
        self.assertIn("import_hsi_csv.py", plan[0][3])

    def test_parse_args(self):
        a = sh.parse_args(["--month", "2026-09", "--month", "2026-10", "--upload"])
        self.assertEqual(a.month, ["2026-09", "2026-10"])
        self.assertTrue(a.upload)
        with self.assertRaises(SystemExit):
            sh.parse_args(["--month", "2026/09"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
