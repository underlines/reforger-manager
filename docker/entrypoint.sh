#!/bin/sh
# Reforger Manager — container entrypoint (Phase 1)
#
# Runs as root. Remaps the in-image `steam` user/group to own the bind mounts,
# then drops privileges with gosu and hands PID 1 to tini for the API + any
# game child processes it spawns.
#
# Ownership target resolution (first match wins):
#   1. PUID / PGID environment variables, if set
#   2. the current owner of $DATA_DIR (adopts an existing bind mount)
#   3. 1000:1000
set -e

DATA_DIR="/home/steam/data"

DET_UID="$(stat -c '%u' "$DATA_DIR" 2>/dev/null || echo 0)"
DET_GID="$(stat -c '%g' "$DATA_DIR" 2>/dev/null || echo 0)"

TARGET_UID="${PUID:-$DET_UID}"
TARGET_GID="${PGID:-$DET_GID}"
[ "$TARGET_UID" = "0" ] && TARGET_UID=1000
[ "$TARGET_GID" = "0" ] && TARGET_GID=1000

WANT="${TARGET_UID}:${TARGET_GID}"
MARKER="$DATA_DIR/.rm-owner"

# Remap the steam user/group (-o allows a non-unique id).
groupmod -o -g "$TARGET_GID" steam
usermod  -o -u "$TARGET_UID" -g "$TARGET_GID" steam

# Always fix the mount points themselves (cheap, non-recursive).
for d in \
    /home/steam \
    "$DATA_DIR" \
    "$DATA_DIR/server" \
    "$DATA_DIR/mods" \
    "$DATA_DIR/profiles" \
    "$DATA_DIR/configs"
do
    mkdir -p "$d" 2>/dev/null || true
    chown steam:steam "$d" 2>/dev/null || true
done

# One-time deep chown of the whole home tree, guarded by a marker so a restart
# with unchanged ids does not re-walk ~30 GB of game + mod content.
if [ "$(cat "$MARKER" 2>/dev/null)" != "$WANT" ]; then
    echo "entrypoint: applying one-time chown -R steam:steam /home/steam ($WANT)"
    chown -R steam:steam /home/steam
    echo "$WANT" > "$MARKER"
    chown steam:steam "$MARKER" 2>/dev/null || true
fi

echo "entrypoint: starting as steam ($WANT): $*"
exec gosu steam tini -- "$@"
