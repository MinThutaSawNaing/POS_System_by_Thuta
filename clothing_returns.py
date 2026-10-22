"""Additive return migration and SQLite transaction boundary (no app import)."""

from functools import wraps

from flask import current_app, jsonify, request, session
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError


def migrate_clothing_returns(db):
    """Repeatable additive migration; call in app context after create_all().

    No financial history or quantities are rewritten. Legacy dispositions read
    as sellable, matching the pre-migration behavior. DDL is serialized across
    simultaneous application startups using SQLite's writer lock.
    """
    db.session.rollback()
    with db.engine.connect() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        try:
            additions = {
                'return_exchange': {
                    'reason': 'VARCHAR(300)',
                    'idempotency_key': 'VARCHAR(128)',
                    'request_fingerprint': 'VARCHAR(64)',
                    'response_json': 'TEXT',
                },
                'return_exchange_item': {
                    'reason': 'VARCHAR(300)',
                    'disposition': "VARCHAR(20) DEFAULT 'sellable'",
                },
            }
            for table, columns in additions.items():
                inspector = inspect(connection)
                if not inspector.has_table(table):
                    continue
                existing = {column['name'] for column in inspector.get_columns(table)}
                for name, definition in columns.items():
                    if name not in existing:
                        connection.exec_driver_sql(
                            f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            if inspect(connection).has_table('return_exchange'):
                connection.exec_driver_sql(
                    'CREATE UNIQUE INDEX IF NOT EXISTS ux_return_exchange_idempotency_key '
                    'ON return_exchange (idempotency_key)')
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def return_exchange_writer(db):
    """Lock BEFORE quantity reads; always release on validation/early returns.

    Authorization hooks run first. Their read-only transaction is discarded so
    a stale SQLite snapshot cannot be upgraded after another writer commits.
    The lock also serializes replacement deductions with other SQLite writers.
    """
    def decorate(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            if request.method != 'POST' or 'user_id' not in session:
                return handler(*args, **kwargs)
            try:
                db.session.rollback()
                db.session.execute(text('BEGIN IMMEDIATE'))
                return handler(*args, **kwargs)
            except OperationalError:
                current_app.logger.warning('Return/exchange writer unavailable', exc_info=True)
                return jsonify(success=False, message='Return/exchange is busy. Retry this request.'), 503
            finally:
                # This is harmless after a successful commit, and essential for
                # validation errors, replay and every other early return.
                db.session.rollback()
        return wrapped
    return decorate


def positive_quantity(value):
    """Reject booleans/fractional quantities instead of silently truncating."""
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError('A positive integer is required')
    result = int(value)
    if result < 1 or result > 2147483647:
        raise ValueError('A positive integer is required')
    return result