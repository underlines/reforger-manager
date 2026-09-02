"""Live-API integration checks for Phase 3a: real Workshop calls + full
run_mod_sync + enrich_one + resolve against sqlite, using the fixture addons."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(r"Z:/reforger-manager/backend")))

FX = Path(__file__).parent / "fixtures" / "addons"
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {name}")
    else:
        FAIL += 1; print(f"  FAIL {name}  {extra}")


class FakeCtx:
    async def progress(self, *a, **k): pass
    async def log(self, *a, **k): pass
    async def update(self, *a, **k): pass


async def main():
    from app.mods import scanner
    from app.mods.workshop import WorkshopClient, ModNotFound
    from app.mods import sync as sync_mod
    from app.mods.resolve import resolve_dependencies
    from app.core.db import Base
    from app.models import Mod, ModDependency, ModScenario
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    c = WorkshopClient()
    print("== live WorkshopClient ==")
    mod = await c.get_mod("595F2BF2F44836FB")
    check("get_mod unwraps 'mod' key", mod.get("id") == "595F2BF2F44836FB", list(mod)[:5])
    check("get_mod flags present", all(k in mod for k in ("unlisted", "private", "obsolete")), list(mod))
    vers = await c.get_versions("595F2BF2F44836FB")
    check("get_versions -> list", isinstance(vers, list) and len(vers) > 1)
    check("get_versions[0].game_version mapped", "game_version" in vers[0], list(vers[0]))
    deps = await c.get_dependencies("595F2BF2F44836FB")
    check("get_dependencies -> list of {id,name}", deps and deps[0].get("id") and deps[0].get("name"), deps[:1])
    scens = await c.get_scenarios("595F2BF2F44836FB")
    check("get_scenarios mapped keys", scens and "game_id" in scens[0] and "player_count" in scens[0], list(scens[0]))
    try:
        await c.get_mod("658756C5760E94DE")
        check("404 raises ModNotFound", False)
    except ModNotFound:
        check("404 raises ModNotFound", True)
    try:
        await c.get_dependencies("658756C5760E94DE")
        check("404 deps raises ModNotFound", False)
    except ModNotFound:
        check("404 deps raises ModNotFound", True)
    sr = await c.search("RHS", limit=5)
    check("search returns <= limit", 0 < len(sr) <= 5, len(sr))
    check("search rows have id/name", sr and sr[0].get("id") and sr[0].get("name"))
    # cache hit: second get_mod should not increase day_count much; just confirm identical object contents
    mod2 = await c.get_mod("595F2BF2F44836FB")
    check("cache returns equal payload", mod2 == mod)

    print("== full run_mod_sync (fixture dirs + live enrich) ==")
    eng = create_async_engine(os.environ["DATABASE_URL"])
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(eng, expire_on_commit=False)

    # point the scanner + sync at the fixture tree, and the job's sessions at our engine
    scanner_root_orig = scanner.addons_root
    scanner.addons_root = lambda: FX
    sync_mod.scan_all.__globals__  # noqa
    import app.mods.scanner as scn
    scn.addons_root = lambda: FX
    sync_mod.SessionLocal = Session
    c.clear_cache()
    sync_mod.workshop.clear_cache()

    summary = await sync_mod.run_mod_sync(FakeCtx())
    print("   summary:", summary)
    check("scanned 5 fixture mods", summary["scanned"] == 5, summary)
    check("kingmaker enriched ok (>=1)", summary["enriched_ok"] >= 1, summary)
    check("658756C5760E94DE -> not_found (>=1)", summary["not_found"] >= 1, summary)
    check("BadMeta in problems", any(p["dir"].startswith("BadMeta") for p in summary["problems"]))

    async with Session() as s:
        km = await s.get(Mod, "6576A4DF3F71360C")
        check("kingmaker row api_state ok", km.api_state.value == "ok", km.api_state)
        check("kingmaker installed_version from local meta", km.installed_version == "1.0.7", km.installed_version)
        check("kingmaker latest_version from API", bool(km.latest_version), km.latest_version)
        check("kingmaker latest_game_version from API", km.latest_game_version is not None, km.latest_game_version)
        check("kingmaker is_local", km.is_local is True)
        check("kingmaker last_checked set after enrich", km.last_checked is not None)

        edges = (await s.execute(select(ModDependency).where(ModDependency.mod_guid == "6576A4DF3F71360C"))).scalars().all()
        by_guid = {e.depends_on_guid: e for e in edges}
        # gproj list:  69349A75..., 5EB13945..., 58D0FB32..., 69A4D664...
        # api list:    5EB13945..., 61B8FA7B..., 69349A75..., 69A4D664...
        check("api edge got name (dedupe upgrade)", by_guid.get("5EB139459EBF5C16") and by_guid["5EB139459EBF5C16"].source == "api"
              and by_guid["5EB139459EBF5C16"].depends_on_name, by_guid.get("5EB139459EBF5C16"))
        check("gproj-only phantom edge kept as gproj", by_guid.get("58D0FB3206B6F859") and by_guid["58D0FB3206B6F859"].source == "gproj",
              by_guid.get("58D0FB3206B6F859"))
        check("api-only edge added (61B8FA7B...)", by_guid.get("61B8FA7B3BF8656B") and by_guid["61B8FA7B3BF8656B"].source == "api",
              by_guid.get("61B8FA7B3BF8656B"))
        check("one row per (mod,dep) — no dupes", len(edges) == len(by_guid), len(edges))
        check("union size = 5 (4 gproj + 1 api-only)", len(edges) == 5, [e.depends_on_guid for e in edges])

        scen = (await s.execute(select(ModScenario).where(ModScenario.mod_guid == "6576A4DF3F71360C"))).scalars().all()
        named = [x for x in scen if x.name]
        check("kingmaker API scenarios have names", len(named) >= 1, [(x.game_id, x.name) for x in scen])

        blk = await s.get(Mod, "658756C5760E94DE")
        check("Blocked mod api_state not_found", blk.api_state.value == "not_found", blk.api_state)
        check("Blocked mod installed_version from local", blk.installed_version == "9.9.9", blk.installed_version)
        check("Blocked mod keeps gproj deps after 404",
              len((await s.execute(select(ModDependency).where(ModDependency.mod_guid == "658756C5760E94DE"))).scalars().all()) == 2)
        check("Blocked mod latest_* untouched after 404", blk.latest_version is None, blk.latest_version)

        good = await s.get(Mod, "ABCDEF0123456789")
        check("GoodMod transient 503 -> api_state left unchecked", good.api_state.value == "unchecked", good.api_state)
        check("GoodMod keeps gproj deps after transient",
              len((await s.execute(select(ModDependency).where(ModDependency.mod_guid == "ABCDEF0123456789"))).scalars().all()) == 3)

        print("== resolve_dependencies (live API for kingmaker root) ==")
        tree = await resolve_dependencies(s, ["6576A4DF3F71360C"])
        nodes = {n.guid: n for n in tree.nodes}
        check("root resolved via api", nodes["6576A4DF3F71360C"].via == "api", nodes["6576A4DF3F71360C"])
        check("phantom 58D0... unresolved", nodes.get("58D0FB3206B6F859") and nodes["58D0FB3206B6F859"].state == "unresolved",
              nodes.get("58D0FB3206B6F859"))
        check("REAPER_CORE child resolved via api and named", nodes.get("5EB139459EBF5C16") and
              nodes["5EB139459EBF5C16"].via == "api" and nodes["5EB139459EBF5C16"].name, nodes.get("5EB139459EBF5C16"))

    await c.aclose()
    await sync_mod.workshop.aclose()
    await eng.dispose()
    scn.addons_root = scanner_root_orig
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
