"""
Tech Collector CLI.

Subcommands:
    backfill  — pull 1-min bars from Alpaca for the configured universe
                over a date range; stores raw bars in SQLite.
    compute   — compute research-schema feature rows from stored raw bars.
    pack      — export an evidence pack (CSV + manifest + summary zip)
                for a date range from computed rows.
    validate  — compare computed rows against a reference research CSV.

Typical workflow (backfill-only static plan):
    export ALPACA_API_KEY=...
    export ALPACA_API_SECRET=...
    python -m tech_collector.cli backfill --start 2024-04-19 --end 2026-04-17
    python -m tech_collector.cli compute  --start 2024-04-19 --end 2026-04-17
    python -m tech_collector.cli validate \\
        --research-csv ./tech_research_dataset.csv
    python -m tech_collector.cli pack     --start 2024-04-19 --end 2026-04-17
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import collector, config, feature_computer, exporter, validate


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s %(levelname)-5s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _cmd_backfill(args: argparse.Namespace) -> int:
    result = collector.collect_range(
        start_date=args.start, end_date=args.end, db_path=args.db_path,
        sector=args.sector,
    )
    print(f"Backfill complete: {result}")
    return 0


def _cmd_compute(args: argparse.Namespace) -> int:
    result = feature_computer.compute_range(
        start_date=args.start, end_date=args.end, db_path=args.db_path,
        sector=args.sector,
    )
    print(f"Compute complete: {result}")
    return 0


def _cmd_pack(args: argparse.Namespace) -> int:
    path = exporter.export_pack(
        start_date=args.start, end_date=args.end,
        out_dir=args.out_dir, db_path=args.db_path,
        sector=args.sector,
    )
    print(f"Pack written: {path}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    report = validate.compare(
        research_csv=args.research_csv,
        db_path=args.db_path,
        sample_size=args.sample,
        sector=args.sector,
    )
    import json
    print(json.dumps(report, indent=2, sort_keys=False))
    bad = [
        f for f, s in report.get("feature_stats", {}).items()
        if (s.get("median_rel_diff_pct") or 0) > 1.0
    ]
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tech Collector (backfill-only)")
    ap.add_argument("--db-path", default=config.DB_PATH)
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # All subcommands take an optional --sector; when omitted, the
    # underlying function falls back to config.DEFAULT_SECTOR. Choices are
    # not constrained here so the error surface comes from
    # universes.get_universe (which prints the valid list).
    def _add_sector(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--sector", default=None,
            help="GICS sector (e.g. 'Information Technology'). Defaults to DEFAULT_SECTOR env var.",
        )

    p = sub.add_parser("backfill", help="pull raw bars from Alpaca")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    _add_sector(p)
    p.set_defaults(func=_cmd_backfill)

    p = sub.add_parser("compute", help="compute feature rows from raw bars")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    _add_sector(p)
    p.set_defaults(func=_cmd_compute)

    p = sub.add_parser("pack", help="export evidence pack zip")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--out-dir", default=config.EVIDENCE_PACK_DIR)
    _add_sector(p)
    p.set_defaults(func=_cmd_pack)

    p = sub.add_parser("validate", help="compare computed rows to research CSV")
    p.add_argument("--research-csv", required=True, type=str)
    p.add_argument("--sample", default=500, type=int)
    _add_sector(p)
    p.set_defaults(func=_cmd_validate)

    args = ap.parse_args(argv)
    _configure_logging(args.log_level)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
