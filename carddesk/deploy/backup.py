"""Consistent daily backup. Deliberately excludes ephemeral account credentials."""
import sqlite3
import os
from pathlib import Path
from datetime import datetime,timezone
os.umask(0o077)
folder=Path('/var/backups/ai-carddesk');folder.mkdir(mode=0o700,parents=True,exist_ok=True)
stamp=datetime.now(timezone.utc).strftime('%Y%m%d')
target=folder/(stamp+'.sqlite')
source=sqlite3.connect('/var/lib/ai-carddesk/carddesk.db')
backup=sqlite3.connect(target)
source.backup(backup)
backup.execute('UPDATE orders SET credential=NULL')
backup.execute('DELETE FROM sessions')
for table in ('customer_sessions','email_challenges','checkout_grants'):
    if backup.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():backup.execute(f'DELETE FROM {table}')
backup.commit();backup.execute('VACUUM');backup.close();source.close()
# Back up the server encryption key separately with owner-only permissions.
(folder/'encryption.key').write_bytes(Path('/var/lib/ai-carddesk/encryption.key').read_bytes())
for old in sorted(folder.glob('*.sqlite'))[:-14]:old.unlink()
