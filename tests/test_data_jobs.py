# -*- coding: utf-8 -*-
"""数据采集作业的回归测试：A股交割日落点（build_data，main 上的实现）与恒指补采规划（sync_hsi）

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
ho = _load("hsi_official", "deploy/local/hsi_official.py")

CN = dt.timezone(dt.timedelta(hours=8))


def cn(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=CN)


class TestSessionOf(unittest.TestCase):
    """main 上 build_data 的行情日期判定（session_of）"""

    def test_closed_session(self):
        self.assertEqual(bd.session_of("20261016150300"), (dt.date(2026, 10, 16), True))

    def test_intraday(self):
        self.assertEqual(bd.session_of("20261125103000"), (dt.date(2026, 11, 25), False))

    def test_unparseable(self):
        for ts in ("", None, "2026-10-16", "abc"):
            self.assertEqual(bd.session_of(ts), (None, False))


class TestCollect(unittest.TestCase):
    """用真实事故场景验证 main 上的 collect()：落点日期取行情日，而非运行日"""

    def _run(self, qts, rule_fn=None, price=6.70):
        items = {"K": {"history": [{"ts": "2026-10-28"}]}}
        report = []
        fetch = mock.Mock(return_value=(6.75, 0.15, "CODE"))
        with mock.patch.object(bd, "qq_price", return_value=(price, qts)):
            bd.collect(items, report, "K", "sh510500", rule_fn or bd.etf_rule_day, fetch)
        return items["K"]["history"], report, fetch

    def test_incident_0826_push_before_open(self):
        """08-26 凌晨 push 触发时行情仍是 08-25 的 → 不是交割日，不落点"""
        hist, report, fetch = self._run("20260825150300")
        self.assertEqual(len(hist), 1)
        fetch.assert_not_called()
        self.assertIn("非交割日", report[0])

    def test_run_after_midnight_keeps_expiry_date(self):
        """交割日 11-20(周五) 的任务拖到周六凌晨才跑：行情日仍是 11-20"""
        hist, report, fetch = self._run("20261120150300", rule_fn=bd.io_rule_day)
        self.assertEqual(hist[-1]["ts"], "2026-11-20")
        self.assertEqual(fetch.call_args[0][1], dt.date(2026, 11, 20))   # 按行情日取下月合约
        self.assertEqual(hist[-1]["expiry"], "2026-12")

    def test_intraday_on_expiry_day_not_recorded(self):
        hist, report, fetch = self._run("20261125103000")
        self.assertEqual(len(hist), 1)
        self.assertIn("尚未收盘", report[0])

    def test_evening_on_expiry_day_recorded(self):
        hist, _r, _f = self._run("20261125150300")
        self.assertEqual(hist[-1]["ts"], "2026-11-25")
        self.assertAlmostEqual(hist[-1]["premium_yield"], round(0.15 / 6.70, 6))

    def test_holiday_shift_window(self):
        """2026-06 第三个周五 06-19 端午休市，顺延到 06-22(周一) 仍在窗口内"""
        hist, _r, _f = self._run("20260622150300", rule_fn=bd.io_rule_day)
        self.assertEqual(hist[-1]["ts"], "2026-06-22")

    def test_unparseable_timestamp_not_recorded(self):
        hist, report, fetch = self._run("")
        self.assertEqual(len(hist), 1)
        fetch.assert_not_called()
        self.assertIn("无法解析", report[0])


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
        self.assertIn("到期 17 天", got["2026-09"][1])         # 合约 10-29 到期，已过 17 天
        self.assertEqual(got["2026-10"][0], "backfill")
        self.assertEqual(got["2026-11"][0], "skip")             # 11-27 未到

    def test_missing_finds_hole_in_middle(self):
        """main 上的真实现状：有 08-28、10-29，缺 09 —— 中间空洞也要能扫到"""
        hist = [{"ts": "2026-08-28"}, {"ts": "2026-10-29"}]
        plan = {ym: act for ym, _e, act, _n in
                sh.plan_months(hist, dt.date(2026, 11, 15), missing=True)}
        self.assertEqual(plan["2026-09"], "backfill")
        self.assertEqual(plan["2026-10"], "skip")      # 已记录
        self.assertEqual(plan["2026-11"], "skip")      # 未到
        self.assertNotIn("2026-07", plan)              # 超出 4 个月回看

    def test_missing_skips_months_beyond_futu_window(self):
        """自动模式下超窗口的空洞直接跳过，免得计划任务天天报失败；显式指定仍会尝试"""
        hist = [{"ts": "2026-08-28"}, {"ts": "2026-10-29"}]
        auto = {ym: (a, n) for ym, _e, a, n in
                sh.plan_months(hist, dt.date(2026, 12, 15), missing=True)}
        self.assertEqual(auto["2026-09"][0], "skip")   # 所需合约 10-29 到期已 47 天
        self.assertIn("港交所日报", auto["2026-09"][1])
        explicit = sh.plan_months(hist, dt.date(2026, 12, 15), months=["2026-09"])
        self.assertEqual(explicit[0][2], "backfill")

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

    def test_too_late_points_to_hkex_report(self):
        plan = sh.plan_months(self.HIST, dt.date(2027, 3, 1), months=["2026-09"])
        self.assertIn("港交所日报", plan[0][3])

    def test_parse_args(self):
        a = sh.parse_args(["--month", "2026-09", "--month", "2026-10", "--upload"])
        self.assertEqual(a.month, ["2026-09", "2026-10"])
        self.assertTrue(a.upload)
        with self.assertRaises(SystemExit):
            sh.parse_args(["--month", "2026/09"])


class HsiOfficialTest(unittest.TestCase):
    """恒指以港交所日报 O.Q.P. 为准：解析与交割日推算（不联网）"""

    # 节选自 2026-10-29 港交所恒指期权日报：| 后第二段为日间时段，第 4 列是 O.Q.P.
    DMR = "\n".join([
        "NOV-26  22800 C      620     640     610     630      12  |     600     660     560     580      -60   16         90  |   1 2 3 4 5",
        "NOV-26  23000 C      500     511     500     505      10  |     490     535     425     449      -58   16         57  |      2029       425         67       723       +24",
        "NOV-26  23000 P      300     310     290     300       5  |     320     340     300     335       40   17         20  |   1 2 3 4 5",
        "DEC-26  23000 C      700     720     690     710       3  |     690     730     650     680      -40   17         11  |   1 2 3 4 5",
    ])

    def test_settle_price_picks_month_strike_and_call(self):
        self.assertEqual(ho.settle_price(self.DMR, 2026, 11, 23000), (449.0, "16"))
        self.assertEqual(ho.settle_price(self.DMR, 2026, 12, 23000), (680.0, "17"))
        self.assertIsNone(ho.settle_price(self.DMR, 2026, 11, 23200))

    def test_expiry_is_second_last_trading_day(self):
        closes = {dt.date(2026, 10, d): 1.0 for d in (26, 27, 28, 29, 30)}
        self.assertEqual(ho.expiry_day(2026, 10, closes, today=dt.date(2026, 11, 15)), dt.date(2026, 10, 29))

    def test_expiry_respects_month_end_holiday(self):
        # 月末最后一个工作日休市（日线里没有）→ 交割日前移一天
        closes = {dt.date(2024, 3, d): 1.0 for d in (25, 26, 27, 28)}   # 3/29 耶稣受难节
        self.assertEqual(ho.expiry_day(2024, 3, closes, today=dt.date(2024, 4, 10)), dt.date(2024, 3, 27))

    def test_expiry_pads_future_weekdays_in_current_month(self):
        closes = {dt.date(2026, 11, d): 1.0 for d in (12, 13)}           # 月中，之后尚未发生
        self.assertEqual(ho.expiry_day(2026, 11, closes, today=dt.date(2026, 11, 15)), dt.date(2026, 11, 27))


if __name__ == "__main__":
    unittest.main(verbosity=2)
