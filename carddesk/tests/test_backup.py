import hashlib
import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest


def test_daily_backup_is_scrubbed_and_survives_a_failed_retry(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('carddesk_backup', Path(__file__).parents[1] / 'deploy/backup.py')
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    data = tmp_path / 'data'
    folder = tmp_path / 'daily'
    data.mkdir()
    key = data / 'encryption.key'
    key.write_bytes(b'synthetic-key')
    with closing(sqlite3.connect(data / 'carddesk.db')) as source:
        source.execute('PRAGMA journal_mode=WAL')
        source.execute('CREATE TABLE orders(id INTEGER, credential TEXT)')
        source.execute("INSERT INTO orders VALUES(1,'synthetic-order-credential')")
        for table in ('sessions', 'customer_sessions', 'email_challenges', 'checkout_grants'):
            source.execute(f'CREATE TABLE {table}(token TEXT)')
            source.execute(f"INSERT INTO {table} VALUES('synthetic-temporary-session')")
        source.commit()
    backup.backup_database(data, folder)
    snapshot, = folder.glob('*.sqlite')
    before = snapshot.read_bytes()
    assert b'synthetic-order-credential' not in before
    assert b'synthetic-temporary-session' not in before
    with closing(sqlite3.connect(snapshot)) as restored:
        assert restored.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert restored.execute('SELECT * FROM orders').fetchall() == [(1, None)]
    key.unlink()
    with pytest.raises(FileNotFoundError):
        backup.backup_database(data, folder)
    assert snapshot.read_bytes() == before
    assert (folder / 'encryption.key').read_bytes() == b'synthetic-key'

    def recovery_key(database):
        with closing(sqlite3.connect(database)) as restored:
            key_id, = restored.execute('SELECT sha256 FROM carddesk_backup_key WHERE id=1').fetchone()
        recovered = (folder / 'keys' / (key_id + '.key')).read_bytes()
        assert hashlib.sha256(recovered).hexdigest() == key_id
        return recovered

    # Simulate a rotation and interruption immediately before snapshot publication.
    key.write_bytes(b'synthetic-rotated-key')
    original_replace = Path.replace
    def interrupt_snapshot(path, destination):
        if destination == snapshot:
            raise OSError('simulated publication interruption')
        return original_replace(path, destination)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', interrupt_snapshot)
        with pytest.raises(OSError):
            backup.backup_database(data, folder)
    assert snapshot.read_bytes() == before
    assert recovery_key(snapshot) == b'synthetic-key'
    backup.backup_database(data, folder)
    assert recovery_key(snapshot) == b'synthetic-rotated-key'
    assert (folder / 'encryption.key').read_bytes() == b'synthetic-key'
