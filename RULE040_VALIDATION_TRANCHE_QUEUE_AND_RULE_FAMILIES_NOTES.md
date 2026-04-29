# v0.7.39 validation tranche patch

This patch is intentionally narrow.

Added replay-only capabilities:
- `max_queue_wait_minutes` for capital-recycling market replay.
- `rule_ids_immediate_only` for replay-only immediate-open handling on selected rule ids.
- Validation-only registry entries for the 12 requested filter variants and 9 requested Rule042/043/044 candidates.

Not changed:
- No live trading enabled.
- No default active rule set changed.
- No existing promoted rule logic changed.
- No Render deployment semantics changed.

Operator intent:
- Run Stage 2 standalone batch using the provided 21-preset JSON batch.
- Run Stage 3/4 replay diagnostics using the provided replay request ZIP.
- Upload the resulting replay/backtest evidence ZIPs back for promotion/rejection review.
