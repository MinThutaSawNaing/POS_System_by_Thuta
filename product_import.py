"""Bounded catalog-only XLSX import, with signed, state-bound previews.

No spreadsheet code is executed, relationships are never fetched, and uploads
are never extracted to disk. Stock belongs to the warehouse receiving workflow.
"""
import hashlib
import csv
import io
import json
import posixpath
import re
import zlib
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from functools import wraps
from zipfile import ZipFile, BadZipFile, ZIP_STORED, ZIP_DEFLATED
from xml.etree import ElementTree as ET

from flask import jsonify, request, session, send_file
from itsdangerous import URLSafeTimedSerializer, BadData
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
import xlsxwriter


MAX_UPLOAD = 5 * 1024 * 1024
MAX_EXPANDED = 20 * 1024 * 1024
MAX_MEMBER = 10 * 1024 * 1024
MAX_ROWS = 2000
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
ALIASES = {
    'name': ('name', 'product name', 'item name'),
    'barcode': ('barcode', 'sku'),
    'price': ('price', 'selling price'),
    'cost': ('cost', 'cost price'),
    'category': ('category',),
    'unit': ('unit',),
    'tax_rate': ('tax rate', 'tax rate (%)', 'tax %'),
}


def _norm(value):
    return ' '.join(str(value or '').strip().casefold().split())


def _xlsx(data):
    """Return bounded rows of (text, formula, numeric, format) cells."""
    try:
        archive = ZipFile(io.BytesIO(data))
        with archive:
            members = archive.infolist()
            if (len(members) > 200 or len({m.filename for m in members}) != len(members)
                    or sum(m.file_size for m in members) > MAX_EXPANDED
                    or any(m.file_size > MAX_MEMBER or m.flag_bits & 1
                           or m.compress_type not in (ZIP_STORED, ZIP_DEFLATED) for m in members)):
                raise ValueError('Workbook exceeds safe ZIP limits')

            def xml(path):
                raw = archive.read(path)
                if re.search(br'<!\s*(DOCTYPE|ENTITY)', raw, re.I) or b'\x00' in raw:
                    raise ValueError('DTD, entities and non-UTF8 XML are not supported')
                return ET.fromstring(raw)

            strings = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                # Only text runs, not phonetic properties/formatting, form values.
                strings = [''.join(t.text or '' for t in si.findall('.//s:t', NS))
                           for si in xml('xl/sharedStrings.xml')]
            formats = []
            if 'xl/styles.xml' in archive.namelist():
                styles = xml('xl/styles.xml')
                custom = {e.get('numFmtId'): e.get('formatCode', '')
                          for e in styles.findall('s:numFmts/s:numFmt', NS)}
                builtins = {9: '0%', 10: '0.00%'}
                builtins.update({i: 'yyyy-mm-dd' for i in (*range(14, 23), *range(27, 37), *range(45, 48), *range(50, 59))})
                formats = [custom.get(e.get('numFmtId'), builtins.get(int(e.get('numFmtId', 0)), ''))
                           for e in styles.findall('s:cellXfs/s:xf', NS)]
            relations = {e.get('Id'): e for e in xml('xl/_rels/workbook.xml.rels')}
            sheets = xml('xl/workbook.xml').findall('s:sheets/s:sheet', NS)
            fallback = None
            for sheet in sheets:
                if sheet.get('state', 'visible') != 'visible':
                    continue
                relation = relations.get(sheet.get(REL))
                if relation is None or relation.get('TargetMode') == 'External':
                    continue
                target = relation.get('Target', '')
                path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
                if not path.startswith('xl/worksheets/') or not path.endswith('.xml'):
                    raise ValueError('Unsupported worksheet relationship')
                rows = []
                for row in xml(path).findall('s:sheetData/s:row', NS):
                    number = int(row.get('r', len(rows) + 1))
                    if number > MAX_ROWS + 25 or len(rows) >= MAX_ROWS + 25:
                        raise ValueError('At most 2000 data rows and 25 header rows are supported')
                    cells = {}
                    for cell in row.findall('s:c', NS):
                        match = re.fullmatch(r'([A-Z]+)([1-9][0-9]*)', cell.get('r', ''))
                        if not match:
                            raise ValueError('Invalid cell address')
                        column = 0
                        for char in match[1]:
                            column = column * 26 + ord(char) - 64
                        if column > 50:
                            raise ValueError('At most 50 columns are supported')
                        if column in cells:
                            raise ValueError('Duplicate cell address')
                        kind = cell.get('t', 'n')
                        value = cell.findtext('s:v', '', NS)
                        if kind == 's':
                            value = strings[int(value)]
                        elif kind == 'inlineStr':
                            value = ''.join(t.text or '' for t in cell.findall('s:is//s:t', NS))
                        style = int(cell.get('s', 0))
                        fmt = formats[style] if 0 <= style < len(formats) else ''
                        if kind not in ('n', 's', 'inlineStr', 'str'):
                            fmt = '!unsupported-cell-type'
                        cells[column] = (value, cell.find('s:f', NS) is not None, kind == 'n', fmt)
                    if cells:
                        rows.append((number, [cells.get(i, ('', False, False, ''))
                                              for i in range(1, max(cells) + 1)]))
                if any(any(c[0] for c in cells) for _, cells in rows):
                    if fallback is None:
                        fallback = rows
                    if any(sum(any(_norm(c[0]) in aliases for c in cells)
                               for aliases in ALIASES.values()) >= 2 for _, cells in rows[:25]):
                        return rows
            if fallback is not None:
                return fallback
    except (BadZipFile, ET.ParseError, KeyError, IndexError, TypeError, OverflowError, RuntimeError, zlib.error) as error:
        raise ValueError('Invalid or unsupported XLSX workbook') from error
    raise ValueError('Workbook has no visible data worksheet')


