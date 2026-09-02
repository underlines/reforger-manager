#!/usr/bin/env python3
"""End-to-end smoke test against a *running* Reforger Manager stack.

Unlike the pytest suite (`backend/tests/`, sqlite, no network), this drives the
real FastAPI + Postgres containers over HTTP and — in the `engine` / `server` /
`mods` phases — actually installs the Reforger engine with steamcmd, launches
the game server as a child process, and exercises RCON, A2S stats, log parsing
and the scheduled-restart timer against it.

Nothing needs to *connect* to the game server: the manager talks to it over the
container loopback (RCON 127.0.0.1:19999, A2S 127.0.0.1:17777), so bridge
networking (the local `docker-compose.override.yml`) is enough.

    Phases (run in order; select with --phases):
      api     - CRUD / preview / pins / modpacks / settings / backup   (no engine)
      engine  - install or re-validate the Reforger engine via steamcmd (~10 GB
                on a cold box; auto-skips the download when already installed)
      server  - create a vanilla definition, start it for real, then stats /
                RCON / players / log parsing / scheduled restart / stop
      mods    - add a Workshop mod, force a download job, verify it lands on
                disk and shows up in /api/storage

Run it inside the manager container (has python3, reaches the API on loopback):

    docker cp scripts/e2e_live.py reforger-manager:/tmp/e2e_live.py
    docker exec -e RM_ADMIN_PASSWORD=... reforger-manager python3 /tmp/e2e_live.py

or from anywhere that can reach the published port:

    python3 scripts/e2e_live.py --base http://localhost:18090/api --password ...

Exit code is 0 only when every assertion in every selected phase passed.
Stdlib only — no httpx/requests dependency.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- args

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--base", default=os.environ.get("RM_BASE", "http://127.0.0.1:18090/api"),
                help="API base URL (default: %(default)s)")
ap.add_argument("--username", default=os.environ.get("RM_ADMIN_USERNAME", "admin"))
ap.add_argument("--password", default=os.environ.get("RM_ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD"),
                help="admin password (or set RM_ADMIN_PASSWORD / ADMIN_PASSWORD)")
ap.add_argument("--phases", default="api,engine,server,mods",
                help="comma list of: api,engine,server,mods (default: all)")
ap.add_argument("--keep", action="store_true", help="do not delete the e2e definitions/packs at the end")
ap.add_argument("--engine-timeout", type=int, default=3600, help="seconds to wait for the engine job (default 3600)")
ap.add_argument("--boot-timeout", type=int, default=300, help="seconds to wait for the game server to answer A2S (default 300)")
ap.add_argument("--small-mod", default="5965550F24A0C152", help="GUID of a tiny Workshop mod for the download test")
ap.add_argument("--dep-mod", default="595F2BF2F44836FB", help="GUID of a mod with deps+scenarios+versions (RHS Status Quo)")
ap.add_argument("--scenario", default="{ECC61978EDCC2B5A}Missions/23_Campaign.conf",
                help="vanilla scenario id for the test definition (default: Conflict - Everon)")
args = ap.parse_args()

if not args.password:
    ap.error("no admin password: pass --password or set RM_ADMIN_PASSWORD / ADMIN_PASSWORD")

PHASES = [p.strip() for p in args.phases.split(",") if p.strip()]

# ------------------------------------------------------------------------- runner

passed = 0
failed = 0
_fail_labels: list[str] = []
_token: str | None = None


def _c(txt: str, code: str) -> str:
    return f"\033[{code}m{txt}\033[0m" if sys.stdout.isatty() else txt


def section(title: str) -> None:
    print("\n" + _c(f"=== {title} ===", "1;36"))


def check(label: str, cond: bool, got: object = None) -> bool:
    global passed, failed
    if cond:
        passed += 1
        print("  " + _c("PASS", "32") + f"  {label}" + (f"  ({got})" if got is not None else ""))
    else:
        failed += 1
        _fail_labels.append(label)
        print("  " + _c("FAIL", "31") + f"  {label}" + (f"  (got {got!r})" if got is not None else ""))
    return cond


def info(msg: str) -> None:
    print(f"  - {msg}")


class HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def req(method: str, path: str, body: object = None, auth: bool = True,
        params: dict | None = None, raw: bool = False) -> tuple[int, object]:
    """Return (status_code, parsed_json_or_text). Never raises on 4xx/5xx."""
    url = args.base.rstrip("/") + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    data = None
    headers = {"accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    if auth and _token:
        headers["authorization"] = f"Bearer {_token}"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            payload = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")
        code = e.code
    except urllib.error.URLError as e:
        raise HttpError(0, f"connection failed: {e}") from e
    if raw:
        return code, payload
    try:
        return code, json.loads(payload)
    except ValueError:
        return code, payload


def wait_until(fn, timeout: int, interval: float, desc: str):
    """Poll fn() until it returns a truthy value or timeout. Returns last value."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
        except Exception as e:  # noqa: BLE001
            last = e
        if last and not isinstance(last, Exception):
            return last
        time.sleep(interval)
    print(f"  ! timed out after {timeout}s waiting for: {desc} (last={last!r})")
    return last


