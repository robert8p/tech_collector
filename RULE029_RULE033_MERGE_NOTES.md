# v0.7.27 merge notes — Rule033 base plus Rule029 live shadow

Base used: `tech_collector_v0_7_26_RULE033_LIVE_SHADOW.zip`.

Added Rule029 without removing Rule033:

- `POST /rule029/shadow/run` endpoint.
- Dashboard button: run Rule029 shadow now.
- Dashboard preset: load/run Rule029 top3.
- Presets under `presets/rule029_live_shadow/`.
- Notes in `RULE029_LIVE_SHADOW_NOTES.md`.

Preserved from v0.7.26:

- Rule033 endpoint: `POST /rule033/shadow/run`.
- Rule033 UI controls.
- Rule033 presets and promotion notes.
- Rule009 live-shadow monitor.

Post-deploy smoke checks:

1. Open `/health` and confirm version `0.7.27`.
2. Open dashboard and confirm Rule009, Rule029, and Rule033 shadow panels are visible.
3. Use the Rule029 button after a 10:30 Technology scan exists; download the generated `rule029_shadow_*.zip`.
4. Use the Rule033 button after a 13:30 Technology scan exists; download the generated `rule033_shadow_*.zip`.
5. Return both packs after two mature qualifying sessions.
