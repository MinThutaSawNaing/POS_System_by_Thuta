"""Create a verified, WAL-aware SQLite backup without importing the POS app."""

import argparse
from contextlib import closing
from datetime import datetime
from pathlib import Path
import os
import sqlite3


def backup_database(source, destination=None):
    source = Path(source).resolve(strict=True)
    if destination is None:
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        destination = source.with_name(f'{source.stem}_backup_{stamp}.db')
    destination = Path(destination).resolve()
    if source == destination:
        raise ValueError('Backup destination must differ from the source database')
    # Exclusive creation prevents an operator typo from overwriting another DB.
    descriptor = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as origin:
            with closing(sqlite3.connect(str(destination))) as target:
                origin.backup(target)
                result = target.execute('PRAGMA integrity_check').fetchall()
                if result != [('ok',)]:
                    raise RuntimeError(f'Backup integrity check failed: {result}')
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path,
                        default=Path(__file__).resolve().parent / 'instance' / 'pos.db')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        destination = backup_database(args.database, args.output)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        parser.exit(1, f'Backup failed: {error}\n')
    print(f'Verified backup created: {destination}')


if __name__ == '__main__':
    main()