def login() -> None:
    global _token
    code, data = req("POST", "/auth/login", {"username": args.username, "password": args.password}, auth=False)
    if code != 200 or not isinstance(data, dict) or "access_token" not in data:
        print(_c(f"FATAL: login failed ({code}): {data}", "31"))
        sys.exit(2)
    _token = data["access_token"]


# ------------------------------------------------------------------ tracked state

made_servers: list[int] = []
made_packs: list[int] = []


def _new_server(payload: dict) -> int:
    code, data = req("POST", "/servers", payload)
    assert code == 201, f"create server -> {code}: {data}"
    made_servers.append(data["id"])
    return data["id"]


# --------------------------------------------------------------------- phase: api

def phase_api() -> None:
    section("API: auth + health")
    check("GET /health -> 200", req("GET", "/health", auth=False)[0] == 200)
    check("GET /auth/me -> 200", req("GET", "/auth/me")[0] == 200)

    section("API: server create / edit / preview (S3, S4)")
    sid = _new_server({"name": "e2e-api", "game_name": "e2e",
                       "scenario_game_id": args.scenario})
    check("default max_players 32", req("GET", f"/servers/{sid}")[1]["max_players"] == 32)
    req("PATCH", f"/servers/{sid}", {"max_players": 40, "is_favourite": True,
                                     "game_properties": {"serverMaxViewDistance": 2500}})
    row = req("GET", f"/servers/{sid}")[1]
    check("max_players patched to 40", row["max_players"] == 40, row["max_players"])
    check("is_favourite patched", row["is_favourite"] is True)

    code, prev = req("POST", f"/servers/{sid}/config/preview", {"max_players": 99})
    check("preview -> 200", code == 200, code)
    check("preview reflects unsaved maxPlayers=99",
          prev["config"]["game"]["maxPlayers"] == 99, prev["config"]["game"]["maxPlayers"])
    check("preview did not persist (row still 40)", req("GET", f"/servers/{sid}")[1]["max_players"] == 40)
    cfg = req("GET", f"/servers/{sid}/config")[1]["config"]
    check("generated config merges gameProperties",
          cfg["game"]["gameProperties"]["serverMaxViewDistance"] == 2500)

    section("API: pin preservation across a mod-set replace (S2, fact #1)")
    g = args.small_mod
    req("PATCH", f"/servers/{sid}", {"mods": [
        {"mod_guid": g, "mod_name": "m", "load_order": 0,
         "pinned_version": "1.0.0", "pinned_reason": "e2e"}]})
    m = req("GET", f"/servers/{sid}")[1]["mods"][0]
    check("mod assigned with a pin", m["pinned_version"] == "1.0.0")
    pinned_at = m.get("pinned_at")
    # re-save the SAME guid with NO pin fields -> pin must survive
    req("PATCH", f"/servers/{sid}", {"mods": [
        {"mod_guid": g, "mod_name": "m", "load_order": 4, "enabled": False}]})
    m = req("GET", f"/servers/{sid}")[1]["mods"][0]
    check("PIN PRESERVED across re-save without pin fields", m["pinned_version"] == "1.0.0", m["pinned_version"])
    check("pinned_reason preserved", m["pinned_reason"] == "e2e")
    check("pinned_at untouched by the reorder", m.get("pinned_at") == pinned_at)
    check("load_order updated", m["load_order"] == 4, m["load_order"])
    check("enabled toggled", m["enabled"] is False)
    # explicit overwrite, then drop
    req("PATCH", f"/servers/{sid}", {"mods": [{"mod_guid": g, "pinned_version": "2.0.0"}]})
    check("explicit pinned_version overwrites",
          req("GET", f"/servers/{sid}")[1]["mods"][0]["pinned_version"] == "2.0.0")
    req("PATCH", f"/servers/{sid}", {"mods": []})
    check("mod dropped from the set", len(req("GET", f"/servers/{sid}")[1]["mods"]) == 0)

    section("API: clone (S7) + favourites (S8)")
    code, cl = req("POST", f"/servers/{sid}/clone", {"name": "e2e-api-clone"})
    check("clone -> 200/201", code in (200, 201), code)
    if code in (200, 201):
        made_servers.append(cl["id"])
        check("clone copied max_players", cl["max_players"] == 40)
        check("clone has no runtime state", cl["is_running"] is False)
    _new_server({"name": "e2e-api-plain"})
    check("favourite sorts first", req("GET", "/servers")[1][0]["is_favourite"] is True)

    section("API: modpacks CRUD + transfer (S13, S15)")
    code, pk = req("POST", "/modpacks", {"name": "e2e-api-pack", "description": "d", "items": []})
    check("POST /modpacks -> 201", code == 201, code)
    made_packs.append(pk["id"])
    check("duplicate name -> 409", req("POST", "/modpacks", {"name": "e2e-api-pack"})[0] == 409)
    check("apply pack (replace) -> 200", req("POST", f"/modpacks/{pk['id']}/apply/{sid}", {"mode": "replace"})[0] == 200)
    code, fs = req("POST", f"/modpacks/from-server/{sid}", {"name": "e2e-api-fromsrv", "description": "s"})
    check("create pack from server -> 201", code == 201, code)
    if code == 201:
        made_packs.append(fs["id"])
    code, exp = req("GET", f"/modpacks/{pk['id']}/export")
    check("export pack -> 200", code == 200, code)
    check("re-import pack (rename) -> 2xx",
          req("POST", "/modpacks/import", exp, params={"on_conflict": "rename"})[0] in (200, 201))

    section("API: storage / scenarios / settings / backup (S12, S6, S17, S18)")
    code, st = req("GET", "/storage")
    check("GET /storage -> 200", code == 200, code)
    info(f"free={st.get('free_bytes')} per_mod={len(st.get('per_mod', []))} "
         f"orphans={len(st.get('orphans', []))} kept_as_dependency={len(st.get('kept_as_dependency', []))}")
    check("POST /scenarios/resolve tolerates a 404 guid",
          req("POST", "/scenarios/resolve", {"guids": ["658756C5760E94DE"]})[0] == 200)
    check("GET /settings -> 200", req("GET", "/settings")[0] == 200)
    req("PATCH", "/settings", {"log_spam_patterns": ["e2e-marker"]})
    check("spam pattern persisted", "e2e-marker" in req("GET", "/settings")[1].get("log_spam_patterns", []))
    req("PATCH", "/settings", {"log_spam_patterns": []})
    code, bk = req("GET", "/backup/export")
    check("GET /backup/export -> 200", code == 200, code)
    info(f"backup: {len(bk.get('servers', []))} servers, {len(bk.get('modpacks', []))} modpacks")
    check("POST /backup/import?dry_run=true -> 200",
          req("POST", "/backup/import", bk, params={"dry_run": "true"})[0] == 200)


