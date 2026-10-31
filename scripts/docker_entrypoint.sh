#!/bin/sh
# Bind-mounted ./data and ./logs keep whatever owner the host (or a previous
# container user) left them with. Compose starts this script as root so a
# rebuild can correct that, then the bot process drops to BOT_UID:BOT_GID.
set -eu

if [ "$(id -u)" = "0" ]; then
  uid="${BOT_UID:-1000}"
  gid="${BOT_GID:-1000}"
  mkdir -p /app/data /app/logs
  chown -R --no-dereference "${uid}:${gid}" /app/data /app/logs
  # Owner write on existing SQLite and log files; execute only where it
  # already belongs (directories). Does not change group/other mode bits.
  chmod -R u+rwX /app/data /app/logs
  exec setpriv \
    --reuid="$uid" \
    --regid="$gid" \
    --clear-groups \
    --no-new-privs \
    --inh-caps=-all \
    --ambient-caps=-all \
    --bounding-set=-all \
    -- "$@"
fi

exec "$@"
