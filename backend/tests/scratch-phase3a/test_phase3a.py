"""Phase 3a parser / client / resolver checks. Run with the scratchpad venv."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(r"Z:/reforger-manager/backend")))

FX = Path(__file__).parent / "fixtures" / "addons"

from app.mods import scanner  # noqa: E402
from app.mods.workshop import (  # noqa: E402
    WorkshopClient, _RateLimiter, _convert, _extract_list, _camel_to_snake,
)

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


print("== scanner.parse_meta ==")
m = scanner.parse_meta(FX / "GoodMod_ABCDEF0123456789")
check("id", m["id"] == "ABCDEF0123456789", m)
check("version from versions[0].version", m["version"] == "2.3.4", m)
check("size from versions[0].package.totalSize", m["size"] == 123456789, m)
check("tags", m["tags"] == ["SYSTEMS", "VEHICLES"], m)
check("unlisted false", m["unlisted"] is False, m)
check("summary", m["summary"] == "A good mod.", m)

mk = scanner.parse_meta(FX / "REAPER_Kingmaker_6576A4DF3F71360C")
check("real kingmaker meta id", mk["id"] == "6576A4DF3F71360C", mk)
check("real kingmaker version", mk["version"] == "1.0.7", mk)
check("real kingmaker size int", isinstance(mk["size"], int) and mk["size"] > 0, mk)

check("missing meta -> None", scanner.parse_meta(FX / "saves") is None)
try:
    scanner.parse_meta(FX / "BadMeta_DDDDDDDDDDDDDDDD")
    check("bad meta raises", False)
except ValueError:
    check("bad meta raises ValueError", True)

print("== scanner.parse_gproj ==")
g = scanner.parse_gproj(FX / "GoodMod_ABCDEF0123456789")
check("gproj guid", g["guid"] == "ABCDEF0123456789", g)
check("gproj id", g["id"] == "Good_Mod", g)
check("gproj title", g["title"] == "Good Mod", g)
check("gproj multiline deps", g["dep_guids"] == ["1111111111111111", "2222222222222222", "3333333333333333"], g)

gk = scanner.parse_gproj(FX / "REAPER_Kingmaker_6576A4DF3F71360C")
check("real kingmaker gproj deps (4, incl phantom 58D0...)",
      gk["dep_guids"] == ["69349A7537394F8D", "5EB139459EBF5C16", "58D0FB3206B6F859", "69A4D664A6284E06"], gk)

check("missing gproj -> empty deps", scanner.parse_gproj(FX / "BareMod_BBBBBBBBBBBBBBBB")["dep_guids"] == [])
check("empty gproj -> empty deps", scanner.parse_gproj(FX / "EmptyGproj_CCCCCCCCCCCCCCCC")["dep_guids"] == [])

print("== scanner.parse_scenarios ==")
sc = scanner.parse_scenarios(FX / "GoodMod_ABCDEF0123456789", "ABCDEF0123456789")
paths = [p for _gid, p in sc]
check("scenario paths found + deduped", paths == ["Missions/Alpha.conf", "Missions/sub/Bravo.conf"], sc)
check("game_id built as {guid}path", sc[0][0] == "{ABCDEF0123456789}Missions/Alpha.conf", sc)
check("Configs/ path excluded", "Configs/Nope.conf" not in paths, sc)

sck = scanner.parse_scenarios(FX / "REAPER_Kingmaker_6576A4DF3F71360C", "6576A4DF3F71360C")
kpaths = [p for _g, p in sck]
check("real kingmaker rdb -> Missions/REAPER_Kingmaker.conf", kpaths == ["Missions/REAPER_Kingmaker.conf"], sck)
check("real kingmaker game_id", sck[0][0] == "{6576A4DF3F71360C}Missions/REAPER_Kingmaker.conf", sck)

check("missing rdb -> []", scanner.parse_scenarios(FX / "BareMod_BBBBBBBBBBBBBBBB", "BBBBBBBBBBBBBBBB") == [])

print("== scanner.parse_serverdata / has_thumbnail ==")
sd = scanner.parse_serverdata(FX / "REAPER_Kingmaker_6576A4DF3F71360C")
check("serverdata id", sd["id"] == "6576A4DF3F71360C", sd)
check("serverdata revision.version", sd["version"] == "1.0.7", sd)
check("missing serverdata -> None", scanner.parse_serverdata(FX / "BareMod_BBBBBBBBBBBBBBBB") is None)
check("has_thumbnail true", scanner.has_thumbnail(FX / "GoodMod_ABCDEF0123456789") is True)
check("has_thumbnail false", scanner.has_thumbnail(FX / "BareMod_BBBBBBBBBBBBBBBB") is False)

print("== scanner.scan_all ==")
res = scanner.scan_all(FX)
guids = sorted(x.guid for x in res.mods)
check("scanned mods (saves/ skipped, BadMeta in problems)",
      guids == ["6576A4DF3F71360C", "ABCDEF0123456789", "BBBBBBBBBBBBBBBB", "CCCCCCCCCCCCCCCC"], guids)
check("problems has BadMeta", any(p["dir"] == "BadMeta_DDDDDDDDDDDDDDDD" for p in res.problems), res.problems)
good = next(x for x in res.mods if x.guid == "ABCDEF0123456789")
check("ScannedMod.dep_guids", good.dep_guids == ["1111111111111111", "2222222222222222", "3333333333333333"])
check("ScannedMod.scenarios", good.scenarios[0][1] == "Missions/Alpha.conf")
check("ScannedMod.has_thumbnail", good.has_thumbnail is True)
cmod = next(x for x in res.mods if x.guid == "CCCCCCCCCCCCCCCC")
check("ScannedMod is_unlisted from local", cmod.is_unlisted is True)
check("ScannedMod is_deleted_local from local", cmod.is_deleted_local is True)

print("== workshop camelCase -> snake_case ==")
check("gameMode", _camel_to_snake("gameMode") == "game_mode")
check("playerCount", _camel_to_snake("playerCount") == "player_count")
check("gameVersion", _camel_to_snake("gameVersion") == "game_version")
check("gameId", _camel_to_snake("gameId") == "game_id")
check("workshopUrl", _camel_to_snake("workshopUrl") == "workshop_url")
check("apiUrl", _camel_to_snake("apiUrl") == "api_url")
check("modId", _camel_to_snake("modId") == "mod_id")
check("sizeFormatted", _camel_to_snake("sizeFormatted") == "size_formatted")
conv = _convert({"gameId": "x", "nested": [{"playerCount": 5, "gameMode": "co"}]})
check("recursive convert", conv == {"game_id": "x", "nested": [{"player_count": 5, "game_mode": "co"}]}, conv)

print("== workshop _extract_list (real envelope shapes) ==")
vers = _convert({"status": "success", "data": {"modId": "x", "count": 2,
                 "versions": [{"version": "1", "gameVersion": "1.8"}, {"version": "0"}]}})
vl = _extract_list(vers, "versions")
check("versions list len", len(vl) == 2, vl)
check("versions game_version mapped", vl[0]["game_version"] == "1.8", vl)
deps = _convert({"status": "s", "data": {"dependencies": [{"id": "A", "name": "n"}]}})
check("deps extract", _extract_list(deps, "dependencies") == [{"id": "A", "name": "n"}])
scen = _convert({"status": "s", "data": {"scenarios": [{"gameId": "{G}Missions/x.conf", "gameMode": "co", "playerCount": 8}]}})
sl = _extract_list(scen, "scenarios")
check("scenarios mapped", sl[0] == {"game_id": "{G}Missions/x.conf", "game_mode": "co", "player_count": 8}, sl)
check("bare list passthrough", _extract_list([{"a": 1}], "versions") == [{"a": 1}])
check("empty on junk", _extract_list({"nope": 1}, "versions") == [])


async def _rate_test():
    print("== rate limiter (burst then pace) ==")
    rl = _RateLimiter(per_minute=60, burst=3, per_day=1000)
    import time
    t0 = time.monotonic()
    for _ in range(3):
        await rl.acquire()
    burst_dt = time.monotonic() - t0
    check("burst of 3 is ~instant", burst_dt < 0.2, f"{burst_dt:.3f}s")
    t1 = time.monotonic()
    await rl.acquire()  # 4th -> must wait ~1s (refill 1/s)
    pace_dt = time.monotonic() - t1
    check("4th call paced ~1s", 0.7 < pace_dt < 1.6, f"{pace_dt:.3f}s")

    rl2 = _RateLimiter(per_minute=6000, burst=1, per_day=2)
    await rl2.acquire()
    await rl2.acquire()
    try:
        await rl2.acquire()
        check("daily cap raises", False)
    except Exception as e:
        check("daily cap raises RateLimitExceeded", type(e).__name__ == "RateLimitExceeded", e)


async def _resolve_test():
    print("== resolve_dependencies (sqlite, offline: use_api=False) ==")
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.core.db import Base
    from app.models import Mod, ModDependency
    from app.models.base import ApiState
    from app.mods.resolve import resolve_dependencies

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    async with Session() as s:
        # root -> local, has gproj deps to CHILD (local) and PHANTOM (nothing)
        s.add(Mod(guid="ROOT000000000001", name="Root", is_local=True, api_state=ApiState.not_found))
        s.add(Mod(guid="CHILD00000000002", name="Child", is_local=True, api_state=ApiState.unchecked))
        s.add(ModDependency(mod_guid="ROOT000000000001", depends_on_guid="CHILD00000000002", source="gproj"))
        s.add(ModDependency(mod_guid="ROOT000000000001", depends_on_guid="PHANTOM000000003", source="gproj"))
        await s.commit()

        tree = await resolve_dependencies(s, ["ROOT000000000001"], use_api=False)
        by = {n.guid: n for n in tree.nodes}
        check("root node via gproj (api_state not_found)", by["ROOT000000000001"].via == "gproj", by["ROOT000000000001"])
        check("root state not_found", by["ROOT000000000001"].state == "not_found", by["ROOT000000000001"])
        check("phantom node state unresolved", by["PHANTOM000000003"].state == "unresolved", by.get("PHANTOM000000003"))
        check("phantom via unknown", by["PHANTOM000000003"].via == "unknown", by.get("PHANTOM000000003"))
        check("child present, depth 1", by["CHILD00000000002"].depth == 1, by.get("CHILD00000000002"))
        check("edges", ("ROOT000000000001", "CHILD00000000002") in tree.edges and
              ("ROOT000000000001", "PHANTOM000000003") in tree.edges, tree.edges)
        check("as_dict shape", set(tree.as_dict()) == {"roots", "nodes", "edges"})
    await eng.dispose()


asyncio.run(_rate_test())
asyncio.run(_resolve_test())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