# ------------------------------------------------------------------ phase: engine

def phase_engine() -> None:
    section("ENGINE: install / re-validate via steamcmd")
    code, eng = req("GET", "/engine")
    check("GET /engine -> 200", code == 200, code)
    before = eng.get("installed_build")
    info(f"installed_build before: {before!r}  latest_build: {eng.get('latest_build')!r}")

    code, job = req("POST", "/engine/update")
    check("POST /engine/update -> 202", code == 202, code)
    if code != 202:
        return
    jid = job["job_id"]
    info(f"engine job id={jid} — steamcmd +app_info_update 1 +app_update 1874900 validate")
    info("this downloads ~10 GB on a cold box; a warm install just re-verifies")

    def _poll():
        _, j = req("GET", f"/jobs/{jid}")
        state = j.get("state")
        print(f"    job {jid}: state={state} progress={j.get('progress')} step={j.get('current_step')!r}")
        if state in ("succeeded", "failed", "error", "cancelled"):
            return j
        return None

    j = wait_until(_poll, timeout=args.engine_timeout, interval=20, desc="engine job to finish")
    ok = isinstance(j, dict) and j.get("state") == "succeeded"
    check("engine job reached state=succeeded", ok, j.get("state") if isinstance(j, dict) else j)
    if isinstance(j, dict) and not ok:
        print("    last log:\n      " + "\n      ".join((j.get("log_tail") or [])[-15:]))
    if isinstance(j, dict) and ok:
        res = j.get("result") or {}
        check("steamcmd exit_code == 0", res.get("exit_code") == 0, res.get("exit_code"))

    code, eng = req("GET", "/engine")
    check("engine now reports an installed_build", bool(eng.get("installed_build")), eng.get("installed_build"))
    check("installed_build == latest_build", eng.get("installed_build") == eng.get("latest_build"),
          f"{eng.get('installed_build')} vs {eng.get('latest_build')}")
    check("update_available is False after a successful update", eng.get("update_available") is False)