def _csv(data):
    try:
        reader = csv.reader(io.StringIO(data.decode('utf-8-sig')), strict=True)
        rows = []
        for index, values in enumerate(reader, 1):
            if index > MAX_ROWS + 25 or len(values) > 50:
                raise ValueError('At most 2000 data rows, 25 header rows and 50 columns are supported')
            # Formula-looking CSV text has no cached formula/result distinction;
            # reject it only when mapped, rather than ever evaluating it.
            rows.append((index, [(v, v.lstrip().startswith('='), False, '') for v in values]))
        if not rows:
            raise ValueError('File is empty')
        return rows
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError('CSV must contain valid UTF-8 comma-separated text') from error


def _table(rows, supplied):
    """Find headers in report metadata, or use first nonblank unknown header."""
    candidates = rows[:25]
    wanted = {_norm(v) for v in supplied.values() if v}

    def score(row):
        labels = {_norm(c[0]) for c in row[1]}
        if wanted:
            return len(labels & wanted) * 10
        return sum(any(alias in labels for alias in aliases) for aliases in ALIASES.values())

    header = max(candidates, key=score)
    if score(header) == 0:
        header = next((r for r in rows if any(c[0].strip() for c in r[1])), rows[0])
    columns = [c[0].strip() for c in header[1]]
    while columns and not columns[-1]:
        columns.pop()
    nonblank = [c for c in columns if c]
    if len({_norm(c) for c in nonblank}) != len(nonblank):
        raise ValueError('Duplicate column headers are ambiguous; rename them before importing')
    mapping = {}
    for key, aliases in ALIASES.items():
        if key in supplied:
            if supplied[key] and supplied[key] not in columns:
                raise ValueError('Mapped column does not exist: ' + str(supplied[key]))
            if supplied[key]:
                mapping[key] = supplied[key]
        else:
            matches = [c for c in columns if _norm(c) in aliases]
            if matches:
                mapping[key] = matches[0]
    if len(set(mapping.values())) != len(mapping):
        raise ValueError('A source column cannot be mapped to multiple catalog fields')
    body = [(n, c) for n, c in rows if n > header[0] and any(x[0].strip() or x[1] for x in c)]
    # Our own report has a fixed heading, column layout and a stock-only TOTAL
    # footer. Never interpret a client product merely named "Total" as a footer.
    own_report = (header[0] == 4 and rows[0][1][0][0] == 'Product List'
                  and columns == ['Barcode', 'Product Name', 'Category', 'Price', 'Stock', 'Unit', 'Tax rate'])
    if own_report:
        if body:
            _, last = body[-1]
            if (last[0][0] == 'TOTAL' and not last[0][1]
                    and all(not cell[0] and not cell[1] for i, cell in enumerate(last) if i not in (0, 4))):
                body.pop()
        cleaned = []
        for n, cells in body:
            cleaned.append((n, [('', *cell[1:]) if i in (2, 5) and cell[0] == '—' else cell
                                for i, cell in enumerate(cells)]))
        body = cleaned
    if len(body) > MAX_ROWS:
        raise ValueError('At most 2000 data rows are supported')
    return columns, mapping, body


