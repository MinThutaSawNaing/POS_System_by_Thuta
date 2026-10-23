"""Bounded inventory query primitives; no application/database side effects."""

import base64
import hashlib
import hmac
import json

from flask import current_app, request, session
from sqlalchemy import select, union_all


EXPORT_ROW_LIMIT = 5000
ORDER_LINE_LIMIT = 500


class InventoryQueryError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def bounded_integer(name, default, maximum):
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InventoryQueryError(f'{name} must be a positive integer') from None
    if value < 1:
        raise InventoryQueryError(f'{name} must be a positive integer')
    return min(value, maximum)


def prefix_pattern(value):
    """Treat user wildcards literally; leading wildcards defeat B-tree search."""
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def _scope():
    filters = sorted((key, value) for key, value in request.args.items(multi=True)
                     if key not in ('cursor', 'page'))
    return hashlib.sha256(json.dumps(
        [request.path, session.get('user_id'), session.get('branch_id'), filters],
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _signature(data):
    return hmac.new(str(current_app.secret_key).encode(), data, hashlib.sha256).hexdigest()


def _encode_cursor(identifier):
    data = json.dumps([identifier, _scope()], separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip('=') + '.' + _signature(data)


def cursor_identifier():
    raw = request.args.get('cursor')
    if not raw:
        return None
    if len(raw) > 1024:
        raise InventoryQueryError('Invalid cursor')
    try:
        encoded, signature = raw.split('.')
        data = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
        if not hmac.compare_digest(signature, _signature(data)):
            raise ValueError()
        identifier, scope = json.loads(data)
        if type(identifier) is not int or identifier < 1 or scope != _scope():
            raise ValueError()
        return identifier
    except (ValueError, TypeError, UnicodeError):
        raise InventoryQueryError('Invalid cursor or changed filters; restart from the first page') from None


def cursor_rows(query, identifier_column):
    size = bounded_integer('per_page', 30, 100)
    cursor = cursor_identifier()
    if cursor:
        query = query.filter(identifier_column < cursor)
    rows = query.order_by(None).order_by(identifier_column.desc()).limit(size + 1).all()
    more = len(rows) > size
    rows = rows[:size]
    return rows, {'has_more': more,
                  'next_cursor': _encode_cursor(rows[-1].id) if more else None}


def bounded_search_ids(probes, identifier_column):
    """Each indexed probe contributes only a page of newest candidates.

    The newest N distinct IDs across a union must occur in the newest N IDs
    of at least one constituent. Push the seek boundary into every probe,
    preventing broad prefix searches from materializing millions of IDs.
    """
    size = bounded_integer('per_page', 30, 100) + 1
    boundary = cursor_identifier()
    selects = []
    for probe in probes:
        if boundary:
            probe = probe.filter(identifier_column < boundary)
        candidates = probe.with_entities(identifier_column.label('id')).order_by(None).order_by(
            identifier_column.desc()).limit(size).subquery()
        selects.append(select(candidates.c.id))
    return union_all(*selects)


def export_rows(query, identifier_column):
    """Probe IDs first: never instantiate an oversized report's ORM graph."""
    ids = [row[0] for row in query.with_entities(identifier_column)
           .order_by(None).limit(EXPORT_ROW_LIMIT + 1).all()]
    if len(ids) > EXPORT_ROW_LIMIT:
        raise InventoryQueryError(
            f'Export exceeds {EXPORT_ROW_LIMIT:,} rows. Narrow the search or date range.', 413)
    return query.filter(identifier_column.in_(ids))