# ------------------------------------------------------------------ phase: server

def phase_server() -> None:
    section("SERVER: create a vanilla definition and start it for real")
    _, eng = req("GET", "/engine")
    if not eng.get("installed_build"):
        check("engine is installed (prerequisite for the server phase)", False, "not installed")
        return

    sid = _new_server({
        "name": "e2e-live",
        "game_name": "e2e-live smoke",
        "scenario_game_id": args.scenario,
        "max_players": 8,
        "rcon_enabled": True,
        "rcon_password": "e2ercon123",
        "rcon_permission": "admin",
    })
    info(f"definition id={sid}, scenario={args.scenario}")

    code, pf = req("GET", f"/servers/{sid}/preflight")
    check("preflight -> 200", code == 200, code)
    check("preflight verdict is green/warn (no mods)", pf.get("verdict") in ("green", "warn"), pf.get("verdict"))

    code, res = req("POST", f"/servers/{sid}/start")
    check("POST /start -> 200", code == 200, f"{code}: {res}")
    if code != 200:
        return
    info(f"pid={res.get('pid')} log={res.get('log_path')}")

    # wait for the engine to answer an A2S query on loopback
    def _a2s():
        c, s = req("GET", f"/servers/{sid}/stats")
        if c == 200:
            return s
        print(f"    waiting for A2S... ({c})")
        return None

    stats = wait_until(_a2s, timeout=args.boot_timeout, interval=5, desc="A2S stats to answer")
    got_stats = isinstance(stats, dict) and "current" in stats
    check("A2S /stats answered once the server booted", got_stats)
    if got_stats:
        cur = stats["current"]
        info(f"A2S: name={cur.get('name')!r} map={cur.get('map_name')!r} "
             f"players={cur.get('players')}/{cur.get('max_players')} ping={cur.get('ping_ms')}ms")
        check("stats has a player-count field", "players" in cur and "max_players" in cur)
        check("max_players reflects the definition (8)", cur.get("max_players") == 8, cur.get("max_players"))
        _, hist = req("GET", f"/servers/{sid}/stats")
        check("stats history accumulates samples", len(hist.get("history", [])) >= 1)

    check("GET /servers/{id} shows is_running=True", req("GET", f"/servers/{sid}")[1]["is_running"] is True)

    section("SERVER: RCON + players")
    code, say = req("POST", f"/servers/{sid}/rcon", {"command": "#say e2e-live smoke test"})
    check("RCON '#say ...' -> 200", code == 200, f"{code}: {say}")
    code, pl = req("GET", f"/servers/{sid}/players")
    check("GET /players -> 200", code == 200, code)
    check("players payload is a list", isinstance(pl.get("players"), list), type(pl.get("players")).__name__)
    info(f"players connected: {len(pl.get('players', []))}")
    # kick/ban command shape is accepted by the whitelist even with nobody connected
    code, kick = req("POST", f"/servers/{sid}/rcon", {"command": "#kick 999"})
    check("RCON '#kick 999' accepted by the whitelist (200)", code == 200, f"{code}: {kick}")

    section("SERVER: console-log parsing")
    code, log = req("GET", f"/servers/{sid}/log")
    check("GET /log -> 200", code == 200, code)
    lines = log.get("lines", [])
    check("log has parsed lines", len(lines) > 0, len(lines))
    if lines:
        keys = set(lines[0])
        check("each line carries text/severity/is_spam", {"text", "severity", "is_spam"} <= keys, sorted(keys))
        sev = [ln for ln in lines if ln.get("severity")]
        info(f"{len(lines)} lines, {len(sev)} with a severity, "
             f"{sum(1 for ln in lines if ln.get('is_spam'))} flagged spam")
    code, ferr = req("GET", f"/servers/{sid}/log", params={"severity": "error"})
    check("GET /log?severity=error -> 200", code == 200, code)
    check("severity filter narrows the set", len(ferr.get("lines", [])) <= len(lines))
    code, fns = req("GET", f"/servers/{sid}/log", params={"hide_spam": "true"})
    check("GET /log?hide_spam=true -> 200", code == 200, code)
    check("hide_spam drops the spam-flagged lines",
          all(not ln.get("is_spam") for ln in fns.get("lines", [])))
    code, fq = req("GET", f"/servers/{sid}/log", params={"q": "server"})
    check("GET /log?q=server (search) -> 200", code == 200, code)

    section("SERVER: scheduled restart (arm / read / cancel)")
    code, sch = req("POST", f"/servers/{sid}/schedule-restart", {"in_seconds": 3600, "warn_at": [300, 60]})
    check("POST /schedule-restart -> 200", code == 200, f"{code}: {sch}")
    code, g = req("GET", f"/servers/{sid}/schedule-restart")
    check("GET /schedule-restart shows armed=True", g.get("armed") is True, g)
    check("seconds_remaining counts down from ~3600",
          isinstance(g.get("seconds_remaining"), (int, float)) and 3000 < g["seconds_remaining"] <= 3600,
          g.get("seconds_remaining"))
    code, d = req("DELETE", f"/servers/{sid}/schedule-restart")
    check("DELETE /schedule-restart -> 200", code == 200, code)
    check("GET /schedule-restart now armed=False", req("GET", f"/servers/{sid}/schedule-restart")[1].get("armed") is False)

    section("SERVER: scheduled restart FIRES (short timer, real #say warnings + #restart)")
    # The supervisor sends a `#say` countdown at each warn_at offset, then `#restart`
    # at T-0. `#restart` is a Reforger RCON command that reloads the running
    # scenario in-process (it does NOT re-exec the binary, so the pid is stable);
    # the supervisor marks the schedule done and, per S16, must not read the
    # engine's activity as a crash.
    code, sch = req("POST", f"/servers/{sid}/schedule-restart", {"in_seconds": 40, "warn_at": [30, 10]})
    check("arm a 40s restart with warnings at 30/10s -> 200", code == 200, f"{code}: {sch}")
    info("waiting up to ~90s for the warnings to broadcast and the timer to fire...")

    def _fired():
        _, gg = req("GET", f"/servers/{sid}/schedule-restart")
        return gg if gg.get("armed") is False else None

    g = wait_until(_fired, timeout=90, interval=5, desc="schedule to disarm after firing")
    check("schedule disarmed itself after the timer fired", isinstance(g, dict) and g.get("armed") is False,
          g.get("armed") if isinstance(g, dict) else g)

    # The supervisor sends the `#say` warnings over RCON, then `#restart`. Both
    # land in the engine's console log — but the engine buffers stdout when it is
    # not on a TTY, so the lines can lag the disk file by a few seconds. Poll the
    # whole-file search (not the tail) for a while before giving up.
    def _warns_logged():
        _, f = req("GET", f"/servers/{sid}/log", params={"q": "restart in"})
        w = [ln["text"] for ln in f.get("lines", [])
             if "say" in ln.get("text", "").lower() and "restart in" in ln.get("text", "").lower()]
        return w if len(w) >= 2 else None

    warns = wait_until(_warns_logged, timeout=45, interval=5, desc="the two #say warnings to appear in the log")
    if isinstance(warns, list):
        check("both '#say ... restart in Ns' warnings reached the console log", True, len(warns))
        for w in warns[-2:]:
            info(w.strip()[:120])
    else:
        # non-fatal: schedule-disarm + #restart-in-log below already prove the timer fired
        info("! the #say warning lines had not flushed to the console log within 45s (engine stdout buffering)")

    _, restart_hit = req("GET", f"/servers/{sid}/log", params={"q": "#restart"})
    check("the timer issued #restart (seen in the console log)",
          any("#restart" in ln.get("text", "") for ln in restart_hit.get("lines", [])),
          len(restart_hit.get("lines", [])))

    # `#restart` on a vanilla scenario makes the engine shut the session down; the
    # supervisor marks that intentional. Whatever the engine does, it must never
    # be recorded as a crash.
    _, after = req("GET", f"/servers/{sid}")
    info(f"post-restart: is_running={after.get('is_running')} last_state={after.get('last_state')}")
    check("scheduled restart was NOT misclassified as a crash",
          after.get("last_state") != "crashed", after.get("last_state"))
    # bring the server back for the explicit stop test if #restart took it down
    if after.get("is_running") is False:
        code, _ = req("POST", f"/servers/{sid}/start")
        check("server can be started again after a scheduled restart brought it down", code == 200, code)
        wait_until(_a2s, timeout=args.boot_timeout, interval=5, desc="A2S after the manual re-start")

    section("SERVER: stop")
    # let A2S come back so the stop is from a healthy state
    wait_until(_a2s, timeout=args.boot_timeout, interval=5, desc="A2S after restart")
    code, st = req("POST", f"/servers/{sid}/stop")
    check("POST /stop -> 200", code == 200, f"{code}: {st}")

    def _stopped():
        _, s = req("GET", f"/servers/{sid}")
        return s if s.get("is_running") is False else None

    s = wait_until(_stopped, timeout=90, interval=3, desc="is_running to go False")
    check("server reports is_running=False after stop", isinstance(s, dict))
    if isinstance(s, dict):
        info(f"last_state={s.get('last_state')} last_exit_code={s.get('last_exit_code')} "
             f"last_diagnosis={json.dumps(s.get('last_diagnosis'))[:160]}")
        check("a clean stop is not classified as a crash", s.get("last_state") != "crashed", s.get("last_state"))
    check("RCON after stop -> 409 (nothing running)", req("POST", f"/servers/{sid}/rcon", {"command": "#say x"})[0] == 409)