def _number(value, field):
    try:
        number = Decimal(value)
        limit = Decimal(100) if field == 'tax_rate' else Decimal('999999999999.99')
        if not number.is_finite() or number < 0 or number > limit:
            raise ValueError()
        return float(number)
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError(field + ' must be a finite nonnegative number' + (' between 0 and 100' if field == 'tax_rate' else '')) from None


def _plan(columns, mapping, rows, products, categories, units, mode, create_categories):
    barcodes = {p.barcode: p for p in products if p.barcode}
    names, cats, measures = defaultdict(list), defaultdict(list), defaultdict(list)
    for product in products:
        names[_norm(product.name)].append(product)
    for category in categories:
        cats[_norm(category.name)].append(category)
    for unit in units:
        for label in {_norm(unit.name), _norm(unit.symbol)}:
            measures[label].append(unit)
    warnings = []
    if any(_norm(c) in ('stock', 'quantity', 'qty', 'current stock') for c in columns):
        warnings.append('Stock is ignored. New products start at zero stock; use warehouse receiving to add stock.')
    unknown = [c for c in columns if c and c not in mapping.values() and _norm(c) not in ('stock', 'quantity', 'qty')]
    if unknown:
        warnings.append('Unmapped columns ignored: ' + ', '.join(unknown))
    prepared = []
    identities = defaultdict(list)
    for number, cells in rows:
        values, errors = {}, []
        for key, source in mapping.items():
            index = columns.index(source)
            value, formula, numeric, fmt = cells[index] if index < len(cells) else ('', False, False, '')
            value = value.strip()
            if formula:
                errors.append('Mapped formulas are not allowed (' + source + ')')
            plain_format = re.sub(r'"[^\"]*"|\\.|\[[^\]]*\]', '', fmt)
            date_format = bool(re.search(r'[ymdhs]', plain_format, re.I))
            if fmt == '!unsupported-cell-type' or (numeric and date_format):
                errors.append('Unsupported boolean, error or date cell in mapped column: ' + source)
            if numeric and value and '%' in plain_format and key in ('price', 'cost', 'tax_rate'):
                if key == 'tax_rate':
                    try:
                        value = str(Decimal(value) * 100)
                    except InvalidOperation:
                        errors.append('Invalid percentage tax rate')
                else:
                    errors.append('Percentage format is not allowed for price or cost')
            if key == 'barcode' and value and numeric:
                try:
                    digits = Decimal(value)
                    if not digits.is_finite() or digits < 0 or digits != digits.to_integral_value() or digits >= Decimal('1e15'):
                        raise ValueError()
                    value = str(int(digits))
                    if re.fullmatch('0+', fmt):
                        value = value.zfill(len(fmt))
                except (InvalidOperation, ValueError, OverflowError):
                    errors.append('Ambiguous numeric barcode; format it as text (maximum 15 numeric digits)')
            values[key] = value
        name, barcode = values.get('name', ''), values.get('barcode', '')
        row = {'row': number, 'name': name, 'barcode': barcode, 'action': 'error', 'message': ''}
        prepared.append((row, values, errors))
        if barcode:
            identities[('barcode', barcode)].append(errors)
        if name:
            identities[('name', _norm(name))].append(errors)
    for repeated in identities.values():
        if len(repeated) > 1:
            for errors in repeated:
                errors.append('Repeated file identity (name or barcode)')
    plans, result = [], []
    for row, values, errors in prepared:
        name, barcode = row['name'], row['barcode']
        existing = barcodes.get(barcode) if barcode else None
        same_names = names.get(_norm(name), []) if name else []
        if len(name) > 100 or len(barcode) > 50:
            errors.append('Name or barcode exceeds maximum length (100/50)')
        if len(same_names) > 1:
            errors.append('Ambiguous existing product name')
        if barcode and same_names and any(p.barcode != barcode for p in same_names):
            errors.append('Barcode/name collision with a different existing product')
        if not name and existing is None:
            errors.append('Name is required for new products; map a name column')
        action = ('skip' if mode == 'skip' else 'update') if existing else ('skip' if not barcode and len(same_names) == 1 else 'create')
        changes = {}
        if name:
            changes['name'] = name
        for key in ('price', 'cost', 'tax_rate'):
            if values.get(key):
                try:
                    changes[key] = _number(values[key], key)
                except ValueError as error:
                    errors.append(str(error))
        if action == 'create' and 'price' not in changes:
            errors.append('Price is required for new products; map a price column')
        category_name = values.get('category', '')
        category = None
        if category_name:
            matches = cats.get(_norm(category_name), [])
            if len(category_name) > 50 or len(matches) > 1:
                errors.append('Category name is too long or ambiguous')
            elif matches:
                category = matches[0]
                if not category.is_active:
                    errors.append('Category is inactive: ' + category_name)
            elif not create_categories:
                errors.append('Unknown category: ' + category_name + '; enable create missing categories to import')
            changes['category'] = category.name if category else category_name
        unit_name = values.get('unit', '')
        if unit_name:
            matches = measures.get(_norm(unit_name), [])
            if len(matches) != 1 or not matches[0].is_active:
                errors.append('Unknown, inactive or ambiguous unit: ' + unit_name)
            else:
                changes['unit_id'] = matches[0].id
        if errors:
            row['message'] = '; '.join(dict.fromkeys(errors))
        else:
            row['action'] = action
            row['message'] = {'create': 'Create product (stock zero)', 'update': 'Update supplied nonblank catalog fields', 'skip': 'Existing product skipped'}[action]
            if not barcode and action == 'skip':
                row['message'] = 'Existing name matched; name-only rows are never updated'
            if category_name and category is None and action != 'skip':
                row['message'] += '; create active category: ' + category_name
                warnings.append('Will create active category: ' + category_name)
            if action != 'skip':
                row['changes'] = changes.copy()
                plans.append((action, existing, barcode, changes, category))
        result.append(row)
    summary = {action: sum(r['action'] == action for r in result) for action in ('create', 'update', 'skip', 'error')}
    return {'rows': result, 'summary': summary}, list(dict.fromkeys(warnings)), plans


