#!/bin/bash
# Update an existing installation. No credentials, database or runtime keys in the archive.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
archive=${1:?release archive required}
# Serialize changes to the stable source and interpreter links.
exec 9>/opt/ai-carddesk/update.lock
flock -n 9 || { echo 'Another update is running.' >&2; exit 1; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
release=/opt/ai-carddesk/releases/$stamp
previous=$(readlink -f /opt/ai-carddesk/current)
backup=/var/backups/ai-carddesk/pre-backend-$stamp
test ! -e "$release"
install -d -m 755 "$release"
tar -xzf "$archive" -C "$release"
chown -R root:root "$release"
chmod -R a+rX "$release"
/opt/ai-carddesk/venv/bin/python -m py_compile "$release/app.py" "$release/worker.py"
install -d -m 700 "$backup"
runtime_dir=/opt/ai-carddesk/venv
runtime="$runtime_dir/bin/python"
plan=$(mktemp)
trap 'rm -f -- "$plan"' EXIT
candidate="$runtime"
previous_runtime=
if ! cmp -s "$previous/requirements.txt" "$release/requirements.txt" ||
   ! "$runtime" -m pip install --dry-run --no-index --report "$plan" -r "$release/requirements.txt" >/dev/null 2>&1 ||
   ! "$runtime" -c 'import json, sys; sys.exit(bool(json.load(open(sys.argv[1]))["install"]))' "$plan"; then
  # Build the full replacement before stopping either service.
  "$runtime" -m venv "$release/.venv"
  candidate="$release/.venv/bin/python"
  "$candidate" -m pip install --only-binary=:all: -r "$release/requirements.txt"
  if [ -L "$runtime_dir" ]; then
    previous_runtime=$(readlink -f "$runtime_dir")
  else
    previous_runtime=/opt/ai-carddesk/runtimes/legacy-$stamp
    install -d -m 755 /opt/ai-carddesk/runtimes
    test ! -e "$previous_runtime"
  fi
fi
"$candidate" -m pip check
runtime_switched=0
# Stop both writers for the upgrade snapshot and schema migration.
systemctl stop ai-carddesk-worker ai-carddesk
recover() {
  systemctl stop ai-carddesk-worker ai-carddesk || true
  ln -sfn "$previous" /opt/ai-carddesk/current
  if [ "$runtime_switched" -eq 1 ]; then
    ln -sfn "$previous_runtime" "$runtime_dir"
  fi
  systemctl start ai-carddesk ai-carddesk-worker
  echo 'Update failed; previous source restored. Database snapshot retained locally on server.' >&2
}
trap recover ERR
python3 - "$backup" <<'PY'
import os,sqlite3,sys
from pathlib import Path
os.umask(0o077)
folder=Path(sys.argv[1]);source=sqlite3.connect('/var/lib/ai-carddesk/carddesk.db');dest=sqlite3.connect(folder/'carddesk.sqlite')
source.backup(dest);assert dest.execute('PRAGMA integrity_check').fetchone()[0]=='ok';dest.close();source.close()
(folder/'encryption.key').write_bytes(Path('/var/lib/ai-carddesk/encryption.key').read_bytes())
PY
printf '%s\n' "$previous" > "$backup/previous-release"
chmod 600 "$backup/previous-release"
# A separate process completes additive migrations before accepting requests.
runuser -u carddesk -- env CARDDESK_DATA=/var/lib/ai-carddesk CARDDESK_ORIGIN=https://ai.mychangliapp.cn PYTHONPATH="$release" "$candidate" -c 'import app'
if [ -n "$previous_runtime" ]; then
  printf '%s\n' "$previous_runtime" > "$backup/previous-runtime"
  if [ ! -L "$runtime_dir" ]; then
    # Existing installations start with a directory at the stable systemd path.
    mv "$runtime_dir" "$previous_runtime"
  fi
  runtime_switched=1
  ln -sfn "$release/.venv" "$runtime_dir"
fi
ln -sfn "$release" /opt/ai-carddesk/current
systemctl start ai-carddesk
healthy=0
for step in $(seq 1 15); do
  if curl -fsS http://127.0.0.1:8808/api/health >/dev/null 2>&1; then healthy=1;break;fi
  sleep 1
done
[ "$healthy" -eq 1 ]
systemctl start ai-carddesk-worker
systemctl is-active --quiet ai-carddesk ai-carddesk-worker
trap - ERR
printf 'Release: %s\nPre-upgrade backup: %s\n' "$release" "$backup"