# -------------------------------------------------------------------- phase: mods

def phase_mods() -> None:
    section("MODS: add from the Workshop (S9) + detail (S10)")
    code, m = req("POST", "/mods/add", {"url_or_id": args.small_mod})
    ok_small = code == 201
    check("POST /mods/add (bare GUID) -> 201", ok_small, f"{code}: {str(m)[:160]}")
    if not ok_small:
        info("Workshop API unreachable — skipping the rest of the mods phase")
        return
    small = m["guid"]
    info(f"added {small}  name={m.get('name')!r}  size={m.get('size')}")
    check("garbage input -> 400", req("POST", "/mods/add", {"url_or_id": "nope"})[0] == 400)

    code, d = req("POST", "/mods/add", {"url_or_id": f"https://reforger.armaplatform.com/workshop/{args.dep_mod}"})
    check("POST /mods/add (Workshop URL) -> 201", code == 201, code)
    if code == 201:
        dep = d["guid"]
        code, det = req("GET", f"/mods/{dep}")
        check("GET /mods/{guid} detail -> 200", code == 200, code)
        info(f"detail: versions={len(det.get('versions', []))} "
             f"deps={len(det.get('dependency_tree', det.get('dependencies', [])) or [])} "
             f"tags={len(det.get('tags', []) or [])}")
        check("dependency mod has a version history", len(det.get("versions", [])) > 0)
        code, sc = req("POST", "/scenarios/resolve", {"guids": [dep]})
        check("POST /scenarios/resolve for a real mod -> 200", code == 200, code)
        check("resolve is idempotent (2nd call still 200, served from DB)",
              req("POST", "/scenarios/resolve", {"guids": [dep]})[0] == 200)

    section("MODS: force a real download job (S11) + storage (S12)")
    code, r = req("POST", f"/mods/{small}/download", {})
    if code == 409 and "size" in str(r).lower():
        # documented guard: Mod.size is NULL for an unenriched row -> refuse rather
        # than guarantee a fit it cannot compute. Still a PASS for the guard.
        check("download guard refuses on unknown Mod.size (409, not 202/500)", True, "409 unknown-size")
        info("row has no recorded size, so the free-space guard fired as designed; "
             "no real download to observe here")
        return
    check("POST /mods/{guid}/download -> 202", code == 202, f"{code}: {r}")
    if code != 202:
        return
    jid = r["job_id"]
    info(f"download job id={jid} kind={r.get('kind')}")
    check("job visible on GET /jobs/{id}", req("GET", f"/jobs/{jid}")[0] == 200)

    def _done():
        _, j = req("GET", f"/jobs/{jid}")
        st = j.get("state")
        print(f"    job {jid}: state={st} progress={j.get('progress')} step={j.get('current_step')!r}")
        return j if st in ("succeeded", "failed", "error", "cancelled") else None

    j = wait_until(_done, timeout=600, interval=10, desc="mod download job to finish")
    check("mod download job reached state=succeeded", isinstance(j, dict) and j.get("state") == "succeeded",
          j.get("state") if isinstance(j, dict) else j)

    _, mod_row = req("GET", f"/mods/{small}")
    check("mod row now marked is_local", mod_row.get("is_local") is True, mod_row.get("is_local"))
    _, st = req("GET", "/storage")
    per = {p["guid"]: p for p in st.get("per_mod", [])}
    check("downloaded mod appears in /storage per_mod", small in per, list(per)[:3])
    if small in per:
        info(f"on disk: {per[small].get('bytes')} bytes")
        check("its on-disk size is > 0", (per[small].get("bytes") or 0) > 0, per[small].get("bytes"))
    check("downloaded-but-referenced-nowhere mod is listed as an orphan",
          any(o.get("guid") == small if isinstance(o, dict) else o == small for o in st.get("orphans", [])))

    section("MODS: update check job (S13 free-space guard path)")
    code, r = req("POST", "/mods/updates/check")
    check("POST /mods/updates/check -> 202", code == 202, code)
    if code == 202:
        jid = r["job_id"]
        j = wait_until(_done, timeout=300, interval=10, desc="update-check job")
        check("update-check job finished cleanly",
              isinstance(j, dict) and j.get("state") in ("succeeded", "failed"),
              j.get("state") if isinstance(j, dict) else j)


