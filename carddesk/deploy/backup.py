"""Publish a consistent backup after removing temporary account credentials."""
import hashlib
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def backup_database(data_folder, folder):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = folder / (datetime.now(timezone.utc).strftime('%Y%m%d') + '.sqlite')
    source_uri = (data_folder / 'carddesk.db').resolve().as_uri() + '?mode=ro'

    # SQLite manages this temporary DB and releases its backing store on close.
    # VACUUM INTO copies only live records to the published snapshot.
    source_key = data_folder / 'encryption.key'
    key_data = source_key.read_bytes()
    key_id = hashlib.sha256(key_data).hexdigest()
    key_dir = folder / 'keys'
    key_dir.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as stage:
        staged = Path(stage)
        snapshot = staged / 'snapshot.sqlite'
        with closing(sqlite3.connect(source_uri, uri=True)) as source, closing(sqlite3.connect('')) as copy:
            source.backup(copy)
            copy.execute('UPDATE orders SET credential=NULL')
            for table in ('sessions', 'customer_sessions', 'email_challenges', 'checkout_grants'):
                if copy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    copy.execute(f'DELETE FROM {table}')
            copy.execute('CREATE TABLE IF NOT EXISTS carddesk_backup_key (id INTEGER PRIMARY KEY CHECK(id=1), sha256 TEXT NOT NULL)')
            copy.execute('INSERT OR REPLACE INTO carddesk_backup_key VALUES(1,?)', (key_id,))
            copy.commit()
            copy.execute('VACUUM INTO ?', (str(snapshot),))
        if source_key.read_bytes() != key_data:
            raise RuntimeError('Encryption key changed during backup; retry the backup')
        key = staged / 'encryption.key'
        key.write_bytes(key_data)
        key.chmod(0o600)
        snapshot.chmod(0o600)
        # A published snapshot names its immutable recovery key. Keep the
        # standalone legacy key untouched for snapshots made before this format.
        recovery_key = key_dir / (key_id + '.key')
        try:
            os.link(key, recovery_key)
        except FileExistsError:
            if recovery_key.read_bytes() != key_data:
                raise RuntimeError('Stored recovery key does not match its fingerprint')
        legacy = folder / 'encryption.key'
        if not legacy.exists():
            legacy_copy = staged / 'legacy.key'
            legacy_copy.write_bytes(key_data)
            legacy_copy.chmod(0o600)
            try:
                os.link(legacy_copy, legacy)
            except FileExistsError:
                pass
        snapshot.replace(target)

    for old in sorted(folder.glob('*.sqlite'))[:-14]:
        old.unlink()


if __name__ == '__main__':
    os.umask(0o077)
    backup_database(Path('/var/lib/ai-carddesk'), Path('/var/backups/ai-carddesk'))
