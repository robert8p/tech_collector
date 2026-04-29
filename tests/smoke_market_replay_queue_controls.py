from unittest.mock import patch
import pandas as pd

from tech_collector import market_replay


def _fake_df():
    return pd.DataFrame([
        {
            "date": "2026-04-01",
            "symbol": "AAA",
            "sector": "Information Technology",
            "scan_time_et": "13:30",
            "minutes_since_open": 240,
            "scan_price": 100.0,
            "spy_ret": -0.02,
            "momentum": -0.01,
            "atr_reach": 5.0,
            "volume_acceleration": 1.5,
            "distance_to_day_low": 0.02,
            "gap_filled": 0,
        },
        {
            "date": "2026-04-01",
            "symbol": "BBB",
            "sector": "Information Technology",
            "scan_time_et": "13:30",
            "minutes_since_open": 240,
            "scan_price": 100.0,
            "spy_ret": -0.02,
            "momentum": -0.01,
            "atr_reach": 5.0,
            "volume_acceleration": 1.5,
            "distance_to_day_low": 0.01,
            "gap_filled": 0,
        },
    ])


def _fake_trade(row, spec, bars, **kwargs):
    entry = kwargs["entry_time_et"]
    exit_time = "13:50" if row["symbol"] == "AAA" else "14:05"
    return {
        "date": row["date"],
        "signal_date": row["date"],
        "symbol": row["symbol"],
        "rule_id": spec.rule_id,
        "entry_time_et": entry,
        "exit_time_et": exit_time,
        "exit_reason": "TIME",
        "net_return_bps": 10.0,
        "queue_wait_minutes": kwargs.get("queue_wait_minutes", 0),
        "portfolio_slot_id": kwargs.get("portfolio_slot_id"),
        "portfolio_entry_reason": kwargs.get("portfolio_entry_reason"),
    }


def main():
    with patch.object(market_replay, "_load_filtered_scan_rows", return_value=_fake_df()), \
         patch.object(market_replay.backtest, "_ensure_raw_bars", return_value=[]), \
         patch.object(market_replay, "_simulate_one_signal_at_entry", side_effect=_fake_trade):
        out = market_replay.run_market_replay(
            start_date="2026-04-01",
            end_date="2026-04-01",
            rule_ids=["rule033_top20", "rule034_conservative_top20"],
            include_virtual_trades=True,
            capital_recycling_enabled=True,
            capital_slots=1,
            max_queue_wait_minutes=10,
            rule_ids_immediate_only=["rule034_conservative_top20"],
            just_in_time_backfill=False,
        )
    diag = out["selection_diagnostics"]
    assert diag["capital_recycling_enabled"] is True
    assert diag["max_queue_wait_minutes"] == 10
    assert diag["rule_ids_immediate_only"] == ["rule034_conservative_top20"]
    reasons = diag.get("rejection_reasons", {})
    assert reasons.get("rule_immediate_only_queue_blocked", 0) == 1, reasons
    print("smoke_market_replay_queue_controls: OK")


if __name__ == "__main__":
    main()
