"""
Alpaca market data collector.

Pulls 1-minute bars for the configured universe over a date range. Uses the
alpaca-py SDK. Writes raw bars to SQLite via storage.insert_bars.

Design:
- One pass per symbol across the full date range (fewer API calls than
  per-day per-symbol).
- Rate limiting is conservative; can be raised by changing config.
- Failures on individual symbols are logged and skipped, not fatal.
- Idempotent: INSERT OR REPLACE means re-running over the same range
  overwrites rather than duplicates.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

from . import config, storage
from .universes import get_universe

logger = logging.getLogger(__name__)


class AlpacaCredentialsError(RuntimeError):
    """Raised when ALPACA_API_KEY / ALPACA_API_SECRET are missing."""


def _get_client():
    """Import alpaca-py lazily so tests and smoke-checks can import this
    module without the SDK installed."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
    except ImportError as e:
        import sys
        raise ImportError(
            f"alpaca-py is not importable in this environment. "
            f"Python: {sys.executable} ({sys.version.split()[0]}). "
            f"Check Render build logs for 'Successfully installed alpaca-py'. "
            f"Original error: {e}"
        ) from e

    key = os.environ.get(config.ALPACA_API_KEY_ENV)
    secret = os.environ.get(config.ALPACA_API_SECRET_ENV)
    if not key or not secret:
        raise AlpacaCredentialsError(
            f"Set {config.ALPACA_API_KEY_ENV} and "
            f"{config.ALPACA_API_SECRET_ENV} in the environment."
        )
    return StockHistoricalDataClient(key, secret)


def _bars_request(symbols: list[str], start: datetime, end: datetime):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    return StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed=config.ALPACA_FEED,  # 'sip'
        adjustment="split",  # split-adjusted matches R2K scanner and original research
    )


def _bar_to_dict(symbol: str, bar) -> dict:
    """Convert an alpaca-py Bar object to a dict row."""
    return {
        "symbol": symbol,
        "timestamp_utc": bar.timestamp.astimezone(timezone.utc).isoformat(),
        "open": float(bar.open),
        "high": float(bar.high),
        "low": float(bar.low),
        "close": float(bar.close),
        "volume": int(bar.volume),
        "vwap": float(bar.vwap) if bar.vwap is not None else None,
        "trade_count": int(bar.trade_count) if bar.trade_count is not None else None,
    }


def collect_range(
    start_date: str,
    end_date: str,
    symbols: Iterable[str] | None = None,
    db_path: str = config.DB_PATH,
    sector: str | None = None,
) -> dict:
    """Pull 1-minute bars for the configured universe across the date range.

    Dates are inclusive. Times passed to Alpaca are interpreted as UTC; the
    SDK handles conversion from market hours.

    `sector` selects one of the 11 GICS sectors (see universes.py). If None,
    falls back to config.DEFAULT_SECTOR. The resolved sector label is
    stamped on every inserted raw_bar row.

    If `symbols` is passed, it overrides the sector's universe entirely
    (useful for one-off pulls). The sector label is still attached to the
    written rows for provenance.

    Returns {'rows', 'errors', 'symbols_done', 'sector'}.
    """
    client = _get_client()
    resolved_sector = sector or config.DEFAULT_SECTOR
    # Validate sector and resolve universe (raises KeyError on bad input)
    universe = get_universe(resolved_sector)
    symbols = list(symbols) if symbols else list(universe)
    # Always pull SPY too — needed for R2K SPY-relative features. SPY gets
    # the same sector label as the current pull; last write wins if the
    # same SPY bar is pulled again under a different sector, which is fine
    # because sector on raw_bars is descriptive metadata not a constraint.
    if "SPY" not in symbols:
        symbols = symbols + ["SPY"]
    start_dt = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    # Alpaca's `end` parameter is exclusive; adding one day ensures the
    # full end_date (and its 16:00 ET close) is included.
    end_dt = (
        datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
        + timedelta(days=1)
    )

    pulled_at = datetime.now(timezone.utc).isoformat()
    storage.init_schema(db_path)

    total_rows = 0
    errors = 0
    with storage.connect(db_path) as conn:
        run_id = storage.log_run_start(
            conn, mode="backfill",
            start_date=start_date, end_date=end_date,
            symbols_n=len(symbols),
            started_at_utc=pulled_at,
        )

        # Batch symbols — Alpaca accepts multiple symbols per request
        batch_size = min(config.MAX_SYMBOLS_PER_REQUEST, len(symbols))
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            logger.info(
                f"Fetching {len(batch)} symbols for {resolved_sector}: "
                f"{batch[0]}..{batch[-1]}"
            )
            try:
                req = _bars_request(batch, start_dt, end_dt)
                bars_response = client.get_stock_bars(req)
                # BarSet.data is dict[symbol -> list[Bar]]
                for sym, bars in bars_response.data.items():
                    rows = [_bar_to_dict(sym, b) for b in bars]
                    if rows:
                        n = storage.insert_bars(
                            conn, rows, feed=config.ALPACA_FEED,
                            pulled_at_utc=pulled_at,
                            sector=resolved_sector,
                        )
                        total_rows += n
                        logger.info(f"  {sym}: {n} bars")
                    else:
                        logger.warning(f"  {sym}: no bars returned")
            except Exception as e:
                errors += 1
                logger.error(f"Batch failed ({batch[0]}..): {e}")

            # Simple rate limit
            time.sleep(1.0 / config.REQUESTS_PER_SECOND)

        storage.log_run_finish(
            conn, run_id,
            finished_at_utc=datetime.now(timezone.utc).isoformat(),
            rows_written=total_rows,
            errors_n=errors,
            notes=(
                f"backfill {start_date}..{end_date} for {len(symbols)} "
                f"symbols (sector={resolved_sector})"
            ),
        )

    return {
        "rows": total_rows,
        "errors": errors,
        "symbols_done": len(symbols) - errors,
        "sector": resolved_sector,
    }
