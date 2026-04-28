# v0.7.37 Rule039/040/041 Validation Pack

What changed:
- Fixed the market-replay default baseline to the real current six-rule set by including Rule038 in defaults.
- Registered Claude candidates Rule039, Rule040, and Rule041 as `candidate_validation` replay rules only.
- Added one integrated 22-preset standalone batch JSON and ready-to-run replay request JSONs.
- Did **not** make any Claude rule live/default.
- Did **not** alter existing promoted rule predicates or execution settings.

Important caution:
Claude's report benchmarked overlap against Rule009/029/033/034/038, but not Rule036B. Treat Rule041's claimed orthogonality as unproven until combined replay confirms it against the real six-rule baseline.
