"""HTTP smoke test of the /api/mods routes via ASGITransport (no lifespan)."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

DB = Path(__file__).parent / "routes_test.db"
if DB.exists():
    DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB.as_posix()}"
sys.path.insert(0, str(Path(r"Z:/reforger-manager/backend")))

FX = Path(__file__).parent / "fixtures" / "addons"
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {name}")
    else:
        FAIL += 1; print(f"  FAIL {name}  {extra}")


async def main():
    import httpx
    from app.core.db import Base, engine, SessionLocal
    from app.core.security import create_access_token, hash_password
    from app.models import User, Mod, Server, ServerMod
    from app.models.base import ApiState
    import app.mods.scanner as scn
    import app.mods.sync as sync_mod
    from app.main import app

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as s:
        s.add(User(username="admin", password_hash=hash_password("x"), is_admin=True, is_active=True))
        # a resolvable mod + a server that uses it
        s.add(Mod(guid="595F2BF2F44836FB", name="RHS - Status Quo", is_local=True,
                  installed_version="0.16.5150", latest_version="0.16.5150", api_state=ApiState.ok))
        s.add(Mod(guid="6576A4DF3F71360C", name="REAPER_Kingmaker", is_local=True,
                  installed_version="1.0.7", latest_version="1.0.9", api_state=ApiState.ok))
        srv = Server(name="badis | reaper kingmaker")
        srv.mods.append(ServerMod(mod_guid="6576A4DF3F71360C", mod_name="REAPER_Kingmaker", load_order=0))
        s.add(srv)
        await s.commit()

    token = create_access_token("admin", extra={"uid": 1})
    H = {"Authorization": f"Bearer {token}"}
    tr = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t") as ac:
        r = await ac.get("/api/mods", headers=H)
        check("GET /api/mods 200", r.status_code == 200, r.text[:200])
        rows = r.json()
        check("list returns 2 mods", len(rows) == 2, rows)
        km = next(x for x in rows if x["guid"] == "6576A4DF3F71360C")
        check("has_update computed true (1.0.7 != 1.0.9)", km["has_update"] is True, km)
        check("stale_pin present", "stale_pin" in km)

        r = await ac.get("/api/mods", params={"update": "true"}, headers=H)
        check("?update=true filters to kingmaker only", [x["guid"] for x in r.json()] == ["6576A4DF3F71360C"], r.json())
        r = await ac.get("/api/mods", params={"q": "status"}, headers=H)
        check("?q=status -> RHS", [x["guid"] for x in r.json()] == ["595F2BF2F44836FB"], r.json())
        r = await ac.get("/api/mods", params={"local": "true"}, headers=H)
        check("?local=true -> 2", len(r.json()) == 2)
        r = await ac.get("/api/mods", params={"state": "ok"}, headers=H)
        check("?state=ok -> 2", len(r.json()) == 2)

        r = await ac.get("/api/mods/6576A4DF3F71360C", headers=H)
        check("GET /api/mods/{guid} 200", r.status_code == 200, r.text[:300])
        detail = r.json()
        check("detail.versions from live API", isinstance(detail["versions"], list) and len(detail["versions"]) > 0, len(detail.get("versions", [])))
        check("detail.used_by lists server", detail["used_by"] == ["badis | reaper kingmaker"], detail["used_by"])
        check("detail.dependency_tree present", detail["dependency_tree"] and detail["dependency_tree"]["roots"] == ["6576A4DF3F71360C"], detail.get("dependency_tree"))
        tree_guids = {n["guid"] for n in detail["dependency_tree"]["nodes"]}
        # no mod_dependencies rows seeded here -> pure API children (4 real deps)
        check("dep tree includes the 4 API deps",
              {"5EB139459EBF5C16", "61B8FA7B3BF8656B", "69349A7537394F8D", "69A4D664A6284E06"} <= tree_guids, tree_guids)

        r = await ac.get("/api/mods/6576A4DF3F71360C/dependencies", headers=H)
        check("GET /{guid}/dependencies 200", r.status_code == 200 and "nodes" in r.json(), r.text[:200])

        r = await ac.get("/api/mods/DEADBEEFDEADBEEF", headers=H)
        check("unknown guid -> 404", r.status_code == 404, r.status_code)

        r = await ac.get("/api/mods/search", params={"q": "RHS", "limit": 5}, headers=H)
        check("GET /api/mods/search 200", r.status_code == 200, r.text[:200])
        sres = r.json()
        check("search <=5 & shaped", 0 < len(sres) <= 5 and "id" in sres[0] and "latest_version" in sres[0], sres[:1])

        r = await ac.post("/api/mods/add", json={"url_or_id": "https://reforger.armaplatform.com/workshop/615F07D39925670F-BattleGen"}, headers=H)
        check("POST /api/mods/add 201", r.status_code == 201, r.text[:300])
        added = r.json()
        check("add resolved BattleGen", added["guid"] == "615F07D39925670F" and added["api_state"] == "ok", added)

        r = await ac.post("/api/mods/add", json={"url_or_id": "658756C5760E94DE"}, headers=H)
        check("POST /api/mods/add 404 for blocked mod", r.status_code == 404 and "not resolvable" in r.text, r.text[:200])

        r = await ac.post("/api/mods/add", json={"url_or_id": "no guid here"}, headers=H)
        check("POST /api/mods/add 400 for junk", r.status_code == 400, r.text[:200])

        r = await ac.get("/api/mods", headers={})
        check("unauthenticated -> 401", r.status_code == 401, r.status_code)

    await engine.dispose()
    if DB.exists():
        DB.unlink()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
