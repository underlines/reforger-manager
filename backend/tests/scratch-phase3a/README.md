# scratch-phase3a — REFERENCE ONLY, not a working suite

These three scripts were written by the Phase 3a implementer and run **green** in a throwaway
virtualenv during that session. They are kept here as a starting point, NOT as a runnable suite:

- they use an ad-hoc `check()` harness, not pytest;
- they `sys.path.insert(0, "Z:/reforger-manager/backend")` — adjust to your layout / use `pytest` rootdir;
- they expect a `fixtures/addons/` directory of sample mod dirs next to the script that was NOT
  preserved. Rebuild it from `docs/mod-fixtures/` (real `meta` / `addon.gproj` / `ServerData.json`
  samples) — lay them out as `fixtures/addons/<Name_GUID>/{meta,addon.gproj,ServerData.json,resourceDatabase.rdb}`.
- `test_phase3a_live.py` hits the real `api.reforgermods.net` — network required, rate-limited.

**Action for the next agent:** fold these into a proper `backend/tests/` pytest suite (add
`pytest` + `pytest-asyncio` to `requirements.txt` or a `requirements-dev.txt`), restore fixtures,
and extend with Phase 3b coverage (preflight/diagnosis against `docs/log-fixtures/`).
