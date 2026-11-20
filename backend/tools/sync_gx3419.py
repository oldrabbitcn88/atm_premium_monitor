"""Import the canonical 3419 coverage series and enrich it from archived snapshots."""
import argparse
import csv
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from backend.coverage import compute_coverage
from backend.data.globalx_adapter import load_local_file
from backend.tools.gx_coverage import CSV_COLUMNS, DEFAULT_DATA_DIR

UPSTREAM = "oldrabbitcn88/3419-covered-call-monitor"


def import_day(source, day, previous):
    date = dt.date.fromisoformat(day["date"]).isoformat()
    details = json.loads((source / "details" / f"{date}.json").read_text(encoding="utf-8"))
    index = Decimal(day["hsi_close"])
    if details["date"] != date or Decimal(details["index_close"]) != index:
        raise ValueError(f"{date}: coverage and option details disagree on date or index close")

    official = Decimal(day["short_call_notional_hkd"])
    option_table = [
        {
            "notional": float(Decimal(row[1].replace("HKD$", "").replace(",", "").strip())),
            "notional_pct_nav": float(Decimal(row[2].strip().removesuffix("%")) / 100),
        }
        for row in details["option_details"]
    ]
    reported = sum(Decimal(str(row["notional"])) for row in option_table)
    if not option_table or official <= 0 or reported != -official:
        raise ValueError(f"{date}: official option notional does not match coverage.csv")

    holdings = source / "holdings" / f"{date}.csv"
    snapshot = load_local_file(str(holdings), as_of=date, index_level=float(index))
    snapshot.update(source_url=day["source"], option_table=option_table)
    fund_facts = {}
    if previous.get("nav_total") is not None:
        fund_facts["nav_total"] = previous["nav_total"]
    if previous.get("cash_pct_nav") is not None:
        fund_facts["cash_pct"] = previous["cash_pct_nav"]
    snapshot["fund_facts"] = fund_facts
    result = compute_coverage(snapshot)

    for field, column in (
        ("equity_mv", "equity_hkd"),
        ("futures_notional", "futures_notional_hkd"),
        ("short_call_contracts", "option_contracts"),
    ):
        if Decimal(str(result[field])) != Decimal(day[column]):
            raise ValueError(f"{date}: archived holdings disagree with {column}")

    long_notional = Decimal(day["equity_hkd"]) + Decimal(day["futures_notional_hkd"])
    coverage = Decimal(day["coverage_pct"]) / 100
    if long_notional <= 0 or (official / long_notional).quantize(Decimal("0.000001")) != coverage:
        raise ValueError(f"{date}: inconsistent upstream coverage ratio")
    implied = Decimal(str(result["short_call_spot_notional"]))
    if abs(implied - official) > max(Decimal("1"), official * Decimal("0.0001")):
        raise ValueError(f"{date}: option notional differs from archived holdings")

    result.update(
        coverage_spot=float(coverage),
        short_call_spot_notional=float(official),
        long_index_equiv=float(long_notional),
        index_level_source="upstream",
    )
    return result


def sync(source, output):
    json_path = output / "coverage.json"
    csv_path = output / "coverage.csv"
    previous = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else {}
    series = {row["as_of"]: row for row in previous.get("series", [])}
    imported = set()
    with (source / "coverage.csv").open(encoding="utf-8-sig", newline="") as handle:
        for day in csv.DictReader(handle):
            date = day["date"]
            if date in imported:
                raise ValueError(f"{date}: duplicate upstream observation")
            series[date] = import_day(source, day, series.get(date, {}))
            imported.add(date)

    missing = set(previous.get("upstream_dates", [])) - imported
    if not imported or missing:
        raise ValueError(f"Upstream series is empty or missing previously imported dates: {sorted(missing)}")

    ordered = [series[date] for date in sorted(series)]
    upstream_dates = sorted(imported)
    if (
        previous.get("series") == ordered
        and previous.get("upstream_dates") == upstream_dates
        and csv_path.exists()
    ):
        print(f"3419 unchanged ({len(imported)} upstream observations, latest {max(imported)})")
        return

    output.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds"),
        "note": "备兑覆盖比率 = 卖出认购期权标的名义 / (正股市值+指数ETF市值+指数期货多头名义)",
        "upstream_repository": UPSTREAM,
        "upstream_dates": upstream_dates,
        "series": ordered,
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ordered)
    print(f"3419 synchronized ({len(imported)} upstream observations, latest {max(imported)})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="data/ directory from the 3419 monitor")
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_DATA_DIR))
    args = parser.parse_args()
    sync(args.source, args.output)
