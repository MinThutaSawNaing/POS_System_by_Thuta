"""Backup utility must include committed WAL data and never overwrite files."""

import sqlite3
from contextlib import closing
import pytest
from backup_database import backup_database


def test_backup_captures_uncheckpointed_wal_and_preserves_source(tmp_path):
    source = tmp_path / 'pos.db'
    with closing(sqlite3.connect(str(source))) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT)')
        connection.execute('INSERT INTO item(name) VALUES (?)', ('committed sale',))
        connection.commit()
        assert source.with_name('pos.db-wal').exists()
        destination = backup_database(source)
        with closing(sqlite3.connect(str(destination))) as backup:
            assert backup.execute('SELECT name FROM item').fetchall() == [('committed sale',)]
            assert backup.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert connection.execute('SELECT COUNT(*) FROM item').fetchone() == (1,)


def test_backup_rejects_existing_target_or_missing_source(tmp_path):
    source = tmp_path / 'pos.db'
    with closing(sqlite3.connect(str(source))) as connection:
        connection.execute('CREATE TABLE item (id INTEGER)')
    target = tmp_path / 'existing.db'
    target.write_bytes(b'keep existing backup')
    with pytest.raises(FileExistsError):
        backup_database(source, target)
    assert target.read_bytes() == b'keep existing backup'
    with pytest.raises(ValueError):
        backup_database(source, source)
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / 'missing.db')


def test_corrupt_source_does_not_leave_misleading_backup(tmp_path):
    source = tmp_path / 'bad.db'
    source.write_bytes(b'not a SQLite database')
    target = tmp_path / 'backup.db'
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, target)
    assert not target.exists()