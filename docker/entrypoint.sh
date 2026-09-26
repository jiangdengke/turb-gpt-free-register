#!/bin/sh
set -eu

umask 077

data_dir="${TURB_DATA_DIR:-/data}"

if [ "$data_dir" = "/" ]; then
  echo "TURB_DATA_DIR must not be /" >&2
  exit 64
fi

mkdir -p \
  "$data_dir/home" \
  "$data_dir/logs" \
  "$data_dir/run" \
  "$data_dir/注册日志" \
  "$data_dir/cache" \
  "$data_dir/accounts" \
  "$data_dir/data" \
  "$data_dir/codex_accounts" \
  "$data_dir/codex_agent_accounts"

if [ ! -e "$data_dir/.env" ]; then
  : > "$data_dir/.env"
fi
if [ ! -e "$data_dir/turb.sqlite3" ]; then
  : > "$data_dir/turb.sqlite3"
fi

chmod 600 "$data_dir/.env" "$data_dir/turb.sqlite3" 2>/dev/null || true

probe="$data_dir/.write-test.$$"
: > "$probe"
rm -f "$probe"

exec "$@"
