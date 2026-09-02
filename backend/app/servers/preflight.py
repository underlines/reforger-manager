"""Read-only preflight checks for saved Reforger server definitions.

``Engine.installed_version`` is normally scraped from a prior server console
log. A deliberately small map of verified build-to-display-version pairs also
supports compatibility checks before the first server run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import shutil

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..models import ENGINE_SINGLETON_ID, Engine, Mod, Server, ServerMod
from ..mods import ModNotFound, WorkshopError, is_stale, resolve_dependencies, workshop
from . import diagnosis


_GUID_RE = re.compile(r"Addon ([0-9A-F]{16})\b")


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    level: str  # green | warn | blocked
    detail: str
    fix: str | None = None

    def as_dict(self) -> dict:
        value = asdict(self)
        if value["fix"] is None:
            value.pop("fix")
        return value


@dataclass(frozen=True)
class PreflightReport:
    verdict: str
    checks: list[PreflightCheck]
    resolved_mods: list[dict]

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "checks": [check.as_dict() for check in self.checks],
            "resolved_mods": self.resolved_mods,
        }


async def preflight(session: AsyncSession, server_id: int) -> PreflightReport:
    """Run only read-only availability, compatibility, pin, and space checks."""
    server = await session.get(Server, server_id)
    if server is None:
        raise ValueError(f"Server {server_id} was not found.")

    assignments = (
        await session.execute(
            select(ServerMod)
            .where(ServerMod.server_id == server_id, ServerMod.enabled.is_(True))
            .order_by(ServerMod.load_order)
        )
    ).scalars().all()
    roots = [assignment.mod_guid.upper() for assignment in assignments]
    tree = await resolve_dependencies(session, roots)
    nodes = {node.guid: node for node in tree.nodes}
    checks: list[PreflightCheck] = []

    # The resolver's last fallback is "unknown".  A failed start can still name
    # the otherwise unavailable parent's dependencies (notably server 9).
    unresolved = [node for node in tree.nodes if node.state == "unresolved"]
    log_guids = _newest_log_addon_guids(server_id)
    if unresolved and log_guids:
        known = set(nodes)
        for guid in log_guids:
            if guid not in known:
                nodes[guid] = _LogNode(guid)
                known.add(guid)
        checks.append(PreflightCheck(
            "dependency enumeration",
            "warn",
            "Workshop and persisted addon.gproj data were unavailable; dependency GUIDs were recovered from the newest server log.",
        ))
    elif unresolved:
        checks.append(PreflightCheck(
            "dependency enumeration",
            "warn",
            "Dependencies are unknown for: " + ", ".join(node.guid for node in unresolved) + ".",
            "Restore Workshop access or rescan the local addon.gproj metadata.",
        ))

    engine = await session.get(Engine, ENGINE_SINGLETON_ID)
    library_mods = {
        mod.guid: mod
        for mod in (await session.execute(select(Mod).where(Mod.guid.in_(list(nodes))))).scalars()
    } if nodes else {}
    server_pins = {assignment.mod_guid.upper(): assignment for assignment in assignments}
    resolved_mods: list[dict] = []
    missing_sizes: list[int] = []
    unknown_missing_sizes: list[str] = []
    version_unknown = False

    for guid, node in nodes.items():
        mod = library_mods.get(guid)
        assignment = server_pins.get(guid)
        pin = assignment if assignment and assignment.pinned_version else mod
        selected_version = pin.pinned_version if pin and pin.pinned_version else None
        detail: dict = {
            "guid": guid,
            "name": getattr(node, "name", None) or (mod.name if mod else None),
            "source": getattr(node, "via", "log"),
            "state": getattr(node, "state", "unresolved"),
            "version": selected_version or (mod.latest_version if mod else None),
        }

        api_mod: dict | None = None
        try:
            api_mod = await workshop.get_mod(guid)
        except ModNotFound:
            checks.append(PreflightCheck(
                f"Workshop {guid}", "blocked", _availability_detail(guid, server_id),
                "Remove or replace this addon and its unavailable dependencies.",
            ))
            detail["availability"] = "not_resolvable"
        except WorkshopError as exc:
            checks.append(PreflightCheck(
                f"Workshop {guid}", "warn", f"Could not verify Workshop availability for {guid}: {exc}.",
            ))
            detail["availability"] = "unknown"
        else:
            flags = [key for key in ("unlisted", "private", "obsolete") if api_mod.get(key)]
            if flags:
                if mod is None or not mod.is_local:
                    checks.append(PreflightCheck(
                        f"Workshop {guid}", "blocked",
                        f"{guid} is marked {', '.join(flags)} on the Workshop.",
                        "Use a listed, public, non-obsolete replacement.",
                    ))
                    detail["availability"] = flags[0]
                else:
                    checks.append(PreflightCheck(
                        f"Workshop {guid}", "warn",
                        f"{guid} is marked {', '.join(flags)} on the Workshop but is already present locally.",
                    ))
                    detail["availability"] = "local"
            else:
                detail["availability"] = "ok"
            detail["name"] = api_mod.get("name") or detail["name"]

        version = await _selected_version(guid, selected_version, mod, api_mod, checks)
        if version:
            detail["version"] = version.get("version") or detail["version"]
            game_version = version.get("game_version")
            detail["game_version"] = game_version
            if game_version:
                if engine is None or not engine.installed_version:
                    version_unknown = True
                else:
                    compat = _version_compat(game_version, engine.installed_version)
                    if compat == "blocked":
                        checks.append(PreflightCheck(
                            f"Engine compatibility {guid}", "blocked",
                            f"{guid} version {detail['version']} declares game version {game_version}; installed engine is {engine.installed_version}.",
                            "Select a version built for the installed engine or replace the addon.",
                        ))
                    elif compat == "warn":
                        checks.append(PreflightCheck(
                            f"Engine compatibility {guid}", "warn",
                            f"{guid} version {detail['version']} declares game version {game_version}; installed engine is {engine.installed_version}. Older-version addons usually run on a newer engine, but verify against a live start.",
                        ))
            else:
                checks.append(PreflightCheck(
                    f"Engine compatibility {guid}", "warn",
                    f"{guid} does not declare a game version for the selected release.",
                ))

            size = _as_size(version.get("size"))
        else:
            checks.append(PreflightCheck(
                f"Engine compatibility {guid}", "warn",
                f"Could not determine the selected release and its declared game version for {guid}.",
            ))
            size = _as_size((api_mod or {}).get("size")) or (mod.size if mod else None)

        if mod is None or not mod.is_local:
            if size is None:
                unknown_missing_sizes.append(guid)
            else:
                missing_sizes.append(size)
        if pin and pin.pinned_version and is_stale(pin, engine):
            checks.append(PreflightCheck(
                f"Stale pin {guid}", "warn",
                f"{guid} is pinned to {pin.pinned_version} for engine build {pin.pinned_at_build}, not installed build {engine.installed_build if engine else None}.",
                "Unpin and take latest.",
            ))
        resolved_mods.append(detail)

    if version_unknown:
        checks.append(PreflightCheck(
            "Engine compatibility", "warn",
            "Installed engine display version is unavailable (normally populated from a prior console log); compatibility cannot be verified.",
        ))
    _disk_checks(checks, missing_sizes, unknown_missing_sizes)
    if not checks:
        checks.append(PreflightCheck("preflight", "green", "All enabled addons and resolved dependencies passed the available checks."))
    return PreflightReport(_verdict(checks), checks, resolved_mods)


async def preflight_all(session: AsyncSession) -> dict[int, PreflightReport]:
    """Return deterministic reports keyed by every saved server id."""
    ids = (await session.execute(select(Server.id).order_by(Server.id))).scalars().all()
    return {server_id: await preflight(session, server_id) for server_id in ids}


@dataclass(frozen=True)
class _LogNode:
    guid: str
    name: str | None = None
    via: str = "log"
    state: str = "unresolved"


async def _selected_version(guid, selected, mod, api_mod, checks):
    try:
        versions = await workshop.get_versions(guid)
    except ModNotFound:
        return None
    except WorkshopError as exc:
        checks.append(PreflightCheck(f"Version {guid}", "warn", f"Could not inspect versions for {guid}: {exc}."))
        return None
    wanted = selected or (api_mod or {}).get("version") or (mod.latest_version if mod else None)
    return next((item for item in versions if item.get("version") == wanted), versions[0] if versions else None)


def _newest_log_addon_guids(server_id: int) -> list[str]:
    logs = list((settings.profile_dir(server_id) / "logs").glob("**/*.log"))
    if not logs:
        return []
    try:
        text = max(logs, key=lambda path: path.stat().st_mtime).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return list(dict.fromkeys(_GUID_RE.findall(text)))


def _availability_detail(guid: str, server_id: int) -> str:
    logs = list((settings.profile_dir(server_id) / "logs").glob("**/*.log"))
    if logs:
        try:
            path = max(logs, key=lambda path: path.stat().st_mtime)
            text = path.read_text(encoding="utf-8", errors="replace")
            # Diagnose remains the canonical parser; these patterns retain the
            # exact per-addon phrase when a single log reports several causes.
            result = diagnosis.diagnose(text)
            if result and guid in result.implicated_guids:
                for pattern, phrase in (
                    (diagnosis.BLOCKED_RE, "Addon is blocked."),
                    (diagnosis.NOT_FOUND_RE, "Addon was not found on workshop."),
                    (diagnosis.DEPS_DELETED_RE, "Addon has dependencies deleted from workshop."),
                ):
                    if any(match.group(1) == guid for match in pattern.finditer(text)):
                        return f"{guid}: {phrase}"
        except OSError:
            pass
    return f"{guid} is not resolvable on the Workshop (deleted, blocked, or private)."


def _disk_checks(checks, sizes, unknown):
    if unknown:
        checks.append(PreflightCheck("Download space", "warn", "Missing addons have unknown download sizes: " + ", ".join(unknown) + "."))
    if not sizes:
        return
    required = sum(sizes)
    try:
        free = shutil.disk_usage(settings.mods_dir).free
    except OSError as exc:
        checks.append(PreflightCheck("Download space", "warn", f"Could not determine free space at {settings.mods_dir}: {exc}."))
        return
    if free < required:
        checks.append(PreflightCheck("Download space", "blocked", f"Missing addons need {required} bytes; only {free} bytes are free at {settings.mods_dir}.", "Free disk space before downloading addons."))
    else:
        checks.append(PreflightCheck("Download space", "green", f"{free} bytes free for {required} bytes of known missing addon downloads."))


def _as_size(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_version(value: str | None) -> tuple[int, int, int, int] | None:
    """Parse ``1.8.0.10`` into four ints; return None on any junk component."""
    if not value:
        return None
    parts = str(value).strip().split(".")
    if len(parts) < 2 or len(parts) > 4:
        return None
    try:
        ints = tuple(int(p) for p in parts)
    except ValueError:
        return None
    return tuple(list(ints) + [0] * (4 - len(ints)))  # type: ignore[return-value]


def _version_compat(declared: str, installed: str) -> str:
    """Engine-compat verdict for a mod's declared gameVersion vs the installed engine.

    Reforger runs addons built for slightly older engine versions (verified: the
    old stack ran 1.7.0.54 addons on the 1.8.0.10 engine), so strict equality is
    wrong. We block only when the addon needs a *newer* engine than we have, or
    when it is old enough that its Enfusion/script surface likely broke (e.g.
    Ronin AI's 1.2.1.173 against 1.8.0.10 fails to compile).
    """
    d = _parse_version(declared)
    i = _parse_version(installed)
    if d is None or i is None:
        return "warn"  # unparseable: be conservative but do not hard-block
    if d > i:
        return "blocked"  # addon needs a newer engine than we have
    if d[0] != i[0] or (i[1] - d[1]) >= 3:
        return "blocked"  # different major or several minors behind -> broken surface
    if d != i:
        return "warn"  # one or two minors behind: usually runs, verify live
    return "ok"


def _verdict(checks):
    if any(check.level == "blocked" for check in checks):
        return "blocked"
    if any(check.level == "warn" for check in checks):
        return "warn"
    return "green"