def register_product_import(app, db, models):
    """Register explicitly authorized routes using the application's models/helpers."""
    Product, Category, Unit = (models[k] for k in ('Product', 'Category', 'Unit'))
    Branch, User = models['Branch'], models['User']
    signer = URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='product-import-v1')

    def authorized(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            if not session.get('user_id'):
                return jsonify(success=False, message='Sign in required'), 401
            if session.get('role') not in ('manager', 'boss'):
                return jsonify(success=False, message='Manager access required'), 403
            user = db.session.get(User, session['user_id'])
            branch_id = models['get_current_branch_id']()
            branch = db.session.get(Branch, branch_id) if branch_id else None
            if not user or user.role not in ('manager', 'boss') or not branch or not branch.is_active:
                return jsonify(success=False, message='Active user and branch required'), 403
            return handler(branch_id, *args, **kwargs)
        return wrapped

    @app.get('/api/products/import/template')
    @authorized
    def product_import_template(branch_id):
        output = io.BytesIO()
        book = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = book.add_worksheet('Products')
        sheet.write_row(0, 0, ['Name', 'Barcode', 'Price', 'Cost', 'Category', 'Unit', 'Tax Rate'])
        sheet.freeze_panes(1, 0)
        sheet.set_column('A:A', 28)
        sheet.set_column('B:B', 22, book.add_format({'num_format': '@'}))
        sheet.set_column('C:G', 18)
        book.close()
        output.seek(0)
        return send_file(output, as_attachment=True, download_name='product_import_template.xlsx',
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def handle(branch_id, commit=False):
        try:
            upload = request.files.get('file')
            if not upload or not upload.filename or not upload.filename.lower().endswith(('.xlsx', '.csv')):
                raise ValueError('Upload an .xlsx workbook or UTF-8 .csv file')
            data = upload.stream.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise ValueError('Workbook upload must be at most 5 MB')
            mode = request.form.get('duplicate_mode', 'skip')
            if mode not in ('skip', 'update'):
                raise ValueError('duplicate_mode must be skip or update')
            supplied = json.loads(request.form.get('mapping') or '{}')
            if not isinstance(supplied, dict) or any(k not in ALIASES or not isinstance(v, str) for k, v in supplied.items()):
                raise ValueError('mapping must be an object of canonical fields to column headers')
            create_categories = request.form.get('create_categories', 'false').lower() == 'true'
            columns, mapping, rows = _table(_csv(data) if upload.filename.lower().endswith('.csv') else _xlsx(data), supplied)
            if commit:
                try:
                    signed = signer.loads(request.form.get('preview_token', ''), max_age=900)
                except BadData:
                    return jsonify(success=False, message='Preview expired or invalid. Preview the file again.'), 409
                # Drop authorization reads before acquiring the SQLite writer lock.
                db.session.rollback()
                db.session.execute(text('BEGIN IMMEDIATE'))
            products = Product.query.filter_by(branch_id=branch_id).order_by(Product.id).all()
            categories = Category.query.filter_by(branch_id=branch_id).order_by(Category.id).all()
            units = Unit.query.order_by(Unit.id).all()
            snapshot = [[{c.name: str(getattr(obj, c.name)) for c in obj.__table__.columns}
                         for obj in objects] for objects in (products, categories, units)]
            context = {'branch': branch_id, 'user': session['user_id'], 'role': session['role'],
                       'file': hashlib.sha256(data).hexdigest(), 'mapping': mapping, 'mode': mode,
                       'create_categories': create_categories,
                       'state': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()}
            if commit and signed != context:
                db.session.rollback()
                return jsonify(success=False, message='Preview is stale or does not match this request. Preview again.'), 409
            preview, warnings, plans = _plan(columns, mapping, rows, products, categories, units, mode, create_categories)
            if not commit:
                return jsonify(success=True, columns=columns, mapping=mapping, preview=preview,
                               warnings=warnings, preview_token=signer.dumps(context))
            if preview['summary']['error'] or not rows:
                db.session.rollback()
                return jsonify(success=False, message='Resolve every preview error before importing.',
                               columns=columns, mapping=mapping, preview=preview, warnings=warnings), 400
            created_categories = {}
            for action, existing, barcode, changes, category in plans:
                if changes.get('category'):
                    if category is None:
                        key = _norm(changes['category'])
                        category = created_categories.get(key)
                        if category is None:
                            category = Category(name=changes['category'], branch_id=branch_id, is_active=True)
                            db.session.add(category)
                            db.session.flush()
                            created_categories[key] = category
                    changes['category_id'] = category.id
                    changes['category'] = category.name
                if action == 'create':
                    db.session.add(Product(branch_id=branch_id, barcode=barcode or None, stock=0, **changes))
                else:
                    for key, value in changes.items():
                        setattr(existing, key, value)
            db.session.commit()
            return jsonify(success=True, summary=preview['summary'], warnings=warnings)
        except (ValueError, json.JSONDecodeError) as error:
            db.session.rollback()
            return jsonify(success=False, message=str(error)), 400
        except (IntegrityError, OperationalError):
            db.session.rollback()
            return jsonify(success=False, message='Catalog changed or is busy. Preview again.'), 409
        except Exception:
            db.session.rollback()
            raise

    @app.post('/api/products/import/preview')
    @authorized
    def product_import_preview(branch_id):
        return handle(branch_id)

    @app.post('/api/products/import/commit')
    @authorized
    def product_import_commit(branch_id):
        return handle(branch_id, commit=True)