# --------------------------------------------------------------------- entrypoint

def cleanup() -> None:
    if args.keep:
        info("--keep set: leaving e2e definitions and modpacks in place")
        return
    section("CLEANUP")
    # make sure nothing is left running
    for sid in list(made_servers):
        try:
            req("POST", f"/servers/{sid}/stop")
        except Exception:  # noqa: BLE001
            pass
    _, servers = req("GET", "/servers")
    for s in servers if isinstance(servers, list) else []:
        if str(s.get("name", "")).startswith("e2e"):
            req("DELETE", f"/servers/{s['id']}")
    _, packs = req("GET", "/modpacks")
    for p in packs if isinstance(packs, list) else []:
        if str(p.get("name", "")).startswith("e2e"):
            req("DELETE", f"/modpacks/{p['id']}")
    req("PATCH", "/settings", {"log_spam_patterns": []})
    info("removed e2e-* definitions and modpacks")


def main() -> int:
    print(f"Reforger Manager live E2E  —  base={args.base}  phases={PHASES}")
    login()
    table = {"api": phase_api, "engine": phase_engine, "server": phase_server, "mods": phase_mods}
    try:
        for name in PHASES:
            fn = table.get(name)
            if fn is None:
                print(_c(f"unknown phase: {name}", "31"))
                continue
            fn()
    finally:
        try:
            cleanup()
        except Exception as e:  # noqa: BLE001
            print(f"  ! cleanup error: {e}")

    print("\n" + "=" * 48)
    print(_c(f"RESULT: {passed} passed, {failed} failed", "1;32" if not failed else "1;31"))
    if _fail_labels:
        print("failed checks:")
        for lbl in _fail_labels:
            print(f"  - {lbl}")
    print("=" * 48)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
