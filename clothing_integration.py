"""Small adapters between optional clothing metadata and existing POS records.

No stock or monetary calculation lives here. Product and catalog identities remain
the checkout/receiving/transfer identities; descriptions are frozen only on new sales.
"""

import json

from sqlalchemy import inspect, text


def migrate_sale_display(db):
    """Add nullable snapshot storage without rewriting a historical sale line."""
    db.session.rollback()
    with db.engine.connect() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        try:
            inspector = inspect(connection)
            if inspector.has_table('sale_item') and 'display_snapshot' not in {
                    column['name'] for column in inspector.get_columns('sale_item')}:
                connection.execute(text('ALTER TABLE sale_item ADD COLUMN display_snapshot TEXT'))
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def variant_metadata(product, variant=None):
    """Return additive display fields; ordinary products remain ordinary products."""
    base = {'catalog_id': product.catalog_id, 'display_name': product.name}
    if variant is None:
        return base
    style = variant.style
    base.update(
        variant_id=variant.id, style_id=variant.style_id,
        style_name=style.name, style_code=style.code,
        brand=style.brand, season=style.season,
        size=variant.size, color=variant.color, archived=bool(variant.archived or style.archived),
        display_name=f'{product.name} — {variant.color} / {variant.size}',
    )
    return base


def product_display_snapshot(product, metadata):
    """Freeze new sale identity/attributes independently of future catalog edits."""
    return dict(metadata, name=metadata.get('display_name') or product.name,
                barcode=product.barcode, tax_rate=product.tax_rate or 0)


def sale_item_display(item, product=None):
    """Never infer clothing attributes for an unsnapshotted historical line."""
    raw = getattr(item, 'display_snapshot', None)
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, dict) and value.get('name'):
                return value
        except (TypeError, ValueError):
            pass
    return {'name': product.name if product else 'Deleted product'}