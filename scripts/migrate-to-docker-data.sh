#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 SOURCE_ROOT DATA_DIR" >&2
  exit 64
}

[[ $# -eq 2 ]] || usage

source_root=$(python3 - "$1" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).resolve(strict=True))
PY
)
data_dir=$(python3 - "$2" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).resolve(strict=False))
PY
)

if [[ "$data_dir" == "/" || "$data_dir" == "$source_root" || "$data_dir" == "$source_root/"* ]]; then
  echo "Refusing unsafe DATA_DIR: $data_dir" >&2
  exit 64
fi

pid_file="$source_root/run/webui.pid"
if [[ -s "$pid_file" ]]; then
  pid=$(tr -cd '0-9' < "$pid_file")
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Source WebUI is still running (PID $pid); stop it before migration" >&2
    exit 73
  fi
fi

if [[ -e "$data_dir" ]] && find "$data_dir" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  echo "DATA_DIR is not empty: $data_dir" >&2
  exit 73
fi

umask 077
install -d -m 700 "$data_dir"

copy_item() {
  local name=$1
  if [[ -e "$source_root/$name" || -L "$source_root/$name" ]]; then
    cp -a "$source_root/$name" "$data_dir/$name"
  fi
}

for name in \
  logs run "注册日志" cache accounts data codex_accounts codex_agent_accounts; do
  copy_item "$name"
done

for name in \
  .env \
  "用于注册的邮箱.json" "用于注册的邮箱.txt" \
  "用于注册的API邮箱.json" "用于注册的API邮箱.txt" \
  "用于注册的域名邮箱.json" \
  "注册成功的邮箱.json" "注册成功的邮箱.txt" \
  "注册成功的token.txt" "注册任务.json" \
  "codex_导出状态.json" accounts_viewer.html \
  outlook_accounts.txt outlook_accounts_used.json sub2api.json; do
  copy_item "$name"
done

source_db="$source_root/turb.sqlite3"
target_db="$data_dir/turb.sqlite3"
if [[ -f "$source_db" ]]; then
  python3 - "$source_db" "$target_db" <<'PY'
import sqlite3
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
target_db = sqlite3.connect(target, timeout=30)
try:
    source_db.backup(target_db)
    result = target_db.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise SystemExit(f"SQLite integrity_check failed: {result}")
    tables = target_db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    print(f"SQLite backup verified: tables={tables}")
finally:
    target_db.close()
    source_db.close()
PY
else
  : > "$target_db"
fi

rm -f "$data_dir/turb.sqlite3-wal" "$data_dir/turb.sqlite3-shm"
chmod 600 "$target_db"
[[ ! -e "$data_dir/.env" ]] || chmod 600 "$data_dir/.env"

python3 - "$data_dir" <<'PY'
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
files = sum(1 for path in root.rglob("*") if path.is_file())
size = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
print(f"Migration complete: data_dir={root} files={files} bytes={size}")
print(f"owner_uid={os.stat(root).st_uid} owner_gid={os.stat(root).st_gid}")
PY
