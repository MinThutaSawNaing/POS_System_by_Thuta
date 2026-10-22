"""Additive clothing metadata; Product remains the branch inventory authority.

Importing this module never opens a database. The host supplies its existing
models/auth helpers to register_clothing, then explicitly runs migrate_clothing.
"""

import csv
import hashlib
import io
import json
import re
import unicodedata
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import jsonify, request, session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split()).casefold()


def migrate_clothing(db, models):
    """Repeatable migration creating ONLY our new tables, never altering Product."""
    names = ('ClothingStyle', 'ClothingVariant', 'ClothingImportCommit')
    db.metadata.create_all(bind=db.engine, tables=[models[name].__table__ for name in names],
                           checkfirst=True)


def register_clothing(app, db, context):
    """Register metadata and routes, returning models for audit/tools integration.

    Required context: Product, get_current_branch_id, login_required,
    manager_or_boss_required. All writes use existing manager/boss authorization.
    """
    Product = context['Product']
    current_branch = context['get_current_branch_id']
    login_required = context['login_required']
    manager_required = context['manager_or_boss_required']
    signer = URLSafeTimedSerializer(app.secret_key, salt='clothing-import-preview')

    def digest(rows):
        return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False).encode('utf-8')).hexdigest()

    class ClothingStyle(db.Model):
        __tablename__ = 'clothing_style'
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(100), nullable=False)
        code = db.Column(db.String(64))
        code_norm = db.Column(db.String(128), unique=True)
        brand = db.Column(db.String(100))
        season = db.Column(db.String(100))
        description = db.Column(db.Text)
        photo_filename = db.Column(db.String(255))
        archived = db.Column(db.Boolean, nullable=False, default=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    class ClothingVariant(db.Model):
        __tablename__ = 'clothing_variant'
        __table_args__ = (db.UniqueConstraint('style_id', 'size_norm', 'color_norm',
                                              name='uq_clothing_style_size_color'),)
        id = db.Column(db.Integer, primary_key=True)
        # Product.catalog_id is intentionally NOT a FK: one logical catalog has
        # multiple branch Products, so it is not a unique parent key.
        catalog_id = db.Column(db.String(64), nullable=False, unique=True)
        style_id = db.Column(db.Integer, db.ForeignKey('clothing_style.id'), nullable=False)
        size = db.Column(db.String(64), nullable=False)
        color = db.Column(db.String(64), nullable=False)
        size_norm = db.Column(db.String(128), nullable=False)
        color_norm = db.Column(db.String(128), nullable=False)
        archived = db.Column(db.Boolean, nullable=False, default=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        style = db.relationship(ClothingStyle, backref='variants')

    class ClothingImportCommit(db.Model):
        __tablename__ = 'clothing_import_commit'
        __table_args__ = (db.UniqueConstraint('branch_id', 'user_id', 'idempotency_key',
                                              name='uq_clothing_import_request'),)
        id = db.Column(db.Integer, primary_key=True)
        branch_id = db.Column(db.Integer, nullable=False)
        user_id = db.Column(db.Integer, nullable=False)
        idempotency_key = db.Column(db.String(128), nullable=False)
        fingerprint = db.Column(db.String(64), nullable=False)
        response_json = db.Column(db.Text, nullable=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    models = dict(ClothingStyle=ClothingStyle, ClothingVariant=ClothingVariant,
                  ClothingImportCommit=ClothingImportCommit)

    def fail(message, status=400, **extra):
        db.session.rollback()
        return jsonify(success=False, message=message, **extra), status

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('Expected a JSON object')
        return data

    def branch_id(data=None):
        branch = current_branch()
        if not branch:
            raise ValueError('Select an active branch')
        for supplied in (request.args.get('branch_id'), (data or {}).get('branch_id')):
            if supplied is not None and str(supplied) != str(branch):
                raise ValueError('Active branch changed. Reload the page')
        return branch

    def writer_lock():
        # Acquire before reading catalog/identity/stock, including import replay.
        db.session.rollback()
        if db.engine.dialect.name == 'sqlite':
            db.session.execute(text('BEGIN IMMEDIATE'))
        else:
            # PostgreSQL transactions serialize catalog writers without locking
            # existing Products, which are not ours to migrate.
            if db.engine.dialect.name == 'postgresql':
                db.session.execute(text('LOCK TABLE clothing_variant IN EXCLUSIVE MODE'))

    def string(value, field, limit, required=False):
        if value is None:
            value = ''
        if not isinstance(value, str):
            raise ValueError(f'{field} must be text')
        value = ' '.join(unicodedata.normalize('NFKC', value).split())
        if (required and not value) or len(value) > limit:
            raise ValueError(f'{field} must contain {"1" if required else "0"} to {limit} characters')
        return value

    def integer(value, field):
        if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]*', str(value)):
            raise ValueError(f'{field} must be a positive integer')
        number = int(value)
        if number > 2147483647:
            raise ValueError(f'{field} is too large')
        return number

    def money(value, field):
        if isinstance(value, bool):
            raise ValueError(f'{field} must be a non-negative amount with at most two decimals')
        try:
            number = Decimal(str(value))
            if not number.is_finite() or number < 0 or number > Decimal('999999999.99') or number != number.quantize(Decimal('.01')):
                raise InvalidOperation()
        except (ValueError, InvalidOperation):
            raise ValueError(f'{field} must be a non-negative amount with at most two decimals') from None
        return format(number, '.2f')

    def style_dict(style):
        return {key: getattr(style, key) for key in
                ('id', 'name', 'code', 'brand', 'season', 'description', 'photo_filename', 'archived')}

    def variant_dict(variant, product):
        result = dict(id=product.id, product_id=product.id, variant_id=variant.id,
                    catalog_id=variant.catalog_id, style_id=variant.style_id,
                    style_name=variant.style.name, size=variant.size, color=variant.color,
                    archived=bool(variant.archived or variant.style.archived),
                    variant_archived=variant.archived, name=product.name,
                    display_name=f'{product.name} — {variant.color} / {variant.size}',
                    barcode=product.barcode, price=product.price,
                    stock=product.stock or 0, branch_id=product.branch_id,
                    tax_rate=product.tax_rate or 0, unit_id=product.unit_id,
                    photo_filename=product.photo_filename or variant.style.photo_filename)
        if session.get('role') in ('manager', 'boss'):
            result['cost'] = product.cost
        return result

    def update_style(style, data):
        allowed = {'name': 100, 'code': 64, 'brand': 100, 'season': 100,
                   'description': 4000, 'photo_filename': 255}
        unknown = set(data) - set(allowed) - {'archived', 'branch_id'}
        if unknown:
            raise ValueError('Unsupported style fields: ' + ', '.join(sorted(unknown)))
        for field, limit in allowed.items():
            if field in data:
                value = string(data[field], field, limit, required=field == 'name')
                if field == 'photo_filename' and value and ('/' in value or '\\' in value or '..' in value):
                    raise ValueError('photo_filename must be a stored image filename')
                if field == 'photo_filename' and value:
                    folder = app.config.get('UPLOAD_FOLDER')
                    if not folder or Path(value).suffix.lower() not in ('.png', '.jpg', '.jpeg', '.gif', '.webp') or not (Path(folder) / value).is_file():
                        raise ValueError('photo_filename must reference an existing uploaded image')
                setattr(style, field, value or None)
        if not style.name:
            raise ValueError('name is required')
        if 'archived' in data:
            if not isinstance(data['archived'], bool):
                raise ValueError('archived must be boolean')
            style.archived = data['archived']
        style.code_norm = normalized(style.code) if style.code else None
        if style.code_norm and ClothingStyle.query.filter(
                ClothingStyle.code_norm == style.code_norm,
                ClothingStyle.id != (style.id or 0)).first():
            raise ValueError('Style code already exists')

    def parse_rows(data):
        if 'rows' in data and 'csv' in data:
            raise ValueError('Supply rows or csv, not both')
        rows = data.get('rows')
        if 'csv' in data:
            raw = data['csv']
            if not isinstance(raw, str) or len(raw) > 2000000:
                raise ValueError('csv must be text up to 2 MB')
            reader = csv.DictReader(io.StringIO(raw.lstrip('\ufeff')), strict=True)
            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)):
                raise ValueError('CSV has duplicate headers')
            rows = list(reader)
        if not isinstance(rows, list) or not rows or len(rows) > 1000:
            raise ValueError('Supply between 1 and 1000 explicit variant rows')
        return rows

    def validate_rows(rows, branch):
        errors, valid = [], []
        seen_combos, seen_barcodes, seen_catalogs = set(), set(), set()
        products = Product.query.all()
        barcode_catalogs = {}
        catalog_barcodes = {}
        for product in products:
            if product.barcode:
                barcode_catalogs.setdefault(normalized(product.barcode), set()).add(product.catalog_id)
            catalog_barcodes.setdefault(product.catalog_id, set()).add(product.barcode or None)
        allowed = {'style_id', 'size', 'color', 'barcode', 'name', 'price', 'cost', 'product_id', 'stock'}
        for index, row in enumerate(rows, 1):
            field = 'row'
            try:
                if not isinstance(row, dict):
                    raise ValueError('Row must be an object')
                unknown = set(row) - allowed
                if unknown:
                    raise ValueError('Unsupported fields: ' + ', '.join(str(x) for x in unknown))
                if 'stock' in row and (isinstance(row['stock'], bool) or str(row['stock']) not in ('0', '0.0')):
                    field = 'stock'
                    raise ValueError('New stock must be 0; receive stock via purchase orders')
                field = 'style_id'
                sid = integer(row.get('style_id'), field)
                style = db.session.get(ClothingStyle, sid)
                if not style or style.archived:
                    raise ValueError('Style not found or archived; select an explicit active style_id')
                field = 'size'
                size = string(row.get('size'), field, 64, required=True)
                field = 'color'
                color = string(row.get('color'), field, 64, required=True)
                combo = (sid, normalized(size), normalized(color))
                if combo in seen_combos or ClothingVariant.query.filter_by(
                        style_id=sid, size_norm=combo[1], color_norm=combo[2]).first():
                    raise ValueError('Style size/color already exists (including archived variants)')
                product = None
                if row.get('product_id') not in (None, ''):
                    field = 'product_id'
                    pid = integer(row['product_id'], field)
                    product = Product.query.filter_by(id=pid, branch_id=branch).first()
                    if not product:
                        raise ValueError('Product not found in current branch')
                    if not product.catalog_id:
                        raise ValueError('Existing product has no catalog identity; repair catalog explicitly first')
                    catalog = product.catalog_id
                    if catalog in seen_catalogs or ClothingVariant.query.filter_by(catalog_id=catalog).first():
                        raise ValueError('Catalog already attached to a variant')
                    if len(catalog_barcodes.get(catalog, set())) != 1:
                        raise ValueError('Barcode identity conflicts across branches for this catalog')
                    # Attach never overwrites historical identity or pricing.
                    for key in ('barcode', 'name', 'price', 'cost'):
                        if row.get(key) not in (None, ''):
                            supplied = money(row[key], key) if key in ('price', 'cost') else string(row[key], key, 100 if key == 'name' else 50)
                            existing = money(getattr(product, key) or 0, key) if key in ('price', 'cost') else (getattr(product, key) or '')
                            if supplied != existing:
                                raise ValueError(f'Attach cannot change existing product {key}')
                    barcode = product.barcode or ''
                    seen_catalogs.add(catalog)
                    result = dict(style_id=sid, size=size, color=color, product_id=product.id)
                else:
                    field = 'barcode'
                    barcode = string(row.get('barcode'), field, 50, required=True)
                    field = 'price'
                    price = money(row.get('price'), field)
                    field = 'cost'
                    cost = money(row.get('cost') if row.get('cost') not in (None, '') else 0, field)
                    field = 'name'
                    name = string(row.get('name') or style.name, field, 100, required=True)
                    result = dict(style_id=sid, size=size, color=color, barcode=barcode,
                                  name=name, price=price, cost=cost)
                field = 'barcode'
                if barcode:
                    barcode_norm = normalized(barcode)
                    # Scan identities may be shared ONLY by Products of the same
                    # logical catalog, never by unrelated catalogs in other branches.
                    identities = barcode_catalogs.get(barcode_norm, set())
                    if identities and (not product or identities != {product.catalog_id}):
                        raise ValueError('Barcode belongs to another catalog (possibly another branch)')
                    if barcode_norm in seen_barcodes:
                        raise ValueError('Duplicate barcode in import')
                    seen_barcodes.add(barcode_norm)
                seen_combos.add(combo)
                valid.append(result)
            except (ValueError, TypeError) as error:
                errors.append(dict(row=index, field=field, message=str(error)))
        return valid, errors

    def create_row(row, branch):
        if row.get('product_id'):
            product = db.session.get(Product, row['product_id'])
        else:
            product = Product(catalog_id=uuid.uuid4().hex, branch_id=branch,
                              barcode=row['barcode'], name=row['name'],
                              price=float(row['price']), cost=float(row['cost']), stock=0)
            db.session.add(product)
            db.session.flush()
        variant = ClothingVariant(catalog_id=product.catalog_id, style_id=row['style_id'],
                                  size=row['size'], color=row['color'],
                                  size_norm=normalized(row['size']), color_norm=normalized(row['color']))
        db.session.add(variant)
        db.session.flush()
        return variant_dict(variant, product)

    @app.route('/api/clothing/styles', methods=['GET', 'POST'], endpoint='clothing_styles')
    @login_required
    def styles():
        if request.method == 'GET':
            try:
                branch_id()
                query = ClothingStyle.query
                if request.args.get('include_archived') != 'true':
                    query = query.filter_by(archived=False)
                return jsonify(success=True, styles=[style_dict(s) for s in query.order_by(ClothingStyle.name, ClothingStyle.id).all()])
            except ValueError as error:
                return fail(str(error))
        return write_style()

    @manager_required
    def write_style(style_id=None):
        try:
            data = body() if request.method != 'DELETE' else (request.get_json(silent=True) or {})
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object')
            branch_id(data)
            writer_lock()
            style = db.session.get(ClothingStyle, style_id) if style_id else ClothingStyle()
            if style is None:
                return fail('Style not found', 404)
            if request.method == 'DELETE':
                style.archived = True
            else:
                update_style(style, data)
            db.session.add(style)
            db.session.flush()
            result = style_dict(style)
            db.session.commit()
            return jsonify(success=True, style=result), (201 if style_id is None else 200)
        except ValueError as error:
            return fail(str(error))
        except (IntegrityError, OperationalError):
            return fail('Catalog changed or is busy. Reload and retry.', 409)

    @app.route('/api/clothing/styles/<int:style_id>', methods=['GET', 'PUT', 'DELETE'], endpoint='clothing_style')
    @login_required
    def single_style(style_id):
        if request.method != 'GET':
            return write_style(style_id)
        try:
            branch_id()
            style = db.session.get(ClothingStyle, style_id)
            if not style:
                return fail('Style not found', 404)
            return jsonify(success=True, style=style_dict(style))
        except ValueError as error:
            return fail(str(error))

    @manager_required
    def write_variant(style_id, attach=False):
        try:
            data = body()
            branch = branch_id(data)
            writer_lock()
            row = {key: value for key, value in data.items() if key != 'branch_id'}
            if 'style_id' in row and row['style_id'] != style_id:
                raise ValueError('style_id does not match URL')
            row['style_id'] = style_id
            if attach and not row.get('product_id'):
                raise ValueError('Explicit product_id is required for attachment')
            if not attach and row.get('product_id'):
                raise ValueError('Use the explicit attach endpoint for existing Products')
            rows, errors = validate_rows([row], branch)
            if errors:
                return fail('Variant validation failed', errors=errors)
            result = create_row(rows[0], branch)
            db.session.commit()
            return jsonify(success=True, variant=result), 201
        except ValueError as error:
            return fail(str(error))
        except (IntegrityError, OperationalError):
            return fail('Catalog changed or is busy. Reload and retry.', 409)

    @app.route('/api/clothing/styles/<int:style_id>/variants', methods=['GET', 'POST'], endpoint='clothing_variants')
    @login_required
    def variants(style_id):
        if request.method == 'POST':
            return write_variant(style_id)
        try:
            branch = branch_id()
            style = db.session.get(ClothingStyle, style_id)
            if not style:
                return fail('Style not found', 404)
            query = db.session.query(ClothingVariant, Product).join(
                Product, Product.catalog_id == ClothingVariant.catalog_id).filter(
                    ClothingVariant.style_id == style_id, Product.branch_id == branch)
            if request.args.get('include_archived') != 'true':
                query = query.join(ClothingStyle, ClothingVariant.style_id == ClothingStyle.id).filter(
                    ClothingVariant.archived.is_(False), ClothingStyle.archived.is_(False))
            return jsonify(success=True, variants=[variant_dict(v, p) for v, p in query.order_by(ClothingVariant.id).all()])
        except ValueError as error:
            return fail(str(error))

    @app.route('/api/clothing/styles/<int:style_id>/attach', methods=['POST'], endpoint='clothing_attach')
    @login_required
    def attach(style_id):
        return write_variant(style_id, attach=True)

    @app.route('/api/clothing/variants/<int:variant_id>', methods=['GET', 'PUT', 'DELETE'], endpoint='clothing_variant')
    @login_required
    def single_variant(variant_id):
        if request.method != 'GET':
            return archive_variant(variant_id)
        try:
            branch = branch_id()
            variant = db.session.get(ClothingVariant, variant_id)
            product = Product.query.filter_by(catalog_id=variant.catalog_id, branch_id=branch).first() if variant else None
            if not product:
                return fail('Variant not found in current branch', 404)
            return jsonify(success=True, variant=variant_dict(variant, product))
        except ValueError as error:
            return fail(str(error))

    @manager_required
    def archive_variant(variant_id):
        try:
            data = body() if request.method != 'DELETE' else (request.get_json(silent=True) or {})
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object')
            branch = branch_id(data)
            writer_lock()
            if set(data) - {'archived', 'branch_id'}:
                raise ValueError('Only archived may be changed; variant identity is immutable')
            archived = True if request.method == 'DELETE' else data.get('archived')
            if not isinstance(archived, bool):
                raise ValueError('archived must be boolean')
            variant = db.session.get(ClothingVariant, variant_id)
            product = Product.query.filter_by(catalog_id=variant.catalog_id, branch_id=branch).first() if variant else None
            if not product:
                return fail('Variant not found in current branch', 404)
            variant.archived = archived
            db.session.commit()
            return jsonify(success=True, variant=variant_dict(variant, product))
        except ValueError as error:
            return fail(str(error))
        except (IntegrityError, OperationalError):
            return fail('Catalog changed or is busy. Reload and retry.', 409)

    @app.route('/api/clothing/import/preview', methods=['POST'], endpoint='clothing_import_preview')
    @login_required
    @manager_required
    def preview():
        try:
            data = body()
            branch = branch_id(data)
            rows, errors = validate_rows(parse_rows(data), branch)
            token = signer.dumps(dict(branch_id=branch, user_id=session['user_id'], fingerprint=digest(rows))) if not errors else None
            return jsonify(success=True, valid=not errors, rows=rows, errors=errors,
                           branch_id=branch, preview_token=token,
                           stock_policy='Stock starts at 0; receive via purchase orders')
        except (ValueError, csv.Error) as error:
            return fail(str(error))

    @app.route('/api/clothing/import/commit', methods=['POST'], endpoint='clothing_import_commit')
    @login_required
    @manager_required
    def commit_import():
        try:
            data = body()
            branch = branch_id(data)
            key = string(data.get('idempotency_key'), 'idempotency_key', 128, required=True)
            rows = parse_rows(data)
            fingerprint = digest(rows)
            token = string(data.get('preview_token'), 'preview_token', 2048, required=True)
            claims = signer.loads(token)
            if claims != dict(branch_id=branch, user_id=session['user_id'], fingerprint=fingerprint):
                raise ValueError('Preview does not match these rows, user or branch; preview again')
            writer_lock()
            prior = ClothingImportCommit.query.filter_by(branch_id=branch, user_id=session['user_id'], idempotency_key=key).first()
            if prior:
                if prior.fingerprint != fingerprint:
                    return fail('Idempotency key was used for different import rows', 409)
                result = json.loads(prior.response_json)
                result['replayed'] = True
                db.session.rollback()
                return jsonify(result)
            # Durable successful retries remain valid after token expiry; only
            # a new import must be committed within the review window.
            signer.loads(token, max_age=1800)
            validated, errors = validate_rows(rows, branch)
            if errors:
                return fail('Import validation failed; nothing was imported', errors=errors, valid=False)
            results = [create_row(row, branch) for row in validated]
            result = dict(success=True, imported=len(results), variants=results, replayed=False)
            db.session.add(ClothingImportCommit(branch_id=branch, user_id=session['user_id'],
                idempotency_key=key, fingerprint=fingerprint, response_json=json.dumps(result)))
            db.session.commit()
            return jsonify(result), 201
        except SignatureExpired:
            return fail('Preview expired; preview the import again')
        except BadSignature:
            return fail('Invalid preview token; preview the import again')
        except (ValueError, TypeError, csv.Error) as error:
            return fail(str(error))
        except (IntegrityError, OperationalError):
            return fail('Catalog changed or is busy. Retry the same idempotency key.', 409)

    return models