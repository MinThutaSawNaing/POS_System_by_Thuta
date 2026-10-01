from flask import Flask, render_template, request, jsonify, session, redirect, url_for, make_response, send_from_directory, send_file, has_request_context, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import os
import re
import uuid
import io
import json
import time
import struct
import hmac
import math
import random
import threading
from sqlalchemy import inspect, text, func, event, or_, and_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as SQLAlchemySession
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib import colors
import pandas as pd
from reportlab.graphics.barcode import createBarcodeDrawing
from receipt import (
    DEFAULT_RECEIPT_BRAND_NAME,
    DEFAULT_RECEIPT_FOOTER,
    DEFAULT_RECEIPT_PAPER_SIZE,
    RECEIPT_PAPER_OPTIONS,
    build_delivery_slip_view,
    build_receipt_snapshot,
    build_receipt_view,
    detect_receipt_logo_extension,
    normalize_receipt_identity,
    normalize_receipt_paper_size,
)
from reports import (
    LOW_STOCK_THRESHOLD,
    build_delivery_performance_report,
    build_delivery_performance_rows,
    build_purchase_order_item_sheet,
    build_purchase_order_report,
    build_report_pdf,
    build_report_xlsx,
    build_warehouse_stock_report,
    delivery_courier_performance,
    describe_filters,
    normalize_report_format,
    purchase_order_status_label,
    report_content_type,
    report_disposition,
    report_filename,
    summarize_delivery_performance,
)
from reportlab.graphics import renderPDF
from reportlab.graphics import renderSVG
from reportlab.graphics.shapes import Drawing
import pytz
from functools import wraps
from reportlab.graphics.barcode import createBarcodeDrawing
import base64
import hashlib
import zlib
from cryptography.fernet import Fernet, InvalidToken

# Import AI Agent modules
from agent_orchestrator import get_orchestrator


def _load_local_env(path=None):
    """Populate os.environ from a local .env file without overriding real vars.

    Deployment secrets (such as the account-creation barrier) are provisioned in
    an untracked ``.env`` file so they never enter version control. Compose reads
    that file on its own, but bare-metal launches did not, which left ``.env``
    secrets unavailable outside Docker. Loading it here keeps a single source of
    truth for every launch. Values already present in the real environment win.
    """
    env_path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    try:
        with open(env_path, 'r', encoding='utf-8') as handle:
            lines = handle.readlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_local_env()


app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your_super_secret_key_here')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'uploads', 'products')
app.config['RECEIPT_LOGO_FOLDER'] = os.path.join(app.root_path, 'uploads', 'receipts')
app.config['MMQR_FOLDER'] = os.path.join(app.root_path, 'uploads', 'mmqr')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB per request
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365)
db = SQLAlchemy(app)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['RECEIPT_LOGO_FOLDER'], exist_ok=True)
os.makedirs(app.config['MMQR_FOLDER'], exist_ok=True)

MONEY_QUANT = Decimal('0.01')
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
CURRENCY_OPTIONS = {
    'USD': '$',
    'MMK': 'MMK',
    'THB': 'THB'
}
UNIT_TYPES = {'weight', 'volume', 'count', 'length', 'custom'}
UNIT_TYPE_LABELS = {
    'weight': 'Weight',
    'volume': 'Volume',
    'count': 'Count',
    'length': 'Length',
    'custom': 'Custom'
}
DELIVERY_STAGE_FLOW = {
    'to_deliver': ['packaged', 'cancelled'],
    'packaged': ['delivering', 'cancelled'],
    'delivering': ['delivered', 'cancelled'],
    'delivered': [],
    'cancelled': []
}
DELIVERY_STAGE_LABELS = {
    'to_deliver': 'To Deliver',
    'packaged': 'Packaged',
    'delivering': 'Delivering',
    'delivered': 'Delivered',
    'cancelled': 'Cancelled'
}
DELIVERY_PRIORITIES = {'low', 'normal', 'high', 'urgent'}

def to_decimal(value):
    return Decimal(str(value))

def safe_to_decimal(value, default=Decimal('0')):
    """Convert a value to Decimal, returning ``default`` for None/NaN/inf/unparseable input."""
    if value is None:
        return default
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return default
    if not result.is_finite():
        return default
    return result


def round_money(value):
    """Round to 2 decimals with ROUND_HALF_UP, staying in Decimal space.

    Intermediate money math stays on Decimal so totals match the JS frontend's
    integer-cent arithmetic (Math.round per-item). Only final persistence/
    display boundaries (Float columns, JSON output) convert to float via
    money_float() or json_default().
    """
    return to_decimal(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def money_float(value):
    """Convert a rounded monetary value to a JSON-safe float.

    Some legitimate monetary fields, such as ``Sale.cash_received`` for debt
    or exchange sales, are nullable. At display/export boundaries, represent a
    missing amount as 0.00 instead of letting Decimal(None) crash the response.
    """
    return float(round_money(0 if value is None else value))


def json_default(o):
    """json.dumps default handler: serialize Decimal as float (display boundary)."""
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

def get_sale_payment_breakdown(sale):
    if not sale.payment_breakdown:
        return None
    try:
        value = json.loads(sale.payment_breakdown)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None

def normalize_payment_breakdown(raw_breakdown, total):
    if not isinstance(raw_breakdown, dict):
        raise ValueError('Payment breakdown is required for split payment')
    normalized = {}
    for raw_method, raw_amount in raw_breakdown.items():
        method = str(raw_method or '').strip().lower()
        if method in normalized:
            raise ValueError('Split payment methods must be different')
        if method not in {'cash', 'credit_card', 'debit_card', 'mobile_payment'}:
            raise ValueError('Invalid split payment method')
        amount = round_money(to_decimal(raw_amount))
        if amount <= 0:
            raise ValueError('Split payment amounts must be greater than zero')
        normalized[method] = amount
    if len(normalized) != 2 or sum(normalized.values(), Decimal('0.00')) != total:
        raise ValueError('Split payment amounts must equal the sale total')
    return normalized

# Settings whose values are credentials are encrypted at rest with a key derived
# from SECRET_KEY, so secrets are never stored in plaintext in the database.
_SECRET_SETTING_KEYS = {'ai_api_key'}


def _fernet():
    """Build a Fernet cipher derived from the application SECRET_KEY."""
    secret = os.environ.get('SECRET_KEY') or app.secret_key or 'your_super_secret_key_here'
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode('utf-8')).digest())
    return Fernet(key)


def encrypt_secret(plain_value):
    """Encrypt a credential value for storage at rest. Empty values stay empty."""
    if not plain_value:
        return plain_value
    return _fernet().encrypt(plain_value.encode('utf-8')).decode('utf-8')


def decrypt_secret(stored_value):
    """Decrypt a credential value read from storage.

    Legacy plaintext values (pre-encryption) are returned unchanged so existing
    installs keep working until the startup migration re-encrypts them. Ciphertext
    that cannot be decrypted (for example after a SECRET_KEY change) is treated as
    unset so a stale secret is never used.
    """
    if not stored_value:
        return stored_value
    try:
        return _fernet().decrypt(stored_value.encode('utf-8')).decode('utf-8')
    except (InvalidToken, ValueError, UnicodeDecodeError):
        if stored_value.startswith('gAAAA'):
            app.logger.warning('Stored AI API key could not be decrypted; SECRET_KEY may have changed.')
            return ''
        return stored_value


def migrate_legacy_secrets():
    """Re-encrypt any legacy plaintext AI API key so it is not stored in the clear."""
    stored_api_key = AppSetting.query.filter_by(key='ai_api_key').first()
    if stored_api_key and stored_api_key.value and decrypt_secret(stored_api_key.value) == stored_api_key.value:
        stored_api_key.value = encrypt_secret(stored_api_key.value)
        db.session.commit()


def get_setting(key, default=None):
    setting = AppSetting.query.filter_by(key=key).first()
    value = setting.value if setting else default
    if key in _SECRET_SETTING_KEYS and value:
        value = decrypt_secret(value)
    return value

def set_setting(key, value):
    if key in _SECRET_SETTING_KEYS:
        value = encrypt_secret(value)
    setting = AppSetting.query.filter_by(key=key).first()
    if setting:
        setting.value = value
    else:
        setting = AppSetting(key=key, value=value)
        db.session.add(setting)
    db.session.commit()


# ---------------------------------------------------------------------------
# Account-creation barrier
#
# The product ships with a vendor-only master credential that gates the ability
# to create additional user accounts, so a customer cannot mint their own users.
# The plaintext credential is never committed: it is provisioned at deploy time
# through POS_ACCOUNT_BARRIER_USERNAME / POS_ACCOUNT_BARRIER_PASSWORD (see .env)
# and only the salted password hash is persisted in the database. A numeric
# captcha and a per-client rate limiter guard the unlock endpoint.
# ---------------------------------------------------------------------------
ACCOUNT_BARRIER_USERNAME_SETTING = 'account_barrier_username'
ACCOUNT_BARRIER_PASSWORD_SETTING = 'account_barrier_password_hash'

# After _BARRIER_MAX_ATTEMPTS failed unlocks inside the rolling window the caller
# is throttled until the oldest failure ages out.
_BARRIER_MAX_ATTEMPTS = 5
_BARRIER_ATTEMPT_WINDOW = 15 * 60  # seconds
_BARRIER_UNLOCK_TTL = 15 * 60      # seconds a successful unlock stays valid
_BARRIER_CAPTCHA_TTL = 5 * 60      # seconds a numeric captcha stays valid
_BARRIER_MAX_CAPTCHAS = 512        # cap on live challenges kept in memory

_barrier_lock = threading.Lock()
_barrier_failures = {}   # client key -> list of failure timestamps
_barrier_captchas = {}   # captcha token -> {'answer': int, 'expires': float}


def _barrier_client_key():
    """Rate-limit key for the barrier.

    Prefers the signed-in account so the throttle follows the manager's identity
    rather than the network address. That matters behind a reverse proxy, where
    every request shares the proxy's address and an IP-only key would let one
    caller lock out (or be masked by) everyone else. The address is only a
    fallback for the unauthenticated case.
    """
    user_id = session.get('user_id')
    if user_id is not None:
        return f'user:{user_id}'
    return 'ip:' + ((request.remote_addr or 'unknown').strip() or 'unknown')


def _barrier_retry_after(client_key):
    """Seconds the caller must wait before trying again (0 when allowed)."""
    now = time.time()
    with _barrier_lock:
        recent = [t for t in _barrier_failures.get(client_key, [])
                  if now - t < _BARRIER_ATTEMPT_WINDOW]
        if recent:
            _barrier_failures[client_key] = recent
        else:
            _barrier_failures.pop(client_key, None)
        if len(recent) < _BARRIER_MAX_ATTEMPTS:
            return 0
        return max(int(math.ceil(recent[0] + _BARRIER_ATTEMPT_WINDOW - now)), 1)


def _barrier_record_failure(client_key):
    now = time.time()
    with _barrier_lock:
        recent = [t for t in _barrier_failures.get(client_key, [])
                  if now - t < _BARRIER_ATTEMPT_WINDOW]
        recent.append(now)
        _barrier_failures[client_key] = recent[-_BARRIER_MAX_ATTEMPTS:]


def _barrier_clear_failures(client_key):
    with _barrier_lock:
        _barrier_failures.pop(client_key, None)


def _barrier_issue_captcha():
    """Create a fresh numeric captcha; return (token, human-readable question)."""
    left = random.randint(2, 9)
    right = random.randint(2, 9)
    token = uuid.uuid4().hex
    now = time.time()
    with _barrier_lock:
        for stale in [t for t, entry in _barrier_captchas.items()
                      if entry['expires'] < now]:
            _barrier_captchas.pop(stale, None)
        # Bound memory even under a burst of challenge requests.
        while len(_barrier_captchas) >= _BARRIER_MAX_CAPTCHAS:
            _barrier_captchas.pop(next(iter(_barrier_captchas)))
        _barrier_captchas[token] = {'answer': left + right,
                                    'expires': now + _BARRIER_CAPTCHA_TTL}
    return token, f'What is {left} + {right}?'


def _barrier_consume_captcha(token, answer):
    """Validate a captcha once; it is destroyed whether or not it matches."""
    if not token:
        return False
    now = time.time()
    with _barrier_lock:
        entry = _barrier_captchas.pop(token, None)
    if not entry or entry['expires'] < now:
        return False
    try:
        return int(str(answer).strip()) == entry['answer']
    except (TypeError, ValueError):
        return False


def _account_barrier_configured():
    """True once a master credential has been provisioned for this deployment."""
    return bool(get_setting(ACCOUNT_BARRIER_PASSWORD_SETTING, '')
                and get_setting(ACCOUNT_BARRIER_USERNAME_SETTING, ''))


def _verify_account_barrier(username, password):
    """Constant-time username check plus a salted-hash password check."""
    stored_user = get_setting(ACCOUNT_BARRIER_USERNAME_SETTING, '') or ''
    stored_hash = get_setting(ACCOUNT_BARRIER_PASSWORD_SETTING, '') or ''
    if not stored_user or not stored_hash or not username or not password:
        return False
    if not hmac.compare_digest(str(stored_user), str(username)):
        return False
    return check_password_hash(stored_hash, password)


def _account_barrier_is_unlocked():
    """Whether this session may create users right now."""
    if not session.get('account_barrier_unlocked'):
        return False
    unlocked_at = session.get('account_barrier_unlocked_at')
    if unlocked_at is None:
        return True
    try:
        return (time.time() - float(unlocked_at)) < _BARRIER_UNLOCK_TTL
    except (TypeError, ValueError):
        return False


def unlock_account_barrier_session():
    session['account_barrier_unlocked'] = True
    session['account_barrier_unlocked_at'] = time.time()


def seed_account_barrier_credential():
    """Provision the vendor master credential from the deployment environment.

    Only the salted password hash is written to the database; the plaintext is
    read from POS_ACCOUNT_BARRIER_PASSWORD and never stored in source control.
    Re-running is a no-op while the configured credential already matches, so a
    restart neither rewrites the hash nor floods the audit log.
    """
    password = (os.environ.get('POS_ACCOUNT_BARRIER_PASSWORD') or '').strip()
    username = (os.environ.get('POS_ACCOUNT_BARRIER_USERNAME') or '').strip()
    if not password or not username:
        if not _account_barrier_configured():
            app.logger.warning(
                'Account-creation barrier is not provisioned. Set '
                'POS_ACCOUNT_BARRIER_USERNAME and POS_ACCOUNT_BARRIER_PASSWORD '
                '(for example in .env) so a vendor can unlock user creation.')
        return
    stored_user = get_setting(ACCOUNT_BARRIER_USERNAME_SETTING, '') or ''
    stored_hash = get_setting(ACCOUNT_BARRIER_PASSWORD_SETTING, '') or ''
    if stored_user == username and stored_hash and check_password_hash(stored_hash, password):
        return
    set_setting(ACCOUNT_BARRIER_USERNAME_SETTING, username)
    set_setting(ACCOUNT_BARRIER_PASSWORD_SETTING, generate_password_hash(password))


def get_agent_autonomy_enabled():
    """Kill switch for AI agent autonomy. Default is OFF."""
    try:
        value = get_setting('agent_autonomy_enabled')
        if value is None:
            return False
        return str(value).strip().lower() in {'true', '1', 'on', 'yes'}
    except Exception:
        return False


def get_currency_code():
    code = get_setting('currency_code', 'USD')
    return code if code in CURRENCY_OPTIONS else 'USD'

def get_currency_suffix(currency_code=None):
    code = currency_code or get_currency_code()
    return CURRENCY_OPTIONS.get(code, '$')

def get_receipt_paper_size():
    return normalize_receipt_paper_size(
        get_setting('receipt_paper_size', DEFAULT_RECEIPT_PAPER_SIZE)
    )

def get_receipt_identity(branch=None):
    return normalize_receipt_identity({
        'brand_name': get_setting('receipt_brand_name', DEFAULT_RECEIPT_BRAND_NAME),
        'logo_filename': get_setting('receipt_logo_filename', ''),
        'email': get_setting('receipt_email', ''),
        'phone': get_setting('receipt_phone', ''),
        'address': get_setting('receipt_address', ''),
        'footer_message': get_setting('receipt_footer_message', DEFAULT_RECEIPT_FOOTER),
    }, {
        'email': branch.email if branch else '',
        'phone': branch.phone if branch else '',
        'address': branch.address if branch else '',
    })

def get_receipt_customization_settings(branch=None):
    identity = get_receipt_identity(branch)
    return {
        'brand_name': get_setting('receipt_brand_name', DEFAULT_RECEIPT_BRAND_NAME),
        'logo_filename': get_setting('receipt_logo_filename', ''),
        'logo_url': receipt_logo_url(get_setting('receipt_logo_filename', '')),
        'email': get_setting('receipt_email', ''),
        'phone': get_setting('receipt_phone', ''),
        'address': get_setting('receipt_address', ''),
        'footer_message': get_setting('receipt_footer_message', DEFAULT_RECEIPT_FOOTER),
        'effective_email': identity['email'],
        'effective_phone': identity['phone'],
        'effective_address': identity['address'],
    }

def receipt_logo_url(filename):
    return url_for('receipt_logo', filename=filename) if filename else None

def mmqr_url(filename):
    return url_for('mmqr_image', filename=filename) if filename else None

def is_valid_mmqr_image(file_path, extension):
    """Reject truncated files that merely start with an image signature."""
    with open(file_path, 'rb') as image_file:
        content = image_file.read(2 * 1024 * 1024 + 1)
    if extension == 'png':
        if len(content) < 33 or not content.startswith(b'\x89PNG\r\n\x1a\n'):
            return False
        if content[12:16] != b'IHDR' or content[-8:-4] != b'IEND':
            return False
        width, height = struct.unpack('>II', content[16:24])
        return 0 < width <= 4096 and 0 < height <= 4096
    if extension == 'jpg':
        return len(content) >= 4 and content.startswith(b'\xff\xd8\xff') and content.endswith(b'\xff\xd9')
    return False

def delete_mmqr_file(filename):
    """Delete only generated MMQR filenames, never an arbitrary path."""
    if not filename or os.path.basename(filename) != filename:
        return
    file_path = os.path.join(app.config['MMQR_FOLDER'], filename)
    if os.path.exists(file_path):
        os.remove(file_path)

def format_currency(value, currency_code=None):
    symbol = get_currency_suffix(currency_code)
    amount = float(value or 0)
    return f"{amount:.2f} {symbol}"

def to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0

    normalized = str(value).strip().lower()
    if normalized in {'1', 'true', 'yes', 'y', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'n', 'off', ''}:
        return False
    return default

def build_inventory_alert_payload(branch_id=None):
    low_stock_items = []
    out_of_stock_count = 0

    query = Product.query
    if branch_id:
        query = query.filter_by(branch_id=branch_id)
    
    products = query.order_by(Product.name.asc()).all()
    for product in products:
        current_stock = int(product.stock or 0)
        reorder_point = max(int(product.reorder_point or 0), 0)
        reorder_quantity = max(int(product.reorder_quantity or 0), 0)
        reorder_enabled = bool(product.reorder_enabled)

        if current_stock <= 0:
            out_of_stock_count += 1

        if not reorder_enabled or current_stock > reorder_point:
            continue

        suggested_qty = reorder_quantity
        if suggested_qty <= 0:
            suggested_qty = max(reorder_point - current_stock, 1)

        low_stock_items.append({
            'product_id': product.id,
            'name': product.name,
            'barcode': product.barcode,
            'category': product.category,
            'current_stock': current_stock,
            'reorder_point': reorder_point,
            'reorder_quantity': reorder_quantity,
            'suggested_qty': suggested_qty
        })

    return {
        'summary': {
            'total_products': len(products),
            'low_stock_count': len(low_stock_items),
            'out_of_stock_count': out_of_stock_count
        },
        'low_stock_items': low_stock_items,
        'suggested_purchase_order': {
            'items': [{
                'product_id': item['product_id'],
                'suggested_qty': item['suggested_qty']
            } for item in low_stock_items]
        }
    }

def resolve_database_file_path():
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    sqlite_prefix = 'sqlite:///'

    if not uri.startswith(sqlite_prefix):
        return None

    raw_path = uri[len(sqlite_prefix):]
    if not raw_path or raw_path == ':memory:':
        return None

    # For relative SQLite paths (e.g. sqlite:///pos.db), Flask stores the file under app.instance_path.
    candidate_path = raw_path if os.path.isabs(raw_path) else os.path.join(app.instance_path, raw_path)
    if os.path.exists(candidate_path):
        return candidate_path

    # Fallback for projects that keep the SQLite file under the app root.
    fallback_path = os.path.join(app.root_path, raw_path)
    return fallback_path if os.path.exists(fallback_path) else None

def allowed_image_file(filename):
    if not filename or '.' not in filename:
        return False
    return filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS

class ProductImageTooLargeError(ValueError):
    """Raised when a product photo exceeds the 2 MB per-image size limit."""


def save_product_image(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    # Extension validation runs before any path/name operations.
    if not allowed_image_file(file_storage.filename):
        raise ValueError('Only image files are allowed (png, jpg, jpeg, gif, webp)')

    original = secure_filename(file_storage.filename)
    extension = original.rsplit('.', 1)[1].lower()
    unique_name = f"{uuid.uuid4().hex}.{extension}"
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
    file_storage.save(file_path)
    # Enforce a per-image size limit after save (mirrors the receipt logo check).
    if os.path.getsize(file_path) > 2 * 1024 * 1024:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise ProductImageTooLargeError('Image must be 2 MB or smaller')
    return unique_name

def delete_product_image(filename):
    if not filename:
        return
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if os.path.exists(file_path):
        os.remove(file_path)

def product_photo_url(filename):
    return url_for('product_image', filename=filename) if filename else None

def serialize_product(product):
    return {
        'id': product.id,
        'barcode': product.barcode,
        'name': product.name,
        'price': product.price,
        'cost': product.cost,
        'stock': product.stock,
        'category': product.category_ref.name if product.category_ref else product.category,
        'category_id': product.category_id,
        'unit_id': product.unit_id,
        'unit_name': product.unit_ref.name if product.unit_ref else None,
        'unit_symbol': product.unit_ref.symbol if product.unit_ref else None,
        'tax_rate': product.tax_rate,
        'reorder_point': product.reorder_point,
        'reorder_quantity': product.reorder_quantity,
        'reorder_enabled': bool(product.reorder_enabled),
        'photo_filename': product.photo_filename,
        'photo_url': product_photo_url(product.photo_filename)
    }

# --- Unit system helpers ---

def unit_factor_to_root(unit):
    """Walk the base chain and return (root_unit, effective factor to root).

    Chains are normally one hop (validation rejects linking to a non-root and
    rejects demoting a base that has children), but the walk multiplies every
    factor and guards against cycles so legacy or hand-edited data can never
    make the conversion math silently wrong. Returns (None, None) when the
    chain is broken.
    """
    factor = Decimal('1')
    current = unit
    seen = set()
    while current is not None and current.base_unit_id and current.id not in seen:
        seen.add(current.id)
        step = safe_to_decimal(current.factor_to_base, default=None)
        if step is None or step <= 0:
            return None, None
        factor *= step
        current = db.session.get(Unit, current.base_unit_id)
    if current is None:
        return None, None
    return current, factor

def unit_root(unit):
    """Return the group base unit a unit converts through (None if broken)."""
    root, _ = unit_factor_to_root(unit)
    return root

def convert_unit_quantity(from_unit, to_unit, quantity):
    """Convert a quantity between two units; None when they are unrelated.

    Units only convert inside the same root group (weight -> weight, never
    weight -> count). The math goes through the group base unit, multiplying
    the full chain of factors on each side.
    """
    if from_unit is None or to_unit is None:
        return None
    from_root, from_factor = unit_factor_to_root(from_unit)
    to_root, to_factor = unit_factor_to_root(to_unit)
    if from_root is None or to_root is None or from_root.id != to_root.id:
        return None
    if to_factor <= 0:
        return None
    quantity_decimal = safe_to_decimal(quantity, default=None)
    if quantity_decimal is None:
        return None
    converted = quantity_decimal * from_factor / to_factor
    try:
        return converted.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP).normalize()
    except InvalidOperation:
        return None

def resolve_product_unit_id(value, allow_inactive_id=None):
    """Validate an incoming product unit_id; returns (unit_id, error_message).

    ``allow_inactive_id`` lets a product keep the unit it already has when
    that unit was deactivated after the assignment (editing the price of such
    a product must not silently wipe its unit).
    """
    if value in (None, '', 'null'):
        return None, None
    try:
        unit_id = int(value)
    except (TypeError, ValueError):
        return None, 'Invalid unit'
    unit = db.session.get(Unit, unit_id)
    if unit is None:
        return None, 'Unit not found'
    if not unit.is_active and unit.id != allow_inactive_id:
        return None, f'Unit "{unit.name}" is inactive'
    return unit_id, None

def validate_unit_payload(data, existing=None):
    """Validate a unit create/update payload; returns (values, error_message)."""
    name = str(data.get('name') or '').strip()
    symbol = str(data.get('symbol') or '').strip()
    unit_type = str(data.get('unit_type') or 'count').strip().lower()

    if not name or len(name) > 50:
        return None, 'Unit name is required (max 50 characters)'
    if not symbol or len(symbol) > 15:
        return None, 'Unit symbol is required (max 15 characters)'
    if unit_type not in UNIT_TYPES:
        return None, 'Invalid unit type'

    symbol_conflict = Unit.query.filter(func.lower(Unit.symbol) == symbol.lower()).first()
    if symbol_conflict and (existing is None or symbol_conflict.id != existing.id):
        return None, f'Symbol "{symbol}" is already used by {symbol_conflict.name}'
    name_conflict = Unit.query.filter(func.lower(Unit.name) == name.lower()).first()
    if name_conflict and (existing is None or name_conflict.id != existing.id):
        return None, f'A unit named "{name}" already exists'

    base_unit = None
    base_unit_id = data.get('base_unit_id')
    if base_unit_id not in (None, '', 0, '0'):
        try:
            base_unit_id = int(base_unit_id)
        except (TypeError, ValueError):
            return None, 'Invalid base unit'
        base_unit = db.session.get(Unit, base_unit_id)
        if base_unit is None:
            return None, 'Base unit not found'
        if existing is not None and base_unit.id == existing.id:
            return None, 'A unit cannot be its own base unit'
        if base_unit.unit_type != unit_type:
            return None, 'Base unit must be of the same unit type'
        if base_unit.base_unit_id is not None:
            return None, 'Base unit must itself be a base unit of its type'
        if existing is not None and existing.child_units:
            # Demoting a base other units convert from would silently change
            # what their stored factors mean (two-hop chains).
            return None, (
                f'Cannot make {existing.symbol} convert from another unit: '
                'other units convert from it. Reassign their base unit first.'
            )

    factor_decimal = safe_to_decimal(data.get('factor_to_base', 1.0), default=None)
    if factor_decimal is None or factor_decimal <= 0:
        return None, 'Conversion factor must be a positive number'
    if base_unit is None and factor_decimal != Decimal('1'):
        return None, 'A base unit must have a conversion factor of 1'
    factor = float(factor_decimal)

    try:
        sort_order = max(int(data.get('sort_order', 0) or 0), 0)
    except (TypeError, ValueError):
        return None, 'Invalid sort order'

    is_active = to_bool(data.get('is_active', True), True)

    return {
        'name': name,
        'symbol': symbol,
        'unit_type': unit_type,
        'base_unit_id': base_unit.id if base_unit else None,
        'factor_to_base': factor,
        'is_active': is_active,
        'sort_order': sort_order
    }, None

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function

def manager_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'manager':
            return jsonify({'error': 'Manager access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def manager_or_boss_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or session.get('role') not in ('manager', 'boss'):
            return jsonify({'error': 'Manager access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def resolve_report_scope():
    """Resolve report scope and branch filtering based on role and query params."""
    role = session.get('role')
    scope = (request.args.get('scope') or 'current').strip().lower()
    report_branch_id = request.args.get('report_branch_id')

    if role not in ('manager', 'boss'):
        return 'current', get_current_branch_id()

    if scope == 'all':
        return 'all', None

    if scope == 'branch':
        if report_branch_id:
            try:
                branch_id = int(report_branch_id)
            except (TypeError, ValueError):
                return 'branch', get_current_branch_id()

            branch = Branch.query.filter_by(id=branch_id, is_active=True).first()
            if branch:
                return 'branch', branch_id
        return 'branch', get_current_branch_id()

    return 'current', get_current_branch_id()

def generate_po_number():
    return f"PO-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"

def generate_delivery_number():
    return f"DLV-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"

def normalize_delivery_stage(stage):
    value = (stage or '').strip().lower()
    return value if value in DELIVERY_STAGE_FLOW else None

def normalize_delivery_priority(priority):
    value = (priority or 'normal').strip().lower()
    return value if value in DELIVERY_PRIORITIES else 'normal'

def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None

def can_transition_delivery_stage(current_stage, next_stage):
    return next_stage in DELIVERY_STAGE_FLOW.get(current_stage, [])

def apply_delivery_stage_timestamp(delivery, stage):
    now = datetime.utcnow()
    if stage == 'packaged' and not delivery.packaged_at:
        delivery.packaged_at = now
    elif stage == 'delivering' and not delivery.out_for_delivery_at:
        delivery.out_for_delivery_at = now
    elif stage == 'delivered':
        delivery.delivered_at = now

# Debt status and aging helpers
DEBT_AGING_THRESHOLDS = {
    'current': 0,      # 0-30 days
    'due_soon': 30,    # 30-60 days
    'overdue': 60,     # 60-90 days
    'critical': 90     # 90+ days
}

def calculate_debt_aging_days(debt_date):
    """Calculate how many days a debt has been outstanding"""
    if not debt_date:
        return 0
    now = datetime.utcnow()
    if debt_date.tzinfo:
        debt_date = debt_date.replace(tzinfo=None)
    delta = now - debt_date
    return delta.days

def get_debt_aging_status(days_outstanding):
    """Get aging category based on days outstanding"""
    if days_outstanding < 30:
        return 'current'
    elif days_outstanding < 60:
        return 'due_soon'
    elif days_outstanding < 90:
        return 'overdue'
    else:
        return 'critical'

def get_debt_aging_color(days_outstanding):
    """Get color for aging indicator"""
    if days_outstanding < 30:
        return 'success'      # Green - current
    elif days_outstanding < 60:
        return 'warning'      # Yellow - due soon
    elif days_outstanding < 90:
        return 'orange'       # Orange - overdue
    else:
        return 'danger'       # Red - critical

def calculate_debt_status(debt):
    """Calculate debt status based on balance and due date"""
    if debt.balance <= 0:
        return 'paid'
    if debt.due_date:
        due_date = debt.due_date
        if due_date.tzinfo:
            # due_date may be parsed as timezone-aware (e.g. 'Z' -> +00:00); compare in naive UTC
            due_date = due_date.replace(tzinfo=None)
        if datetime.utcnow() > due_date:
            return 'overdue'
    if debt.balance < debt.amount:
        return 'partial'
    return 'pending'

# Branch helper functions
def get_current_branch_id():
    """Get the current branch ID from session, or return the default branch"""
    branch_id = session.get('branch_id')
    if branch_id:
        # Verify the branch still exists and is active
        branch = Branch.query.get(branch_id)
        if branch and branch.is_active:
            return branch_id
        # Branch no longer valid, clear from session
        session.pop('branch_id', None)
    
    # Get default branch
    default_branch = Branch.query.filter_by(is_default=True, is_active=True).first()
    if default_branch:
        session['branch_id'] = default_branch.id
        return default_branch.id
    
    # Fallback to first active branch
    first_branch = Branch.query.filter_by(is_active=True).first()
    if first_branch:
        session['branch_id'] = first_branch.id
        return first_branch.id
    
    return None

def get_current_branch():
    """Get the current branch object"""
    branch_id = get_current_branch_id()
    if branch_id:
        return Branch.query.get(branch_id)
    return None

def get_default_branch_id():
    """Get the default active branch ID for operational modules."""
    default_branch = Branch.query.filter_by(is_default=True, is_active=True).first()
    if default_branch:
        return default_branch.id

    first_branch = Branch.query.filter_by(is_active=True).first()
    if first_branch:
        return first_branch.id

    return None

def build_branch_scoped_barcode(base_barcode, branch):
    """Create a unique fallback barcode when the same product barcode is reused across branches."""
    cleaned = (base_barcode or '').strip()
    if not cleaned:
        return None

    branch_code = (branch.code or f'B{branch.id}').strip().upper()
    candidate = f"{cleaned}-{branch_code}"
    suffix = 1
    while Product.query.filter_by(barcode=candidate).first():
        candidate = f"{cleaned}-{branch_code}-{suffix}"
        suffix += 1
    return candidate

def get_requested_branch_id(default_to_current=False):
    """Resolve an optional branch_id from query string or JSON payload."""
    raw_branch_id = request.args.get('branch_id')
    if raw_branch_id in (None, '') and request.is_json:
        payload = request.get_json(silent=True) or {}
        raw_branch_id = payload.get('branch_id')

    if raw_branch_id in (None, '', 'all'):
        return get_current_branch_id() if default_to_current else None

    if str(raw_branch_id).lower() == 'current':
        return get_current_branch_id()

    try:
        return int(raw_branch_id)
    except (TypeError, ValueError):
        return get_current_branch_id() if default_to_current else None

def serialize_debt(debt):
    """Serialize debt record with all computed fields"""
    days_outstanding = calculate_debt_aging_days(debt.date)
    computed_status = calculate_debt_status(debt)
    aging_status = get_debt_aging_status(days_outstanding)
    aging_color = get_debt_aging_color(days_outstanding)
    
    # Get payment history
    payment_history = []
    total_paid = 0
    if hasattr(debt, 'payments') and debt.payments:
        for p in debt.payments:
            payment_history.append({
                'id': p.id,
                'amount': p.amount,
                'date': p.payment_date.isoformat() if p.payment_date else None,
                'notes': p.notes,
                'processed_by': p.processor.username if p.processor else None
            })
            total_paid += p.amount
    
    # Calculate paid amount from balance difference if no payment records exist
    if total_paid == 0:
        total_paid = debt.amount - debt.balance
    
    return {
        'id': debt.id,
        'customer_id': debt.customer_id,
        'customer_name': debt.customer.name if debt.customer else 'Unknown',
        'customer_phone': debt.customer.phone if debt.customer else None,
        'customer_email': debt.customer.email if debt.customer else None,
        'sale_id': debt.sale_id,
        'sale_transaction_id': debt.sale.transaction_id if debt.sale else None,
        'amount': debt.amount,
        'balance': debt.balance,
        'paid_amount': total_paid,
        'date': debt.date.isoformat() if debt.date else None,
        'due_date': debt.due_date.isoformat() if debt.due_date else None,
        'status': debt.status or computed_status,
        'computed_status': computed_status,
        'days_outstanding': days_outstanding,
        'aging_status': aging_status,
        'aging_color': aging_color,
        'notes': debt.notes,
        'communication_notes': debt.communication_notes,
        'last_contacted_at': debt.last_contacted_at.isoformat() if debt.last_contacted_at else None,
        'created_by': debt.created_by,
        'created_by_name': debt.creator.username if debt.creator else None,
        'created_at': debt.created_at.isoformat() if debt.created_at else None,
        'updated_at': debt.updated_at.isoformat() if debt.updated_at else None,
        'payment_history': payment_history
    }

def calculate_sale_item_unit_tax(sale_item):
    qty = int(sale_item.quantity or 0)
    if qty <= 0:
        return Decimal('0.00')
    return (to_decimal(sale_item.tax or 0) / Decimal(qty)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)

def get_returned_quantity_map_for_sale(sale_id):
    rows = (
        db.session.query(
            ReturnExchangeItem.original_sale_item_id,
            func.sum(ReturnExchangeItem.quantity)
        )
        .join(ReturnExchange, ReturnExchange.id == ReturnExchangeItem.return_exchange_id)
        .filter(
            ReturnExchange.original_sale_id == sale_id,
            ReturnExchangeItem.movement == 'return',
            ReturnExchangeItem.original_sale_item_id.isnot(None)
        )
        .group_by(ReturnExchangeItem.original_sale_item_id)
        .all()
    )
    return {sale_item_id: int(qty or 0) for sale_item_id, qty in rows}

# Database Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), default='cashier')

class AppSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=False)

class Branch(db.Model):
    """Multi-branch support for POS system"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(20), unique=True, nullable=False)
    address = db.Column(db.String(300))
    phone = db.Column(db.String(30))
    email = db.Column(db.String(100))
    is_active = db.Column(db.Boolean, default=True)
    is_default = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'code': self.code,
            'address': self.address,
            'phone': self.phone,
            'email': self.email,
            'is_active': self.is_active,
            'is_default': self.is_default,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class MemoryRegistry(db.Model):
    """Local, queryable registry of records accepted by the memory backend.

    The memory content remains owned by the configured embedded backend.  This
    table intentionally stores only a short, validated display summary so the
    management API can enforce ownership even when a backend uses opaque IDs.
    """
    __tablename__ = 'memory_registry'

    id = db.Column(db.Integer, primary_key=True)
    memory_id = db.Column(db.String(191), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'), nullable=False, index=True)
    scope = db.Column(db.String(20), nullable=False, default='private')
    summary = db.Column(db.String(500), nullable=False)
    source = db.Column(db.String(50), nullable=False, default='manual')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.CheckConstraint("scope IN ('private', 'branch_shared')", name='ck_memory_registry_scope'),
        db.Index('idx_memory_registry_owner', 'user_id', 'branch_id', 'scope'),
    )

    def to_dict(self):
        return {
            'id': self.memory_id,
            'scope': self.scope,
            'summary': self.summary,
            'source': self.source,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class MemoryAudit(db.Model):
    """Append-only local audit trail; never persist raw submitted memory text."""
    __tablename__ = 'memory_audit'

    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'), nullable=True, index=True)
    memory_id = db.Column(db.String(191), nullable=True, index=True)
    action = db.Column(db.String(30), nullable=False)
    scope = db.Column(db.String(20), nullable=True)
    details = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        db.Index('idx_memory_audit_actor_branch', 'actor_user_id', 'branch_id', 'created_at'),
    )

class AgentTask(db.Model):
    """Persistent record of one AI-agent command, its plan and step results.

    New table for GOAL 1 (task persistence). Created automatically by the
    existing startup ``db.create_all()`` call (app.py ~line 1160) — no
    ALTER-TABLE migration needed because this is a brand-new table.
    """
    __tablename__ = 'agent_task'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    command = db.Column(db.Text, nullable=False)
    plan_json = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), nullable=False, default='pending_approval')
    # allowed statuses: pending_approval | executing | completed | failed | clarification
    step_results_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def to_dict(self):
        def _load(raw):
            try:
                return json.loads(raw) if raw else None
            except (ValueError, TypeError):
                return None
        return {
            'id': self.id,
            'user_id': self.user_id,
            'command': self.command,
            'plan': _load(self.plan_json),
            'status': self.status,
            'step_results': _load(self.step_results_json),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

class Category(db.Model):
    """Centralized category management for products and suppliers"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(200))
    color = db.Column(db.String(7), default='#6c757d')  # Hex color for UI display
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    products = db.relationship('Product', backref='category_ref', lazy=True, foreign_keys='Product.category_id')
    suppliers = db.relationship('Supplier', backref='category_ref', lazy=True, foreign_keys='Supplier.category_id')
    branch = db.relationship('Branch', backref='categories')
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'color': self.color,
            'is_active': self.is_active,
            'sort_order': self.sort_order,
            'branch_id': self.branch_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'product_count': len(self.products) if self.products else 0,
            'supplier_count': len(self.suppliers) if self.suppliers else 0
        }

class Unit(db.Model):
    """A measurement unit (gram, pound, liter, piece...) with conversion links.

    Units are grouped by ``unit_type``. Exactly one unit per group is the base
    (``base_unit_id`` is NULL and its factor is 1.0); every other unit points
    directly at its group base with a multiplicative ``factor_to_base``, so any
    two units of the same group convert through the base:

        quantity_in_base = quantity * from_unit.factor_to_base
        quantity_in_to   = quantity_in_base / to_unit.factor_to_base

    One-hop links to a root keep the conversion graph cycle-free by design.
    The whole list is editable by managers in Settings -> Units of Measurement.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    symbol = db.Column(db.String(15), nullable=False)
    unit_type = db.Column(db.String(20), nullable=False, default='count')
    base_unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'))
    factor_to_base = db.Column(db.Float, nullable=False, default=1.0)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    base_unit = db.relationship('Unit', remote_side=[id], backref='child_units')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'symbol': self.symbol,
            'unit_type': self.unit_type,
            'unit_type_label': UNIT_TYPE_LABELS.get(self.unit_type, self.unit_type),
            'base_unit_id': self.base_unit_id,
            'base_unit_name': self.base_unit.name if self.base_unit else None,
            'base_unit_symbol': self.base_unit.symbol if self.base_unit else None,
            'factor_to_base': self.factor_to_base,
            'is_active': bool(self.is_active),
            'sort_order': self.sort_order
        }

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(50))  # Branch-scoped: uniqueness enforced per (barcode, branch_id) at app level
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    cost = db.Column(db.Float)
    stock = db.Column(db.Integer, default=0)
    category = db.Column(db.String(50))  # Legacy field, kept for backward compatibility
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))  # New foreign key
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'))  # How quantities of this product are counted
    tax_rate = db.Column(db.Float, default=0.0)
    photo_filename = db.Column(db.String(255))
    reorder_point = db.Column(db.Integer, default=10)
    reorder_quantity = db.Column(db.Integer, default=50)
    reorder_enabled = db.Column(db.Boolean, default=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))

    unit_ref = db.relationship('Unit', backref='products')

class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    contact_person = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    email = db.Column(db.String(120))
    address = db.Column(db.String(250))
    payment_terms = db.Column(db.String(120))
    lead_time_days = db.Column(db.Integer)
    is_active = db.Column(db.Boolean, default=True)
    notes = db.Column(db.String(300))
    # Enhanced fields
    category = db.Column(db.String(50))  # Legacy field, kept for backward compatibility
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))  # New foreign key
    tax_id = db.Column(db.String(50))
    website = db.Column(db.String(200))
    bank_name = db.Column(db.String(100))
    bank_account = db.Column(db.String(50))
    quality_rating = db.Column(db.Float, default=0.0)
    delivery_rating = db.Column(db.Float, default=0.0)
    total_orders = db.Column(db.Integer, default=0)
    on_time_deliveries = db.Column(db.Integer, default=0)
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class PurchaseOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(40), unique=True, nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)
    status = db.Column(db.String(25), default='draft')  # draft, pending, approved, partially_received, received, cancelled
    total_amount = db.Column(db.Float, default=0.0)
    expected_delivery_date = db.Column(db.DateTime)
    notes = db.Column(db.String(300))
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    approved_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    cancelled_reason = db.Column(db.String(300))
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    supplier = db.relationship('Supplier', backref='purchase_orders')
    creator = db.relationship('User', foreign_keys=[created_by], backref='created_purchase_orders')
    approver = db.relationship('User', foreign_keys=[approved_by], backref='approved_purchase_orders')

class PurchaseOrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey('purchase_order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    ordered_qty = db.Column(db.Integer, nullable=False)
    received_qty = db.Column(db.Integer, default=0)
    unit_cost = db.Column(db.Float, default=0.0)

    purchase_order = db.relationship('PurchaseOrder', backref='items')
    product = db.relationship('Product', backref='purchase_order_items')

class SupplierCommunication(db.Model):
    """Track supplier communications and interactions"""
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)
    communication_type = db.Column(db.String(20), nullable=False)  # call, email, meeting, other
    subject = db.Column(db.String(200))
    content = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    supplier = db.relationship('Supplier', backref='communications')
    creator = db.relationship('User', backref='supplier_communications')

class SupplierPriceAgreement(db.Model):
    """Supplier-specific product pricing agreements"""
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    agreed_price = db.Column(db.Float, nullable=False)
    valid_from = db.Column(db.DateTime, default=datetime.utcnow)
    valid_to = db.Column(db.DateTime)
    notes = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    supplier = db.relationship('Supplier', backref='price_agreements')
    product = db.relationship('Product', backref='supplier_prices')

# Warehouse Management Models
class WarehouseInventory(db.Model):
    """Tracks products received from purchase orders, stored in warehouse before restocking to main inventory"""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity = db.Column(db.Integer, default=0)
    location = db.Column(db.String(50))  # e.g., "Shelf A1", "Bin B2"
    batch_number = db.Column(db.String(50))  # Track by PO number
    received_date = db.Column(db.DateTime, default=datetime.utcnow)
    expiry_date = db.Column(db.DateTime)  # Optional for perishables
    unit_cost = db.Column(db.Float)  # Cost at time of receiving
    notes = db.Column(db.String(200))
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    product = db.relationship('Product', backref='warehouse_items')

class WarehouseTransfer(db.Model):
    """Records transfers from warehouse to main product stock"""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    from_warehouse = db.Column(db.Boolean, default=True)  # True = warehouse to stock
    batch_number = db.Column(db.String(50))  # Reference to warehouse batch
    performed_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    notes = db.Column(db.String(200))
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    product = db.relationship('Product', backref='transfers')
    performer = db.relationship('User', backref='warehouse_transfers')

class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.String(128), unique=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    total = db.Column(db.Float, nullable=False)
    tax = db.Column(db.Float, nullable=False)
    cash_received = db.Column(db.Float)
    refund_amount = db.Column(db.Float, default=0.0)
    payment_method = db.Column(db.String(20))
    payment_breakdown = db.Column(db.Text)
    receipt_snapshot = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    user = db.relationship('User', backref='sales')

# Database Model for Promotions
class Promotion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    discount_type = db.Column(db.String(10), nullable=False)  # 'percent' or 'fixed'
    discount_value = db.Column(db.Float, nullable=False)     # e.g., 10 for 10%, or $2 off
    start_date = db.Column(db.DateTime, nullable=False)
    end_date = db.Column(db.DateTime, nullable=False)
    
    product = db.relationship('Product', backref='promotions')


def active_promotion_prices(product, now=None):
    """Return the discounted prices implied by currently-active promotions for a product.

    Only promotions whose [start_date, end_date] window contains ``now``
    (Asia/Yangon, matching how promotions are stored) are considered.
    Returns a list of rounded Decimal prices; an empty list means no active
    promotion applies, i.e. only the normal price is valid.
    """
    if not product:
        return []
    myanmar_tz = pytz.timezone('Asia/Yangon')
    now = now or datetime.now(myanmar_tz)
    normal_price = to_decimal(product.price or 0)
    prices = []
    for promo in (product.promotions or []):
        start = promo.start_date
        end = promo.end_date
        if start.tzinfo is None:
            start = myanmar_tz.localize(start)
        if end.tzinfo is None:
            end = myanmar_tz.localize(end)
        if not (start <= now <= end):
            continue
        try:
            discount_value = to_decimal(promo.discount_value or 0)
        except Exception:
            continue
        if promo.discount_type == 'percent':
            candidate = normal_price - normal_price * discount_value / Decimal('100')
        elif promo.discount_type == 'fixed':
            candidate = normal_price - discount_value
        else:
            continue
        prices.append(round_money(max(candidate, Decimal('0'))))
    return prices


class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    tax = db.Column(db.Float, nullable=False)

class ReturnExchange(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workflow_id = db.Column(db.String(36), unique=True, nullable=False)
    mode = db.Column(db.String(20), nullable=False)  # return or exchange
    original_sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=False)
    adjustment_sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'))
    return_total = db.Column(db.Float, default=0.0)
    exchange_total = db.Column(db.Float, default=0.0)
    net_total = db.Column(db.Float, default=0.0)
    refund_amount = db.Column(db.Float, default=0.0)
    collected_amount = db.Column(db.Float, default=0.0)
    settlement_method = db.Column(db.String(30))
    notes = db.Column(db.String(300))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    original_sale = db.relationship('Sale', foreign_keys=[original_sale_id], backref='return_exchange_records')
    adjustment_sale = db.relationship('Sale', foreign_keys=[adjustment_sale_id], backref='adjustment_for_returns')
    user = db.relationship('User', backref='processed_return_exchanges')

class ReturnExchangeItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    return_exchange_id = db.Column(db.Integer, db.ForeignKey('return_exchange.id'), nullable=False)
    original_sale_item_id = db.Column(db.Integer, db.ForeignKey('sale_item.id'))
    # Nullable: a deleted product keeps its return/exchange history rows and is
    # only unlinked from them, so refunds and returned quantities stay intact.
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    movement = db.Column(db.String(20), nullable=False)  # return or exchange
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    tax_rate = db.Column(db.Float, default=0.0)
    line_total = db.Column(db.Float, nullable=False)
    line_tax = db.Column(db.Float, nullable=False)

    return_exchange = db.relationship('ReturnExchange', backref='items')
    original_sale_item = db.relationship('SaleItem', backref='return_exchange_items')
    product = db.relationship('Product', backref='return_exchange_items')

# New Models for Customer Debt/Credit Feature
class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20))
    email = db.Column(db.String(100))
    address = db.Column(db.String(200))
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationship with debts
    debts = db.relationship('Debt', backref='customer', lazy=True)

class Debt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=True)
    amount = db.Column(db.Float, nullable=False)
    balance = db.Column(db.Float, nullable=False)  # Remaining balance
    date = db.Column(db.DateTime, default=datetime.utcnow)
    due_date = db.Column(db.DateTime, nullable=True)  # Expected payment date
    status = db.Column(db.String(20), default='pending')  # 'pending', 'partial', 'paid', 'overdue'
    notes = db.Column(db.String(500))
    communication_notes = db.Column(db.Text)  # Track customer communications
    last_contacted_at = db.Column(db.DateTime)  # Last communication date
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship with sale
    sale = db.relationship('Sale', backref='debt', lazy=True)
    creator = db.relationship('User', backref='created_debts')
    
    # Relationship with payments
    payments = db.relationship('DebtPayment', backref='debt', lazy=True, order_by='desc(DebtPayment.payment_date)')

class DebtPayment(db.Model):
    """Track individual payments made towards debts"""
    id = db.Column(db.Integer, primary_key=True)
    debt_id = db.Column(db.Integer, db.ForeignKey('debt.id'), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    payment_date = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.String(500))
    processed_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    customer = db.relationship('Customer', backref='debt_payments')
    processor = db.relationship('User', backref='processed_debt_payments')

class Delivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    delivery_number = db.Column(db.String(40), unique=True, nullable=False)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=False, unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    stage = db.Column(db.String(20), default='to_deliver', nullable=False)
    priority = db.Column(db.String(20), default='normal', nullable=False)
    recipient_name = db.Column(db.String(120))
    recipient_phone = db.Column(db.String(30))
    delivery_address = db.Column(db.String(300))
    township = db.Column(db.String(120))
    instructions = db.Column(db.String(400))
    courier_name = db.Column(db.String(120))
    courier_phone = db.Column(db.String(30))
    tracking_code = db.Column(db.String(120))
    delivery_fee = db.Column(db.Float, default=0.0)
    scheduled_at = db.Column(db.DateTime)
    packaged_at = db.Column(db.DateTime)
    out_for_delivery_at = db.Column(db.DateTime)
    delivered_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    proof_note = db.Column(db.String(300))
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sale = db.relationship('Sale', backref=db.backref('delivery', uselist=False))
    customer = db.relationship('Customer', backref='deliveries')
    creator = db.relationship('User', backref='created_deliveries')


class AuditLog(db.Model):
    """Immutable history of committed business-data changes."""
    __tablename__ = 'audit_log'

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    actor_user_id = db.Column(db.Integer, nullable=True)
    actor_username = db.Column(db.String(80), nullable=False, default='System')
    actor_role = db.Column(db.String(20), nullable=True)
    branch_id = db.Column(db.Integer, nullable=True)
    branch_name = db.Column(db.String(100), nullable=True)
    category = db.Column(db.String(40), nullable=False)
    action = db.Column(db.String(20), nullable=False)
    entity_type = db.Column(db.String(80), nullable=False)
    entity_id = db.Column(db.String(128), nullable=True)
    entity_label = db.Column(db.String(200), nullable=True)
    summary = db.Column(db.String(300), nullable=False)
    # New records keep details in compact zlib-compressed UTF-8 JSON. The old
    # text column remains temporarily readable for rolling upgrades/backups.
    changes_blob = db.Column(db.LargeBinary, nullable=True)
    changes_json = db.Column(db.Text, nullable=True)
    request_method = db.Column(db.String(10), nullable=True)
    request_path = db.Column(db.String(300), nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(300), nullable=True)

    __table_args__ = (
        db.Index('idx_audit_log_branch_created', 'branch_id', 'created_at'),
        db.Index('idx_audit_log_category_created', 'category', 'created_at'),
        db.Index('idx_audit_log_action_created', 'action', 'created_at'),
    )


AUDIT_CATEGORY_BY_ENTITY = {
    'Sale': 'Sales', 'SaleItem': 'Sales',
    'ReturnExchange': 'Returns & Exchanges',
    'ReturnExchangeItem': 'Returns & Exchanges',
    'Product': 'Products', 'Category': 'Products', 'Unit': 'Products',
    'WarehouseInventory': 'Inventory', 'WarehouseTransfer': 'Inventory',
    'PurchaseOrder': 'Purchasing', 'PurchaseOrderItem': 'Purchasing',
    'Supplier': 'Purchasing', 'SupplierCommunication': 'Purchasing',
    'SupplierPriceAgreement': 'Purchasing',
    'Customer': 'Customers', 'Debt': 'Customers', 'DebtPayment': 'Customers',
    'Delivery': 'Deliveries', 'Promotion': 'Promotions',
    'User': 'Users', 'Branch': 'Settings', 'AppSetting': 'Settings',
    'AgentTask': 'AI Assistant', 'MemoryRegistry': 'AI Assistant',
    'MemoryAudit': 'AI Assistant',
}
AUDIT_SENSITIVE_FIELDS = {
    'password', 'value', 'receipt_snapshot', 'command', 'plan_json',
    'step_results_json', 'details', 'bank_account', 'tax_id',
    'payment_breakdown', 'phone', 'email', 'address', 'delivery_address',
    'recipient_phone', 'courier_phone', 'notes', 'communication_notes',
    'instructions',
}
AUDIT_LABEL_FIELDS = (
    'transaction_id', 'workflow_id', 'po_number', 'delivery_number', 'name',
    'username', 'key', 'barcode', 'memory_id', 'batch_number', 'id',
)
AUDIT_TIMEZONE = pytz.timezone('Asia/Yangon')
AUDIT_PAGE_SIZE = 30
# Upper bound on a single TXT download. Larger ranges must be narrowed so one
# export cannot monopolise a Waitress worker thread.
AUDIT_EXPORT_LIMIT = 10000


def encode_audit_changes(changes):
    """Return the smaller of compact JSON and zlib, tagged for decoding."""
    if changes is None or changes == {}:
        return None
    raw = json.dumps(
        changes, ensure_ascii=False, separators=(',', ':'), default=json_default
    ).encode('utf-8')
    compressed = zlib.compress(raw, level=9)
    return (b'Z' + compressed) if len(compressed) < len(raw) else (b'J' + raw)


def decode_audit_changes(row):
    """Read compressed details, falling back to pre-migration JSON text."""
    try:
        if row.changes_blob:
            payload = bytes(row.changes_blob)
            if payload[:1] == b'Z':
                raw = zlib.decompress(payload[1:])
            elif payload[:1] == b'J':
                raw = payload[1:]
            else:
                raise ValueError('Unknown audit detail encoding')
            return json.loads(raw.decode('utf-8'))
        if row.changes_json is not None:
            return json.loads(row.changes_json)
    except (TypeError, ValueError, UnicodeDecodeError, zlib.error, json.JSONDecodeError):
        app.logger.warning('Could not decode audit detail for log %s', getattr(row, 'id', '?'))
        # During rolling migration, a valid legacy copy may still be available.
        if row.changes_json is not None:
            try:
                return json.loads(row.changes_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    return {}


def migrate_audit_detail_storage(batch_size=500):
    """Add/backfill compact details safely in bounded, restartable batches."""
    inspector = inspect(db.engine)
    if not inspector.has_table('audit_log'):
        return
    audit_columns = {col['name'] for col in inspector.get_columns('audit_log')}
    if 'changes_blob' not in audit_columns:
        try:
            db.session.execute(text('ALTER TABLE audit_log ADD COLUMN changes_blob BLOB'))
            db.session.commit()
        except OperationalError as error:
            db.session.rollback()
            # Another startup may have completed the additive DDL while this
            # process waited for SQLite's writer lock. Re-inspect before failing.
            refreshed = {col['name'] for col in inspect(db.engine).get_columns('audit_log')}
            if 'changes_blob' not in refreshed:
                raise error

    last_id = 0
    while True:
        legacy_rows = db.session.execute(text(
            "SELECT id, changes_json FROM audit_log "
            "WHERE id > :last_id AND changes_json IS NOT NULL "
            "AND changes_blob IS NULL ORDER BY id LIMIT :batch_size"
        ), {'last_id': last_id, 'batch_size': batch_size}).all()
        if not legacy_rows:
            break
        for log_id, raw_details in legacy_rows:
            last_id = log_id
            try:
                parsed = json.loads(raw_details)
                packed = encode_audit_changes(parsed)
                # Empty objects intentionally have no payload; clearing '{}' is
                # still lossless because decode returns the same empty object.
                if packed is None and parsed != {}:
                    continue
            except (TypeError, ValueError, json.JSONDecodeError):
                # Never truncate or discard malformed historical evidence.
                app.logger.warning('Leaving malformed legacy audit detail in row %s', log_id)
                continue
            db.session.execute(text(
                'UPDATE audit_log SET changes_blob = :packed, changes_json = NULL '
                'WHERE id = :id AND changes_blob IS NULL AND changes_json = :original'
            ), {'packed': packed, 'id': log_id, 'original': raw_details})
        db.session.commit()

    # Reconcile interrupted/rolling deployments that temporarily wrote both.
    last_id = 0
    while True:
        duplicate_rows = db.session.execute(text(
            "SELECT id, changes_blob, changes_json FROM audit_log "
            "WHERE id > :last_id AND changes_blob IS NOT NULL "
            "AND changes_json IS NOT NULL ORDER BY id LIMIT :batch_size"
        ), {'last_id': last_id, 'batch_size': batch_size}).all()
        if not duplicate_rows:
            break
        for log_id, blob, raw_details in duplicate_rows:
            last_id = log_id
            try:
                holder = type('AuditDetail', (), {
                    'id': log_id, 'changes_blob': blob, 'changes_json': None
                })()
                if decode_audit_changes(holder) != json.loads(raw_details):
                    continue
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            db.session.execute(text(
                'UPDATE audit_log SET changes_json = NULL '
                'WHERE id = :id AND changes_blob = :blob AND changes_json = :original'
            ), {'id': log_id, 'blob': blob, 'original': raw_details})
        db.session.commit()


def audit_local_datetime(moment):
    """Convert a stored UTC audit timestamp to the business timezone."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = pytz.utc.localize(moment)
    else:
        moment = moment.astimezone(pytz.utc)
    return moment.astimezone(AUDIT_TIMEZONE)


def audit_day_utc_bounds(raw_date):
    """Return [start, end) UTC-naive bounds for one Asia/Yangon date."""
    try:
        local_start = AUDIT_TIMEZONE.localize(
            datetime.strptime(str(raw_date).strip(), '%Y-%m-%d'))
    except (TypeError, ValueError):
        return None
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(pytz.utc).replace(tzinfo=None),
        local_end.astimezone(pytz.utc).replace(tzinfo=None),
    )


def _audit_json_value(value):
    """Return a bounded, JSON-safe audit value without leaking large payloads."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 1000:
            return value[:1000] + '…'
        return value
    return str(value)[:1000]


def _audit_column_value(obj, column_name):
    lowered = column_name.lower()
    if lowered in AUDIT_SENSITIVE_FIELDS or any(
            token in lowered for token in ('password', 'secret', 'token', 'api_key')):
        return '[REDACTED]'
    return _audit_json_value(getattr(obj, column_name, None))


def _audit_snapshot(obj):
    return {
        column.key: _audit_column_value(obj, column.key)
        for column in inspect(obj).mapper.column_attrs
        if getattr(obj, column.key, None) is not None
    }


def _audit_entity_label(obj):
    for field in AUDIT_LABEL_FIELDS:
        value = getattr(obj, field, None)
        if value not in (None, ''):
            return str(value)[:200]
    return None


def _audit_branch_id(obj):
    value = getattr(obj, 'branch_id', None)
    if value is not None:
        return value
    if isinstance(obj, Branch):
        return obj.id
    return session.get('branch_id') if has_request_context() else None


def _audit_request_context():
    if not has_request_context() or request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    # request.remote_addr is authoritative unless ProxyFix is deliberately
    # configured for a trusted reverse proxy. Never trust client-supplied XFF.
    ip_address = request.remote_addr
    return {
        'actor_user_id': session.get('user_id'),
        'actor_username': session.get('username') or 'System',
        'actor_role': session.get('role'),
        'session_branch_id': session.get('branch_id'),
        'request_method': request.method,
        'request_path': request.path[:300],
        'ip_address': (ip_address or '')[:64] or None,
        'user_agent': str(request.user_agent)[:300] or None,
    }


def _audit_summary(action, entity_type, label, changed_fields):
    readable = re.sub(r'(?<!^)(?=[A-Z])', ' ', entity_type).lower()
    target = f' “{label}”' if label else ''
    if action == 'create':
        return f'Created {readable}{target}'[:300]
    if action == 'delete':
        return f'Deleted {readable}{target}'[:300]
    fields = ', '.join(field.replace('_', ' ') for field in changed_fields[:8])
    suffix = f' ({fields})' if fields else ''
    return f'Updated {readable}{target}{suffix}'[:300]


def record_audit_event(category, action, entity_type, entity_id=None,
                       entity_label=None, changes=None, branch_id=None,
                       summary=None):
    """Record a change made through SQL that bypasses normal ORM history.

    Callers add the event before their normal commit. It therefore shares the
    business transaction and is discarded automatically on rollback.
    """
    context = _audit_request_context()
    if not context:
        return None
    row = AuditLog(
        actor_user_id=context['actor_user_id'],
        actor_username=context['actor_username'],
        actor_role=context['actor_role'],
        branch_id=branch_id or context['session_branch_id'],
        category=category,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        entity_label=str(entity_label)[:200] if entity_label is not None else None,
        summary=(summary or _audit_summary(action, entity_type, entity_label,
                                           list((changes or {}).keys())))[:300],
        changes_blob=encode_audit_changes(changes),
        request_method=context['request_method'],
        request_path=context['request_path'],
        ip_address=context['ip_address'],
        # User agent is not shown in the audit UI and can be hundreds of bytes;
        # omit it from new rows to keep the append-only table lean.
        user_agent=None,
    )
    db.session.add(row)
    return row


@event.listens_for(SQLAlchemySession, 'before_flush')
def _collect_audit_changes(db_session, flush_context, instances):
    """Capture ORM changes so their logs commit or roll back atomically."""
    if any(isinstance(obj, AuditLog) for obj in db_session.dirty) or any(
            isinstance(obj, AuditLog) for obj in db_session.deleted):
        raise ValueError('System audit logs are append-only')

    context = _audit_request_context()
    if not context or db_session.info.get('_audit_collecting'):
        return

    pending = []
    for obj in list(db_session.new):
        if isinstance(obj, db.Model) and not isinstance(obj, AuditLog):
            pending.append({'object': obj, 'action': 'create',
                            'changed_fields': []})

    for obj in list(db_session.dirty):
        if not isinstance(obj, db.Model) or isinstance(obj, AuditLog):
            continue
        state = inspect(obj)
        changes = {}
        for attribute in state.mapper.column_attrs:
            history = state.attrs[attribute.key].history
            if not history.has_changes():
                continue
            old_value = history.deleted[0] if history.deleted else None
            new_value = getattr(obj, attribute.key, None)
            lowered = attribute.key.lower()
            if lowered in AUDIT_SENSITIVE_FIELDS or any(
                    token in lowered for token in ('password', 'secret', 'token', 'api_key')):
                old_value = new_value = '[REDACTED]'
            changes[attribute.key] = {
                'before': _audit_json_value(old_value),
                'after': _audit_json_value(new_value),
            }
        if changes:
            pending.append({'object': obj, 'action': 'update',
                            'changes': changes,
                            'changed_fields': list(changes)})

    for obj in list(db_session.deleted):
        if isinstance(obj, db.Model) and not isinstance(obj, AuditLog):
            pending.append({'object': obj, 'action': 'delete',
                            'before': _audit_snapshot(obj), 'changed_fields': []})

    if pending:
        existing_context, existing = db_session.info.get(
            '_audit_pending', (context, []))
        db_session.info['_audit_pending'] = (existing_context, existing + pending)


@event.listens_for(SQLAlchemySession, 'do_orm_execute', retval=True)
def _audit_bulk_orm_write(execute_state):
    """Leave evidence for Query.update/delete paths that bypass object history."""
    if not (execute_state.is_update or execute_state.is_delete):
        return execute_state.invoke_statement()
    context = _audit_request_context()
    mapper = execute_state.bind_mapper
    entity_type = mapper.class_.__name__ if mapper is not None else 'Database records'
    if entity_type == 'AuditLog':
        if not execute_state.session.info.get('_allow_audit_log_maintenance'):
            raise ValueError('System audit logs are append-only')
        return execute_state.invoke_statement()
    result = execute_state.invoke_statement()
    if not context:
        return result
    action = 'update' if execute_state.is_update else 'delete'
    count = max(int(getattr(result, 'rowcount', 0) or 0), 0)
    execute_state.session.add(AuditLog(
        actor_user_id=context['actor_user_id'],
        actor_username=context['actor_username'],
        actor_role=context['actor_role'],
        branch_id=context['session_branch_id'],
        category=AUDIT_CATEGORY_BY_ENTITY.get(entity_type, 'System'),
        action=action,
        entity_type=entity_type,
        summary=f'Bulk {action} affected {count} {entity_type} record(s)'[:300],
        changes_blob=encode_audit_changes({'affected_records': count}),
        request_method=context['request_method'], request_path=context['request_path'],
        ip_address=context['ip_address'], user_agent=None,
    ))
    return result


@event.listens_for(SQLAlchemySession, 'after_flush_postexec')
def _write_audit_changes(db_session, flush_context):
    queued = db_session.info.pop('_audit_pending', None)
    if not queued:
        return
    context, pending = queued
    db_session.info['_audit_collecting'] = True
    try:
        for entry in pending:
            obj = entry['object']
            action = entry['action']
            entity_type = type(obj).__name__
            label = _audit_entity_label(obj)
            if action == 'create':
                changes = {'after': _audit_snapshot(obj)}
            elif action == 'delete':
                changes = {'before': entry['before']}
            else:
                changes = entry['changes']
            branch_id = _audit_branch_id(obj) or context['session_branch_id']
            db_session.add(AuditLog(
                actor_user_id=context['actor_user_id'],
                actor_username=context['actor_username'],
                actor_role=context['actor_role'],
                branch_id=branch_id,
                branch_name=obj.name if isinstance(obj, Branch) else None,
                category=AUDIT_CATEGORY_BY_ENTITY.get(entity_type, 'System'),
                action=action,
                entity_type=entity_type,
                entity_id=str(getattr(obj, 'id', '') or '') or None,
                entity_label=label,
                summary=_audit_summary(action, entity_type, label,
                                       entry['changed_fields']),
                changes_blob=encode_audit_changes(changes),
                request_method=context['request_method'],
                request_path=context['request_path'],
                ip_address=context['ip_address'],
                user_agent=None,
            ))
    finally:
        db_session.info.pop('_audit_collecting', None)


@event.listens_for(SQLAlchemySession, 'after_rollback')
def _discard_rolled_back_audit_changes(db_session):
    """A reused scoped session must not carry failed events into its next commit."""
    db_session.info.pop('_audit_pending', None)
    db_session.info.pop('_audit_collecting', None)

def serialize_delivery(delivery):
    return {
        'id': delivery.id,
        'delivery_number': delivery.delivery_number,
        'sale_id': delivery.sale_id,
        'sale_transaction_id': delivery.sale.transaction_id if delivery.sale else None,
        'sale_total': delivery.sale.total if delivery.sale else 0,
        'customer_id': delivery.customer_id,
        'customer_name': delivery.customer.name if delivery.customer else None,
        'stage': delivery.stage,
        'stage_label': DELIVERY_STAGE_LABELS.get(delivery.stage, delivery.stage),
        'priority': delivery.priority,
        'recipient_name': delivery.recipient_name,
        'recipient_phone': delivery.recipient_phone,
        'delivery_address': delivery.delivery_address,
        'township': delivery.township,
        'instructions': delivery.instructions,
        'courier_name': delivery.courier_name,
        'courier_phone': delivery.courier_phone,
        'tracking_code': delivery.tracking_code,
        'delivery_fee': delivery.delivery_fee or 0,
        'scheduled_at': delivery.scheduled_at.isoformat() if delivery.scheduled_at else None,
        'packaged_at': delivery.packaged_at.isoformat() if delivery.packaged_at else None,
        'out_for_delivery_at': delivery.out_for_delivery_at.isoformat() if delivery.out_for_delivery_at else None,
        'delivered_at': delivery.delivered_at.isoformat() if delivery.delivered_at else None,
        'cancelled_at': delivery.cancelled_at.isoformat() if delivery.cancelled_at else None,
        'proof_note': delivery.proof_note,
        'created_by': delivery.created_by,
        'created_by_name': delivery.creator.username if delivery.creator else None,
        'created_at': delivery.created_at.isoformat() if delivery.created_at else None,
        'updated_at': delivery.updated_at.isoformat() if delivery.updated_at else None,
        'can_transition_to': DELIVERY_STAGE_FLOW.get(delivery.stage, [])
    }

# Create database tables and admin user
with app.app_context():
    # Harden SQLite for concurrent POS writes: WAL journaling plus a busy timeout
    # on every pooled connection so concurrent sales retry instead of failing fast.
    @event.listens_for(db.engine, 'connect')
    def _set_sqlite_busy_timeout(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA busy_timeout=5000')
        cursor.close()

    db.session.execute(text('PRAGMA journal_mode=WAL'))
    db.session.execute(text('PRAGMA busy_timeout=5000'))
    db.create_all()
    inspector = inspect(db.engine)

    # Audit-detail storage migration: compact existing JSON into a BLOB in bounded,
    # restartable batches and stop duplicating it in the legacy text column.
    if inspector.has_table('audit_log'):
        migrate_audit_detail_storage()

        # Early builds created one index per filtered column. Composite indexes
        # cover the actual date-ordered queries with fewer index pages/writes.
        for obsolete_index in (
            'ix_audit_log_actor_user_id', 'ix_audit_log_branch_id',
            'ix_audit_log_category', 'ix_audit_log_action',
            'ix_audit_log_entity_type', 'ix_audit_log_entity_id',
            'idx_audit_log_actor_created',
        ):
            db.session.execute(text(f'DROP INDEX IF EXISTS {obsolete_index}'))
        db.session.execute(text(
            'CREATE INDEX IF NOT EXISTS idx_audit_log_action_created '
            'ON audit_log (action, created_at)'
        ))
        db.session.commit()

    # Persist immutable financial and display data for reliable receipt reprints.
    if inspector.has_table('sale'):
        sale_columns = [col['name'] for col in inspector.get_columns('sale')]
        if 'receipt_snapshot' not in sale_columns:
            db.session.execute(text('ALTER TABLE sale ADD COLUMN receipt_snapshot TEXT'))
            db.session.commit()
    
    # Branch table migration for multi-branch support
    if not inspector.has_table('branch'):
        db.session.execute(text('''
            CREATE TABLE branch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                code VARCHAR(20) NOT NULL UNIQUE,
                address VARCHAR(300),
                phone VARCHAR(30),
                email VARCHAR(100),
                is_active BOOLEAN DEFAULT 1,
                is_default BOOLEAN DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME
            )
        '''))
        db.session.commit()
    
    # Create default branch if none exists
    if inspector.has_table('branch'):
        branch_count = db.session.execute(text('SELECT COUNT(*) FROM branch')).scalar()
        if branch_count == 0:
            db.session.execute(text('''
                INSERT INTO branch (name, code, address, is_active, is_default, created_at, updated_at)
                VALUES ('Main Branch', 'MAIN', 'Main Location', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            '''))
            db.session.commit()
    
    # Add branch_id columns to existing tables
    tables_to_migrate = [
        ('category', 'branch_id', 'ALTER TABLE category ADD COLUMN branch_id INTEGER REFERENCES branch (id)'),
        ('product', 'branch_id', 'ALTER TABLE product ADD COLUMN branch_id INTEGER REFERENCES branch (id)'),
        ('supplier', 'branch_id', 'ALTER TABLE supplier ADD COLUMN branch_id INTEGER REFERENCES branch (id)'),
        ('purchase_order', 'branch_id', 'ALTER TABLE purchase_order ADD COLUMN branch_id INTEGER REFERENCES branch (id)'),
        ('sale', 'branch_id', 'ALTER TABLE sale ADD COLUMN branch_id INTEGER REFERENCES branch (id)'),
        ('customer', 'branch_id', 'ALTER TABLE customer ADD COLUMN branch_id INTEGER REFERENCES branch (id)'),
        ('debt', 'branch_id', 'ALTER TABLE debt ADD COLUMN branch_id INTEGER REFERENCES branch (id)'),
        ('debt_payment', 'branch_id', 'ALTER TABLE debt_payment ADD COLUMN branch_id INTEGER REFERENCES branch (id)'),
        ('delivery', 'branch_id', 'ALTER TABLE delivery ADD COLUMN branch_id INTEGER REFERENCES branch (id)'),
        ('warehouse_inventory', 'branch_id', 'ALTER TABLE warehouse_inventory ADD COLUMN branch_id INTEGER REFERENCES branch (id)'),
        ('warehouse_transfer', 'branch_id', 'ALTER TABLE warehouse_transfer ADD COLUMN branch_id INTEGER REFERENCES branch (id)')
    ]
    
    for table_name, column_name, migration_sql in tables_to_migrate:
        if inspector.has_table(table_name):
            columns = [col['name'] for col in inspector.get_columns(table_name)]
            if column_name not in columns:
                try:
                    db.session.execute(text(migration_sql))
                    db.session.commit()
                except Exception as e:
                    app.logger.warning(f"Could not add {column_name} to {table_name}: {str(e)}")
    
    # Set existing records to default branch
    default_branch = db.session.execute(text('SELECT id FROM branch WHERE is_default = 1 LIMIT 1')).scalar()
    if default_branch:
        for table_name, _, _ in tables_to_migrate:
            if inspector.has_table(table_name):
                try:
                    db.session.execute(text(f'UPDATE {table_name} SET branch_id = :branch_id WHERE branch_id IS NULL'), {'branch_id': default_branch})
                    db.session.commit()
                except Exception as e:
                    app.logger.warning(f"Could not update branch_id in {table_name}: {str(e)}")
    
    # Category table migration
    if not inspector.has_table('category'):
        db.session.execute(text('''
            CREATE TABLE category (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(50) NOT NULL UNIQUE,
                description VARCHAR(200),
                color VARCHAR(7) DEFAULT '#6c757d',
                is_active BOOLEAN DEFAULT 1,
                sort_order INTEGER DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME
            )
        '''))
        db.session.commit()
    
    # Ensure all existing categories have a default color value
    if inspector.has_table('category'):
        db.session.execute(text("""
            UPDATE category 
            SET color = '#6c757d' 
            WHERE color IS NULL OR color = ''
        """))
        db.session.commit()
    
    # Add category_id columns to product and supplier tables
    if inspector.has_table('product'):
        product_columns = [col['name'] for col in inspector.get_columns('product')]
        if 'category_id' not in product_columns:
            db.session.execute(text('ALTER TABLE product ADD COLUMN category_id INTEGER REFERENCES category (id)'))
            db.session.commit()
    
    if inspector.has_table('supplier'):
        supplier_cols = [col['name'] for col in inspector.get_columns('supplier')]
        if 'category_id' not in supplier_cols:
            db.session.execute(text('ALTER TABLE supplier ADD COLUMN category_id INTEGER REFERENCES category (id)'))
            db.session.commit()
    
    # Migrate existing category strings to Category table
    if inspector.has_table('category') and inspector.has_table('product'):
        # Get unique categories from products
        existing_categories = db.session.execute(text(
            "SELECT DISTINCT category FROM product WHERE category IS NOT NULL AND category != ''"
        )).fetchall()
        existing_categories = [c[0] for c in existing_categories if c[0]]
        
        # Get unique categories from suppliers
        supplier_categories = db.session.execute(text(
            "SELECT DISTINCT category FROM supplier WHERE category IS NOT NULL AND category != ''"
        )).fetchall()
        supplier_categories = [c[0] for c in supplier_categories if c[0]]
        
        # Combine and deduplicate
        all_categories = set(existing_categories + supplier_categories)
        
        # Create category records for existing categories
        for idx, cat_name in enumerate(sorted(all_categories)):
            # Check if category already exists
            existing = db.session.execute(text(
                "SELECT id FROM category WHERE LOWER(name) = LOWER(:name)"
            ), {'name': cat_name}).fetchone()
            if not existing:
                db.session.execute(text('''
                    INSERT INTO category (name, color, is_active, sort_order, created_at, updated_at)
                    VALUES (:name, '#6c757d', 1, :sort_order, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                '''), {'name': cat_name, 'sort_order': idx})
        db.session.commit()
        
        # Update product category_id references
        products_with_category = db.session.execute(text(
            "SELECT id, category FROM product WHERE category IS NOT NULL AND category != '' AND category_id IS NULL"
        )).fetchall()
        for prod_id, cat_name in products_with_category:
            cat_record = db.session.execute(text(
                "SELECT id FROM category WHERE LOWER(name) = LOWER(:name)"
            ), {'name': cat_name}).fetchone()
            if cat_record:
                db.session.execute(text(
                    "UPDATE product SET category_id = :cat_id WHERE id = :prod_id"
                ), {'cat_id': cat_record[0], 'prod_id': prod_id})
        db.session.commit()
        
        # Update supplier category_id references
        suppliers_with_category = db.session.execute(text(
            "SELECT id, category FROM supplier WHERE category IS NOT NULL AND category != '' AND category_id IS NULL"
        )).fetchall()
        for sup_id, cat_name in suppliers_with_category:
            cat_record = db.session.execute(text(
                "SELECT id FROM category WHERE LOWER(name) = LOWER(:name)"
            ), {'name': cat_name}).fetchone()
            if cat_record:
                db.session.execute(text(
                    "UPDATE supplier SET category_id = :cat_id WHERE id = :sup_id"
                ), {'cat_id': cat_record[0], 'sup_id': sup_id})
        db.session.commit()
    
    product_columns = [col['name'] for col in inspector.get_columns('product')]
    if 'photo_filename' not in product_columns:
        db.session.execute(text('ALTER TABLE product ADD COLUMN photo_filename VARCHAR(255)'))
        db.session.commit()
    product_migrations = [
        ('reorder_point', 'ALTER TABLE product ADD COLUMN reorder_point INTEGER DEFAULT 10'),
        ('reorder_quantity', 'ALTER TABLE product ADD COLUMN reorder_quantity INTEGER DEFAULT 50'),
        ('reorder_enabled', 'ALTER TABLE product ADD COLUMN reorder_enabled BOOLEAN DEFAULT 1'),
        ('unit_id', 'ALTER TABLE product ADD COLUMN unit_id INTEGER REFERENCES unit (id)')
    ]
    for column_name, migration_sql in product_migrations:
        if column_name not in product_columns:
            db.session.execute(text(migration_sql))
            db.session.commit()

    db.session.execute(text('UPDATE product SET reorder_point = 10 WHERE reorder_point IS NULL'))
    db.session.execute(text('UPDATE product SET reorder_quantity = 50 WHERE reorder_quantity IS NULL'))
    db.session.execute(text('UPDATE product SET reorder_enabled = 1 WHERE reorder_enabled IS NULL'))
    db.session.commit()

    # Barcode migration for branch-scoped uniqueness.
    # The Product.barcode column no longer declares a global UNIQUE constraint;
    # uniqueness is enforced per (barcode, branch_id) at the application level.
    #
    # Two legacy schema variants must be handled:
    #   1. A standalone unique index named ix_product_barcode (created by the old
    #      model definition via SQLAlchemy). Droppable with DROP INDEX.
    #   2. An inline "UNIQUE (barcode)" table constraint (SQLite autoindex, e.g.
    #      sqlite_autoindex_product_1) baked into the CREATE TABLE statement.
    #      SQLite cannot drop such a constraint with ALTER TABLE, so the product
    #      table must be rebuilt without it (standard SQLite migration pattern).
    #      FK enforcement is OFF for this app's SQLite connection, so dropping
    #      and renaming the table is safe; child tables reference it by name.
    try:
        db.session.execute(text('DROP INDEX IF EXISTS ix_product_barcode'))
        db.session.commit()
    except Exception as e:
        app.logger.warning(f"Could not drop legacy barcode unique index: {str(e)}")

    product_indexes = [idx[0] for idx in db.session.execute(text(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'product'"
    )).fetchall()]

    barcode_unique_index = None
    for idx_name in product_indexes:
        idx_cols = [col[2] for col in db.session.execute(
            text(f'PRAGMA index_info("{idx_name}")')
        ).fetchall()]
        if idx_cols == ['barcode']:
            idx_list = db.session.execute(text('PRAGMA index_list("product")')).fetchall()
            for row in idx_list:
                # row: (seq, name, unique, origin, partial); origin 'u' = UNIQUE constraint
                if row[1] == idx_name and row[2] == 1 and row[3] == 'u':
                    barcode_unique_index = idx_name
                    break
        if barcode_unique_index:
            break

    if barcode_unique_index:
        app.logger.warning(
            f"Rebuilding product table to remove global UNIQUE constraint on barcode ({barcode_unique_index})"
        )
        db.session.execute(text('PRAGMA foreign_keys=OFF'))
        db.session.execute(text('''
            CREATE TABLE product_new (
                id INTEGER NOT NULL PRIMARY KEY,
                barcode VARCHAR(50),
                name VARCHAR(100) NOT NULL,
                price FLOAT NOT NULL,
                cost FLOAT,
                stock INTEGER,
                category VARCHAR(50),
                tax_rate FLOAT,
                photo_filename VARCHAR(255),
                reorder_point INTEGER DEFAULT 10,
                reorder_quantity INTEGER DEFAULT 50,
                reorder_enabled BOOLEAN DEFAULT 1,
                branch_id INTEGER REFERENCES branch (id),
                category_id INTEGER REFERENCES category (id)
            )
        '''))
        db.session.execute(text('''
            INSERT INTO product_new (id, barcode, name, price, cost, stock, category, tax_rate,
                                     photo_filename, reorder_point, reorder_quantity, reorder_enabled,
                                     branch_id, category_id)
            SELECT id, barcode, name, price, cost, stock, category, tax_rate,
                   photo_filename, reorder_point, reorder_quantity, reorder_enabled,
                   branch_id, category_id
            FROM product
        '''))
        db.session.execute(text('DROP TABLE product'))
        db.session.execute(text('ALTER TABLE product_new RENAME TO product'))
        db.session.commit()
        # Recreate the standard lookup indexes that were dropped with the old table,
        # plus the branch-scoped composite index (non-unique; uniqueness is app-level).
        # IF NOT EXISTS keeps this idempotent even if a stale index entry lingers.
        db.session.execute(text('CREATE INDEX IF NOT EXISTS idx_product_name ON product (name)'))
        db.session.execute(text('CREATE INDEX IF NOT EXISTS idx_product_category ON product (category)'))
        db.session.execute(text('CREATE INDEX IF NOT EXISTS idx_product_barcode_branch ON product (barcode, branch_id)'))
        db.session.execute(text('CREATE INDEX IF NOT EXISTS idx_product_branch_id ON product (branch_id, id DESC)'))
        db.session.commit()
    else:
        # Additive indexes: standard lookups plus the branch-scoped composite index
        # (non-unique; uniqueness is enforced at the application level).
        db.session.execute(text('CREATE INDEX IF NOT EXISTS idx_product_name ON product (name)'))
        db.session.execute(text('CREATE INDEX IF NOT EXISTS idx_product_category ON product (category)'))
        db.session.execute(text('CREATE INDEX IF NOT EXISTS idx_product_barcode_branch ON product (barcode, branch_id)'))
        db.session.execute(text('CREATE INDEX IF NOT EXISTS idx_product_branch_id ON product (branch_id, id DESC)'))
        db.session.commit()

    supplier_columns = [col['name'] for col in inspector.get_columns('supplier')]
    supplier_migrations = [
        ('payment_terms', 'ALTER TABLE supplier ADD COLUMN payment_terms VARCHAR(120)'),
        ('lead_time_days', 'ALTER TABLE supplier ADD COLUMN lead_time_days INTEGER'),
        ('is_active', 'ALTER TABLE supplier ADD COLUMN is_active BOOLEAN DEFAULT 1'),
        ('notes', 'ALTER TABLE supplier ADD COLUMN notes VARCHAR(300)'),
        ('updated_at', 'ALTER TABLE supplier ADD COLUMN updated_at DATETIME')
    ]
    for column_name, migration_sql in supplier_migrations:
        if column_name not in supplier_columns:
            db.session.execute(text(migration_sql))
            if column_name == 'updated_at':
                db.session.execute(text('UPDATE supplier SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL'))
            db.session.commit()

    sale_columns = [col['name'] for col in inspector.get_columns('sale')]
    sale_migrations = [
        ('cash_received', 'ALTER TABLE sale ADD COLUMN cash_received FLOAT'),
        ('refund_amount', 'ALTER TABLE sale ADD COLUMN refund_amount FLOAT DEFAULT 0'),
        ('payment_breakdown', 'ALTER TABLE sale ADD COLUMN payment_breakdown TEXT')
    ]
    for column_name, migration_sql in sale_migrations:
        if column_name not in sale_columns:
            db.session.execute(text(migration_sql))
            db.session.commit()

    if inspector.has_table('delivery'):
        delivery_columns = [col['name'] for col in inspector.get_columns('delivery')]
        delivery_migrations = [
            ('priority', "ALTER TABLE delivery ADD COLUMN priority VARCHAR(20) DEFAULT 'normal'"),
            ('township', 'ALTER TABLE delivery ADD COLUMN township VARCHAR(120)'),
            ('instructions', 'ALTER TABLE delivery ADD COLUMN instructions VARCHAR(400)'),
            ('delivery_fee', 'ALTER TABLE delivery ADD COLUMN delivery_fee FLOAT DEFAULT 0'),
            ('scheduled_at', 'ALTER TABLE delivery ADD COLUMN scheduled_at DATETIME'),
            ('packaged_at', 'ALTER TABLE delivery ADD COLUMN packaged_at DATETIME'),
            ('out_for_delivery_at', 'ALTER TABLE delivery ADD COLUMN out_for_delivery_at DATETIME'),
            ('delivered_at', 'ALTER TABLE delivery ADD COLUMN delivered_at DATETIME'),
            ('cancelled_at', 'ALTER TABLE delivery ADD COLUMN cancelled_at DATETIME'),
            ('proof_note', 'ALTER TABLE delivery ADD COLUMN proof_note VARCHAR(300)'),
            ('updated_at', 'ALTER TABLE delivery ADD COLUMN updated_at DATETIME')
        ]
        for column_name, migration_sql in delivery_migrations:
            if column_name not in delivery_columns:
                db.session.execute(text(migration_sql))
                if column_name == 'updated_at':
                    db.session.execute(text('UPDATE delivery SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL'))
                db.session.commit()

    # Debt table migrations for enhanced debt management
    if inspector.has_table('debt'):
        debt_columns = [col['name'] for col in inspector.get_columns('debt')]
        debt_migrations = [
            ('due_date', 'ALTER TABLE debt ADD COLUMN due_date DATETIME'),
            ('status', "ALTER TABLE debt ADD COLUMN status VARCHAR(20) DEFAULT 'pending'"),
            ('communication_notes', 'ALTER TABLE debt ADD COLUMN communication_notes TEXT'),
            ('last_contacted_at', 'ALTER TABLE debt ADD COLUMN last_contacted_at DATETIME'),
            ('created_by', 'ALTER TABLE debt ADD COLUMN created_by INTEGER'),
            ('created_at', 'ALTER TABLE debt ADD COLUMN created_at DATETIME'),
            ('updated_at', 'ALTER TABLE debt ADD COLUMN updated_at DATETIME')
        ]
        for column_name, migration_sql in debt_migrations:
            if column_name not in debt_columns:
                db.session.execute(text(migration_sql))
                if column_name == 'created_at':
                    db.session.execute(text('UPDATE debt SET created_at = date WHERE created_at IS NULL'))
                if column_name == 'updated_at':
                    db.session.execute(text('UPDATE debt SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL'))
                if column_name == 'status':
                    # Update existing debts with proper status based on balance
                    db.session.execute(text("UPDATE debt SET status = CASE WHEN balance > 0 THEN 'pending' WHEN balance <= 0 THEN 'paid' ELSE 'pending' END WHERE status IS NULL OR status = 'pending'"))
                db.session.commit()

    # Create debt_payment table for tracking payments
    if not inspector.has_table('debt_payment'):
        db.session.execute(text('''
            CREATE TABLE debt_payment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debt_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                payment_date DATETIME,
                notes VARCHAR(500),
                processed_by INTEGER,
                created_at DATETIME,
                FOREIGN KEY (debt_id) REFERENCES debt (id),
                FOREIGN KEY (customer_id) REFERENCES customer (id),
                FOREIGN KEY (processed_by) REFERENCES user (id)
            )
        '''))
        db.session.commit()

    # Migrate old payment records to DebtPayment table and clean up
    if inspector.has_table('debt') and inspector.has_table('debt_payment'):
        # Check if there are old payment records (legacy schema had debt.type='payment')
        # Only run this migration when the legacy column actually exists.
        try:
            debt_columns = [col['name'] for col in inspector.get_columns('debt')]
            if 'type' in debt_columns:
                old_payments = db.session.execute(text("SELECT id, customer_id, amount, date, notes FROM debt WHERE type = 'payment'"))
                old_payments = old_payments.fetchall()
                if old_payments:
                    db.session.execute(text("DELETE FROM debt WHERE type = 'payment'"))
                    db.session.commit()
                    app.logger.info(f"Migrated {len(old_payments)} old payment records removed")
        except Exception as e:
            app.logger.warning(f"Could not migrate old payment records: {str(e)}")

    # Supplier table migrations for enhanced fields
    if inspector.has_table('supplier'):
        supplier_columns = [col['name'] for col in inspector.get_columns('supplier')]
        supplier_migrations = [
            ('category', 'ALTER TABLE supplier ADD COLUMN category VARCHAR(50)'),
            ('tax_id', 'ALTER TABLE supplier ADD COLUMN tax_id VARCHAR(50)'),
            ('website', 'ALTER TABLE supplier ADD COLUMN website VARCHAR(200)'),
            ('bank_name', 'ALTER TABLE supplier ADD COLUMN bank_name VARCHAR(100)'),
            ('bank_account', 'ALTER TABLE supplier ADD COLUMN bank_account VARCHAR(50)'),
            ('quality_rating', 'ALTER TABLE supplier ADD COLUMN quality_rating REAL DEFAULT 0.0'),
            ('delivery_rating', 'ALTER TABLE supplier ADD COLUMN delivery_rating REAL DEFAULT 0.0'),
            ('total_orders', 'ALTER TABLE supplier ADD COLUMN total_orders INTEGER DEFAULT 0'),
            ('on_time_deliveries', 'ALTER TABLE supplier ADD COLUMN on_time_deliveries INTEGER DEFAULT 0'),
        ]
        for column_name, migration_sql in supplier_migrations:
            if column_name not in supplier_columns:
                db.session.execute(text(migration_sql))
                db.session.commit()

    # PurchaseOrder table migrations for enhanced fields
    if inspector.has_table('purchase_order'):
        po_columns = [col['name'] for col in inspector.get_columns('purchase_order')]
        po_migrations = [
            ('total_amount', 'ALTER TABLE purchase_order ADD COLUMN total_amount REAL DEFAULT 0.0'),
            ('expected_delivery_date', 'ALTER TABLE purchase_order ADD COLUMN expected_delivery_date DATETIME'),
            ('approved_by', 'ALTER TABLE purchase_order ADD COLUMN approved_by INTEGER REFERENCES user (id)'),
            ('approved_at', 'ALTER TABLE purchase_order ADD COLUMN approved_at DATETIME'),
            ('cancelled_at', 'ALTER TABLE purchase_order ADD COLUMN cancelled_at DATETIME'),
            ('cancelled_reason', 'ALTER TABLE purchase_order ADD COLUMN cancelled_reason VARCHAR(300)'),
        ]
        for column_name, migration_sql in po_migrations:
            if column_name not in po_columns:
                db.session.execute(text(migration_sql))
                db.session.commit()
        
        # Update status column length if needed (for longer status values)
        # SQLite doesn't support ALTER COLUMN, but the data will still work

    # Create supplier_communication table
    if not inspector.has_table('supplier_communication'):
        db.session.execute(text('''
            CREATE TABLE supplier_communication (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id INTEGER NOT NULL,
                communication_type VARCHAR(20) NOT NULL,
                subject VARCHAR(200),
                content TEXT,
                created_by INTEGER,
                created_at DATETIME,
                FOREIGN KEY (supplier_id) REFERENCES supplier (id),
                FOREIGN KEY (created_by) REFERENCES user (id)
            )
        '''))
        db.session.commit()

    # Create supplier_price_agreement table
    if not inspector.has_table('supplier_price_agreement'):
        db.session.execute(text('''
            CREATE TABLE supplier_price_agreement (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                agreed_price REAL NOT NULL,
                valid_from DATETIME,
                valid_to DATETIME,
                notes VARCHAR(200),
                created_at DATETIME,
                FOREIGN KEY (supplier_id) REFERENCES supplier (id),
                FOREIGN KEY (product_id) REFERENCES product (id)
            )
        '''))
        db.session.commit()

    # Create warehouse_inventory table
    if not inspector.has_table('warehouse_inventory'):
        db.session.execute(text('''
            CREATE TABLE warehouse_inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                quantity INTEGER DEFAULT 0,
                location VARCHAR(50),
                batch_number VARCHAR(50),
                received_date DATETIME,
                expiry_date DATETIME,
                unit_cost REAL,
                notes VARCHAR(200),
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY (product_id) REFERENCES product (id)
            )
        '''))
        db.session.commit()

    # Create warehouse_transfer table
    if not inspector.has_table('warehouse_transfer'):
        db.session.execute(text('''
            CREATE TABLE warehouse_transfer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                from_warehouse INTEGER DEFAULT 1,
                batch_number VARCHAR(50),
                performed_by INTEGER,
                notes VARCHAR(200),
                created_at DATETIME,
                FOREIGN KEY (product_id) REFERENCES product (id),
                FOREIGN KEY (performed_by) REFERENCES user (id)
            )
        '''))
        db.session.commit()

    # Create Promotion table
    if not hasattr(Product, 'promotions'):
        db.create_all()
    if not User.query.filter_by(username='admin').first():
        admin_user = User(
            username='admin',
            password=generate_password_hash('admin123'),
            role='manager'
        )
        db.session.add(admin_user)
        db.session.commit()

    # Provision the vendor-only account-creation barrier from the deployment
    # environment (POS_ACCOUNT_BARRIER_*). Only the password hash is stored.
    seed_account_barrier_credential()

    if not AppSetting.query.filter_by(key='currency_code').first():
        db.session.add(AppSetting(key='currency_code', value='USD'))
        db.session.commit()

    if not AppSetting.query.filter_by(key='receipt_paper_size').first():
        db.session.add(AppSetting(key='receipt_paper_size', value=DEFAULT_RECEIPT_PAPER_SIZE))
        db.session.commit()

    # Seed the default unit system: one base unit per type plus common
    # conversions (1 kg = 1000 g, 1 lb = 453.59237 g, ...). Managers can
    # rename, adjust or extend everything in Settings -> Units of Measurement.
    # The units_seeded flag makes this a true first-startup seed: a manager
    # who later deletes every unit does not get the defaults resurrected.
    if not AppSetting.query.filter_by(key='units_seeded').first():
        if db.session.execute(text('SELECT COUNT(*) FROM unit')).scalar() == 0:
            default_units = [
                # (name, symbol, unit_type, base symbol, factor_to_base, sort_order)
                ('Unit', 'unit', 'count', None, 1.0, 1),
                ('Pair', 'pr', 'count', 'unit', 2.0, 2),
                ('Dozen', 'dz', 'count', 'unit', 12.0, 3),
                ('Gram', 'g', 'weight', None, 1.0, 10),
                ('Kilogram', 'kg', 'weight', 'g', 1000.0, 11),
                ('Pound', 'lb', 'weight', 'g', 453.59237, 12),
                ('Ounce', 'oz', 'weight', 'g', 28.349523125, 13),
                ('Milliliter', 'ml', 'volume', None, 1.0, 20),
                ('Liter', 'l', 'volume', 'ml', 1000.0, 21),
                ('Meter', 'm', 'length', None, 1.0, 30),
                ('Centimeter', 'cm', 'length', 'm', 0.01, 31),
                ('Foot', 'ft', 'length', 'm', 0.3048, 32),
                ('Inch', 'in', 'length', 'm', 0.0254, 33),
            ]
            seeded_units = {}
            for name, symbol, unit_type, base_symbol, factor, sort_order in default_units:
                unit = Unit(
                    name=name, symbol=symbol, unit_type=unit_type,
                    factor_to_base=factor, sort_order=sort_order
                )
                db.session.add(unit)
                db.session.flush()
                seeded_units[symbol] = unit
            for name, symbol, unit_type, base_symbol, factor, sort_order in default_units:
                if base_symbol:
                    seeded_units[symbol].base_unit_id = seeded_units[base_symbol].id
            db.session.commit()
        db.session.add(AppSetting(key='units_seeded', value='true'))
        db.session.commit()
        db.session.execute(text(
            'CREATE INDEX IF NOT EXISTS idx_product_unit ON product (unit_id)'
        ))
        db.session.commit()

    # Encrypt any legacy plaintext AI API key so it is never stored in the clear.
    migrate_legacy_secrets()

    # Deleting a product keeps its history rows and only unlinks them, so
    # return/exchange lines must accept a NULL product_id. Legacy SQLite tables
    # declare product_id NOT NULL, which ALTER TABLE cannot drop, so the table
    # is rebuilt with the standard SQLite migration pattern.
    if inspector.has_table('return_exchange_item'):
        product_column = {
            col['name']: col for col in inspector.get_columns('return_exchange_item')
        }.get('product_id')
        if product_column is not None and not product_column.get('nullable', True):
            app.logger.warning(
                "Rebuilding return_exchange_item so product_id can be NULL after "
                "a product is deleted"
            )
            db.session.execute(text('PRAGMA foreign_keys=OFF'))
            db.session.execute(text('''
                CREATE TABLE return_exchange_item_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    return_exchange_id INTEGER NOT NULL REFERENCES return_exchange (id),
                    original_sale_item_id INTEGER REFERENCES sale_item (id),
                    product_id INTEGER REFERENCES product (id),
                    movement VARCHAR(20) NOT NULL,
                    quantity INTEGER NOT NULL,
                    unit_price FLOAT NOT NULL,
                    tax_rate FLOAT DEFAULT 0.0,
                    line_total FLOAT NOT NULL,
                    line_tax FLOAT NOT NULL
                )
            '''))
            db.session.execute(text('''
                INSERT INTO return_exchange_item_new (id, return_exchange_id,
                                                      original_sale_item_id, product_id,
                                                      movement, quantity, unit_price,
                                                      tax_rate, line_total, line_tax)
                SELECT id, return_exchange_id, original_sale_item_id, product_id,
                       movement, quantity, unit_price, tax_rate, line_total, line_tax
                FROM return_exchange_item
            '''))
            db.session.execute(text('DROP TABLE return_exchange_item'))
            db.session.execute(text(
                'ALTER TABLE return_exchange_item_new RENAME TO return_exchange_item'
            ))
            db.session.execute(text(
                'CREATE INDEX IF NOT EXISTS idx_return_exchange_item_product '
                'ON return_exchange_item (product_id)'
            ))
            db.session.commit()

    # Performance indexes (safe for repeated startup)
    performance_indexes = [
        'CREATE INDEX IF NOT EXISTS idx_product_name ON product(name)',
        'CREATE INDEX IF NOT EXISTS idx_product_category ON product(category)',
        'CREATE INDEX IF NOT EXISTS idx_product_branch_id ON product(branch_id, id DESC)',
        'CREATE INDEX IF NOT EXISTS idx_sale_date ON sale(date)',
        'CREATE INDEX IF NOT EXISTS idx_sale_user_date ON sale(user_id, date)',
        'CREATE INDEX IF NOT EXISTS idx_sale_item_sale_id ON sale_item(sale_id)',
        'CREATE INDEX IF NOT EXISTS idx_sale_item_product_id ON sale_item(product_id)',
        'CREATE INDEX IF NOT EXISTS idx_debt_customer_balance ON debt(customer_id, balance)',
        'CREATE INDEX IF NOT EXISTS idx_debt_status_date ON debt(status, date)',
        'CREATE INDEX IF NOT EXISTS idx_purchase_order_status_created ON purchase_order(status, created_at)',
        'CREATE INDEX IF NOT EXISTS idx_delivery_stage_priority ON delivery(stage, priority)',
        'CREATE INDEX IF NOT EXISTS idx_delivery_created_at ON delivery(created_at)',
        'CREATE INDEX IF NOT EXISTS idx_warehouse_product_qty ON warehouse_inventory(product_id, quantity)',
        'CREATE INDEX IF NOT EXISTS idx_product_unit ON product(unit_id)',
        # Keep these explicit for databases created before the memory models
        # existed.  IF NOT EXISTS makes startup safe and idempotent on SQLite.
        'CREATE INDEX IF NOT EXISTS idx_memory_registry_owner ON memory_registry(user_id, branch_id, scope)',
        'CREATE INDEX IF NOT EXISTS idx_memory_audit_actor_branch ON memory_audit(actor_user_id, branch_id, created_at)'
    ]
    for index_sql in performance_indexes:
        try:
            db.session.execute(text(index_sql))
        except Exception as e:
            app.logger.warning(f'Failed to create index: {e}')
    db.session.commit()

# Authentication routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session.permanent = request.form.get('remember') == 'on'
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = user.role
            # Set default branch in session
            default_branch = Branch.query.filter_by(is_default=True, is_active=True).first()
            if default_branch:
                session['branch_id'] = default_branch.id
            else:
                # Fallback to first active branch
                first_branch = Branch.query.filter_by(is_active=True).first()
                if first_branch:
                    session['branch_id'] = first_branch.id
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# Dashboard route
@app.route('/')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template(
        'dashboard.html',
        pos_name='Parrot POS',
        currency_code=get_currency_code(),
        currency_suffix=get_currency_suffix()
    )

@app.route('/api/settings', methods=['GET', 'PUT'])
def api_settings():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        # Check if AI API key is configured (don't return the actual key)
        ai_api_key = get_setting('ai_api_key', '')
        return jsonify({
            'pos_name': 'Parrot POS',
            'currency_code': get_currency_code(),
            'currency_suffix': get_currency_suffix(),
            'receipt_paper_size': get_receipt_paper_size(),
            'label_geometry': get_label_geometry(),
            'receipt_customization': get_receipt_customization_settings(
                db.session.get(Branch, get_current_branch_id())
            ),
            'mmqr_url': mmqr_url(get_setting('mmqr_filename', '')),
            'ai_api_key_configured': bool(ai_api_key and len(ai_api_key) > 10)
        })

    if session.get('role') != 'manager':
        return jsonify({'success': False, 'message': 'Manager access required'}), 403

    data = request.get_json() or {}
    
    updated_settings = {}

    # Handle currency code update
    currency_code = data.get('currency_code')
    if currency_code is not None:
        if currency_code not in CURRENCY_OPTIONS:
            return jsonify({'success': False, 'message': 'Invalid currency code'}), 400
        updated_settings['currency_code'] = currency_code

    receipt_paper_size = data.get('receipt_paper_size')
    if receipt_paper_size is not None:
        normalized_paper_size = str(receipt_paper_size).strip().upper()
        if normalized_paper_size not in RECEIPT_PAPER_OPTIONS:
            return jsonify({'success': False, 'message': 'Invalid receipt paper size'}), 400
        updated_settings['receipt_paper_size'] = normalized_paper_size

    label_geometry = data.get('label_geometry')
    if label_geometry is not None:
        if not isinstance(label_geometry, dict):
            return jsonify({'success': False, 'message': 'Invalid label geometry'}), 400
        for key, value in label_geometry.items():
            if key not in LABEL_SETTING_LIMITS:
                continue  # ignore unknown keys so older clients cannot break saves
            normalized = normalize_label_setting(key, value)
            low, high = LABEL_SETTING_LIMITS[key]
            if normalized is None:
                label = key.replace('label_', '').replace('_', ' ').replace('mm', '(mm)').strip()
                return jsonify({
                    'success': False,
                    'message': f'Invalid label {label}: use a number between {low:g} and {high:g}'
                }), 400
            updated_settings[key] = str(normalized)

    customization = data.get('receipt_customization')
    if customization is not None:
        if not isinstance(customization, dict):
            return jsonify({'success': False, 'message': 'Invalid receipt customization'}), 400
        if not str(customization.get('brand_name') or '').strip():
            return jsonify({'success': False, 'message': 'Receipt brand name is required'}), 400
        try:
            normalized_identity = normalize_receipt_identity({
                'brand_name': customization.get('brand_name'),
                'logo_filename': get_setting('receipt_logo_filename', ''),
                'email': customization.get('email'),
                'phone': customization.get('phone'),
                'address': customization.get('address'),
                'footer_message': customization.get('footer_message'),
            })
        except ValueError as error:
            return jsonify({'success': False, 'message': str(error)}), 400
        updated_settings.update({
            'receipt_brand_name': normalized_identity['brand_name'],
            'receipt_email': str(customization.get('email') or '').strip(),
            'receipt_phone': str(customization.get('phone') or '').strip(),
            'receipt_address': str(customization.get('address') or '').strip(),
            'receipt_footer_message': normalized_identity['footer_message'],
        })

    if updated_settings:
        for key, value in updated_settings.items():
            setting = AppSetting.query.filter_by(key=key).first()
            if setting:
                setting.value = value
            else:
                db.session.add(AppSetting(key=key, value=value))
        db.session.commit()
        effective_currency = updated_settings.get('currency_code', get_currency_code())
        return jsonify({
            'success': True,
            'message': 'Settings updated',
            'currency_code': effective_currency,
            'currency_suffix': get_currency_suffix(effective_currency),
            'receipt_paper_size': updated_settings.get('receipt_paper_size', get_receipt_paper_size()),
            'label_geometry': get_label_geometry(),
            'receipt_customization': get_receipt_customization_settings(
                db.session.get(Branch, get_current_branch_id())
            )
        })
    
    # Handle AI API key update
    ai_api_key = data.get('ai_api_key')
    if ai_api_key is not None:
        if ai_api_key == "":
            # Clear the API key
            set_setting('ai_api_key', '')
            # Also update environment variable for current session
            os.environ.pop('APIFREE_API_KEY', None)
            # Reset AI agent to pick up the change
            from agent_orchestrator import reset_orchestrator
            from ai_agent import reset_agent
            reset_orchestrator()
            reset_agent()
            return jsonify({
                'success': True,
                'message': 'API key cleared',
                'ai_api_key_configured': False
            })
        else:
            # Validate API key format (basic check)
            if len(ai_api_key) < 10:
                return jsonify({'success': False, 'message': 'Invalid API key format'}), 400
            
            # Save the API key
            set_setting('ai_api_key', ai_api_key)
            # Update environment variable for current session
            os.environ['APIFREE_API_KEY'] = ai_api_key
            # Reset AI agent to pick up the new key
            from agent_orchestrator import reset_orchestrator
            from ai_agent import reset_agent
            reset_orchestrator()
            reset_agent()
            return jsonify({
                'success': True,
                'message': 'API key saved',
                'ai_api_key_configured': True
            })
    
    return jsonify({'success': False, 'message': 'No valid settings provided'}), 400

@app.route('/api/settings/database_backup', methods=['GET'])
@manager_required
def api_settings_database_backup():
    db_file_path = resolve_database_file_path()
    if not db_file_path:
        return jsonify({'success': False, 'message': 'Database file not found'}), 404

    backup_filename = f"pos_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.db"
    return send_file(
        db_file_path,
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name=backup_filename,
        conditional=True
    )

@app.route('/api/settings/database_restore', methods=['POST'])
@manager_required
def api_settings_database_restore():
    """Restore database from a backup file."""
    if 'database' not in request.files:
        return jsonify({'success': False, 'message': 'No database file provided'}), 400
    
    file = request.files['database']
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No file selected'}), 400
    
    # Check file extension
    if not file.filename.lower().endswith('.db'):
        return jsonify({'success': False, 'message': 'Invalid file type. Please select a .db file'}), 400
    
    # Get current database path
    db_file_path = resolve_database_file_path()
    if not db_file_path:
        return jsonify({'success': False, 'message': 'Current database file not found'}), 404
    
    try:
        # Read the uploaded file
        uploaded_data = file.read()
        
        # Validate it's a valid SQLite database by checking the header
        # SQLite databases start with "SQLite format 3\x00"
        if not uploaded_data.startswith(b'SQLite format 3\x00'):
            return jsonify({'success': False, 'message': 'Invalid database file. The file is not a valid SQLite database.'}), 400
        
        # Create a backup of the current database before replacing
        backup_path = db_file_path + '.pre_restore_backup'
        if os.path.exists(db_file_path):
            import shutil
            shutil.copy2(db_file_path, backup_path)
        
        # Close all database connections
        db.session.remove()
        db.engine.dispose()
        
        # Write the new database
        with open(db_file_path, 'wb') as f:
            f.write(uploaded_data)
        
        # Re-initialize the database connection
        db.session.configure(bind=db.engine)
        
        # Verify the restored database by trying to query it
        try:
            db.session.execute(text('SELECT 1'))
            db.session.commit()
        except Exception as verify_error:
            # Restore failed, revert to the backup
            if os.path.exists(backup_path):
                shutil.copy2(backup_path, db_file_path)
            return jsonify({'success': False, 'message': f'Restored database is corrupted. Rolled back to previous state. Error: {str(verify_error)}'}), 500
        
        # Clean up the pre-restore backup
        if os.path.exists(backup_path):
            os.remove(backup_path)
        
        return jsonify({
            'success': True,
            'message': 'Database restored successfully. Please refresh the page to see the changes.'
        })
        
    except Exception as e:
        app.logger.error(f"Error restoring database: {str(e)}")
        return jsonify({'success': False, 'message': f'Failed to restore database: {str(e)}'}), 500

# Branch Management API Endpoints
@app.route('/api/branches', methods=['GET', 'POST'])
def api_branches():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    if request.method == 'GET':
        # List all branches
        branches = Branch.query.order_by(Branch.is_default.desc(), Branch.name.asc()).all()
        return jsonify([branch.to_dict() for branch in branches])
    
    # POST - Create new branch (manager only)
    if session.get('role') != 'manager':
        return jsonify({'success': False, 'message': 'Manager access required'}), 403
    
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    code = (data.get('code') or '').strip().upper()
    address = (data.get('address') or '').strip()
    phone = (data.get('phone') or '').strip()
    email = (data.get('email') or '').strip()
    is_default = data.get('is_default', False)
    
    if not name:
        return jsonify({'success': False, 'message': 'Branch name is required'}), 400
    if not code:
        return jsonify({'success': False, 'message': 'Branch code is required'}), 400
    
    # Check for duplicate code
    existing = Branch.query.filter_by(code=code).first()
    if existing:
        return jsonify({'success': False, 'message': 'Branch code already exists'}), 400
    
    try:
        # If this is set as default, unset other defaults
        if is_default:
            Branch.query.update({'is_default': False})
        
        branch = Branch(
            name=name,
            code=code,
            address=address or None,
            phone=phone or None,
            email=email or None,
            is_default=is_default,
            is_active=True
        )
        db.session.add(branch)
        db.session.commit()
        return jsonify({'success': True, 'branch': branch.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/branches/<int:branch_id>', methods=['GET', 'PUT', 'DELETE'])
def api_branch(branch_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    branch = db.session.get(Branch, branch_id)
    if not branch:
        return jsonify({'success': False, 'message': 'Branch not found'}), 404
    
    if request.method == 'GET':
        return jsonify(branch.to_dict())
    
    # PUT and DELETE require manager access
    if session.get('role') != 'manager':
        return jsonify({'success': False, 'message': 'Manager access required'}), 403
    
    if request.method == 'PUT':
        data = request.get_json() or {}
        name = (data.get('name') or '').strip()
        code = (data.get('code') or '').strip().upper()
        address = (data.get('address') or '').strip()
        phone = (data.get('phone') or '').strip()
        email = (data.get('email') or '').strip()
        is_active = data.get('is_active', branch.is_active)
        is_default = data.get('is_default', False)
        
        if name:
            branch.name = name
        if code:
            # Check for duplicate code
            existing = Branch.query.filter(Branch.code == code, Branch.id != branch_id).first()
            if existing:
                return jsonify({'success': False, 'message': 'Branch code already exists'}), 400
            branch.code = code
        branch.address = address or None
        branch.phone = phone or None
        branch.email = email or None
        branch.is_active = is_active
        
        # Handle default branch
        if is_default and not branch.is_default:
            Branch.query.update({'is_default': False})
            branch.is_default = True
        
        db.session.commit()
        return jsonify({'success': True, 'branch': branch.to_dict()})
    
    # DELETE - Deactivate branch (soft delete)
    if branch.is_default:
        return jsonify({'success': False, 'message': 'Cannot delete the default branch'}), 400
    
    # Check if branch has data
    has_products = Product.query.filter_by(branch_id=branch_id).count() > 0
    has_sales = Sale.query.filter_by(branch_id=branch_id).count() > 0
    
    if has_products or has_sales:
        # Soft delete - just deactivate
        branch.is_active = False
        db.session.commit()
        return jsonify({'success': True, 'message': 'Branch deactivated (has associated data)'})
    
    db.session.delete(branch)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Branch deleted'})

@app.route('/api/branches/switch/<int:branch_id>', methods=['POST'])
def api_switch_branch(branch_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    branch = db.session.get(Branch, branch_id)
    if not branch:
        return jsonify({'success': False, 'message': 'Branch not found'}), 404
    
    if not branch.is_active:
        return jsonify({'success': False, 'message': 'Cannot switch to inactive branch'}), 400
    
    session['branch_id'] = branch_id
    return jsonify({
        'success': True,
        'branch': branch.to_dict(),
        'message': f'Switched to {branch.name}'
    })

@app.route('/api/branches/current', methods=['GET'])
def api_current_branch():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    branch = get_current_branch()
    if branch:
        return jsonify(branch.to_dict())
    return jsonify({'error': 'No active branch found'}), 404

@app.route('/api/branches/<int:branch_id>/set_default', methods=['POST'])
def api_set_default_branch(branch_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    if session.get('role') != 'manager':
        return jsonify({'success': False, 'message': 'Manager access required'}), 403
    
    branch = db.session.get(Branch, branch_id)
    if not branch:
        return jsonify({'success': False, 'message': 'Branch not found'}), 404
    
    if not branch.is_active:
        return jsonify({'success': False, 'message': 'Cannot set inactive branch as default'}), 400
    
    # Unset all defaults
    Branch.query.update({'is_default': False})
    branch.is_default = True
    db.session.commit()
    
    return jsonify({'success': True, 'branch': branch.to_dict()})

@app.route('/api/inventory/alerts', methods=['GET'])
@manager_required
def api_inventory_alerts():
    return jsonify(build_inventory_alert_payload(get_current_branch_id()))

@app.route('/api/inventory/suggested_purchase_order', methods=['POST'])
@manager_required
def api_inventory_suggested_purchase_order():
    data = request.get_json() or {}
    supplier_id = data.get('supplier_id')
    if not supplier_id:
        return jsonify({'success': False, 'message': 'Supplier is required'}), 400

    supplier = db.session.get(Supplier, supplier_id)
    if not supplier:
        return jsonify({'success': False, 'message': 'Supplier not found'}), 404

    payload = build_inventory_alert_payload(get_current_branch_id())
    suggested_items = payload.get('suggested_purchase_order', {}).get('items', [])
    if not suggested_items:
        return jsonify({'success': False, 'message': 'No low-stock items to generate purchase order'}), 400

    try:
        po = PurchaseOrder(
            po_number=generate_po_number(),
            supplier_id=supplier.id,
            status='draft',
            notes=(data.get('notes') or '').strip() or 'System generated from inventory alerts',
            created_by=session.get('user_id'),
            branch_id=get_current_branch_id()
        )
        db.session.add(po)
        db.session.flush()

        total_amount = 0.0
        for item in suggested_items:
            product = db.session.get(Product, item['product_id'])
            if not product:
                continue
            ordered_qty = max(int(item.get('suggested_qty') or 0), 1)
            unit_cost = float(product.cost or 0)
            db.session.add(PurchaseOrderItem(
                purchase_order_id=po.id,
                product_id=product.id,
                ordered_qty=ordered_qty,
                received_qty=0,
                unit_cost=unit_cost
            ))
            total_amount += ordered_qty * unit_cost

        po.total_amount = total_amount
        db.session.commit()
        return jsonify({
            'success': True,
            'message': 'Suggested purchase order created',
            'purchase_order_id': po.id,
            'po_number': po.po_number,
            'items_count': len(suggested_items)
        }), 201
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error creating suggested purchase order: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to create suggested purchase order'}), 500

@app.route('/uploads/products/<path:filename>')
def product_image(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/uploads/receipts/<path:filename>')
@login_required
def receipt_logo(filename):
    response = make_response(send_from_directory(app.config['RECEIPT_LOGO_FOLDER'], filename))
    response.headers['Cache-Control'] = 'private, max-age=86400'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.route('/uploads/mmqr/<path:filename>')
@login_required
def mmqr_image(filename):
    response = make_response(send_from_directory(app.config['MMQR_FOLDER'], filename))
    response.headers['Cache-Control'] = 'private, max-age=86400'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.route('/api/settings/receipt-logo', methods=['POST', 'DELETE'])
@manager_required
def api_receipt_logo():
    if request.method == 'DELETE':
        set_setting('receipt_logo_filename', '')
        return jsonify({'success': True, 'message': 'Receipt logo removed', 'logo_url': None})

    logo = request.files.get('logo')
    if not logo or not logo.filename:
        return jsonify({'success': False, 'message': 'Select a logo image'}), 400
    if request.content_length and request.content_length > 600 * 1024:
        return jsonify({'success': False, 'message': 'Receipt logo must be 512 KB or smaller'}), 413
    header = logo.stream.read(16)
    logo.stream.seek(0)
    extension = detect_receipt_logo_extension(header)
    if extension not in {'png', 'jpg', 'webp'}:
        return jsonify({'success': False, 'message': 'Use a valid PNG, JPG, or WebP logo'}), 400

    filename = f"{uuid.uuid4().hex}.{extension}"
    destination = os.path.join(app.config['RECEIPT_LOGO_FOLDER'], filename)
    logo.save(destination)
    if os.path.getsize(destination) > 512 * 1024:
        os.remove(destination)
        return jsonify({'success': False, 'message': 'Receipt logo must be 512 KB or smaller'}), 413
    set_setting('receipt_logo_filename', filename)
    return jsonify({
        'success': True,
        'message': 'Receipt logo updated',
        'logo_filename': filename,
        'logo_url': receipt_logo_url(filename)
    })

@app.route('/api/settings/mmqr', methods=['POST', 'DELETE'])
@manager_required
def api_mmqr():
    if request.method == 'DELETE':
        previous = get_setting('mmqr_filename', '')
        set_setting('mmqr_filename', '')
        delete_mmqr_file(previous)
        return jsonify({'success': True, 'message': 'MMQR removed', 'mmqr_url': None})

    image = request.files.get('mmqr')
    if not image or not image.filename:
        return jsonify({'success': False, 'message': 'Select an MMQR image'}), 400
    if request.content_length and request.content_length > 2 * 1024 * 1024:
        return jsonify({'success': False, 'message': 'MMQR image must be 2 MB or smaller'}), 413

    header = image.stream.read(16)
    image.stream.seek(0)
    extension = detect_receipt_logo_extension(header)
    if extension not in {'png', 'jpg'}:
        return jsonify({'success': False, 'message': 'Use a valid PNG or JPG MMQR image'}), 400

    filename = f"{uuid.uuid4().hex}.{extension}"
    destination = os.path.join(app.config['MMQR_FOLDER'], filename)
    image.save(destination)
    if os.path.getsize(destination) > 2 * 1024 * 1024:
        os.remove(destination)
        return jsonify({'success': False, 'message': 'MMQR image must be 2 MB or smaller'}), 413
    if not is_valid_mmqr_image(destination, extension):
        os.remove(destination)
        return jsonify({'success': False, 'message': 'The uploaded MMQR image is invalid or incomplete'}), 400

    previous = get_setting('mmqr_filename', '')
    set_setting('mmqr_filename', filename)
    if previous and previous != filename:
        delete_mmqr_file(previous)
    return jsonify({
        'success': True,
        'message': 'MMQR updated',
        'mmqr_filename': filename,
        'mmqr_url': mmqr_url(filename)
    })

@app.route('/public/<path:filename>')
def public_file(filename):
    return send_from_directory(os.path.join(app.root_path, 'public'), filename)


@app.route('/sw.js')
def service_worker_js():
    """Serve the offline Service Worker at root scope so it can control '/'.

    A Service Worker only runs in secure contexts (HTTPS or localhost); on a
    plain-HTTP LAN the browser ignores it and the localStorage data caches in
    dashboard.html still provide offline POS data. The 'no-cache' header forces
    the browser to re-check the worker script on each visit after a deploy.
    """
    response = make_response(send_from_directory(os.path.join(app.root_path, 'public'), 'sw.js'))
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response

# Category API Endpoints
@app.route('/api/categories', methods=['GET', 'POST'])
def api_categories():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    branch_id = get_default_branch_id()

    if request.method == 'GET':
        # Get all categories with optional filtering
        active_only = request.args.get('active_only', 'false').lower() == 'true'
        query = Category.query.filter_by(branch_id=branch_id)
        if active_only:
            query = query.filter_by(is_active=True)
        categories = query.order_by(Category.sort_order, Category.name).all()
        return jsonify([c.to_dict() for c in categories])

    elif request.method == 'POST':
        # Create new category
        data = request.get_json() or {}
        name = (data.get('name') or '').strip()
        
        if not name:
            return jsonify({'success': False, 'message': 'Category name is required'}), 400
        
        # Check for duplicate within same branch
        existing = Category.query.filter(db.func.lower(Category.name) == name.lower(), Category.branch_id == branch_id).first()
        if existing:
            return jsonify({'success': False, 'message': f'Category "{name}" already exists'}), 400
        
        category = Category(
            name=name,
            description=(data.get('description') or '').strip(),
            color=data.get('color', '#6c757d'),
            is_active=data.get('is_active', True),
            sort_order=data.get('sort_order', 0),
            branch_id=branch_id
        )
        db.session.add(category)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Category created', 'category': category.to_dict()}), 201

@app.route('/api/categories/<int:category_id>', methods=['GET', 'PUT', 'DELETE'])
def api_single_category(category_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    category = Category.query.filter_by(id=category_id, branch_id=get_default_branch_id()).first()
    if not category:
        return jsonify({'success': False, 'message': 'Category not found'}), 404

    if request.method == 'GET':
        return jsonify(category.to_dict())

    elif request.method == 'PUT':
        data = request.get_json() or {}
        name = (data.get('name') or '').strip()
        
        if not name:
            return jsonify({'success': False, 'message': 'Category name is required'}), 400
        
        # Check for duplicate within same branch (excluding current category)
        existing = Category.query.filter(
            db.func.lower(Category.name) == name.lower(),
            Category.id != category_id,
            Category.branch_id == category.branch_id
        ).first()
        if existing:
            return jsonify({'success': False, 'message': f'Category "{name}" already exists'}), 400
        
        category.name = name
        category.description = (data.get('description') or '').strip()
        category.color = data.get('color', category.color)
        category.is_active = data.get('is_active', category.is_active)
        category.sort_order = data.get('sort_order', category.sort_order)
        
        # Update legacy category field in products and suppliers
        old_name = category.category if hasattr(category, 'category') else None
        if old_name and old_name != name:
            Product.query.filter_by(category=old_name).update({'category': name})
            Supplier.query.filter_by(category=old_name).update({'category': name})
        
        db.session.commit()
        return jsonify({'success': True, 'message': 'Category updated', 'category': category.to_dict()})

    elif request.method == 'DELETE':
        # Check if category is in use
        product_count = Product.query.filter_by(category_id=category_id).count()
        supplier_count = Supplier.query.filter_by(category_id=category_id).count()
        
        if product_count > 0 or supplier_count > 0:
            return jsonify({
                'success': False, 
                'message': f'Cannot delete category. It is used by {product_count} product(s) and {supplier_count} supplier(s).',
                'product_count': product_count,
                'supplier_count': supplier_count
            }), 400
        
        db.session.delete(category)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Category deleted'})

@app.route('/api/categories/bulk-update', methods=['POST'])
def api_categories_bulk_update():
    """Bulk update category assignments for products or suppliers"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json() or {}
    action = data.get('action')
    item_type = data.get('item_type')  # 'product' or 'supplier'
    item_ids = data.get('item_ids', [])
    category_id = data.get('category_id')
    
    if not item_ids:
        return jsonify({'success': False, 'message': 'No items selected'}), 400
    
    if item_type == 'product':
        if category_id:
            category = db.session.get(Category, category_id)
            if not category:
                return jsonify({'success': False, 'message': 'Category not found'}), 404
            Product.query.filter(Product.id.in_(item_ids)).update(
                {'category_id': category_id, 'category': category.name},
                synchronize_session=False
            )
        else:
            Product.query.filter(Product.id.in_(item_ids)).update(
                {'category_id': None, 'category': None},
                synchronize_session=False
            )
    elif item_type == 'supplier':
        if category_id:
            category = db.session.get(Category, category_id)
            if not category:
                return jsonify({'success': False, 'message': 'Category not found'}), 404
            Supplier.query.filter(Supplier.id.in_(item_ids)).update(
                {'category_id': category_id, 'category': category.name},
                synchronize_session=False
            )
        else:
            Supplier.query.filter(Supplier.id.in_(item_ids)).update(
                {'category_id': None, 'category': None},
                synchronize_session=False
            )
    else:
        return jsonify({'success': False, 'message': 'Invalid item type'}), 400
    
    db.session.commit()
    return jsonify({'success': True, 'message': f'Updated {len(item_ids)} item(s)'})

# Unit API Endpoints
@app.route('/api/units', methods=['GET', 'POST'])
def api_units():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        active_only = to_bool(request.args.get('active_only'), False)
        query = Unit.query
        if active_only:
            query = query.filter_by(is_active=True)
        units = query.order_by(Unit.unit_type, Unit.sort_order, Unit.id).all()
        product_counts = dict(
            db.session.query(Product.unit_id, func.count(Product.id))
            .filter(Product.unit_id.isnot(None))
            .group_by(Product.unit_id)
            .all()
        )
        items = []
        for unit in units:
            item = unit.to_dict()
            item['product_count'] = product_counts.get(unit.id, 0)
            items.append(item)
        return jsonify({'items': items, 'unit_types': sorted(UNIT_TYPES)})

    # Creating units is a manager action, like every other settings change.
    if session.get('role') != 'manager':
        return jsonify({'success': False, 'message': 'Manager access required'}), 403

    data = request.get_json() or {}
    values, error = validate_unit_payload(data)
    if error:
        return jsonify({'success': False, 'message': error}), 400
    unit = Unit(**values)
    db.session.add(unit)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Unit added', 'unit': unit.to_dict()}), 201

@app.route('/api/units/<int:unit_id>', methods=['PUT', 'DELETE'])
def api_single_unit(unit_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    if session.get('role') != 'manager':
        return jsonify({'success': False, 'message': 'Manager access required'}), 403

    unit = db.session.get(Unit, unit_id)
    if not unit:
        return jsonify({'success': False, 'message': 'Unit not found'}), 404

    if request.method == 'PUT':
        data = request.get_json() or {}
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400
        merged = {
            'name': data.get('name', unit.name),
            'symbol': data.get('symbol', unit.symbol),
            'unit_type': data.get('unit_type', unit.unit_type),
            'base_unit_id': data.get('base_unit_id', unit.base_unit_id),
            'factor_to_base': data.get('factor_to_base', unit.factor_to_base),
            'sort_order': data.get('sort_order', unit.sort_order),
            'is_active': data.get('is_active', unit.is_active),
        }
        values, error = validate_unit_payload(merged, existing=unit)
        if error:
            return jsonify({'success': False, 'message': error}), 400
        if values['unit_type'] != unit.unit_type and unit.child_units:
            return jsonify({
                'success': False,
                'message': (
                    f'Cannot change the type of {unit.symbol}: other units '
                    'convert from it. Reassign their base unit first.'
                )
            }), 400
        for key, value in values.items():
            setattr(unit, key, value)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Unit updated', 'unit': unit.to_dict()})

    # DELETE: a unit that products count in, or that other units convert
    # from, is never removed silently - the references would become nonsense.
    product_count = Product.query.filter_by(unit_id=unit.id).count()
    if product_count:
        return jsonify({
            'success': False,
            'message': (
                f'Cannot delete "{unit.name}": {product_count} product(s) still '
                'use it. Change their unit first, or deactivate the unit instead.'
            )
        }), 409
    child_units = [child for child in unit.child_units]
    if child_units:
        symbols = ', '.join(sorted(child.symbol for child in child_units))
        return jsonify({
            'success': False,
            'message': (
                f'Cannot delete "{unit.name}": {symbols} convert from it. '
                'Reassign their base unit first.'
            )
        }), 409
    db.session.delete(unit)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Unit deleted'})

@app.route('/api/units/convert', methods=['GET'])
def api_units_convert():
    """Convert a quantity between two related units (same type group)."""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        from_unit = db.session.get(Unit, int(request.args.get('from_id')))
        to_unit = db.session.get(Unit, int(request.args.get('to_id')))
        quantity = safe_to_decimal(request.args.get('quantity', '1'), default=None)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Invalid conversion request'}), 400
    if from_unit is None or to_unit is None or quantity is None or quantity < 0:
        return jsonify({'success': False, 'message': 'Invalid conversion request'}), 400
    if quantity > Decimal('1000000000000'):
        return jsonify({'success': False, 'message': 'Quantity is too large to convert'}), 400
    converted = convert_unit_quantity(from_unit, to_unit, quantity)
    if converted is None:
        return jsonify({
            'success': False,
            'message': f'{from_unit.symbol} and {to_unit.symbol} are not related units'
        }), 400
    return jsonify({
        'success': True,
        'from_symbol': from_unit.symbol,
        'to_symbol': to_unit.symbol,
        'quantity': float(quantity.normalize()),
        'converted': float(converted)
    })

# Product API Endpoints
@app.route('/api/products', methods=['GET', 'POST'])
def api_products():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        q = (request.args.get('q') or '').strip()
        page = request.args.get('page', type=int)
        per_page = request.args.get('per_page', type=int)
        branch_id = get_requested_branch_id(default_to_current=True)
        if branch_id is None:
            branch_id = get_current_branch_id()

        # The POS grid only needs a small card payload and appends successive
        # pages. Cursor pagination avoids an increasingly expensive OFFSET and
        # COUNT(*) as a branch catalog grows.
        if request.args.get('view') == 'pos':
            safe_per_page = max(1, min(per_page or 50, 100))
            cursor = request.args.get('cursor', type=int)
            if request.args.get('cursor') and (cursor is None or cursor < 1):
                return jsonify({'success': False, 'message': 'Invalid cursor'}), 400

            pos_query = db.session.query(
                Product.id, Product.barcode, Product.name, Product.price,
                Product.stock, Product.tax_rate, Product.photo_filename,
                Product.unit_id,
            ).filter(Product.branch_id == branch_id)
            if cursor:
                pos_query = pos_query.filter(Product.id < cursor)
            rows = pos_query.order_by(Product.id.desc()).limit(safe_per_page + 1).all()
            has_more = len(rows) > safe_per_page
            rows = rows[:safe_per_page]
            unit_ids = {row.unit_id for row in rows if row.unit_id}
            unit_symbols = dict(
                db.session.query(Unit.id, Unit.symbol)
                .filter(Unit.id.in_(unit_ids)).all()
            ) if unit_ids else {}
            items = [{
                'id': row.id,
                'barcode': row.barcode,
                'name': row.name,
                'price': row.price,
                'stock': row.stock,
                'tax_rate': row.tax_rate,
                'unit_symbol': unit_symbols.get(row.unit_id),
                'photo_url': product_photo_url(row.photo_filename),
            } for row in rows]
            return jsonify({
                'items': items,
                'has_more': has_more,
                'next_cursor': rows[-1].id if has_more else None,
            })

        query = Product.query.filter_by(branch_id=branch_id)
        if q:
            like_q = f'%{q}%'
            query = query.filter(
                (Product.name.ilike(like_q)) |
                (Product.barcode.ilike(like_q)) |
                (Product.category.ilike(like_q))
            )

        query = query.order_by(Product.id.desc())

        if page and per_page:
            safe_per_page = max(1, min(per_page, 100))
            pagination = query.paginate(page=page, per_page=safe_per_page, error_out=False)
            return jsonify({
                'items': [serialize_product(p) for p in pagination.items],
                'page': pagination.page,
                'per_page': safe_per_page,
                'total': pagination.total,
                'total_pages': pagination.pages
            })

        products = query.all()
        return jsonify([serialize_product(p) for p in products])

    elif request.method == 'POST':
        is_multipart = request.content_type and 'multipart/form-data' in request.content_type.lower()
        data = request.form if is_multipart else (request.get_json() or {})

        name = (data.get('name') or '').strip()
        price = data.get('price')
        stock = data.get('stock')

        if not name or price is None or stock is None:
            return jsonify({'success': False, 'message': 'Missing required fields'}), 400

        try:
            price = safe_to_decimal(price, default=None)
            stock = int(stock)
            cost = safe_to_decimal(data.get('cost', 0) or 0)
            tax_rate = safe_to_decimal(data.get('tax_rate', 0) or 0)
            reorder_point = max(int(data.get('reorder_point', 10) or 0), 0)
            reorder_quantity = max(int(data.get('reorder_quantity', 50) or 0), 0)
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': 'Invalid numeric values'}), 400

        if price is None or price.is_nan() or price < 0:
            return jsonify({'success': False, 'message': 'Price cannot be negative'}), 400
        if cost < 0 or tax_rate < 0:
            return jsonify({'success': False, 'message': 'Cost cannot be negative'}), 400

        reorder_enabled = to_bool(data.get('reorder_enabled'), True)
        
        branch_id = get_current_branch_id()

        # Check for duplicate barcode within same branch
        barcode = data.get('barcode')
        if barcode:
            existing = Product.query.filter_by(barcode=barcode, branch_id=branch_id).first()
            if existing:
                return jsonify({'success': False, 'message': 'Barcode already in use in this branch'}), 400

        photo_filename = None
        photo_file = request.files.get('photo') if is_multipart else None
        if photo_file and photo_file.filename:
            try:
                photo_filename = save_product_image(photo_file)
            except ProductImageTooLargeError as e:
                return jsonify({'success': False, 'message': str(e)}), 413
            except ValueError as e:
                return jsonify({'success': False, 'message': str(e)}), 400

        unit_id, unit_error = resolve_product_unit_id(data.get('unit_id'))
        if unit_error:
            return jsonify({'success': False, 'message': unit_error}), 400

        product = Product(
            barcode=barcode,
            name=name,
            price=price,
            cost=cost,
            stock=stock,
            category_id=int(data.get('category_id')) if data.get('category_id') else None,
            unit_id=unit_id,
            tax_rate=tax_rate,
            reorder_point=reorder_point,
            reorder_quantity=reorder_quantity,
            reorder_enabled=reorder_enabled,
            photo_filename=photo_filename,
            branch_id=branch_id
        )
        # Set legacy category field for backward compatibility
        if product.category_id:
            cat = db.session.get(Category, product.category_id)
            if cat:
                product.category = cat.name
        db.session.add(product)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Product added'}), 201

# --- Single Product Endpoint (GET, PUT, DELETE) ---
# --- Product deletion helpers ---
# Shown wherever a sale/return line points at a product that was deleted.
DELETED_PRODUCT_LABEL = 'Deleted product'


def get_product_dependency_summary(product):
    """List every tab/record type a product is wired to, with its delete policy.

    Groups are tagged with the action the cleanup window will take:
      * 'delete' — catalog records owned by this product; removed on confirm.
      * 'keep'   — history that stays readable: sale lines, deliveries and
                   return/exchange lines are kept and only unlinked from the
                   deleted product, so reports, receipts, refunds and
                   already-returned quantities all stay accurate.
      * 'block'  — nothing emits this today; the UI still supports it for a
                   reference that must be handled in its own tab first.
    """
    warehouse_items = WarehouseInventory.query.filter_by(product_id=product.id).all()
    warehouse_quantity = sum(int(item.quantity or 0) for item in warehouse_items)
    transfers = WarehouseTransfer.query.filter_by(product_id=product.id).count()
    promotions = Promotion.query.filter_by(product_id=product.id).count()
    agreements = SupplierPriceAgreement.query.filter_by(product_id=product.id).count()
    purchase_order_items = PurchaseOrderItem.query.filter_by(product_id=product.id).all()
    purchase_order_count = len({item.purchase_order_id for item in purchase_order_items})
    sales_lines = SaleItem.query.filter_by(product_id=product.id).count()
    returns = ReturnExchangeItem.query.filter_by(product_id=product.id).count()
    deliveries = (
        db.session.query(db.func.count(db.distinct(Delivery.id)))
        .select_from(Delivery)
        .join(Sale, Sale.id == Delivery.sale_id)
        .join(SaleItem, SaleItem.sale_id == Sale.id)
        .filter(SaleItem.product_id == product.id)
        .scalar()
    ) or 0

    groups = []

    def add_group(key, label, count, action, detail=None, **extra):
        if not count:
            return
        group = {'key': key, 'label': label, 'count': int(count), 'action': action}
        if detail:
            group['detail'] = detail
        group.update(extra)
        groups.append(group)

    add_group('warehouse_inventory', 'Warehouse stock', len(warehouse_items), 'delete',
              f'{warehouse_quantity} unit(s) in {len(warehouse_items)} batch(es) will be removed',
              quantity=warehouse_quantity)
    add_group('warehouse_transfers', 'Warehouse transfers', transfers, 'delete',
              'warehouse transfer history for this product will be removed')
    add_group('promotions', 'Promotions', promotions, 'delete',
              'promotion records for this product will be removed')
    add_group('supplier_price_agreements', 'Supplier price agreements', agreements,
              'delete', 'agreed supplier prices for this product will be removed')
    add_group('purchase_order_items', 'Purchase order lines', len(purchase_order_items),
              'delete',
              f'lines removed from {purchase_order_count} purchase order(s); their totals '
              'are recalculated',
              purchase_order_count=purchase_order_count)
    add_group('sales_history', 'Sales history lines', sales_lines, 'keep',
              'kept for reports and receipts; the lines are only unlinked from the product')
    add_group('deliveries', 'Deliveries on those sales', deliveries, 'keep',
              'delivery records belong to the sales and are kept')
    add_group('returns_exchanges', 'Return / exchange lines', returns, 'keep',
              'kept for refund history; the lines are only unlinked from the product')

    return {
        'groups': groups,
        'removable_count': sum(g['count'] for g in groups if g['action'] == 'delete'),
        'blocked_by': [g['key'] for g in groups if g['action'] == 'block'],
        'has_sales_history': sales_lines > 0,
        'sales_history_count': sales_lines,
        'returns_exchanges_count': returns,
    }


def remove_product_catalog_records(product):
    """Remove the catalog records wired to a product that is being deleted.

    Sales and return rows are never touched here: sale lines are only unlinked
    by the caller and return/exchange records block the delete entirely.
    Purchase orders stay, but the totals of the orders that lost a line are
    recalculated from their remaining lines exactly like order creation does.
    """
    removed = {
        'warehouse_inventory': WarehouseInventory.query.filter_by(
            product_id=product.id).delete(synchronize_session=False),
        'warehouse_transfers': WarehouseTransfer.query.filter_by(
            product_id=product.id).delete(synchronize_session=False),
        'promotions': Promotion.query.filter_by(
            product_id=product.id).delete(synchronize_session=False),
        'supplier_price_agreements': SupplierPriceAgreement.query.filter_by(
            product_id=product.id).delete(synchronize_session=False),
    }

    purchase_order_ids = [
        row[0] for row in db.session.query(PurchaseOrderItem.purchase_order_id)
        .filter(PurchaseOrderItem.product_id == product.id).distinct().all()
    ]
    removed['purchase_order_items'] = PurchaseOrderItem.query.filter_by(
        product_id=product.id).delete(synchronize_session=False)

    for purchase_order_id in purchase_order_ids:
        purchase_order = db.session.get(PurchaseOrder, purchase_order_id)
        if not purchase_order:
            continue
        remaining = db.session.query(
            PurchaseOrderItem.ordered_qty, PurchaseOrderItem.unit_cost
        ).filter(PurchaseOrderItem.purchase_order_id == purchase_order_id).all()
        purchase_order.total_amount = float(
            sum((quantity or 0) * (unit_cost or 0) for quantity, unit_cost in remaining)
        )
    removed['purchase_orders_recalculated'] = len(purchase_order_ids)
    return removed


@app.route('/api/products/<int:product_id>', methods=['GET', 'PUT', 'DELETE'])
def api_single_product(product_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    branch_id = get_requested_branch_id(default_to_current=True)
    if branch_id is None:
        branch_id = get_current_branch_id()
    product = Product.query.filter_by(id=product_id, branch_id=branch_id).first()
    if not product:
        return jsonify({'success': False, 'message': 'Product not found'}), 404

    if request.method == 'GET':
        return jsonify({
            'id': product.id,
            'barcode': product.barcode,
            'name': product.name,
            'price': product.price,
            'cost': product.cost,
            'stock': product.stock,
            'category': product.category_ref.name if product.category_ref else product.category,
            'category_id': product.category_id,
            'unit_id': product.unit_id,
            'unit_name': product.unit_ref.name if product.unit_ref else None,
            'unit_symbol': product.unit_ref.symbol if product.unit_ref else None,
            'tax_rate': product.tax_rate,
            'reorder_point': product.reorder_point,
            'reorder_quantity': product.reorder_quantity,
            'reorder_enabled': bool(product.reorder_enabled),
            'photo_filename': product.photo_filename,
            'photo_url': product_photo_url(product.photo_filename),
            'promotions': [{
                'id': p.id,
                'discount_type': p.discount_type,
                'discount_value': p.discount_value,
                'start_date': p.start_date.isoformat(),
                'end_date': p.end_date.isoformat()
            } for p in product.promotions]
        })

    elif request.method == 'PUT':
        is_multipart = request.content_type and 'multipart/form-data' in request.content_type.lower()
        data = request.form if is_multipart else (request.get_json() or {})
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400

        # Check for duplicate barcode within same branch
        if 'barcode' in data and data['barcode']:
            existing = Product.query.filter(
                Product.barcode == data['barcode'], 
                Product.id != product_id,
                Product.branch_id == branch_id
            ).first()
            if existing:
                return jsonify({'success': False, 'message': 'Barcode already in use'}), 400
            product.barcode = data['barcode']

        if 'name' in data:
            product.name = data.get('name', product.name)
        if 'price' in data:
            try:
                new_price = safe_to_decimal(data.get('price'), default=None)
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': 'Invalid price value'}), 400
            if new_price is None or new_price < 0:
                return jsonify({'success': False, 'message': 'Price cannot be negative'}), 400
            product.price = new_price
        if 'cost' in data:
            try:
                new_cost = safe_to_decimal(data.get('cost') or 0, default=Decimal('-1'))
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': 'Invalid cost value'}), 400
            if new_cost < 0:
                return jsonify({'success': False, 'message': 'Cost cannot be negative'}), 400
            product.cost = new_cost
        if 'stock' in data:
            try:
                product.stock = int(data.get('stock'))
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': 'Invalid stock value'}), 400

        if 'category_id' in data:
            category_id = data.get('category_id')
            if category_id:
                try:
                    product.category_id = int(category_id)
                    cat = db.session.get(Category, product.category_id)
                    if cat:
                        product.category = cat.name
                except (ValueError, TypeError):
                    return jsonify({'success': False, 'message': 'Invalid category_id value'}), 400
            else:
                product.category_id = None
                product.category = None
        elif 'category' in data:
            product.category = data.get('category', product.category)
        if 'unit_id' in data:
            unit_id, unit_error = resolve_product_unit_id(
                data.get('unit_id'), allow_inactive_id=product.unit_id
            )
            if unit_error:
                return jsonify({'success': False, 'message': unit_error}), 400
            product.unit_id = unit_id
        if 'tax_rate' in data:
            try:
                new_tax_rate = safe_to_decimal(data.get('tax_rate') or 0, default=Decimal('-1'))
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': 'Invalid tax rate value'}), 400
            if new_tax_rate < 0:
                return jsonify({'success': False, 'message': 'Invalid tax rate value'}), 400
            product.tax_rate = new_tax_rate
        if 'reorder_point' in data:
            try:
                product.reorder_point = max(int(data.get('reorder_point') or 0), 0)
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': 'Invalid reorder point value'}), 400
        if 'reorder_quantity' in data:
            try:
                product.reorder_quantity = max(int(data.get('reorder_quantity') or 0), 0)
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': 'Invalid reorder quantity value'}), 400
        if 'reorder_enabled' in data:
            product.reorder_enabled = to_bool(data.get('reorder_enabled'), True)

        remove_photo = str(data.get('remove_photo', '')).lower() in ('1', 'true', 'yes', 'on')
        if remove_photo and product.photo_filename:
            delete_product_image(product.photo_filename)
            product.photo_filename = None

        photo_file = request.files.get('photo') if is_multipart else None
        if photo_file and photo_file.filename:
            try:
                new_photo = save_product_image(photo_file)
                delete_product_image(product.photo_filename)
                product.photo_filename = new_photo
            except ProductImageTooLargeError as e:
                return jsonify({'success': False, 'message': str(e)}), 413
            except ValueError as e:
                return jsonify({'success': False, 'message': str(e)}), 400

        db.session.commit()
        return jsonify({'success': True, 'message': 'Product updated'})

    elif request.method == 'DELETE':
        # Deleting products is a manager (or owner) action: cashiers keep
        # selling and editing, but they can never remove catalog entries.
        if session.get('role') not in ('manager', 'boss'):
            return jsonify({
                'success': False,
                'message': 'Only a manager can delete products.'
            }), 403

        # A product that was already sold is only removed after the user
        # confirms the cleanup window, which retries with force=1 (keep the
        # sales history) and cascade=1 (also clear the other tabs it is used
        # in, for example warehouse, promotions and purchase order lines).
        force = to_bool(request.args.get('force'), False)
        cascade = to_bool(request.args.get('cascade'), False)
        if not (force and cascade):
            body = request.get_json(silent=True)
            if isinstance(body, dict):
                force = force or to_bool(body.get('force'), False)
                cascade = cascade or to_bool(body.get('cascade'), False)

        summary = get_product_dependency_summary(product)

        if summary['removable_count'] and not cascade:
            return jsonify({
                'success': False,
                'requires_cascade': True,
                'has_sales_history': summary['has_sales_history'],
                'sales_history_count': summary['sales_history_count'],
                'dependency_groups': summary['groups'],
                'dependencies': {
                    group['key']: group['count'] for group in summary['groups']
                },
                'message': (
                    f"Cannot delete product '{product.name}': it is still used in other "
                    "tabs. Confirm deleting it everywhere."
                )
            }), 400

        if summary['has_sales_history'] and not force:
            return jsonify({
                'success': False,
                'has_sales_history': True,
                'requires_confirmation': True,
                'sales_history_count': summary['sales_history_count'],
                'message': f"Cannot delete product '{product.name}': it has sales history."
            }), 400

        photo_filename = product.photo_filename
        cleaned_up = {}
        history_kept = {}
        try:
            if cascade:
                cleaned_up = remove_product_catalog_records(product)
            if summary['has_sales_history']:
                # Keep every sale row (quantity, price, tax and the sale total)
                # so sales history and reports stay accurate; only the link to
                # the catalog entry that is going away is cleared.
                SaleItem.query.filter_by(product_id=product.id).update(
                    {'product_id': None}, synchronize_session=False
                )
                history_kept['sales_history_lines'] = summary['sales_history_count']
            if summary['returns_exchanges_count']:
                # Keep the refund/exchange rows too: unlinking them leaves the
                # refund money, the settlement and the already-returned
                # quantities exactly as they were.
                ReturnExchangeItem.query.filter_by(product_id=product.id).update(
                    {'product_id': None}, synchronize_session=False
                )
                history_kept['returns_exchanges_lines'] = summary[
                    'returns_exchanges_count']
            db.session.delete(product)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify({
                'success': False,
                'message': 'Cannot delete product because it is referenced by existing records.'
            }), 400

        if photo_filename:
            delete_product_image(photo_filename)
        return jsonify({
            'success': True,
            'message': 'Product deleted',
            'cleaned_up': cleaned_up,
            'history_kept': history_kept
        })

@app.route('/api/products/<int:product_id>/dependencies', methods=['GET'])
def api_product_dependencies(product_id):
    """Report every tab a product is wired to before the manager deletes it."""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    if session.get('role') not in ('manager', 'boss'):
        return jsonify({
            'success': False,
            'message': 'Only a manager can delete products.'
        }), 403

    branch_id = get_requested_branch_id(default_to_current=True)
    if branch_id is None:
        branch_id = get_current_branch_id()
    product = Product.query.filter_by(id=product_id, branch_id=branch_id).first()
    if not product:
        return jsonify({'success': False, 'message': 'Product not found'}), 404

    summary = get_product_dependency_summary(product)
    return jsonify({
        'success': True,
        'product_id': product.id,
        'product_name': product.name,
        'can_delete': not summary['blocked_by'],
        **summary
    })


@app.route('/api/products/search', methods=['GET'])
def api_search_products():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    branch_id = get_requested_branch_id(default_to_current=True)
    if branch_id is None:
        branch_id = get_current_branch_id()

    products = Product.query.filter(
        (Product.name.ilike(f'%{query}%')) | 
        (Product.barcode.ilike(f'%{query}%')) |
        (Product.category.ilike(f'%{query}%'))
    ).filter(Product.branch_id == branch_id).limit(10).all()
    return jsonify([{
        'id': p.id,
        'barcode': p.barcode,
        'name': p.name,
        'price': p.price,
        'stock': p.stock,
        'tax_rate': p.tax_rate,
        'unit_symbol': p.unit_ref.symbol if p.unit_ref else None,
        'photo_url': product_photo_url(p.photo_filename)
    } for p in products])

# ==================== Barcode Label Printing (thermal 3-up roll) ====================
# Physical label geometry in millimetres for the shop's sticker roll:
# 32 x 19 mm labels, 3 columns, 3 mm gaps. One printed browser page is one
# row (page height = label height + gap = the roll pitch) so the feeder gap
# sensor lines up with every row, matching how the receipt/slip windows print.
LABEL_COLUMNS = 3
LABEL_WIDTH_MM = 32.0
LABEL_HEIGHT_MM = 19.0
LABEL_GAP_MM = 3.0
LABEL_MAX_PER_PRODUCT = 500
MM_TO_PT = 2.83465

# Settings-adjustable label geometry with sane physical bounds so a typo in
# Settings can never produce an unprintable sheet.
LABEL_DEFAULTS = {
    'label_width_mm': LABEL_WIDTH_MM,
    'label_height_mm': LABEL_HEIGHT_MM,
    'label_gap_mm': LABEL_GAP_MM,
    'label_columns': LABEL_COLUMNS,
}
LABEL_SETTING_LIMITS = {
    'label_width_mm': (10.0, 200.0),
    'label_height_mm': (5.0, 100.0),
    'label_gap_mm': (0.0, 20.0),
    'label_columns': (1, 10),
}


def normalize_label_setting(key, value):
    """Validate one label geometry value; return None when unusable."""
    if key not in LABEL_SETTING_LIMITS:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float('inf'), float('-inf')):
        return None
    low, high = LABEL_SETTING_LIMITS[key]
    if number < low or number > high:
        return None
    return int(number) if key == 'label_columns' else round(number, 1)


def get_label_geometry():
    """Effective label sheet geometry (stored settings with safe fallbacks)."""
    geometry = {}
    for key, fallback in LABEL_DEFAULTS.items():
        normalized = normalize_label_setting(key, get_setting(key, fallback))
        geometry[key] = fallback if normalized is None else normalized
    geometry['columns'] = geometry['label_columns']
    geometry['gap_mm'] = geometry['label_gap_mm']
    geometry['page_width_mm'] = (
        geometry['label_width_mm'] * geometry['label_columns']
        + geometry['label_gap_mm'] * (geometry['label_columns'] - 1)
    )
    geometry['row_pitch_mm'] = geometry['label_height_mm'] + geometry['label_gap_mm']
    return geometry


def build_label_barcode_svg(value, max_width_mm=29.0, height_mm=8.0):
    """Render a Code128 barcode as inline SVG sized to fit the label.

    The old PDF path used a fixed bar width, so longer barcodes overflowed the
    32 mm label. Here the natural width is measured at barWidth=1.0 and the
    bars are scaled down to fit. The SVG root is stamped with physical mm
    dimensions (reportlab emits unit-less point values, which browsers would
    otherwise read as px and print 25% too small); the viewBox keeps the
    drawing crisp at that size.
    """
    text = str(value or '').strip() or '0'
    max_width_pt = max_width_mm * MM_TO_PT
    bar_height_pt = height_mm * MM_TO_PT
    bar_width = 1.0
    drawing = createBarcodeDrawing(
        'Code128', value=text, barHeight=bar_height_pt, barWidth=bar_width,
    )
    # reportlab rounds each bar to whole render units, so one proportional
    # step can still overshoot; shrink against the measured width until the
    # barcode fits the label (a couple of iterations in practice).
    for _ in range(8):
        actual_width = float(drawing.width or 0)
        if actual_width <= 0 or actual_width <= max_width_pt:
            break
        bar_width = max(0.05, bar_width * (max_width_pt / actual_width) * 0.98)
        drawing = createBarcodeDrawing(
            'Code128', value=text, barHeight=bar_height_pt, barWidth=bar_width,
        )
    svg = renderSVG.drawToString(drawing)
    if isinstance(svg, bytes):
        svg = svg.decode('utf-8')
    start = svg.find('<svg')
    if start == -1:
        return ''
    svg = svg[start:]
    width_mm = float(drawing.width or 0) / MM_TO_PT
    height_mm_actual = float(drawing.height or 0) / MM_TO_PT
    svg = re.sub(r'(<svg[^>]*?)width="[^"]*"', rf'\1width="{width_mm:.2f}mm"', svg, count=1)
    svg = re.sub(r'(<svg[^>]*?)height="[^"]*"', rf'\1height="{height_mm_actual:.2f}mm"', svg, count=1)
    return svg


@app.route('/api/products/barcode_labels/print', methods=['POST'])
def print_barcode_labels():
    """Label sheet as a print-ready window page (32x19mm labels, 3 per row)."""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    product_ids = []
    for chunk in str(request.form.get('product_ids') or '').split(','):
        chunk = chunk.strip()
        if chunk.isdigit():
            product_ids.append(int(chunk))
    if not product_ids:
        return jsonify({'success': False, 'message': 'No products selected'}), 400

    try:
        quantities = json.loads(request.form.get('quantities') or '{}')
        if not isinstance(quantities, dict):
            quantities = {}
    except (TypeError, ValueError, json.JSONDecodeError):
        quantities = {}

    products = Product.query.filter(Product.id.in_(product_ids)).all()
    if not products:
        return jsonify({'success': False, 'message': 'No products found'}), 404
    products_by_id = {product.id: product for product in products}

    labels = []
    geometry = get_label_geometry()
    # Barcode footprint scales with the configured label so content always fits.
    svg_max_width = max(5.0, geometry['label_width_mm'] - 3.0)
    svg_height = max(4.0, min(12.0, geometry['label_height_mm'] * 0.45))
    for product_id in product_ids:  # keep the order the user selected
        product = products_by_id.get(product_id)
        if not product:
            continue
        try:
            qty = int(quantities.get(str(product_id), 1))
        except (TypeError, ValueError):
            qty = 1
        qty = max(1, min(qty, LABEL_MAX_PER_PRODUCT))
        label = {
            'svg': build_label_barcode_svg(
                product.barcode or str(product.id),
                max_width_mm=svg_max_width,
                height_mm=svg_height,
            ),
            'name': product.name or f'Product #{product.id}',
            'price_display': format_currency(product.price),
        }
        for _ in range(qty):
            labels.append(label)

    columns = geometry['label_columns']
    rows = []
    for index in range(0, len(labels), columns):
        chunk = labels[index:index + columns]
        rows.append({'labels': chunk, 'fillers': range(columns - len(chunk))})

    response = make_response(render_template(
        'barcode_labels.html',
        rows=rows,
        geometry=geometry,
        label_count=len(labels),
        auto_print=request.args.get('autoprint') == '1',
    ))
    response.headers['Cache-Control'] = 'private, no-store, max-age=0'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.route('/api/products/barcode_labels', methods=['POST'])
def generate_barcode_labels():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    if not data or 'product_ids' not in data:
        return jsonify({'success': False, 'message': 'Missing product IDs'}), 400

    try:
        product_ids = data.get('product_ids') or []
        quantities = data.get('quantities') or {}

        products = Product.query.filter(Product.id.in_(product_ids)).all()
        if not products:
            return jsonify({'success': False, 'message': 'No products found'}), 404

        buffer = io.BytesIO()

        # Label dimensions (points, 1mm = 2.83465 points)
        label_width = 32 * 2.83465   # 32mm
        label_height = 19 * 2.83465  # 19mm
        horizontal_gap = 3 * 2.83465
        vertical_gap = 3 * 2.83465

        page_width = (label_width * 3) + (horizontal_gap * 2)
        page_height = (label_height * 10) + (vertical_gap * 9)

        doc = SimpleDocTemplate(
            buffer,
            pagesize=(page_width, page_height),
            rightMargin=0, leftMargin=0,
            topMargin=0, bottomMargin=0
        )

        elements = []
        styles = getSampleStyleSheet()
        normal_style = styles["Normal"]
        normal_style.fontSize = 8
        normal_style.alignment = 1  # center

        label_list = []

        # Add products based on explicitly requested label quantities
        for product in products:
            raw_qty = quantities.get(str(product.id), 1)
            try:
                qty = int(raw_qty)
            except (TypeError, ValueError):
                qty = 1
            qty = max(1, qty)
            for _ in range(qty):
                label_list.append(product)

        # ✅ Build rows of 3 labels
        for i in range(0, len(label_list), 3):
            row_products = label_list[i:i + 3]
            row_data = []

            for product in row_products:
                # Create barcode drawing
                barcode_value = product.barcode if product.barcode else str(product.id)
                barcode_drawing = createBarcodeDrawing(
                    "Code128",
                    value=barcode_value,
                    barHeight=12,
                    barWidth=0.8
                )

                # Create text flowables
                product_name = Paragraph(product.name, normal_style)
                price = Paragraph(format_currency(product.price), normal_style)

                # Stack vertically
                label_table = Table(
                    [[barcode_drawing],
                     [product_name],
                     [price]],
                    colWidths=[label_width],
                    rowHeights=[label_height * 0.55,
                                label_height * 0.2,
                                label_height * 0.25],
                    style=TableStyle([
                        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                        ('LEFTPADDING', (0, 0), (-1, -1), 0),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                        ('TOPPADDING', (0, 0), (-1, -1), 0),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                    ])
                )

                row_data.append(label_table)

            # Fill empty cells
            while len(row_data) < 3:
                row_data.append(Spacer(label_width, label_height))

            # Add row of labels
            t = Table(
                [row_data],
                colWidths=[label_width] * 3,
                rowHeights=[label_height],
                style=TableStyle([
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 0),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                    ('TOPPADDING', (0, 0), (-1, -1), 0),
                ])
            )

            elements.append(t)
            if i + 3 < len(label_list):
                elements.append(Spacer(1, vertical_gap))

        # Build PDF
        doc.build(elements)
        buffer.seek(0)

        # ✅ Inline view only (no forced download)
        response = make_response(buffer.getvalue())
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = 'inline; filename=barcode_labels.pdf'
        return response

    except Exception as e:
        app.logger.error(f"Error generating barcode labels: {str(e)}")
        return jsonify({'success': False, 'message': f'Error generating labels: {str(e)}'}), 500

@app.route('/api/sales', methods=['POST'])
def api_create_sale():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    if not data or 'items' not in data:
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400

    # SQLite can raise "database is locked" under concurrent POS writes; retry the
    # whole transaction (stock checks + guarded decrements) up to 3 times before failing.
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            return _create_sale_transaction(data)
        except OperationalError as exc:
            db.session.rollback()
            if 'database is locked' in str(exc).lower() and attempt < max_attempts - 1:
                time.sleep(0.2 * (attempt + 1))
                app.logger.warning(f"Sale transaction hit DB lock (attempt {attempt + 1}); retrying...")
                continue
            app.logger.error(f"Error creating sale: {str(exc)}")
            return jsonify({'success': False, 'message': f'Error creating sale: {str(exc)}'}), 500
        except IntegrityError:
            # Two concurrent requests raced on the same client transaction_id:
            # both passed the "does it exist" check, then one hit the UNIQUE
            # constraint on insert. The winner may not have committed yet, so
            # re-run the whole transaction — the pre-check will find the sale
            # and reply as an idempotent replay instead of an error.
            db.session.rollback()
            client_txn_id = str(data.get('transaction_id') or '').strip()
            if not client_txn_id:
                app.logger.error("IntegrityError creating sale")
                return jsonify({'success': False, 'message': 'Error creating sale'}), 500
            existing = Sale.query.filter_by(
                transaction_id=client_txn_id,
                branch_id=get_current_branch_id(),
            ).first()
            if existing:
                return _sale_replay_response(existing)
            if attempt < max_attempts - 1:
                time.sleep(0.2)
                continue
            app.logger.error("IntegrityError creating sale; replay not found after retries")
            return jsonify({'success': False, 'message': 'Error creating sale'}), 500

    return jsonify({'success': False, 'message': 'Error creating sale'}), 500


def _sale_replay_response(existing):
    """Idempotent replay response for a sale that already exists."""
    response = {
        'success': True,
        'message': 'Sale already synced',
        'transaction_id': existing.transaction_id,
        'total': money_float(existing.total),
        'refund_amount': money_float(existing.refund_amount),
        'payment_method': existing.payment_method,
        'payment_breakdown': get_sale_payment_breakdown(existing),
        'duplicate': True,
    }
    existing_date = getattr(existing, 'date', None)
    if existing_date is not None:
        response['created_at'] = existing_date.isoformat()
    return jsonify(response), 200


def _create_sale_transaction(data):
    """Run a single sale transaction. Raises OperationalError on DB lock so the caller can retry."""
    try:
        # Idempotency guard for offline sales: if the client already sent this
        # transaction_id (e.g. a retried sync after a lost response), return the
        # original sale instead of creating a duplicate.
        client_txn_id = str(data.get('transaction_id') or '').strip()
        if client_txn_id:
            existing = Sale.query.filter_by(transaction_id=client_txn_id, branch_id=get_current_branch_id()).first()
            if existing:
                return _sale_replay_response(existing)

        # Calculate totals
        subtotal = Decimal('0.00')
        tax_total = Decimal('0.00')
        items = []

        for item in data['items']:
            product = db.session.get(Product, item['product_id'])
            if not product:
                return jsonify({'success': False, 'message': f'Product {item["product_id"]} not found'}), 404

            quantity = int(item.get('quantity', 0))
            if quantity <= 0:
                return jsonify({'success': False, 'message': 'Quantity must be greater than 0'}), 400

            if quantity > product.stock:
                return jsonify({'success': False, 'message': f'Insufficient stock for {product.name}. Available: {product.stock}'}), 400

            price = to_decimal(item.get('price', 0))
            if price < 0:
                return jsonify({'success': False, 'message': 'Price cannot be negative'}), 400

            # Server-side promotion validation: a price below the product's normal
            # price is only accepted when it matches a currently-active promotion
            # (within 0.01 tolerance). Normal-price sales are always accepted.
            normal_price = to_decimal(product.price or 0)
            if price < normal_price:
                promo_prices = active_promotion_prices(product)
                if not any(abs(price - promo_price) <= Decimal('0.01') for promo_price in promo_prices):
                    return jsonify({'success': False, 'message': 'Invalid price or expired promotion'}), 400

            item_total = price * quantity
            item_tax = (item_total * to_decimal(product.tax_rate or 0) / Decimal('100')).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
            
            subtotal += item_total
            tax_total += item_tax
            
            items.append({
                'product': product,
                'price': round_money(price),
                'quantity': quantity,
                'tax': item_tax
            })

        total = subtotal + tax_total
        total_rounded = round_money(total)

        payment_method = str(data.get('payment_method', 'cash') or 'cash').strip().lower()
        allowed_payment_methods = {'cash', 'credit_card', 'debit_card', 'mobile_payment', 'debt', 'split_payment'}
        if payment_method not in allowed_payment_methods:
            return jsonify({'success': False, 'message': 'Invalid payment method'}), 400
        payment_breakdown = None
        cash_received_raw = data.get('cash_received')
        cash_received = None
        refund_amount = 0.0

        if payment_method == 'split_payment':
            if data.get('customer_id'):
                return jsonify({'success': False, 'message': 'Split payment cannot be charged to a customer account'}), 400
            try:
                payment_breakdown = normalize_payment_breakdown(data.get('payment_breakdown'), total_rounded)
            except (TypeError, ValueError, ArithmeticError) as error:
                return jsonify({'success': False, 'message': str(error)}), 400
            cash_amount = payment_breakdown.get('cash', Decimal('0.00'))
            if cash_amount:
                cash_received = cash_amount
            payment_breakdown = {method: float(amount) for method, amount in payment_breakdown.items()}
        elif payment_method == 'cash':
            if cash_received_raw in (None, ''):
                return jsonify({'success': False, 'message': 'Cash received is required for cash payment'}), 400
            try:
                cash_received_decimal = to_decimal(cash_received_raw)
            except Exception:
                return jsonify({'success': False, 'message': 'Invalid cash received amount'}), 400

            if cash_received_decimal < 0:
                return jsonify({'success': False, 'message': 'Cash received cannot be negative'}), 400

            cash_received = round_money(cash_received_decimal)
            refund_decimal = (cash_received_decimal - to_decimal(total_rounded)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
            if refund_decimal < 0:
                return jsonify({'success': False, 'message': 'Cash received is less than total amount'}), 400
            refund_amount = round_money(refund_decimal)
        
        myanmar_tz = pytz.timezone('Asia/Yangon')
        sale_time = datetime.now(myanmar_tz)

        # Create sale record
        sale = Sale(
            transaction_id=client_txn_id or str(uuid.uuid4()),
            date=sale_time,
            total=total_rounded,
            tax=round_money(tax_total),
            cash_received=cash_received,
            refund_amount=refund_amount,
            payment_method=payment_method,
            payment_breakdown=json.dumps(payment_breakdown, ensure_ascii=False) if payment_breakdown else None,
            user_id=session['user_id'],
            branch_id=get_current_branch_id()
        )
        db.session.add(sale)
        db.session.flush()  # To get the sale.id before commit

        # Create sale items
        for item in items:
            sale_item = SaleItem(
                sale_id=sale.id,
                product_id=item['product'].id,
                quantity=item['quantity'],
                price=item['price'],
                tax=round_money(item['tax'])
            )
            db.session.add(sale_item)
            # Update product stock atomically: only decrement when sufficient stock remains.
            # This prevents overselling when two concurrent sales race past the app-level check.
            stock_result = db.session.execute(
                text('UPDATE product SET stock = stock - :qty WHERE id = :pid AND stock >= :qty'),
                {'qty': item['quantity'], 'pid': item['product'].id}
            )
            if stock_result.rowcount == 0:
                db.session.rollback()
                return jsonify({'success': False, 'message': f'Insufficient stock for {item["product"].name}. Available: {item["product"].stock}'}), 400
            previous_stock = int(item['product'].stock or 0)
            record_audit_event(
                category='Inventory', action='update', entity_type='Product',
                entity_id=item['product'].id, entity_label=item['product'].name,
                branch_id=sale.branch_id,
                changes={'stock': {
                    'before': previous_stock,
                    'after': previous_stock - item['quantity'],
                }},
                summary=f"Sale {sale.transaction_id}: reduced {item['product'].name} stock by {item['quantity']}",
            )

        # Handle debt transactions if customer_id is provided
        if 'customer_id' in data and data['customer_id']:
            customer_id = data['customer_id']
            customer = db.session.get(Customer, customer_id)
            if not customer:
                return jsonify({'success': False, 'message': 'Customer not found'}), 404
            
            # Create a debt record for this sale
            debt = Debt(
                customer_id=customer_id,
                sale_id=sale.id,
                amount=round_money(total),
                balance=round_money(total),
                notes=f'Sale transaction {sale.transaction_id}',
                branch_id=sale.branch_id
            )
            db.session.add(debt)
            
            # Update sale payment method to indicate debt
            sale.payment_method = 'debt'

        delivery_payload = data.get('delivery') or {}
        if delivery_payload.get('enabled'):
            recipient_name = (delivery_payload.get('recipient_name') or '').strip()
            recipient_phone = (delivery_payload.get('recipient_phone') or '').strip()
            delivery_address = (delivery_payload.get('delivery_address') or '').strip()
            if not recipient_name or not recipient_phone or not delivery_address:
                return jsonify({'success': False, 'message': 'Recipient name, phone and address are required for delivery'}), 400

            delivery = Delivery(
                delivery_number=generate_delivery_number(),
                sale_id=sale.id,
                customer_id=data.get('customer_id'),
                stage='to_deliver',
                priority=normalize_delivery_priority(delivery_payload.get('priority')),
                recipient_name=recipient_name,
                recipient_phone=recipient_phone,
                delivery_address=delivery_address,
                township=(delivery_payload.get('township') or '').strip() or None,
                instructions=(delivery_payload.get('instructions') or '').strip() or None,
                courier_name=(delivery_payload.get('courier_name') or '').strip() or None,
                courier_phone=(delivery_payload.get('courier_phone') or '').strip() or None,
                tracking_code=(delivery_payload.get('tracking_code') or '').strip() or None,
                delivery_fee=round_money(delivery_payload.get('delivery_fee') or 0),
                scheduled_at=parse_iso_datetime(delivery_payload.get('scheduled_at')),
                created_by=session.get('user_id'),
                branch_id=sale.branch_id
            )
            db.session.add(delivery)

        receipt_branch = db.session.get(Branch, sale.branch_id) if sale.branch_id else None
        sale.receipt_snapshot = json.dumps(
            build_receipt_snapshot(
                transaction_id=sale.transaction_id,
                sale_date=sale.date,
                pos_name='Parrot POS',
                currency_code=get_currency_code(),
                currency_suffix=get_currency_suffix(),
                branch={
                    'name': receipt_branch.name if receipt_branch else '',
                    'code': receipt_branch.code if receipt_branch else '',
                    'address': receipt_branch.address if receipt_branch else '',
                    'phone': receipt_branch.phone if receipt_branch else '',
                    'email': receipt_branch.email if receipt_branch else ''
                },
                cashier_name=session.get('username', 'Unknown'),
                payment_method=sale.payment_method,
                cash_received=sale.cash_received,
                change_given=sale.refund_amount,
                payment_breakdown=get_sale_payment_breakdown(sale),
                items=[{
                    'product_id': item['product'].id,
                    'name': item['product'].name,
                    'quantity': item['quantity'],
                    'unit_price': item['price'],
                    'tax_rate': item['product'].tax_rate or 0,
                    'tax_amount': item['tax']
                } for item in items],
                subtotal=subtotal,
                tax=tax_total,
                total=total_rounded,
                receipt_identity=get_receipt_identity(receipt_branch)
            ),
            ensure_ascii=False,
            default=json_default
        )

        db.session.commit()

        return jsonify({
            'success': True,
            'message': 'Sale completed',
            'transaction_id': sale.transaction_id,
            'delivery_number': sale.delivery.delivery_number if getattr(sale, 'delivery', None) else None
        }), 201

    except OperationalError:
        db.session.rollback()
        raise
    except IntegrityError:
        # Let api_create_sale handle the concurrent-replay race (idempotent
        # replay lookup + retry) instead of turning it into a generic 500.
        db.session.rollback()
        raise
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error creating sale: {str(e)}")
        return jsonify({'success': False, 'message': f'Error creating sale: {str(e)}'}), 500

# --- Get Sales History (GET /api/sales) ---
@app.route('/api/sales', methods=['GET'])
def api_sales():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    # Sales list honors the same scope rule as /api/reports/sales (resolve_report_scope):
    # managers/bosses with scope=all see every branch; everyone else stays on their branch.
    scope, branch_id = resolve_report_scope()

    # Get query parameters
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    q = (request.args.get('q') or '').strip()
    start_date = request.args.get('start')
    end_date = request.args.get('end')

    # Base query filtered by branch (None branch_id = all branches for manager scope=all)
    query = Sale.query
    if branch_id:
        query = query.filter_by(branch_id=branch_id)
    
    # Apply date filters
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            query = query.filter(Sale.date >= start_dt)
        except ValueError:
            pass
    
    if end_date:
        try:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')
            query = query.filter(Sale.date <= end_dt.replace(hour=23, minute=59, second=59))
        except ValueError:
            pass
    
    # Apply search filter
    if q:
        like_q = f'%{q}%'
        query = query.outerjoin(User, User.id == Sale.user_id).filter(
            (Sale.transaction_id.ilike(like_q)) |
            (Sale.payment_method.ilike(like_q)) |
            (User.username.ilike(like_q))
        )
    
    # Cashiers can only see their own sales
    if session.get('role') == 'cashier':
        query = query.filter(Sale.user_id == session['user_id'])
    
    # Order by date descending
    query = query.order_by(Sale.date.desc())
    
    # Paginate results
    safe_per_page = max(1, min(per_page, 100))
    pagination = query.paginate(page=page, per_page=safe_per_page, error_out=False)
    
    return jsonify({
        'items': [{
            'transaction_id': s.transaction_id,
            'date': s.date.isoformat() if s.date else None,
            'total': s.total,
            'tax': s.tax,
            'payment_method': s.payment_method,
            'payment_breakdown': get_sale_payment_breakdown(s),
            'user_id': s.user_id,
            'username': s.user.username if s.user else 'Unknown',
            'branch_id': s.branch_id
        } for s in pagination.items],
        'page': pagination.page,
        'per_page': safe_per_page,
        'total': pagination.total,
        'total_pages': pagination.pages
    })

# --- Get Single Sale with Items ---
@app.route('/api/sales/<string:transaction_id>', methods=['GET'])
def api_single_sale(transaction_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    branch_id = get_current_branch_id()
    sale = Sale.query.filter_by(transaction_id=transaction_id, branch_id=branch_id).first()
    if not sale:
        return jsonify({'success': False, 'message': 'Sale not found'}), 404

    items = SaleItem.query.filter_by(sale_id=sale.id).all()
    returned_qty_map = get_returned_quantity_map_for_sale(sale.id)
    return_exchange_history = ReturnExchange.query.filter_by(original_sale_id=sale.id).order_by(ReturnExchange.created_at.desc()).all()
    sale_data = {
        'transaction_id': sale.transaction_id,
        'date': sale.date.isoformat(),
        'total': sale.total,
        'tax': sale.tax,
        'cash_received': sale.cash_received,
        'refund_amount': sale.refund_amount or 0,
        'payment_method': sale.payment_method,
        'payment_breakdown': get_sale_payment_breakdown(sale),
        'user_id' : sale.user_id,
        'username' : sale.user.username if sale.user else 'Unknown',
        'delivery': serialize_delivery(sale.delivery) if getattr(sale, 'delivery', None) else None,
        'items': [],
        'return_exchange_history': [{
            'workflow_id': r.workflow_id,
            'mode': r.mode,
            'created_at': r.created_at.isoformat() if r.created_at else None,
            'return_total': r.return_total,
            'exchange_total': r.exchange_total,
            'net_total': r.net_total,
            'refund_amount': r.refund_amount,
            'collected_amount': r.collected_amount,
            'settlement_method': r.settlement_method
        } for r in return_exchange_history]
    }
    for item in items:
        product = db.session.get(Product, item.product_id) if item.product_id else None
        already_returned = returned_qty_map.get(item.id, 0)
        available_to_return = max(item.quantity - already_returned, 0)
        if product is None:
            # A sold product may be deleted while its sales history is kept;
            # the line keeps its money values but can no longer be returned
            # because there is no catalog entry left to restock.
            available_to_return = 0
        sale_data['items'].append({
            'sale_item_id': item.id,
            'product_id': item.product_id,
            'name': product.name if product else DELETED_PRODUCT_LABEL,
            'price': item.price,
            'quantity': item.quantity,
            'tax': item.tax,
            'tax_rate': product.tax_rate if product else 0.0,
            'already_returned_quantity': already_returned,
            'available_return_quantity': available_to_return
        })
    return jsonify(sale_data)

@app.route('/api/returns_exchanges', methods=['GET', 'POST'])
def api_returns_exchanges():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        query = ReturnExchange.query
        sale_transaction_id = (request.args.get('sale_transaction_id') or '').strip()

        if sale_transaction_id:
            sale = Sale.query.filter_by(transaction_id=sale_transaction_id).first()
            if not sale:
                return jsonify([])
            query = query.filter(ReturnExchange.original_sale_id == sale.id)

        records = query.order_by(ReturnExchange.created_at.desc()).all()
        return jsonify([{
            'workflow_id': r.workflow_id,
            'mode': r.mode,
            'original_transaction_id': r.original_sale.transaction_id if r.original_sale else None,
            'adjustment_transaction_id': r.adjustment_sale.transaction_id if r.adjustment_sale else None,
            'return_total': r.return_total,
            'exchange_total': r.exchange_total,
            'net_total': r.net_total,
            'refund_amount': r.refund_amount,
            'collected_amount': r.collected_amount,
            'settlement_method': r.settlement_method,
            'created_at': r.created_at.isoformat() if r.created_at else None,
            'processed_by': r.user.username if r.user else 'Unknown'
        } for r in records])

    data = request.get_json() or {}
    original_transaction_id = (data.get('original_transaction_id') or '').strip()
    if not original_transaction_id:
        return jsonify({'success': False, 'message': 'Original transaction ID is required'}), 400

    original_sale = Sale.query.filter_by(transaction_id=original_transaction_id).first()
    if not original_sale:
        return jsonify({'success': False, 'message': 'Original sale not found'}), 404

    original_sale_items = SaleItem.query.filter_by(sale_id=original_sale.id).all()
    sale_item_map = {item.id: item for item in original_sale_items}
    returned_qty_map = get_returned_quantity_map_for_sale(original_sale.id)

    return_items_payload = data.get('return_items') or []
    exchange_items_payload = data.get('exchange_items') or []

    if not return_items_payload:
        return jsonify({'success': False, 'message': 'At least one return item is required'}), 400

    return_lines = []
    return_total = Decimal('0.00')
    return_tax_total = Decimal('0.00')

    try:
        for row in return_items_payload:
            sale_item_id = int(row.get('sale_item_id', 0) or 0)
            quantity = int(row.get('quantity', 0) or 0)
            if sale_item_id <= 0 or quantity <= 0:
                return jsonify({'success': False, 'message': 'Invalid return item values'}), 400

            sale_item = sale_item_map.get(sale_item_id)
            if not sale_item:
                return jsonify({'success': False, 'message': f'Return item {sale_item_id} not found in original sale'}), 400

            already_returned = returned_qty_map.get(sale_item_id, 0)
            available_qty = max(int(sale_item.quantity) - already_returned, 0)
            if quantity > available_qty:
                return jsonify({'success': False, 'message': f'Return qty exceeds available qty for item #{sale_item_id}. Available: {available_qty}'}), 400

            product = db.session.get(Product, sale_item.product_id)
            if not product:
                return jsonify({'success': False, 'message': 'Product not found for return item'}), 404

            unit_price = to_decimal(sale_item.price)
            unit_tax = calculate_sale_item_unit_tax(sale_item)
            line_total = (unit_price * Decimal(quantity)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
            line_tax = (unit_tax * Decimal(quantity)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)

            return_total += line_total + line_tax
            return_tax_total += line_tax
            return_lines.append({
                'sale_item': sale_item,
                'product': product,
                'quantity': quantity,
                'unit_price': unit_price,
                'tax_rate': float(product.tax_rate or 0),
                'line_total': line_total,
                'line_tax': line_tax
            })
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Invalid return item values'}), 400

    exchange_lines = []
    exchange_total = Decimal('0.00')
    exchange_tax_total = Decimal('0.00')

    try:
        for row in exchange_items_payload:
            product_id = int(row.get('product_id', 0) or 0)
            quantity = int(row.get('quantity', 0) or 0)
            if product_id <= 0 or quantity <= 0:
                return jsonify({'success': False, 'message': 'Invalid exchange item values'}), 400

            product = db.session.get(Product, product_id)
            if not product:
                return jsonify({'success': False, 'message': f'Exchange product {product_id} not found'}), 404

            if quantity > int(product.stock or 0):
                return jsonify({'success': False, 'message': f'Insufficient stock for exchange product {product.name}. Available: {product.stock}'}), 400

            raw_price = row.get('price', product.price)
            unit_price = to_decimal(raw_price)
            if unit_price < 0:
                return jsonify({'success': False, 'message': 'Exchange item price cannot be negative'}), 400

            line_total = (unit_price * Decimal(quantity)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
            line_tax = (line_total * to_decimal(product.tax_rate or 0) / Decimal('100')).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)

            exchange_total += line_total + line_tax
            exchange_tax_total += line_tax
            exchange_lines.append({
                'product': product,
                'quantity': quantity,
                'unit_price': unit_price,
                'tax_rate': float(product.tax_rate or 0),
                'line_total': line_total,
                'line_tax': line_tax
            })
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Invalid exchange item values'}), 400

    mode = 'exchange' if exchange_lines else 'return'
    net_total = (exchange_total - return_total).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    refund_amount = abs(net_total) if net_total < 0 else Decimal('0.00')
    collected_amount = net_total if net_total > 0 else Decimal('0.00')
    settlement_method = (data.get('settlement_method') or 'cash').strip().lower()

    try:
        adjustment_sale = None
        if exchange_lines:
            myanmar_tz = pytz.timezone('Asia/Yangon')
            adjustment_sale = Sale(
                transaction_id=str(uuid.uuid4()),
                date=datetime.now(myanmar_tz),
                total=round_money(exchange_total),
                tax=round_money(exchange_tax_total),
                cash_received=round_money(collected_amount) if collected_amount > 0 else None,
                refund_amount=0.0,
                payment_method='exchange',
                user_id=session['user_id']
            )
            db.session.add(adjustment_sale)
            db.session.flush()

            for line in exchange_lines:
                sale_item = SaleItem(
                    sale_id=adjustment_sale.id,
                    product_id=line['product'].id,
                    quantity=line['quantity'],
                    price=round_money(line['unit_price']),
                    tax=round_money(line['line_tax'])
                )
                db.session.add(sale_item)
                line['product'].stock -= line['quantity']

        workflow = ReturnExchange(
            workflow_id=str(uuid.uuid4()),
            mode=mode,
            original_sale_id=original_sale.id,
            adjustment_sale_id=adjustment_sale.id if adjustment_sale else None,
            return_total=round_money(return_total),
            exchange_total=round_money(exchange_total),
            net_total=round_money(net_total),
            refund_amount=round_money(refund_amount),
            collected_amount=round_money(collected_amount),
            settlement_method=settlement_method,
            notes=(data.get('notes') or '').strip() or None,
            user_id=session['user_id']
        )
        db.session.add(workflow)
        db.session.flush()

        for line in return_lines:
            line['product'].stock += line['quantity']
            item = ReturnExchangeItem(
                return_exchange_id=workflow.id,
                original_sale_item_id=line['sale_item'].id,
                product_id=line['product'].id,
                movement='return',
                quantity=line['quantity'],
                unit_price=round_money(line['unit_price']),
                tax_rate=line['tax_rate'],
                line_total=round_money(line['line_total']),
                line_tax=round_money(line['line_tax'])
            )
            db.session.add(item)

        for line in exchange_lines:
            item = ReturnExchangeItem(
                return_exchange_id=workflow.id,
                original_sale_item_id=None,
                product_id=line['product'].id,
                movement='exchange',
                quantity=line['quantity'],
                unit_price=round_money(line['unit_price']),
                tax_rate=line['tax_rate'],
                line_total=round_money(line['line_total']),
                line_tax=round_money(line['line_tax'])
            )
            db.session.add(item)

        db.session.commit()
        return jsonify({
            'success': True,
            'message': 'Return/exchange processed successfully',
            'workflow_id': workflow.workflow_id,
            'mode': workflow.mode,
            'original_transaction_id': original_transaction_id,
            'adjustment_transaction_id': adjustment_sale.transaction_id if adjustment_sale else None,
            'return_total': workflow.return_total,
            'exchange_total': workflow.exchange_total,
            'net_total': workflow.net_total,
            'refund_amount': workflow.refund_amount,
            'collected_amount': workflow.collected_amount
        }), 201
    except Exception as e:
        db.session.rollback()
        # Expire all in-memory ORM instances (including product.stock values mutated
        # before the failed commit) so they are reloaded from the DB on next access
        # instead of serving stale in-memory state.
        db.session.expire_all()
        app.logger.error(f"Error processing return/exchange: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to process return/exchange'}), 500

@app.route('/api/returns_exchanges/<string:workflow_id>', methods=['GET'])
def api_single_return_exchange(workflow_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    workflow = ReturnExchange.query.filter_by(workflow_id=workflow_id).first()
    if not workflow:
        return jsonify({'success': False, 'message': 'Return/exchange workflow not found'}), 404

    return jsonify({
        'workflow_id': workflow.workflow_id,
        'mode': workflow.mode,
        'original_transaction_id': workflow.original_sale.transaction_id if workflow.original_sale else None,
        'adjustment_transaction_id': workflow.adjustment_sale.transaction_id if workflow.adjustment_sale else None,
        'return_total': workflow.return_total,
        'exchange_total': workflow.exchange_total,
        'net_total': workflow.net_total,
        'refund_amount': workflow.refund_amount,
        'collected_amount': workflow.collected_amount,
        'settlement_method': workflow.settlement_method,
        'notes': workflow.notes,
        'created_at': workflow.created_at.isoformat() if workflow.created_at else None,
        'processed_by': workflow.user.username if workflow.user else 'Unknown',
        'items': [{
            'id': item.id,
            'movement': item.movement,
            'product_id': item.product_id,
            'product_name': item.product.name if item.product else DELETED_PRODUCT_LABEL,
            'quantity': item.quantity,
            'unit_price': item.unit_price,
            'tax_rate': item.tax_rate,
            'line_total': item.line_total,
            'line_tax': item.line_tax,
            'original_sale_item_id': item.original_sale_item_id
        } for item in workflow.items]
    })

@app.route('/api/deliveries', methods=['GET', 'POST'])
def api_deliveries():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    branch_id = get_default_branch_id()

    if request.method == 'GET':
        query = Delivery.query.filter_by(branch_id=branch_id)
        stage = normalize_delivery_stage(request.args.get('stage'))
        priority = normalize_delivery_priority(request.args.get('priority') or 'normal') if request.args.get('priority') else None
        q = (request.args.get('q') or '').strip().lower()

        if stage:
            query = query.filter(Delivery.stage == stage)
        if priority:
            query = query.filter(Delivery.priority == priority)

        deliveries = query.order_by(Delivery.created_at.desc()).all()
        if q:
            deliveries = [
                d for d in deliveries
                if q in (d.delivery_number or '').lower()
                or q in (d.sale.transaction_id if d.sale else '').lower()
                or q in (d.recipient_name or '').lower()
                or q in (d.recipient_phone or '').lower()
                or q in (d.delivery_address or '').lower()
                or q in (d.tracking_code or '').lower()
            ]

        return jsonify([serialize_delivery(d) for d in deliveries])

    if session.get('role') != 'manager':
        return jsonify({'success': False, 'message': 'Manager access required'}), 403

    data = request.get_json() or {}
    sale_transaction_id = (data.get('sale_transaction_id') or '').strip()
    if not sale_transaction_id:
        return jsonify({'success': False, 'message': 'Sale transaction ID is required'}), 400

    sale = Sale.query.filter_by(transaction_id=sale_transaction_id).first()
    if not sale:
        return jsonify({'success': False, 'message': 'Sale not found'}), 404
    if getattr(sale, 'delivery', None):
        return jsonify({'success': False, 'message': 'Delivery already exists for this sale'}), 400

    recipient_name = (data.get('recipient_name') or '').strip()
    recipient_phone = (data.get('recipient_phone') or '').strip()
    delivery_address = (data.get('delivery_address') or '').strip()
    if not recipient_name or not recipient_phone or not delivery_address:
        return jsonify({'success': False, 'message': 'Recipient name, phone and address are required'}), 400

    delivery = Delivery(
        delivery_number=generate_delivery_number(),
        sale_id=sale.id,
        customer_id=data.get('customer_id') or None,
        stage='to_deliver',
        priority=normalize_delivery_priority(data.get('priority')),
        recipient_name=recipient_name,
        recipient_phone=recipient_phone,
        delivery_address=delivery_address,
        township=(data.get('township') or '').strip() or None,
        instructions=(data.get('instructions') or '').strip() or None,
        courier_name=(data.get('courier_name') or '').strip() or None,
        courier_phone=(data.get('courier_phone') or '').strip() or None,
        tracking_code=(data.get('tracking_code') or '').strip() or None,
        delivery_fee=round_money(data.get('delivery_fee') or 0),
        scheduled_at=parse_iso_datetime(data.get('scheduled_at')),
        created_by=session.get('user_id'),
        branch_id=branch_id
    )
    db.session.add(delivery)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Delivery created', 'delivery': serialize_delivery(delivery)}), 201

@app.route('/api/deliveries/<int:delivery_id>', methods=['GET', 'PUT'])
def api_single_delivery(delivery_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    delivery = Delivery.query.filter_by(id=delivery_id, branch_id=get_default_branch_id()).first()
    if not delivery:
        return jsonify({'success': False, 'message': 'Delivery not found'}), 404

    if request.method == 'GET':
        return jsonify(serialize_delivery(delivery))

    data = request.get_json() or {}

    next_stage = normalize_delivery_stage(data.get('stage')) if 'stage' in data else None
    if next_stage and next_stage != delivery.stage:
        if not can_transition_delivery_stage(delivery.stage, next_stage):
            return jsonify({'success': False, 'message': f'Invalid stage transition: {delivery.stage} -> {next_stage}'}), 400
        delivery.stage = next_stage
        apply_delivery_stage_timestamp(delivery, next_stage)
        if next_stage == 'cancelled':
            delivery.cancelled_at = datetime.utcnow()

    if 'priority' in data:
        delivery.priority = normalize_delivery_priority(data.get('priority'))

    editable_fields = [
        'recipient_name', 'recipient_phone', 'delivery_address', 'township', 'instructions',
        'courier_name', 'courier_phone', 'tracking_code', 'proof_note'
    ]
    for field in editable_fields:
        if field in data:
            setattr(delivery, field, (data.get(field) or '').strip() or None)

    if 'delivery_fee' in data:
        delivery.delivery_fee = round_money(data.get('delivery_fee') or 0)
    if 'scheduled_at' in data:
        delivery.scheduled_at = parse_iso_datetime(data.get('scheduled_at'))

    db.session.commit()
    return jsonify({'success': True, 'message': 'Delivery updated', 'delivery': serialize_delivery(delivery)})

@app.route('/api/deliveries/stats', methods=['GET'])
def api_delivery_stats():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    branch_id = get_default_branch_id()
    deliveries = Delivery.query.filter_by(branch_id=branch_id).all()
    stage_counts = {key: 0 for key in DELIVERY_STAGE_FLOW.keys()}
    for d in deliveries:
        if d.stage in stage_counts:
            stage_counts[d.stage] += 1

    return jsonify({
        'total': len(deliveries),
        'by_stage': stage_counts,
        'high_priority_open': sum(1 for d in deliveries if d.priority in ('high', 'urgent') and d.stage not in ('delivered', 'cancelled')),
        'ready_dispatch': stage_counts.get('packaged', 0)
    })

# --- Delivery Slip (driver copy) ---
@app.route('/api/deliveries/<int:delivery_id>/print', methods=['GET'])
def print_delivery_slip(delivery_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    delivery = Delivery.query.filter_by(id=delivery_id, branch_id=get_default_branch_id()).first()
    if not delivery:
        return jsonify({'success': False, 'message': 'Delivery not found'}), 404

    sale = delivery.sale
    packing_items = []
    order_total = Decimal('0.00')
    sale_transaction_id = ''
    payment_method = ''
    if sale:
        sale_transaction_id = sale.transaction_id or ''
        payment_method = sale.payment_method or ''
        order_total = safe_to_decimal(sale.total)
        for sale_item in SaleItem.query.filter_by(sale_id=sale.id).all():
            # Skip malformed rows so bad legacy data can never break the slip.
            quantity = int(sale_item.quantity or 0)
            if quantity <= 0:
                continue
            product = db.session.get(Product, sale_item.product_id)
            packing_items.append({
                'name': product.name if product else f'Unavailable item #{sale_item.product_id}',
                'quantity': quantity,
            })

    delivery_fee = safe_to_decimal(delivery.delivery_fee)
    branch = db.session.get(Branch, delivery.branch_id) if delivery.branch_id else None

    slip_view = build_delivery_slip_view({
        'currency_suffix': get_currency_suffix(),
        'branch': {
            'name': branch.name if branch else '',
            'code': branch.code if branch else '',
            'address': branch.address if branch else '',
            'phone': branch.phone if branch else '',
            'email': branch.email if branch else ''
        },
        'receipt_identity': get_receipt_identity(branch),
        'delivery_number': delivery.delivery_number,
        'stage_label': DELIVERY_STAGE_LABELS.get(delivery.stage, delivery.stage),
        'priority': delivery.priority,
        'created_at': delivery.created_at.isoformat() if delivery.created_at else '',
        'sale_transaction_id': sale_transaction_id,
        'payment_method': payment_method,
        'recipient_name': delivery.recipient_name,
        'recipient_phone': delivery.recipient_phone,
        'delivery_address': delivery.delivery_address,
        'township': delivery.township,
        'instructions': delivery.instructions,
        'courier_name': delivery.courier_name,
        'courier_phone': delivery.courier_phone,
        'tracking_code': delivery.tracking_code,
        'items': packing_items,
        'order_total': order_total,
        'delivery_fee': delivery_fee,
        'collect_total': order_total + delivery_fee,
    }, get_receipt_paper_size())
    slip_view['logo_url'] = receipt_logo_url(slip_view.get('logo_filename'))

    response = make_response(render_template('delivery_slip.html', slip=slip_view))
    response.headers['Cache-Control'] = 'private, no-store, max-age=0'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

# --- Delivery Performance Reporting ---
@app.route('/delivery-report')
def delivery_report_page():
    """Standalone window page for delivery performance reporting.

    Opened from the Deliveries tab's Reporting button; data is fetched
    client-side from /api/deliveries/report. Exports stay manager/boss only,
    mirroring the /api/deliveries/export guard.
    """
    if 'user_id' not in session:
        return redirect(url_for('login'))
    response = make_response(render_template(
        'delivery_report.html',
        can_export=session.get('role') in ('manager', 'boss'),
    ))
    response.headers['Cache-Control'] = 'private, no-store, max-age=0'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def parse_delivery_report_date(value, end_of_day=False):
    """Parse a YYYY-MM-DD filter into a UTC datetime bound (None when blank/bad).

    ``end_of_day`` shifts to the following midnight so an inclusive date picked
    in the UI becomes an exclusive upper bound for the query.
    """
    text = str(value or '').strip()
    if not text:
        return None
    try:
        day = datetime.strptime(text, '%Y-%m-%d')
    except ValueError:
        return None
    return day + timedelta(days=1) if end_of_day else day


def delivery_report_records(date_from=None, date_to=None, branch_id=None):
    """Serialized deliveries for the performance report and its exports.

    Shared by /api/deliveries/report and /api/deliveries/export so a downloaded
    report always matches the on-screen analysis (same contract as the
    Warehouse and Purchase tabs).
    """
    if branch_id is None:
        branch_id = get_default_branch_id()
    query = Delivery.query.filter_by(branch_id=branch_id)
    start = parse_delivery_report_date(date_from)
    end = parse_delivery_report_date(date_to, end_of_day=True)
    if start:
        query = query.filter(Delivery.created_at >= start)
    if end:
        query = query.filter(Delivery.created_at < end)
    deliveries = query.order_by(Delivery.created_at.desc()).all()
    return [serialize_delivery(delivery) for delivery in deliveries]


@app.route('/api/deliveries/report', methods=['GET'])
def api_delivery_report():
    """Delivery performance KPIs, courier stats and the open-delivery watchlist."""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    if (date_from and parse_delivery_report_date(date_from) is None) or \
            (date_to and parse_delivery_report_date(date_to) is None):
        return jsonify({'success': False, 'message': 'Invalid date filter; use YYYY-MM-DD'}), 400

    records = delivery_report_records(date_from, date_to)
    rows = build_delivery_performance_rows(records)

    by_stage = {key: 0 for key in DELIVERY_STAGE_FLOW.keys()}
    for row in rows:
        if row['stage'] in by_stage:
            by_stage[row['stage']] += 1

    open_rows = sorted(
        (row for row in rows if row['stage'] not in ('delivered', 'cancelled')),
        key=lambda row: row['age_hours'] or 0,
        reverse=True,
    )[:50]
    attention = [{
        'id': row['id'],
        'delivery_number': row['delivery_number'],
        'recipient_name': row['recipient_name'],
        'recipient_phone': row['recipient_phone'],
        'township': row['township'],
        'stage': row['stage'],
        'stage_label': row['stage_label'],
        'priority': row['priority'],
        'courier_name': row['courier_name'],
        'age_hours': row['age_hours'],
        'timing_flag': row['timing_flag'],
    } for row in open_rows]

    return jsonify({
        'total': len(rows),
        'kpis': summarize_delivery_performance(rows),
        'by_stage': by_stage,
        'courier_performance': delivery_courier_performance(rows),
        'attention': attention,
    })


@app.route('/api/deliveries/export', methods=['GET'])
@manager_or_boss_required
def export_deliveries():
    """Download the delivery register as a professional PDF or Excel report."""
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    report_format = normalize_report_format(request.args.get('format'))
    branch_id = get_default_branch_id()
    branch = db.session.get(Branch, branch_id) if branch_id else None

    records = delivery_report_records(date_from, date_to, branch_id)
    rows = build_delivery_performance_rows(records)
    report = build_delivery_performance_report(
        rows,
        brand=get_receipt_identity(branch),
        branch_name=branch.name if branch else '',
        generated_by=session.get('username') or '',
        filters_text=describe_filters({'From': date_from, 'To': date_to}),
        currency_suffix=get_currency_suffix(),
    )

    payload = build_report_pdf(report) if report_format == 'pdf' else build_report_xlsx(report)
    filename = report_filename(report['file_stem'], report_format)
    response = make_response(payload)
    response.headers['Content-Type'] = report_content_type(report_format)
    response.headers['Content-Disposition'] = report_disposition(filename, report_format)
    return response

# --- Thermal Receipt ---
@app.route('/api/sales/<string:transaction_id>/print', methods=['GET'])
def print_receipt(transaction_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    branch_id = get_current_branch_id()
    sale = Sale.query.filter_by(transaction_id=transaction_id, branch_id=branch_id).first()
    if not sale:
        return jsonify({'success': False, 'message': 'Sale not found'}), 404

    snapshot = None
    if sale.receipt_snapshot:
        try:
            snapshot = json.loads(sale.receipt_snapshot)
        except (TypeError, ValueError, json.JSONDecodeError):
            app.logger.warning('Invalid receipt snapshot for sale %s; using legacy data', sale.id)

    if not snapshot:
        sale_items = SaleItem.query.filter_by(sale_id=sale.id).all()
        branch = db.session.get(Branch, sale.branch_id) if sale.branch_id else None
        legacy_items = []
        subtotal = Decimal('0.00')
        for sale_item in sale_items:
            product = db.session.get(Product, sale_item.product_id)
            unit_price = safe_to_decimal(sale_item.price)
            quantity = safe_to_decimal(sale_item.quantity)
            line_subtotal = unit_price * quantity
            # Skip rows with missing/zero/negative price or quantity so malformed
            # legacy data can never poison the receipt or divide by zero.
            if line_subtotal <= 0:
                continue
            subtotal += line_subtotal
            tax_amount = safe_to_decimal(sale_item.tax)
            tax_rate = (tax_amount / line_subtotal * Decimal('100')) if line_subtotal else Decimal('0')
            legacy_items.append({
                'product_id': sale_item.product_id,
                'name': product.name if product else f'Unavailable item #{sale_item.product_id}',
                'quantity': int(sale_item.quantity or 0),
                'unit_price': float(unit_price),
                'tax_rate': tax_rate,
                'tax_amount': float(tax_amount)
            })

        snapshot = build_receipt_snapshot(
            transaction_id=sale.transaction_id,
            sale_date=sale.date,
            pos_name='Parrot POS',
            currency_code=get_currency_code(),
            currency_suffix=get_currency_suffix(),
            branch={
                'name': branch.name if branch else '',
                'code': branch.code if branch else '',
                'address': branch.address if branch else '',
                'phone': branch.phone if branch else '',
                'email': branch.email if branch else ''
            },
            cashier_name=sale.user.username if sale.user else 'Unknown',
            payment_method=sale.payment_method,
            cash_received=sale.cash_received,
            change_given=sale.refund_amount,
            payment_breakdown=get_sale_payment_breakdown(sale),
            items=legacy_items,
            subtotal=subtotal,
            tax=sale.tax,
            total=sale.total,
            receipt_identity=get_receipt_identity(branch)
        )

    receipt_view = build_receipt_view(snapshot, get_receipt_paper_size())
    receipt_view['logo_url'] = receipt_logo_url(receipt_view.get('logo_filename'))
    response = make_response(render_template('receipt.html', receipt=receipt_view))
    response.headers['Cache-Control'] = 'private, no-store, max-age=0'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

# --- Excel Export ---
@app.route('/api/reports/sales/export', methods=['GET'])
@manager_or_boss_required
def export_sales_report():
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    _, branch_id = resolve_report_scope()
    
    query = Sale.query
    if branch_id:
        query = query.filter_by(branch_id=branch_id)
    try:
        if start_date:
            start_date_obj = datetime.strptime(start_date, '%Y-%m-%d')
            query = query.filter(Sale.date >= start_date_obj)
        if end_date:
            end_date_obj = datetime.strptime(end_date, '%Y-%m-%d')
            query = query.filter(Sale.date <= end_date_obj.replace(hour=23, minute=59, second=59))
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid date format'}), 400

    sales = query.order_by(Sale.date).all()
    data = []
    for sale in sales:
        data.append({
            'Transaction ID': sale.transaction_id,
            'Date': sale.date.strftime('%Y-%m-%d %H:%M:%S'),
            'Total': money_float(sale.total),
            'Tax': money_float(sale.tax),
            'Cash Received': money_float(sale.cash_received),
            'Refund Given': money_float(sale.refund_amount or 0),
            'Payment Method': sale.payment_method,
            'Payment Breakdown': json.dumps(get_sale_payment_breakdown(sale), ensure_ascii=False) if get_sale_payment_breakdown(sale) else '',
            'User ID': sale.user_id
        })
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, sheet_name='Sales Report', index=False)
        for column in df:
            column_width = max(df[column].astype(str).map(len).max(), len(column))
            col_idx = df.columns.get_loc(column)
            writer.sheets['Sales Report'].set_column(col_idx, col_idx, column_width)
    output.seek(0)
    filename = f"sales_report_{start_date or 'all'}_to_{end_date or 'all'}.xlsx"
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response

@app.route('/api/reports/sales', methods=['GET'])
def api_report_sales():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    start_date = request.args.get('start')  # Format: 'YYYY-MM-DD'
    end_date = request.args.get('end')      # Format: 'YYYY-MM-DD'
    scope, branch_id = resolve_report_scope()

    query = Sale.query
    if branch_id:
        query = query.filter_by(branch_id=branch_id)
    myanmar_tz = pytz.timezone('Asia/Yangon')
    q = (request.args.get('q') or '').strip()
    page = request.args.get('page', type=int)
    per_page = request.args.get('per_page', type=int)

    try:
        # Apply date filters if provided
        if start_date:
            start_date_obj = datetime.strptime(start_date, '%Y-%m-%d')
            start_date_obj = myanmar_tz.localize(start_date_obj)
            query = query.filter(Sale.date >= start_date_obj)

        if end_date:
            end_date_obj = datetime.strptime(end_date, '%Y-%m-%d')
            end_date_obj = myanmar_tz.localize(end_date_obj).replace(hour=23, minute=59, second=59)
            query = query.filter(Sale.date <= end_date_obj)

        # Cashiers can only see their own sales
        if session.get('role') == 'cashier':
            query = query.filter(Sale.user_id == session['user_id'])

        if q:
            like_q = f'%{q}%'
            query = query.outerjoin(User, User.id == Sale.user_id).filter(
                (Sale.transaction_id.ilike(like_q)) |
                (Sale.payment_method.ilike(like_q)) |
                (User.username.ilike(like_q))
            )

        query = query.order_by(Sale.date.desc())

        def serialize_sale_row(s):
            return {
                'id': s.id,
                'transaction_id': s.transaction_id,
                'date': s.date.isoformat(),
                'total': money_float(s.total),
                'tax': money_float(s.tax),
                'cash_received': money_float(s.cash_received),
                'refund_amount': money_float(s.refund_amount or 0),
                'payment_method': s.payment_method,
                'payment_breakdown': get_sale_payment_breakdown(s),
                'user_id': s.user_id,
                'username': s.user.username if s.user else 'Unknown',
                'has_delivery': hasattr(s, 'delivery') and s.delivery is not None,
                'branch_id': s.branch_id,
                'report_scope': scope
            }

        if page and per_page:
            safe_per_page = max(1, min(per_page, 100))
            pagination = query.paginate(page=page, per_page=safe_per_page, error_out=False)
            return jsonify({
                'items': [serialize_sale_row(s) for s in pagination.items],
                'page': pagination.page,
                'per_page': safe_per_page,
                'total': pagination.total,
                'total_pages': pagination.pages
            })

        sales = query.all()
        return jsonify([serialize_sale_row(s) for s in sales])

    except ValueError as e:
        app.logger.error(f"Date parsing error: {str(e)}")
        return jsonify({'success': False, 'message': 'Invalid date format. Use YYYY-MM-DD.'}), 400

@app.route('/api/dashboard/sales_data')
def api_dashboard_sales_data():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    branch_id = get_current_branch_id()

    # Get sales for the last 7 days
    end_date = datetime.now(pytz.timezone('Asia/Yangon'))
    start_date = end_date - timedelta(days=7)
    
    sales = Sale.query.filter(
        Sale.branch_id == branch_id,
        Sale.date >= start_date,
        Sale.date <= end_date
    ).order_by(Sale.date).all()

    # Group sales by day
    sales_by_day = {}
    for sale in sales:
        sale_date = sale.date.strftime('%Y-%m-%d')
        if sale_date not in sales_by_day:
            sales_by_day[sale_date] = Decimal('0')
        sales_by_day[sale_date] += safe_to_decimal(sale.total)

    # Fill in missing days with 0
    result = []
    current_date = start_date
    while current_date <= end_date:
        date_str = current_date.strftime('%Y-%m-%d')
        result.append({
            'date': date_str,
            'total': money_float(sales_by_day.get(date_str, Decimal('0')))
        })
        current_date += timedelta(days=1)

    return jsonify(result)

@app.route('/api/dashboard/top_products', methods=['GET'])
def api_dashboard_top_products():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    branch_id = get_current_branch_id()

    rows = (
        db.session.query(
            Product.id,
            Product.name,
            Product.price,
            Product.stock,
            func.sum(SaleItem.quantity).label('units_sold'),
            func.sum(SaleItem.price * SaleItem.quantity).label('sales_amount')
        )
        .join(SaleItem, SaleItem.product_id == Product.id)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .filter(Sale.branch_id == branch_id)
        .group_by(Product.id)
        .order_by(func.sum(SaleItem.quantity).desc())
        .limit(5)
        .all()
    )

    return jsonify([
        {
            'id': row.id,
            'name': row.name,
            'price': row.price,
            'stock': row.stock,
            'units_sold': int(row.units_sold or 0),
            'sales_amount': money_float(row.sales_amount or 0)
        }
        for row in rows
    ])

# Account-creation barrier API: unlocks the ability to add users for this
# session after the vendor master credential, a numeric captcha and the rate
# limiter all pass.
@app.route('/api/account_barrier/status', methods=['GET'])
@manager_required
def api_account_barrier_status():
    return jsonify({
        'success': True,
        'configured': _account_barrier_configured(),
        'unlocked': _account_barrier_is_unlocked(),
        'retry_after': _barrier_retry_after(_barrier_client_key()),
        'max_attempts': _BARRIER_MAX_ATTEMPTS,
    })


@app.route('/api/account_barrier/challenge', methods=['GET'])
@manager_required
def api_account_barrier_challenge():
    if not _account_barrier_configured():
        return jsonify({
            'success': False,
            'configured': False,
            'message': 'Account creation is not available on this deployment.',
        }), 403
    retry_after = _barrier_retry_after(_barrier_client_key())
    if retry_after:
        return jsonify({
            'success': False,
            'retry_after': retry_after,
            'message': f'Too many attempts. Try again in {retry_after} seconds.',
        }), 429
    token, question = _barrier_issue_captcha()
    session['account_barrier_captcha_token'] = token
    return jsonify({
        'success': True,
        'configured': True,
        'unlocked': _account_barrier_is_unlocked(),
        'question': question,
    })


@app.route('/api/account_barrier/unlock', methods=['POST'])
@manager_required
def api_account_barrier_unlock():
    if not _account_barrier_configured():
        return jsonify({
            'success': False,
            'configured': False,
            'message': 'Account creation is not available on this deployment.',
        }), 403

    client_key = _barrier_client_key()
    retry_after = _barrier_retry_after(client_key)
    if retry_after:
        return jsonify({
            'success': False,
            'retry_after': retry_after,
            'message': f'Too many attempts. Try again in {retry_after} seconds.',
        }), 429

    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    captcha_answer = data.get('captcha_answer')
    captcha_token = session.pop('account_barrier_captcha_token', None)

    # The captcha is single-use and checked first, so a guessed credential cannot
    # be replayed and every attempt costs a fresh human-solvable sum.
    if not _barrier_consume_captcha(captcha_token, captcha_answer):
        _barrier_record_failure(client_key)
        return jsonify({
            'success': False,
            'refresh_captcha': True,
            'message': 'Incorrect captcha answer. Please solve the new sum.',
        }), 400

    if not _verify_account_barrier(username, password):
        _barrier_record_failure(client_key)
        return jsonify({
            'success': False,
            'refresh_captcha': True,
            'message': 'Incorrect master username or password.',
        }), 400

    _barrier_clear_failures(client_key)
    unlock_account_barrier_session()
    record_audit_event(
        category='System', action='update', entity_type='User',
        entity_label='Account creation barrier',
        summary='Account-creation barrier unlocked for the current session.',
    )
    db.session.commit()
    return jsonify({'success': True, 'message': 'Unlocked. You can now add users.'})


# User API Endpoints
@app.route('/api/users', methods=['GET'])
@manager_required
def api_users():
    q = (request.args.get('q') or '').strip()
    page = request.args.get('page', type=int)
    per_page = request.args.get('per_page', type=int)

    query = User.query
    if q:
        like_q = f'%{q}%'
        query = query.filter(
            (User.username.ilike(like_q)) |
            (User.role.ilike(like_q))
        )

    query = query.order_by(User.id.asc())

    def serialize_user_row(u):
        return {
            'id': u.id,
            'username': u.username,
            'role': u.role
        }

    if page and per_page:
        safe_per_page = max(1, min(per_page, 100))
        pagination = query.paginate(page=page, per_page=safe_per_page, error_out=False)
        return jsonify({
            'items': [serialize_user_row(u) for u in pagination.items],
            'page': pagination.page,
            'per_page': safe_per_page,
            'total': pagination.total,
            'total_pages': pagination.pages
        })

    users = query.all()
    return jsonify([serialize_user_row(u) for u in users])

@app.route('/api/users', methods=['POST'])
@manager_required
def api_create_user():
    # Creating accounts stays locked until the vendor master credential unlocks
    # it for this session, so a customer cannot mint their own users.
    if not _account_barrier_is_unlocked():
        return jsonify({
            'success': False,
            'code': 'account_barrier_locked',
            'message': 'Adding users is locked. Unlock the account-creation '
                       'barrier with the master credential first.',
        }), 403

    data = request.get_json()
    if not data or not all(k in data for k in ['username', 'password', 'role']):
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400
        
    if User.query.filter_by(username=data['username']).first():
        return jsonify({'success': False, 'message': 'Username already exists'}), 400
        
    user = User(
        username=data['username'],
        password=generate_password_hash(data['password']),
        role=data['role']
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({'success': True, 'message': 'User created'}), 201

@app.route('/api/users/<int:user_id>', methods=['GET', 'PUT', 'DELETE'])
@manager_required
def api_single_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'success': False, 'message': 'User not found'}), 404
        
    if request.method == 'GET':
        return jsonify({
            'id': user.id,
            'username': user.username,
            'role': user.role
        })
        
    elif request.method == 'PUT':
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400

        requested_role = data.get('role', user.role)
        if requested_role not in {'cashier', 'manager', 'boss'}:
            return jsonify({'success': False, 'message': 'Invalid user role'}), 400

        # Changing an existing account's role can grant (or remove) manager
        # access. Treat it with the same vendor-only barrier used for creating
        # accounts; this is enforced here so neither the dashboard nor another
        # client (including the AI agent) can bypass it.
        if requested_role != user.role and not _account_barrier_is_unlocked():
            return jsonify({
                'success': False,
                'code': 'account_barrier_locked',
                'message': 'Changing user roles is locked. Unlock the account-'
                           'creation barrier with the master credential first.',
            }), 403
            
        # Check if username already exists (excluding current user)
        if 'username' in data and data['username'] != user.username:
            existing_user = User.query.filter_by(username=data['username']).first()
            if existing_user and existing_user.id != user.id:
                return jsonify({'success': False, 'message': 'Username already exists'}), 400
                
        user.username = data.get('username', user.username)
        user.role = requested_role
        
        # Update password if provided
        if 'password' in data and data['password']:
            user.password = generate_password_hash(data['password'])
            
        db.session.commit()
        return jsonify({'success': True, 'message': 'User updated'})
        
    elif request.method == 'DELETE':
        # Prevent deleting yourself
        if user.id == session['user_id']:
            return jsonify({'success': False, 'message': 'Cannot delete your own account'}), 400
            
        db.session.delete(user)
        db.session.commit()
        return jsonify({'success': True, 'message': 'User deleted'})


def filtered_audit_log_query(args):
    """Build the one filter contract shared by the list and TXT export."""
    query = AuditLog.query

    raw_branch_id = (args.get('branch_id') or '').strip()
    branch_id = args.get('branch_id', type=int)
    if raw_branch_id and branch_id is None:
        raise ValueError('Invalid branch filter')
    if branch_id:
        query = query.filter(AuditLog.branch_id == branch_id)

    category = (args.get('category') or '').strip()
    if category:
        query = query.filter(AuditLog.category == category)

    action = (args.get('action') or '').strip().lower()
    if action and action not in ('create', 'update', 'delete'):
        raise ValueError('Invalid action filter')
    if action in ('create', 'update', 'delete'):
        query = query.filter(AuditLog.action == action)

    actor = (args.get('actor') or '').strip()
    if actor:
        query = query.filter(AuditLog.actor_username.ilike(f'%{actor}%'))

    search = (args.get('q') or '').strip()
    if search:
        like_search = f'%{search}%'
        query = query.filter(or_(
            AuditLog.summary.ilike(like_search),
            AuditLog.entity_type.ilike(like_search),
            AuditLog.entity_id.ilike(like_search),
            AuditLog.entity_label.ilike(like_search),
            AuditLog.request_path.ilike(like_search),
        ))

    raw_start = (args.get('start') or '').strip()
    raw_end = (args.get('end') or '').strip()
    start_bounds = audit_day_utc_bounds(raw_start)
    if raw_start and not start_bounds:
        raise ValueError('Invalid start date; expected YYYY-MM-DD')
    if start_bounds:
        query = query.filter(AuditLog.created_at >= start_bounds[0])
    end_bounds = audit_day_utc_bounds(raw_end)
    if raw_end and not end_bounds:
        raise ValueError('Invalid end date; expected YYYY-MM-DD')
    if start_bounds and end_bounds and start_bounds[0] >= end_bounds[1]:
        raise ValueError('Start date must not be after end date')
    if end_bounds:
        query = query.filter(AuditLog.created_at < end_bounds[1])
    return query


def serialize_audit_log(row, branch_names=None):
    changes = decode_audit_changes(row)
    local_time = audit_local_datetime(row.created_at)
    branch_names = branch_names or {}
    return {
        'id': row.id,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'local_created_at': local_time.isoformat() if local_time else None,
        'local_date': local_time.strftime('%Y-%m-%d') if local_time else None,
        'local_date_label': local_time.strftime('%A, %d %B %Y') if local_time else 'Unknown date',
        'local_time': local_time.strftime('%H:%M:%S') if local_time else None,
        'timezone': 'Asia/Yangon',
        'actor_user_id': row.actor_user_id,
        'actor_username': row.actor_username,
        'actor_role': row.actor_role,
        'branch_id': row.branch_id,
        'branch_name': row.branch_name or branch_names.get(row.branch_id),
        'category': row.category,
        'action': row.action,
        'entity_type': row.entity_type,
        'entity_id': row.entity_id,
        'entity_label': row.entity_label,
        'summary': row.summary,
        'changes': changes,
        'request_method': row.request_method,
        'request_path': row.request_path,
        'ip_address': row.ip_address,
    }


@app.route('/api/logs', methods=['GET'])
@manager_or_boss_required
def api_audit_logs():
    """Return the immutable audit trail with server-side filters/pagination."""
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    # Fixed small pages keep response memory and DOM work predictable. Remaining
    # rows stay write-only in SQLite until the operator chooses another page.
    per_page = AUDIT_PAGE_SIZE
    try:
        query = filtered_audit_log_query(request.args)
    except ValueError as error:
        return jsonify({'success': False, 'message': str(error)}), 400

    today_bounds = audit_day_utc_bounds(datetime.now(AUDIT_TIMEZONE).strftime('%Y-%m-%d'))
    filtered_total = query.count()
    filtered_today = query.filter(
        AuditLog.created_at >= today_bounds[0],
        AuditLog.created_at < today_bounds[1],
    ).count()
    pagination = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).paginate(
        page=page, per_page=per_page, error_out=False)

    branch_ids = {row.branch_id for row in pagination.items if row.branch_id}
    branch_names = {
        branch.id: branch.name
        for branch in Branch.query.filter(Branch.id.in_(branch_ids)).all()
    } if branch_ids else {}

    response = jsonify({
        'items': [serialize_audit_log(row, branch_names) for row in pagination.items],
        'page': pagination.page,
        'per_page': per_page,
        'total': pagination.total,
        'total_pages': pagination.pages,
        'summary': {
            'matching': filtered_total,
            'today': filtered_today,
            'actors': query.with_entities(AuditLog.actor_username).distinct().count(),
        },
        'categories': sorted(set(AUDIT_CATEGORY_BY_ENTITY.values()) | {'System'}),
    })
    response.headers['Cache-Control'] = 'private, no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Vary'] = 'Cookie'
    return response


def audit_text_line(label, value):
    value = value if value not in (None, '') else '—'
    return f'{label}: {audit_text_value(value)}\n'


def audit_text_value(value):
    """Keep one logical value on one line so text cannot forge event headings."""
    return ''.join(
        character if character >= ' ' and character != '\x7f' else ' '
        for character in str(value).replace('\r', ' ').replace('\n', ' ')
    ).strip()


@app.route('/api/logs/export.txt', methods=['GET'])
@manager_or_boss_required
def export_audit_logs_text():
    """Download every filtered audit event, grouped by Asia/Yangon day."""
    try:
        query = filtered_audit_log_query(request.args)
    except ValueError as error:
        return jsonify({'success': False, 'message': str(error)}), 400
    export_limit = AUDIT_EXPORT_LIMIT
    matching_count = query.count()
    if matching_count > export_limit:
        return jsonify({
            'success': False,
            'message': f'{matching_count} events match. Narrow the date or other filters to {export_limit} events or fewer.'
        }), 413
    query = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    branch_names = {branch.id: branch.name for branch in Branch.query.all()}
    generated_at = datetime.now(AUDIT_TIMEZONE)

    def generate():
        # UTF-8 BOM makes Myanmar text open correctly in Windows Notepad/Excel.
        yield '\ufeffPARROT POS — SYSTEM AUDIT LOGS\n'
        yield f'Generated: {generated_at.strftime("%Y-%m-%d %H:%M:%S")} Asia/Yangon\n'
        yield 'Filters: ' + ', '.join(
            f'{audit_text_value(key)}={audit_text_value(value)}' for key, value in request.args.items()
            if key in {'q', 'category', 'action', 'actor', 'branch_id', 'start', 'end'}
        ) + '\n'
        yield '=' * 78 + '\n'
        current_date = None
        found = False
        for row in query.yield_per(500):
            found = True
            item = serialize_audit_log(row, branch_names)
            if item['local_date'] != current_date:
                current_date = item['local_date']
                yield f'\n## {audit_text_value(item["local_date_label"])} ({audit_text_value(current_date)})\n'
                yield '-' * 78 + '\n'
            yield f'\n[{audit_text_value(item["local_time"])}] {audit_text_value(item["action"].upper())} · {audit_text_value(item["category"])}\n'
            yield audit_text_line('Event', item['summary'])
            yield audit_text_line('Performed by', f'{item["actor_username"]} ({item["actor_role"] or "system"})')
            yield audit_text_line('Branch', item['branch_name'] or (f'Branch #{item["branch_id"]}' if item['branch_id'] else 'System-wide'))
            record = item['entity_type'] + (f' #{item["entity_id"]}' if item['entity_id'] else '')
            yield audit_text_line('Record', record)
            yield audit_text_line('Request', f'{item["request_method"] or "—"} {item["request_path"] or "—"}')
            yield audit_text_line('IP address', item['ip_address'])
            yield 'Changes:\n' + json.dumps(item['changes'], ensure_ascii=False, indent=2) + '\n'
        if not found:
            yield '\nNo audit events matched the selected filters.\n'

    filename = f'parrot_pos_logs_{generated_at.strftime("%Y%m%d_%H%M%S")}.txt'
    return Response(
        stream_with_context(generate()),
        content_type='text/plain; charset=utf-8',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Cache-Control': 'private, no-store, max-age=0',
            'Pragma': 'no-cache',
            'Vary': 'Cookie',
        },
    )

# --- PROMOTIONS API ---
@app.route('/api/promotions', methods=['GET', 'POST'])
@manager_required
def api_promotions():
    myanmar_tz = pytz.timezone('Asia/Yangon')
    
    if request.method == 'GET':
        promotions = Promotion.query.all()
        now = datetime.now(myanmar_tz)

        return jsonify([{
            'id': p.id,
            'product_id': p.product_id,
            'product_name': p.product.name,
            'discount_type': p.discount_type,
            'discount_value': p.discount_value,
            'start_date': p.start_date.astimezone(myanmar_tz).isoformat() if p.start_date.tzinfo else myanmar_tz.localize(p.start_date).isoformat(),
            'end_date': p.end_date.astimezone(myanmar_tz).isoformat() if p.end_date.tzinfo else myanmar_tz.localize(p.end_date).isoformat(),
            'is_active': (myanmar_tz.localize(p.start_date) <= now <= myanmar_tz.localize(p.end_date))
        } for p in promotions])

    elif request.method == 'POST':
        data = request.get_json()
        required = ['product_id', 'discount_type', 'discount_value', 'start_date', 'end_date']
        if not data or not all(k in data for k in required):
            return jsonify({'success': False, 'message': 'Missing required fields'}), 400

        try:
            # Convert to Myanmar timezone
            start_date = datetime.fromisoformat(data['start_date'].replace('Z', '+00:00'))
            end_date = datetime.fromisoformat(data['end_date'].replace('Z', '+00:00'))
            
            start_date = start_date.astimezone(myanmar_tz)
            end_date = end_date.astimezone(myanmar_tz)

            if end_date <= start_date:
                return jsonify({'success': False, 'message': 'End date must be after start date'}), 400

            promotion = Promotion(
                product_id=data['product_id'],
                discount_type=data['discount_type'],
                discount_value=data['discount_value'],
                start_date=start_date,
                end_date=end_date
            )
            db.session.add(promotion)
            db.session.commit()

            return jsonify({'success': True, 'message': 'Promotion created!'}), 201
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/promotions/<int:promo_id>', methods=['GET', 'PUT', 'DELETE'])
@manager_required
def api_single_promotion(promo_id):
    promo = Promotion.query.get_or_404(promo_id)
    myanmar_tz = pytz.timezone('Asia/Yangon')

    if request.method == 'GET':
        return jsonify({
            'id': promo.id,
            'product_id': promo.product_id,
            'product_name': promo.product.name,
            'discount_type': promo.discount_type,
            'discount_value': promo.discount_value,
            'start_date': promo.start_date.astimezone(myanmar_tz).isoformat() if promo.start_date.tzinfo else myanmar_tz.localize(promo.start_date).isoformat(),
            'end_date': promo.end_date.astimezone(myanmar_tz).isoformat() if promo.end_date.tzinfo else myanmar_tz.localize(promo.end_date).isoformat()
        })

    elif request.method == 'DELETE':
        db.session.delete(promo)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Promotion deleted'})

    elif request.method == 'PUT':
        data = request.get_json()
        try:
            # Update fields if provided
            if 'start_date' in data:
                start_dt = datetime.fromisoformat(data['start_date'])
                promo.start_date = myanmar_tz.localize(start_dt)
            if 'end_date' in data:
                end_dt = datetime.fromisoformat(data['end_date'])
                promo.end_date = myanmar_tz.localize(end_dt)
            if 'discount_type' in data:
                promo.discount_type = data['discount_type']
            if 'discount_value' in data:
                promo.discount_value = data['discount_value']

            # Validate date range
            if promo.end_date <= promo.start_date:
                return jsonify({'success': False, 'message': 'End date must be after start date'}), 400

            db.session.commit()
            return jsonify({'success': True, 'message': 'Promotion updated'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': str(e)}), 500

# Customer API Endpoints
@app.route('/api/customers', methods=['GET', 'POST'])
@manager_required
def api_customers():
    branch_id = get_current_branch_id()
    
    if request.method == 'GET':
        customers = Customer.query.filter_by(branch_id=branch_id).all()
        return jsonify([{
            'id': c.id,
            'name': c.name,
            'phone': c.phone,
            'email': c.email,
            'address': c.address,
            'created_at': c.created_at.isoformat(),
            'total_debt': money_float(sum(max(to_decimal(d.balance), Decimal('0.00')) for d in c.debts if d.balance > 0))
        } for c in customers])
    
    elif request.method == 'POST':
        data = request.get_json()
        if not data or not 'name' in data:
            return jsonify({'success': False, 'message': 'Missing required fields'}), 400
        
        customer = Customer(
            name=data['name'],
            phone=data.get('phone'),
            email=data.get('email'),
            address=data.get('address'),
            branch_id=branch_id
        )
        db.session.add(customer)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Customer added'}), 201

# Purchase Order & Receiving API Endpoints
def purchase_orders_for_filters(search_query='', status_filter='', supplier_filter='',
                                start_date='', end_date='', branch_id=None):
    """Purchase orders for the tab's filters, newest first.

    Shared by the purchase order list API and its PDF/Excel exports so a
    downloaded report always matches the tab's filtered view.
    """
    if branch_id is None:
        branch_id = get_default_branch_id()
    query = PurchaseOrder.query.filter_by(branch_id=branch_id)

    if search_query:
        like_query = f"%{search_query}%"
        query = query.filter(
            (PurchaseOrder.po_number.ilike(like_query)) |
            (Supplier.name.ilike(like_query))
        ).join(Supplier)

    if status_filter:
        query = query.filter(PurchaseOrder.status == status_filter)

    if supplier_filter:
        try:
            query = query.filter(PurchaseOrder.supplier_id == int(supplier_filter))
        except ValueError:
            pass

    if start_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            query = query.filter(PurchaseOrder.created_at >= start_dt)
        except ValueError:
            pass

    if end_date:
        try:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
            query = query.filter(PurchaseOrder.created_at < end_dt)
        except ValueError:
            pass

    return query.order_by(PurchaseOrder.created_at.desc()).all()


def serialize_purchase_order(po, include_items=False):
    """Purchase order payload shared by the list API and the exports."""
    payload = {
        'id': po.id,
        'po_number': po.po_number,
        'supplier_id': po.supplier_id,
        'supplier_name': po.supplier.name if po.supplier else 'Unknown',
        'status': po.status,
        'status_label': purchase_order_status_label(po.status),
        'total_amount': po.total_amount,
        'expected_delivery_date': po.expected_delivery_date.isoformat() if po.expected_delivery_date else None,
        'notes': po.notes,
        'created_by': po.creator.username if po.creator else None,
        'approved_by': po.approver.username if po.approver else None,
        'approved_at': po.approved_at.isoformat() if po.approved_at else None,
        'created_at': po.created_at.isoformat(),
        'updated_at': po.updated_at.isoformat() if po.updated_at else None,
        'items_count': len(po.items),
        'received_items_count': sum(1 for i in po.items if i.received_qty >= i.ordered_qty),
        'total_ordered': sum(i.ordered_qty for i in po.items),
        'total_received': sum(i.received_qty for i in po.items),
    }
    if include_items:
        payload['items'] = [{
            'product_name': item.product.name if item.product else 'Unknown product',
            'ordered_qty': item.ordered_qty,
            'received_qty': item.received_qty,
            'unit_cost': item.unit_cost,
            'line_total': money_float(
                safe_to_decimal(item.ordered_qty) * safe_to_decimal(item.unit_cost or 0)
            ),
        } for item in po.items]
    return payload


@app.route('/api/purchase_orders', methods=['GET', 'POST'])
@manager_required
def api_purchase_orders():
    branch_id = get_default_branch_id()

    if request.method == 'GET':
        # Get filter parameters
        search_query = (request.args.get('q') or '').strip()
        status_filter = (request.args.get('status') or '').strip()
        supplier_filter = (request.args.get('supplier_id') or '').strip()
        start_date = (request.args.get('start_date') or '').strip()
        end_date = (request.args.get('end_date') or '').strip()

        purchase_orders = purchase_orders_for_filters(
            search_query, status_filter, supplier_filter,
            start_date, end_date, branch_id,
        )
        return jsonify([serialize_purchase_order(po) for po in purchase_orders])

    data = request.get_json() or {}
    supplier_id = data.get('supplier_id')
    items = data.get('items') or []
    expected_delivery_date = data.get('expected_delivery_date')

    if not supplier_id or not items:
        return jsonify({'success': False, 'message': 'Supplier and at least one item are required'}), 400

    supplier = db.session.get(Supplier, supplier_id)
    if not supplier:
        return jsonify({'success': False, 'message': 'Supplier not found'}), 404

    try:
        # Parse expected delivery date
        exp_delivery_dt = None
        if expected_delivery_date:
            try:
                exp_delivery_dt = datetime.fromisoformat(expected_delivery_date.replace('Z', '+00:00'))
            except ValueError:
                try:
                    exp_delivery_dt = datetime.strptime(expected_delivery_date, '%Y-%m-%d')
                except ValueError:
                    pass
        
        po = PurchaseOrder(
            po_number=generate_po_number(),
            supplier_id=supplier_id,
            status='draft',
            notes=(data.get('notes') or '').strip() or None,
            expected_delivery_date=exp_delivery_dt,
            created_by=session.get('user_id'),
            branch_id=get_default_branch_id()
        )
        db.session.add(po)
        db.session.flush()

        total_amount = 0.0
        for item in items:
            product_id = item.get('product_id')
            ordered_qty = int(item.get('ordered_qty', 0) or 0)
            unit_cost = float(item.get('unit_cost', 0) or 0)

            if ordered_qty <= 0:
                db.session.rollback()
                return jsonify({'success': False, 'message': 'Ordered quantity must be greater than 0'}), 400

            product = db.session.get(Product, product_id)
            if not product:
                db.session.rollback()
                return jsonify({'success': False, 'message': f'Product not found: {product_id}'}), 404

            po_item = PurchaseOrderItem(
                purchase_order_id=po.id,
                product_id=product_id,
                ordered_qty=ordered_qty,
                received_qty=0,
                unit_cost=unit_cost
            )
            db.session.add(po_item)
            total_amount += ordered_qty * unit_cost

        po.total_amount = total_amount
        db.session.commit()
        return jsonify({'success': True, 'message': 'Purchase order created', 'purchase_order_id': po.id, 'po_number': po.po_number}), 201
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error creating purchase order: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to create purchase order'}), 500

@app.route('/api/purchase_orders/export', methods=['GET'])
@manager_required
def export_purchase_orders():
    """Download the purchase order register as a professional PDF or Excel report.

    Uses exactly the tab's filters (search, status, supplier, date range), and
    the workbook adds a line-item sheet plus a report-info cover sheet.
    """
    search_query = (request.args.get('q') or '').strip()
    status_filter = (request.args.get('status') or '').strip()
    supplier_filter = (request.args.get('supplier_id') or '').strip()
    start_date = (request.args.get('start_date') or '').strip()
    end_date = (request.args.get('end_date') or '').strip()
    report_format = normalize_report_format(request.args.get('format'))
    branch_id = get_default_branch_id()
    branch = db.session.get(Branch, branch_id) if branch_id else None

    purchase_orders = purchase_orders_for_filters(
        search_query, status_filter, supplier_filter,
        start_date, end_date, branch_id,
    )
    supplier_name = ''
    if supplier_filter:
        supplier = db.session.get(Supplier, int(supplier_filter)) if supplier_filter.isdigit() else None
        supplier_name = supplier.name if supplier else supplier_filter

    records = [serialize_purchase_order(po, include_items=True) for po in purchase_orders]
    filters_text = describe_filters({
        'Search': search_query,
        'Status': purchase_order_status_label(status_filter) if status_filter else '',
        'Supplier': supplier_name,
        'From': start_date,
        'To': end_date,
    })
    report = build_purchase_order_report(
        records,
        brand=get_receipt_identity(branch),
        branch_name=branch.name if branch else '',
        generated_by=session.get('username') or '',
        filters_text=filters_text,
        currency_suffix=get_currency_suffix(),
    )

    if report_format == 'pdf':
        payload = build_report_pdf(report)
    else:
        payload = build_report_xlsx(
            report,
            extra_sheets=[
                build_purchase_order_item_sheet(
                    records, currency_suffix=get_currency_suffix()
                )
            ],
        )
    filename = report_filename(report['file_stem'], report_format)
    response = make_response(payload)
    response.headers['Content-Type'] = report_content_type(report_format)
    response.headers['Content-Disposition'] = report_disposition(filename, report_format)
    return response


@app.route('/api/purchase_orders/summary', methods=['GET'])
@manager_required
def api_purchase_orders_summary():
    """Get purchase order summary statistics"""
    branch_id = get_default_branch_id()
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    base_query = PurchaseOrder.query.filter_by(branch_id=branch_id)
    
    total_pos = base_query.count()
    pending_approval = base_query.filter(PurchaseOrder.status == 'pending').count()
    approved = base_query.filter(PurchaseOrder.status == 'approved').count()
    partially_received = base_query.filter(PurchaseOrder.status == 'partially_received').count()
    received = base_query.filter(PurchaseOrder.status == 'received').count()
    cancelled = base_query.filter(PurchaseOrder.status == 'cancelled').count()
    
    # This month's totals
    monthly_received = base_query.filter(
        PurchaseOrder.status == 'received',
        PurchaseOrder.updated_at >= month_start
    ).count()
    
    monthly_amount = db.session.query(db.func.sum(PurchaseOrder.total_amount)).filter(
        PurchaseOrder.branch_id == branch_id,
        PurchaseOrder.created_at >= month_start,
        PurchaseOrder.status != 'cancelled'
    ).scalar() or 0
    
    return jsonify({
        'total': total_pos,
        'draft': base_query.filter(PurchaseOrder.status == 'draft').count(),
        'pending_approval': pending_approval,
        'approved': approved,
        'partially_received': partially_received,
        'received': received,
        'cancelled': cancelled,
        'monthly_received': monthly_received,
        'monthly_amount': round(monthly_amount, 2)
    })

@app.route('/api/purchase_orders/<int:po_id>', methods=['GET', 'PUT'])
@manager_required
def api_single_purchase_order(po_id):
    branch_id = get_default_branch_id()
    po = PurchaseOrder.query.filter_by(id=po_id, branch_id=branch_id).first()
    if not po:
        return jsonify({'success': False, 'message': 'Purchase order not found'}), 404

    if request.method == 'GET':
        return jsonify({
            'id': po.id,
            'po_number': po.po_number,
            'supplier_id': po.supplier_id,
            'supplier_name': po.supplier.name if po.supplier else 'Unknown',
            'supplier_phone': po.supplier.phone if po.supplier else None,
            'supplier_email': po.supplier.email if po.supplier else None,
            'supplier_address': po.supplier.address if po.supplier else None,
            'status': po.status,
            'total_amount': po.total_amount,
            'expected_delivery_date': po.expected_delivery_date.isoformat() if po.expected_delivery_date else None,
            'notes': po.notes,
            'created_by': po.creator.username if po.creator else None,
            'approved_by': po.approver.username if po.approver else None,
            'approved_at': po.approved_at.isoformat() if po.approved_at else None,
            'cancelled_at': po.cancelled_at.isoformat() if po.cancelled_at else None,
            'cancelled_reason': po.cancelled_reason,
            'created_at': po.created_at.isoformat(),
            'updated_at': po.updated_at.isoformat() if po.updated_at else None,
            'items': [{
                'id': item.id,
                'product_id': item.product_id,
                'product_name': item.product.name if item.product else 'Unknown',
                'product_sku': item.product.barcode if item.product else None,
                'ordered_qty': item.ordered_qty,
                'received_qty': item.received_qty,
                'remaining_qty': item.ordered_qty - item.received_qty,
                'unit_cost': item.unit_cost,
                'line_total': item.ordered_qty * item.unit_cost
            } for item in po.items]
        })
    
    elif request.method == 'PUT':
        # Update PO (only draft status)
        if po.status != 'draft':
            return jsonify({'success': False, 'message': 'Only draft purchase orders can be edited'}), 400
        
        data = request.get_json() or {}
        
        if 'notes' in data:
            po.notes = (data['notes'] or '').strip() or None
        
        if 'expected_delivery_date' in data:
            if data['expected_delivery_date']:
                try:
                    po.expected_delivery_date = datetime.fromisoformat(data['expected_delivery_date'].replace('Z', '+00:00'))
                except ValueError:
                    try:
                        po.expected_delivery_date = datetime.strptime(data['expected_delivery_date'], '%Y-%m-%d')
                    except ValueError:
                        pass
            else:
                po.expected_delivery_date = None
        
        db.session.commit()
        return jsonify({'success': True, 'message': 'Purchase order updated'})

@app.route('/api/purchase_orders/<int:po_id>/approve', methods=['POST'])
@manager_required
def api_approve_purchase_order(po_id):
    """Approve a purchase order"""
    po = db.session.get(PurchaseOrder, po_id)
    if not po:
        return jsonify({'success': False, 'message': 'Purchase order not found'}), 404
    
    if po.status not in ('draft', 'pending'):
        return jsonify({'success': False, 'message': 'Only draft or pending purchase orders can be approved'}), 400
    
    po.status = 'approved'
    po.approved_by = session.get('user_id')
    po.approved_at = datetime.utcnow()
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Purchase order approved'})

@app.route('/api/purchase_orders/<int:po_id>/submit', methods=['POST'])
@manager_required
def api_submit_purchase_order(po_id):
    """Submit a draft purchase order for approval"""
    po = db.session.get(PurchaseOrder, po_id)
    if not po:
        return jsonify({'success': False, 'message': 'Purchase order not found'}), 404
    
    if po.status != 'draft':
        return jsonify({'success': False, 'message': 'Only draft purchase orders can be submitted'}), 400
    
    po.status = 'pending'
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Purchase order submitted for approval'})

@app.route('/api/purchase_orders/<int:po_id>/cancel', methods=['POST'])
@manager_required
def api_cancel_purchase_order(po_id):
    """Cancel a purchase order"""
    po = db.session.get(PurchaseOrder, po_id)
    if not po:
        return jsonify({'success': False, 'message': 'Purchase order not found'}), 404
    
    if po.status in ('received', 'cancelled'):
        return jsonify({'success': False, 'message': 'Received or already cancelled orders cannot be cancelled'}), 400
    
    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip() or None
    
    po.status = 'cancelled'
    po.cancelled_at = datetime.utcnow()
    po.cancelled_reason = reason
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Purchase order cancelled'})

@app.route('/api/purchase_orders/<int:po_id>/receive', methods=['POST'])
@manager_required
def api_receive_purchase_order(po_id):
    po = db.session.get(PurchaseOrder, po_id)
    if not po:
        return jsonify({'success': False, 'message': 'Purchase order not found'}), 404

    if po.status in ('received', 'cancelled'):
        return jsonify({'success': False, 'message': f'Cannot receive items for {po.status} purchase order'}), 400

    data = request.get_json() or {}
    received_items = data.get('items') or []
    if not received_items:
        return jsonify({'success': False, 'message': 'No receiving items provided'}), 400

    item_map = {item.id: item for item in po.items}

    try:
        for received in received_items:
            po_item_id = received.get('purchase_order_item_id')
            receive_qty = int(received.get('received_qty', 0) or 0)

            if receive_qty <= 0:
                continue

            po_item = item_map.get(po_item_id)
            if not po_item:
                db.session.rollback()
                return jsonify({'success': False, 'message': f'Invalid PO item: {po_item_id}'}), 400

            remaining = po_item.ordered_qty - po_item.received_qty
            if receive_qty > remaining:
                db.session.rollback()
                return jsonify({'success': False, 'message': f'Receive qty exceeds remaining for {po_item.product.name}'}), 400

            po_item.received_qty += receive_qty
            
            # Add to warehouse inventory instead of directly to product stock
            warehouse_item = WarehouseInventory(
                product_id=po_item.product_id,
                quantity=receive_qty,
                batch_number=po.po_number,  # Use PO number as batch reference
                received_date=datetime.utcnow(),
                unit_cost=po_item.unit_cost,
                branch_id=po.branch_id
            )
            db.session.add(warehouse_item)

            if po_item.unit_cost and po_item.unit_cost > 0:
                po_item.product.cost = po_item.unit_cost

        all_received = all(item.received_qty >= item.ordered_qty for item in po.items)
        any_received = any(item.received_qty > 0 for item in po.items)
        
        if all_received:
            po.status = 'received'
        elif any_received:
            po.status = 'partially_received'
        
        # Update supplier stats
        if po.supplier:
            po.supplier.total_orders = (po.supplier.total_orders or 0) + 1

        db.session.commit()
        return jsonify({'success': True, 'message': 'Receiving recorded', 'status': po.status})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error receiving purchase order: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to process receiving'}), 500

@app.route('/api/purchase_orders/<int:po_id>/print', methods=['GET'])
@manager_required
def api_print_purchase_order(po_id):
    """Generate PDF for purchase order (internal use invoice)"""
    po = db.session.get(PurchaseOrder, po_id)
    if not po:
        return jsonify({'success': False, 'message': 'Purchase order not found'}), 404
    
    buffer = io.BytesIO()
    page_width = 210 * mm
    page_height = 297 * mm
    
    doc = SimpleDocTemplate(buffer, pagesize=(page_width, page_height),
                           rightMargin=20*mm, leftMargin=20*mm,
                           topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    elements = []
    
    # Title
    elements.append(Paragraph("PURCHASE ORDER", styles['Heading1']))
    elements.append(Spacer(1, 10))
    
    # PO Info
    info_data = [
        ['PO Number:', po.po_number],
        ['Date:', po.created_at.strftime('%Y-%m-%d')],
        ['Status:', po.status.upper()],
    ]
    if po.expected_delivery_date:
        info_data.append(['Expected Delivery:', po.expected_delivery_date.strftime('%Y-%m-%d')])
    
    info_table = Table(info_data, colWidths=[80, 200])
    info_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 15))
    
    # Supplier Info
    elements.append(Paragraph("Supplier Information", styles['Heading2']))
    supplier_info = [
        ['Name:', po.supplier.name if po.supplier else 'N/A'],
        ['Contact:', po.supplier.contact_person if po.supplier and po.supplier.contact_person else '-'],
        ['Phone:', po.supplier.phone if po.supplier and po.supplier.phone else '-'],
        ['Email:', po.supplier.email if po.supplier and po.supplier.email else '-'],
    ]
    supplier_table = Table(supplier_info, colWidths=[80, 200])
    supplier_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(supplier_table)
    elements.append(Spacer(1, 15))
    
    # Items Table
    elements.append(Paragraph("Items", styles['Heading2']))
    items_header = ['Product', 'Qty', 'Unit Cost', 'Total']
    items_data = [items_header]
    
    for item in po.items:
        line_total = item.ordered_qty * item.unit_cost
        items_data.append([
            item.product.name if item.product else 'Unknown',
            str(item.ordered_qty),
            format_currency(item.unit_cost),
            format_currency(line_total)
        ])
    
    # Add total row
    items_data.append(['', '', 'TOTAL:', format_currency(po.total_amount)])
    
    items_table = Table(items_data, colWidths=[200, 50, 80, 80])
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONT', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -2), 1, colors.black),
    ]))
    elements.append(items_table)
    
    # Notes
    if po.notes:
        elements.append(Spacer(1, 15))
        elements.append(Paragraph("Notes", styles['Heading2']))
        elements.append(Paragraph(po.notes, styles['Normal']))
    
    # Footer
    elements.append(Spacer(1, 30))
    elements.append(Paragraph("This is an internal document for record keeping purposes.", styles['Normal']))
    
    doc.build(elements)
    buffer.seek(0)
    
    response = make_response(buffer.getvalue())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'inline; filename=PO_{po.po_number}.pdf'
    return response

# Supplier API Endpoints
@app.route('/api/suppliers', methods=['GET', 'POST'])
@manager_required
def api_suppliers():
    branch_id = get_default_branch_id()
    
    if request.method == 'GET':
        query = Supplier.query.filter_by(branch_id=branch_id)
        search_query = (request.args.get('q') or '').strip()
        active_filter = (request.args.get('active') or '').strip().lower()
        category_filter = (request.args.get('category') or '').strip()
        category_id_filter = request.args.get('category_id')

        if search_query:
            like_query = f"%{search_query}%"
            query = query.filter(
                (Supplier.name.ilike(like_query)) |
                (Supplier.contact_person.ilike(like_query)) |
                (Supplier.phone.ilike(like_query)) |
                (Supplier.email.ilike(like_query))
            )

        if active_filter in ('active', 'inactive'):
            query = query.filter(Supplier.is_active.is_(active_filter == 'active'))
        
        if category_id_filter:
            try:
                query = query.filter(Supplier.category_id == int(category_id_filter))
            except ValueError:
                pass  # Invalid category_id, ignore filter
        elif category_filter:
            query = query.filter(Supplier.category == category_filter)

        suppliers = query.order_by(Supplier.name.asc()).all()
        return jsonify([{
            'id': s.id,
            'name': s.name,
            'contact_person': s.contact_person,
            'phone': s.phone,
            'email': s.email,
            'address': s.address,
            'payment_terms': s.payment_terms,
            'lead_time_days': s.lead_time_days,
            'is_active': bool(s.is_active),
            'notes': s.notes,
            'category': s.category_ref.name if s.category_ref else s.category,
            'category_id': s.category_id,
            'tax_id': s.tax_id,
            'website': s.website,
            'bank_name': s.bank_name,
            'bank_account': s.bank_account,
            'quality_rating': s.quality_rating,
            'delivery_rating': s.delivery_rating,
            'total_orders': s.total_orders,
            'on_time_deliveries': s.on_time_deliveries,
            'performance_score': round(((s.quality_rating or 0) + (s.delivery_rating or 0)) / 2, 1) if s.quality_rating or s.delivery_rating else 0,
            'created_at': s.created_at.isoformat(),
            'updated_at': s.updated_at.isoformat() if s.updated_at else None
        } for s in suppliers])

    elif request.method == 'POST':
        data = request.get_json()
        if not data or not data.get('name'):
            return jsonify({'success': False, 'message': 'Supplier name is required'}), 400

        supplier_name = str(data['name']).strip()
        if not supplier_name:
            return jsonify({'success': False, 'message': 'Supplier name is required'}), 400

        email = (data.get('email') or '').strip() or None
        if email and '@' not in email:
            return jsonify({'success': False, 'message': 'Invalid supplier email address'}), 400

        lead_time_days = data.get('lead_time_days')
        if lead_time_days in ('', None):
            lead_time_days = None
        else:
            try:
                lead_time_days = int(lead_time_days)
                if lead_time_days < 0:
                    return jsonify({'success': False, 'message': 'Lead time cannot be negative'}), 400
            except (TypeError, ValueError):
                return jsonify({'success': False, 'message': 'Lead time must be a whole number'}), 400

        existing_supplier = Supplier.query.filter(Supplier.name.ilike(supplier_name)).first()
        if existing_supplier:
            return jsonify({'success': False, 'message': 'Supplier with this name already exists'}), 400

        supplier = Supplier(
            name=supplier_name,
            contact_person=(data.get('contact_person') or '').strip() or None,
            phone=(data.get('phone') or '').strip() or None,
            email=email,
            address=(data.get('address') or '').strip() or None,
            payment_terms=(data.get('payment_terms') or '').strip() or None,
            lead_time_days=lead_time_days,
            is_active=bool(data.get('is_active', True)),
            notes=(data.get('notes') or '').strip() or None,
            category_id=data.get('category_id'),
            category=(data.get('category') or '').strip() or None,
            tax_id=(data.get('tax_id') or '').strip() or None,
            website=(data.get('website') or '').strip() or None,
            bank_name=(data.get('bank_name') or '').strip() or None,
            bank_account=(data.get('bank_account') or '').strip() or None,
            quality_rating=float(data.get('quality_rating', 0) or 0),
            delivery_rating=float(data.get('delivery_rating', 0) or 0),
            branch_id=branch_id
        )
        # Sync category name from Category table if category_id is provided
        if supplier.category_id:
            cat = db.session.get(Category, supplier.category_id)
            if cat:
                supplier.category = cat.name
        db.session.add(supplier)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Supplier added', 'supplier_id': supplier.id}), 201

@app.route('/api/suppliers/<int:supplier_id>', methods=['GET', 'PUT', 'DELETE'])
@manager_required
def api_single_supplier(supplier_id):
    branch_id = get_default_branch_id()
    supplier = Supplier.query.filter_by(id=supplier_id, branch_id=branch_id).first()
    if not supplier:
        return jsonify({'success': False, 'message': 'Supplier not found'}), 404

    if request.method == 'GET':
        # Calculate performance metrics
        total_orders = supplier.total_orders or 0
        on_time = supplier.on_time_deliveries or 0
        on_time_rate = round((on_time / total_orders) * 100, 1) if total_orders > 0 else 0
        
        return jsonify({
            'id': supplier.id,
            'name': supplier.name,
            'contact_person': supplier.contact_person,
            'phone': supplier.phone,
            'email': supplier.email,
            'address': supplier.address,
            'payment_terms': supplier.payment_terms,
            'lead_time_days': supplier.lead_time_days,
            'is_active': bool(supplier.is_active),
            'notes': supplier.notes,
            'category': supplier.category_ref.name if supplier.category_ref else supplier.category,
            'category_id': supplier.category_id,
            'tax_id': supplier.tax_id,
            'website': supplier.website,
            'bank_name': supplier.bank_name,
            'bank_account': supplier.bank_account,
            'quality_rating': supplier.quality_rating,
            'delivery_rating': supplier.delivery_rating,
            'total_orders': total_orders,
            'on_time_deliveries': on_time,
            'on_time_rate': on_time_rate,
            'created_at': supplier.created_at.isoformat(),
            'updated_at': supplier.updated_at.isoformat() if supplier.updated_at else None
        })

    elif request.method == 'PUT':
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400

        name = data.get('name', supplier.name)
        if not name or not str(name).strip():
            return jsonify({'success': False, 'message': 'Supplier name is required'}), 400

        cleaned_name = str(name).strip()
        duplicate = Supplier.query.filter(
            Supplier.id != supplier.id,
            Supplier.name.ilike(cleaned_name)
        ).first()
        if duplicate:
            return jsonify({'success': False, 'message': 'Supplier with this name already exists'}), 400

        email = data.get('email', supplier.email)
        email = (email or '').strip() or None
        if email and '@' not in email:
            return jsonify({'success': False, 'message': 'Invalid supplier email address'}), 400

        lead_time_days = data.get('lead_time_days', supplier.lead_time_days)
        if lead_time_days in ('', None):
            lead_time_days = None
        else:
            try:
                lead_time_days = int(lead_time_days)
                if lead_time_days < 0:
                    return jsonify({'success': False, 'message': 'Lead time cannot be negative'}), 400
            except (TypeError, ValueError):
                return jsonify({'success': False, 'message': 'Lead time must be a whole number'}), 400

        supplier.name = cleaned_name
        supplier.contact_person = (data.get('contact_person', supplier.contact_person) or '').strip() or None
        supplier.phone = (data.get('phone', supplier.phone) or '').strip() or None
        supplier.email = email
        supplier.address = (data.get('address', supplier.address) or '').strip() or None
        supplier.payment_terms = (data.get('payment_terms', supplier.payment_terms) or '').strip() or None
        supplier.lead_time_days = lead_time_days
        supplier.is_active = bool(data.get('is_active', supplier.is_active))
        supplier.notes = (data.get('notes', supplier.notes) or '').strip() or None
        supplier.category = (data.get('category', supplier.category) or '').strip() or None
        supplier.category_id = data.get('category_id') or supplier.category_id
        if supplier.category_id:
            cat = db.session.get(Category, supplier.category_id)
            if cat:
                supplier.category = cat.name
        supplier.tax_id = (data.get('tax_id', supplier.tax_id) or '').strip() or None
        supplier.website = (data.get('website', supplier.website) or '').strip() or None
        supplier.bank_name = (data.get('bank_name', supplier.bank_name) or '').strip() or None
        supplier.bank_account = (data.get('bank_account', supplier.bank_account) or '').strip() or None
        if 'quality_rating' in data:
            supplier.quality_rating = float(data['quality_rating'] or 0)
        if 'delivery_rating' in data:
            supplier.delivery_rating = float(data['delivery_rating'] or 0)
        supplier.updated_at = datetime.utcnow()

        db.session.commit()
        return jsonify({'success': True, 'message': 'Supplier updated'})

    elif request.method == 'DELETE':
        db.session.delete(supplier)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Supplier deleted'})

@app.route('/api/suppliers/<int:supplier_id>/orders', methods=['GET'])
@manager_required
def api_supplier_orders(supplier_id):
    """Get all purchase orders for a supplier"""
    supplier = db.session.get(Supplier, supplier_id)
    if not supplier:
        return jsonify({'success': False, 'message': 'Supplier not found'}), 404
    
    orders = PurchaseOrder.query.filter_by(
        supplier_id=supplier_id,
        branch_id=get_default_branch_id()
    ).order_by(PurchaseOrder.created_at.desc()).all()
    return jsonify([{
        'id': po.id,
        'po_number': po.po_number,
        'status': po.status,
        'total_amount': po.total_amount,
        'created_at': po.created_at.isoformat(),
        'items_count': len(po.items)
    } for po in orders])

@app.route('/api/suppliers/<int:supplier_id>/communications', methods=['GET', 'POST'])
@manager_required
def api_supplier_communications(supplier_id):
    """Get or add supplier communications"""
    supplier = db.session.get(Supplier, supplier_id)
    if not supplier:
        return jsonify({'success': False, 'message': 'Supplier not found'}), 404
    
    if request.method == 'GET':
        communications = SupplierCommunication.query.filter_by(supplier_id=supplier_id).order_by(SupplierCommunication.created_at.desc()).all()
        return jsonify([{
            'id': c.id,
            'communication_type': c.communication_type,
            'subject': c.subject,
            'content': c.content,
            'created_by': c.creator.username if c.creator else None,
            'created_at': c.created_at.isoformat()
        } for c in communications])
    
    elif request.method == 'POST':
        data = request.get_json() or {}
        comm_type = (data.get('communication_type') or '').strip()
        subject = (data.get('subject') or '').strip()
        content = (data.get('content') or '').strip()
        
        if not comm_type or comm_type not in ('call', 'email', 'meeting', 'other'):
            return jsonify({'success': False, 'message': 'Valid communication type required (call, email, meeting, other)'}), 400
        
        comm = SupplierCommunication(
            supplier_id=supplier_id,
            communication_type=comm_type,
            subject=subject or None,
            content=content or None,
            created_by=session.get('user_id')
        )
        db.session.add(comm)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Communication logged'})

@app.route('/api/suppliers/<int:supplier_id>/ratings', methods=['POST'])
@manager_required
def api_supplier_ratings(supplier_id):
    """Update supplier ratings"""
    supplier = db.session.get(Supplier, supplier_id)
    if not supplier:
        return jsonify({'success': False, 'message': 'Supplier not found'}), 404
    
    data = request.get_json() or {}
    
    if 'quality_rating' in data:
        try:
            quality = float(data['quality_rating'])
            if 0 <= quality <= 5:
                supplier.quality_rating = quality
            else:
                return jsonify({'success': False, 'message': 'Quality rating must be between 0 and 5'}), 400
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'Invalid quality rating'}), 400
    
    if 'delivery_rating' in data:
        try:
            delivery = float(data['delivery_rating'])
            if 0 <= delivery <= 5:
                supplier.delivery_rating = delivery
            else:
                return jsonify({'success': False, 'message': 'Delivery rating must be between 0 and 5'}), 400
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'Invalid delivery rating'}), 400
    
    db.session.commit()
    return jsonify({'success': True, 'message': 'Ratings updated'})

@app.route('/api/suppliers/<int:supplier_id>/products', methods=['GET', 'POST'])
@manager_required
def api_supplier_products(supplier_id):
    """Get or add supplier-specific product pricing"""
    supplier = db.session.get(Supplier, supplier_id)
    if not supplier:
        return jsonify({'success': False, 'message': 'Supplier not found'}), 404
    
    if request.method == 'GET':
        agreements = SupplierPriceAgreement.query.filter_by(supplier_id=supplier_id).all()
        return jsonify([{
            'id': a.id,
            'product_id': a.product_id,
            'product_name': a.product.name if a.product else 'Unknown',
            'agreed_price': a.agreed_price,
            'valid_from': a.valid_from.isoformat() if a.valid_from else None,
            'valid_to': a.valid_to.isoformat() if a.valid_to else None,
            'notes': a.notes
        } for a in agreements])
    
    elif request.method == 'POST':
        data = request.get_json() or {}
        product_id = data.get('product_id')
        agreed_price = data.get('agreed_price')
        
        if not product_id or agreed_price is None:
            return jsonify({'success': False, 'message': 'Product and agreed price are required'}), 400
        
        product = db.session.get(Product, product_id)
        if not product:
            return jsonify({'success': False, 'message': 'Product not found'}), 404
        
        valid_from = None
        valid_to = None
        if data.get('valid_from'):
            try:
                valid_from = datetime.fromisoformat(data['valid_from'].replace('Z', '+00:00'))
            except ValueError:
                pass
        if data.get('valid_to'):
            try:
                valid_to = datetime.fromisoformat(data['valid_to'].replace('Z', '+00:00'))
            except ValueError:
                pass
        
        agreement = SupplierPriceAgreement(
            supplier_id=supplier_id,
            product_id=product_id,
            agreed_price=float(agreed_price),
            valid_from=valid_from,
            valid_to=valid_to,
            notes=(data.get('notes') or '').strip() or None
        )
        db.session.add(agreement)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Price agreement added'})

# ==================== Warehouse Management APIs ====================

def warehouse_inventory_records(search_query='', low_stock=False, branch_id=None):
    """Warehouse stock rows (quantity > 0) with the tab's optional filters.

    Shared by the stock list API and its PDF/Excel exports so a downloaded
    report always matches what the Warehouse tab is showing.
    """
    if branch_id is None:
        branch_id = get_default_branch_id()
    query = WarehouseInventory.query.filter(
        WarehouseInventory.quantity > 0,
        WarehouseInventory.branch_id == branch_id,
    )

    if search_query:
        like_query = f"%{search_query}%"
        query = query.join(Product).filter(
            (Product.name.ilike(like_query)) |
            (Product.barcode.ilike(like_query))
        )

    inventory = query.order_by(WarehouseInventory.updated_at.desc()).all()

    result = []
    for item in inventory:
        total_warehouse_qty = sum(w.quantity for w in item.product.warehouse_items) if item.product.warehouse_items else 0
        result.append({
            'id': item.id,
            'product_id': item.product_id,
            'product_name': item.product.name if item.product else 'Unknown',
            'barcode': item.product.barcode if item.product else None,
            'quantity': item.quantity,
            'total_warehouse_qty': total_warehouse_qty,
            'main_stock': item.product.stock if item.product else 0,
            'location': item.location,
            'batch_number': item.batch_number,
            'unit_cost': item.unit_cost,
            'total_value': money_float(safe_to_decimal(item.quantity) * safe_to_decimal(item.unit_cost or 0)),
            'received_date': item.received_date.isoformat() if item.received_date else None,
            'expiry_date': item.expiry_date.isoformat() if item.expiry_date else None,
            'notes': item.notes,
            'created_at': item.created_at.isoformat(),
            'updated_at': item.updated_at.isoformat() if item.updated_at else None
        })

    if low_stock:
        result = [r for r in result if r['quantity'] <= LOW_STOCK_THRESHOLD]

    return result

@app.route('/api/warehouse', methods=['GET'])
@manager_required
def api_warehouse_inventory():
    """Get all warehouse inventory with optional filters"""
    search_query = (request.args.get('q') or '').strip()
    low_stock = request.args.get('low_stock', '').strip().lower() == 'true'

    return jsonify(warehouse_inventory_records(search_query, low_stock))

@app.route('/api/warehouse/export', methods=['GET'])
@manager_required
def export_warehouse_stock():
    """Download the warehouse stock list as a professional PDF or Excel report.

    Honours the same search / low-stock filters as the Warehouse tab, then adds
    the letterhead, KPI summary, totals and page numbering the report needs.
    """
    search_query = (request.args.get('q') or '').strip()
    low_stock = request.args.get('low_stock', '').strip().lower() == 'true'
    report_format = normalize_report_format(request.args.get('format'))
    branch_id = get_default_branch_id()
    branch = db.session.get(Branch, branch_id) if branch_id else None

    records = warehouse_inventory_records(search_query, low_stock, branch_id)
    report = build_warehouse_stock_report(
        records,
        brand=get_receipt_identity(branch),
        branch_name=branch.name if branch else '',
        generated_by=session.get('username') or '',
        filters_text=describe_filters({
            'Search': search_query,
            'Low stock only': low_stock,
        }),
        currency_suffix=get_currency_suffix(),
    )

    payload = build_report_pdf(report) if report_format == 'pdf' else build_report_xlsx(report)
    filename = report_filename(report['file_stem'], report_format)
    response = make_response(payload)
    response.headers['Content-Type'] = report_content_type(report_format)
    response.headers['Content-Disposition'] = report_disposition(filename, report_format)
    return response

@app.route('/api/warehouse/summary', methods=['GET'])
@manager_required
def api_warehouse_summary():
    """Get warehouse summary statistics"""
    branch_id = get_default_branch_id()
    inventory = WarehouseInventory.query.filter(WarehouseInventory.quantity > 0, WarehouseInventory.branch_id == branch_id).all()
    
    total_skus = len(set(item.product_id for item in inventory))
    total_units = sum(item.quantity for item in inventory)
    total_value = sum(
        (round_money(safe_to_decimal(item.quantity) * safe_to_decimal(item.unit_cost or 0))
         for item in inventory),
        Decimal('0')
    )
    
    # Get recent transfers count (last 7 days)
    week_ago = datetime.utcnow() - timedelta(days=7)
    recent_transfers = WarehouseTransfer.query.filter(
        WarehouseTransfer.created_at >= week_ago,
        WarehouseTransfer.branch_id == branch_id
    ).count()
    
    # Low stock items (quantity <= 5)
    low_stock_count = sum(1 for item in inventory if item.quantity <= 5)
    
    return jsonify({
        'total_skus': total_skus,
        'total_units': total_units,
        'total_value': money_float(total_value),
        'recent_transfers': recent_transfers,
        'low_stock_count': low_stock_count
    })

@app.route('/api/warehouse/transfer', methods=['POST'])
@manager_required
def api_warehouse_transfer():
    """Transfer products from warehouse to main stock"""
    data = request.get_json() or {}
    
    product_id = data.get('product_id')
    quantity = int(data.get('quantity', 0) or 0)
    batch_number = data.get('batch_number')
    target_branch_id = data.get('target_branch_id')
    notes = (data.get('notes') or '').strip() or None
    branch_id = get_default_branch_id()
    
    if not product_id or quantity <= 0:
        return jsonify({'success': False, 'message': 'Product and valid quantity are required'}), 400
    
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({'success': False, 'message': 'Product not found'}), 404

    if target_branch_id in (None, '', 'current'):
        target_branch_id = get_current_branch_id()
    else:
        try:
            target_branch_id = int(target_branch_id)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'Invalid target branch'}), 400

    target_branch = Branch.query.filter_by(id=target_branch_id, is_active=True).first()
    if not target_branch:
        return jsonify({'success': False, 'message': 'Target branch not found'}), 404
    
    # Get warehouse inventory for this product (filtered by branch)
    warehouse_query = WarehouseInventory.query.filter_by(product_id=product_id, branch_id=branch_id)
    if batch_number:
        warehouse_query = warehouse_query.filter_by(batch_number=batch_number)
    
    warehouse_items = warehouse_query.filter(WarehouseInventory.quantity > 0).all()
    
    if not warehouse_items:
        return jsonify({'success': False, 'message': 'No warehouse inventory found for this product'}), 400
    
    total_available = sum(item.quantity for item in warehouse_items)
    if quantity > total_available:
        return jsonify({'success': False, 'message': f'Insufficient warehouse stock. Available: {total_available}'}), 400
    
    try:
        remaining_to_transfer = quantity
        
        # Transfer from warehouse batches (FIFO - oldest first)
        for item in sorted(warehouse_items, key=lambda x: x.received_date or datetime.min):
            if remaining_to_transfer <= 0:
                break
            
            transfer_from_this = min(remaining_to_transfer, item.quantity)
            item.quantity -= transfer_from_this
            item.updated_at = datetime.utcnow()
            remaining_to_transfer -= transfer_from_this
        
        target_product = Product.query.filter_by(branch_id=target_branch_id, barcode=product.barcode).first() if product.barcode else None
        if not target_product:
            target_product = Product.query.filter_by(branch_id=target_branch_id, name=product.name).first()

        if not target_product:
            target_barcode = product.barcode
            if target_barcode:
                barcode_conflict = Product.query.filter(
                    Product.barcode == target_barcode,
                    Product.branch_id != target_branch_id
                ).first()
                if barcode_conflict:
                    target_barcode = build_branch_scoped_barcode(target_barcode, target_branch)

            target_product = Product(
                barcode=target_barcode,
                name=product.name,
                price=product.price,
                cost=product.cost,
                stock=0,
                category=product.category,
                category_id=product.category_id,
                tax_rate=product.tax_rate,
                photo_filename=product.photo_filename,
                reorder_point=product.reorder_point,
                reorder_quantity=product.reorder_quantity,
                reorder_enabled=product.reorder_enabled,
                branch_id=target_branch_id
            )
            db.session.add(target_product)
            db.session.flush()

        # Update target branch stock
        target_product.stock += quantity
        
        # Record the transfer
        transfer = WarehouseTransfer(
            product_id=product_id,
            quantity=quantity,
            from_warehouse=True,
            batch_number=batch_number,
            performed_by=session.get('user_id'),
            branch_id=target_branch_id,
            notes=notes or f'Transferred from warehouse to {target_branch.name}'
        )
        db.session.add(transfer)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Transferred {quantity} units to {target_branch.name}',
            'new_main_stock': target_product.stock
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error transferring from warehouse: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to process transfer'}), 500

@app.route('/api/warehouse/transfers', methods=['GET'])
@manager_required
def api_warehouse_transfers():
    """Get transfer history"""
    branch_id = get_default_branch_id()
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    
    query = WarehouseTransfer.query.filter_by(branch_id=branch_id)
    transfers = query.order_by(WarehouseTransfer.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    total = query.count()
    
    return jsonify({
        'items': [{
            'id': t.id,
            'product_id': t.product_id,
            'product_name': t.product.name if t.product else 'Unknown',
            'barcode': t.product.barcode if t.product else None,
            'quantity': t.quantity,
            'from_warehouse': t.from_warehouse,
            'batch_number': t.batch_number,
            'performed_by': t.performer.username if t.performer else None,
            'notes': t.notes,
            'created_at': t.created_at.isoformat()
        } for t in transfers],
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page
    })

@app.route('/api/customers/<int:customer_id>', methods=['GET', 'PUT', 'DELETE'])
@manager_required
def api_single_customer(customer_id):
    branch_id = get_current_branch_id()
    customer = Customer.query.filter_by(id=customer_id, branch_id=branch_id).first()
    if not customer:
        return jsonify({'success': False, 'message': 'Customer not found'}), 404
    
    if request.method == 'GET':
        return jsonify({
            'id': customer.id,
            'name': customer.name,
            'phone': customer.phone,
            'email': customer.email,
            'address': customer.address,
            'created_at': customer.created_at.isoformat()
        })
    
    elif request.method == 'PUT':
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400
        
        customer.name = data.get('name', customer.name)
        customer.phone = data.get('phone', customer.phone)
        customer.email = data.get('email', customer.email)
        customer.address = data.get('address', customer.address)
        
        db.session.commit()
        return jsonify({'success': True, 'message': 'Customer updated'})
    
    elif request.method == 'DELETE':
        # Check if customer has outstanding debts
        total_debt = sum(d.balance for d in customer.debts if d.balance > 0)
        if total_debt > 0:
            return jsonify({'success': False, 'message': 'Cannot delete customer with outstanding debts'}), 400
        
        db.session.delete(customer)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Customer deleted'})

# Debt API Endpoints
@app.route('/api/debts', methods=['GET', 'POST'])
@manager_required
def api_debts():
    branch_id = get_current_branch_id()
    
    if request.method == 'GET':
        query = Debt.query.join(Customer, Debt.customer_id == Customer.id).filter(Debt.branch_id == branch_id)
        search_query = (request.args.get('q') or '').strip()
        debt_type = (request.args.get('type') or '').strip().lower()
        status = (request.args.get('status') or '').strip().lower()
        aging = (request.args.get('aging') or '').strip().lower()
        customer_id = request.args.get('customer_id')
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        min_amount = request.args.get('min_amount')
        max_amount = request.args.get('max_amount')

        if search_query:
            like_query = f"%{search_query}%"
            query = query.filter(
                (Customer.name.ilike(like_query)) |
                (Customer.phone.ilike(like_query)) |
                (Debt.notes.ilike(like_query))
            )

        # Status filters (all debts are now actual debts, no payment type)
        if status == 'open':
            query = query.filter(Debt.balance > 0)
        elif status == 'closed':
            query = query.filter(Debt.balance <= 0)
        elif status == 'pending':
            query = query.filter(Debt.balance == Debt.amount)
        elif status == 'partial':
            query = query.filter(Debt.balance > 0, Debt.balance < Debt.amount)
        elif status == 'overdue':
            query = query.filter(Debt.balance > 0, Debt.due_date != None, Debt.due_date < datetime.utcnow())

        if customer_id:
            query = query.filter(Debt.customer_id == customer_id)

        if start_date:
            try:
                start_dt = datetime.strptime(start_date, '%Y-%m-%d')
                query = query.filter(Debt.date >= start_dt)
            except ValueError:
                pass

        if end_date:
            try:
                end_dt = datetime.strptime(end_date, '%Y-%m-%d')
                query = query.filter(Debt.date <= end_dt)
            except ValueError:
                pass

        if min_amount:
            try:
                query = query.filter(Debt.amount >= float(min_amount))
            except ValueError:
                pass

        if max_amount:
            try:
                query = query.filter(Debt.amount <= float(max_amount))
            except ValueError:
                pass

        debts = query.order_by(Debt.date.desc(), Debt.id.desc()).all()
        
        # Filter by aging if specified
        if aging:
            filtered_debts = []
            for d in debts:
                days = calculate_debt_aging_days(d.date)
                aging_status = get_debt_aging_status(days)
                if aging_status == aging:
                    filtered_debts.append(d)
            debts = filtered_debts
        
        return jsonify([serialize_debt(d) for d in debts])
    
    elif request.method == 'POST':
        data = request.get_json() or {}
        if not data or not all(k in data for k in ['customer_id', 'amount']):
            return jsonify({'success': False, 'message': 'Missing required fields'}), 400

        customer = db.session.get(Customer, data['customer_id'])
        if not customer:
            return jsonify({'success': False, 'message': 'Customer not found'}), 404

        debt_type = (data.get('type') or 'debt').strip().lower()
        if debt_type != 'debt':
            return jsonify({'success': False, 'message': 'Only debt records can be created from this endpoint'}), 400

        sale_id = data.get('sale_id')
        if sale_id:
            sale = db.session.get(Sale, sale_id)
            if not sale:
                return jsonify({'success': False, 'message': 'Referenced sale not found'}), 404

        try:
            amount = to_decimal(data['amount'])
        except Exception:
            return jsonify({'success': False, 'message': 'Invalid amount value'}), 400

        if amount <= 0:
            return jsonify({'success': False, 'message': 'Amount must be greater than 0'}), 400

        # Parse due date if provided
        due_date = None
        if data.get('due_date'):
            try:
                due_date = datetime.fromisoformat(str(data['due_date']).replace('Z', '+00:00'))
            except ValueError:
                pass

        try:
            debt = Debt(
                customer_id=customer.id,
                sale_id=sale_id,
                amount=round_money(amount),
                balance=round_money(amount),
                due_date=due_date,
                status='pending',
                notes=(data.get('notes') or '').strip() or None,
                created_by=session.get('user_id'),
                branch_id=branch_id
            )
            db.session.add(debt)
            db.session.commit()
            return jsonify({'success': True, 'message': 'Debt record added', 'debt': serialize_debt(debt)}), 201
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error creating debt: {str(e)}")
            return jsonify({'success': False, 'message': 'Failed to create debt record'}), 500

@app.route('/api/debts/summary', methods=['GET'])
@manager_required
def api_debts_summary():
    """Get debt summary statistics"""
    branch_id = get_current_branch_id()
    # Get all outstanding debts (all debts are actual debts now, no type filter needed)
    outstanding_debts = Debt.query.filter(Debt.balance > 0, Debt.branch_id == branch_id).all()
    
    total_outstanding = sum(d.balance for d in outstanding_debts)
    total_debts = len(outstanding_debts)
    
    # Calculate aging breakdown
    aging_breakdown = {'current': 0, 'due_soon': 0, 'overdue': 0, 'critical': 0}
    aging_amounts = {'current': 0, 'due_soon': 0, 'overdue': 0, 'critical': 0}
    
    for d in outstanding_debts:
        days = calculate_debt_aging_days(d.date)
        aging_status = get_debt_aging_status(days)
        aging_breakdown[aging_status] += 1
        aging_amounts[aging_status] += d.balance
    
    # Get customers with outstanding debts
    customers_with_debt = set(d.customer_id for d in outstanding_debts)
    
    # Get this month's payments from DebtPayment table
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_payments = DebtPayment.query.filter(
        DebtPayment.payment_date >= month_start
    ).all()
    total_payments_this_month = sum(p.amount for p in monthly_payments)
    
    # Get overdue count
    overdue_count = sum(1 for d in outstanding_debts if d.due_date and d.due_date < now)
    
    return jsonify({
        'total_outstanding': money_float(total_outstanding),
        'total_debts': total_debts,
        'customers_with_debt': len(customers_with_debt),
        'aging_breakdown': aging_breakdown,
        'aging_amounts': {k: money_float(v) for k, v in aging_amounts.items()},
        'total_payments_this_month': money_float(total_payments_this_month),
        'overdue_count': overdue_count
    })

@app.route('/api/debts/aging', methods=['GET'])
@manager_required
def api_debts_aging():
    """Get debt aging analysis"""
    branch_id = get_current_branch_id()
    outstanding_debts = Debt.query.filter(Debt.balance > 0, Debt.branch_id == branch_id).all()
    
    aging_data = {
        'current': [],
        'due_soon': [],
        'overdue': [],
        'critical': []
    }
    
    for d in outstanding_debts:
        days = calculate_debt_aging_days(d.date)
        aging_status = get_debt_aging_status(days)
        debt_data = serialize_debt(d)
        aging_data[aging_status].append(debt_data)
    
    return jsonify(aging_data)

@app.route('/api/debts/export', methods=['GET'])
@manager_required
def export_debts():
    """Export debts to Excel"""
    branch_id = get_current_branch_id()
    query = Debt.query.join(Customer, Debt.customer_id == Customer.id).filter(Debt.branch_id == branch_id)
    
    # Apply filters
    status = request.args.get('status')
    customer_id = request.args.get('customer_id')
    
    if status == 'open':
        query = query.filter(Debt.balance > 0)
    elif status == 'closed':
        query = query.filter(Debt.balance <= 0)
    if customer_id:
        query = query.filter(Debt.customer_id == customer_id)
    
    debts = query.order_by(Debt.date.desc()).all()
    
    data = []
    for d in debts:
        days_outstanding = calculate_debt_aging_days(d.date)
        data.append({
            'ID': d.id,
            'Customer': d.customer.name if d.customer else 'Unknown',
            'Phone': d.customer.phone if d.customer else '',
            'Amount': d.amount,
            'Balance': d.balance,
            'Days Outstanding': days_outstanding,
            'Due Date': d.due_date.strftime('%Y-%m-%d') if d.due_date else '',
            'Status': calculate_debt_status(d),
            'Date': d.date.strftime('%Y-%m-%d %H:%M'),
            'Notes': d.notes or ''
        })
    
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, sheet_name='Debts', index=False)
    output.seek(0)
    
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    response.headers['Content-Disposition'] = 'attachment; filename=debts_report.xlsx'
    return response

@app.route('/api/debts/bulk', methods=['POST'])
@manager_required
def bulk_debt_operations():
    """Perform bulk operations on debts"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'No data provided'}), 400
    
    action = data.get('action')
    debt_ids = data.get('debt_ids', [])
    
    if not action or not debt_ids:
        return jsonify({'success': False, 'message': 'Action and debt IDs are required'}), 400
    
    debts = Debt.query.filter(Debt.id.in_(debt_ids)).all()
    if not debts:
        return jsonify({'success': False, 'message': 'No debts found'}), 404
    
    try:
        if action == 'mark_contacted':
            for d in debts:
                d.last_contacted_at = datetime.utcnow()
                if data.get('notes'):
                    existing_notes = d.communication_notes or ''
                    d.communication_notes = existing_notes + '\n' + data.get('notes') if existing_notes else data.get('notes')
            db.session.commit()
            return jsonify({'success': True, 'message': f'{len(debts)} debts marked as contacted'})
        
        elif action == 'update_due_date':
            due_date = data.get('due_date')
            if not due_date:
                return jsonify({'success': False, 'message': 'Due date is required'}), 400
            try:
                new_due_date = datetime.fromisoformat(str(due_date).replace('Z', '+00:00'))
            except ValueError:
                return jsonify({'success': False, 'message': 'Invalid date format'}), 400
            
            for d in debts:
                d.due_date = new_due_date
            db.session.commit()
            return jsonify({'success': True, 'message': f'{len(debts)} debts updated'})
        
        elif action == 'delete':
            deleted = 0
            for d in debts:
                # Skip debts with payments (balance less than original amount)
                if d.amount > d.balance:
                    continue  # Skip debts with payment history
                db.session.delete(d)
                deleted += 1
            db.session.commit()
            return jsonify({'success': True, 'message': f'{deleted} debts deleted'})
        
        else:
            return jsonify({'success': False, 'message': 'Unknown action'}), 400
    
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error in bulk operation: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to perform operation'}), 500

@app.route('/api/debts/<int:debt_id>', methods=['GET', 'PUT', 'DELETE'])
@manager_required
def api_single_debt(debt_id):
    debt = db.session.get(Debt, debt_id)
    if not debt:
        return jsonify({'success': False, 'message': 'Debt record not found'}), 404
    
    if request.method == 'GET':
        return jsonify(serialize_debt(debt))
    
    elif request.method == 'PUT':
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400
        
        debt.notes = data.get('notes', debt.notes)
        
        if data.get('due_date'):
            try:
                debt.due_date = datetime.fromisoformat(str(data['due_date']).replace('Z', '+00:00'))
            except ValueError:
                pass
        
        if data.get('communication_notes'):
            existing = debt.communication_notes or ''
            timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
            debt.communication_notes = f"{existing}\n[{timestamp}] {data['communication_notes']}".strip()
            debt.last_contacted_at = datetime.utcnow()
        
        debt.status = calculate_debt_status(debt)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Debt record updated', 'debt': serialize_debt(debt)})
    
    elif request.method == 'DELETE':
        if debt.amount > debt.balance:
            return jsonify({'success': False, 'message': 'Cannot delete debt record with existing payments'}), 400

        db.session.delete(debt)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Debt record deleted'})

@app.route('/api/customers/<int:customer_id>/debts', methods=['GET'])
@manager_required
def api_customer_debts(customer_id):
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return jsonify({'success': False, 'message': 'Customer not found'}), 404
    
    debts = Debt.query.filter_by(customer_id=customer_id).order_by(Debt.date.desc(), Debt.id.desc()).all()
    
    # Calculate customer summary - total outstanding debt balance
    total_debt = sum(d.balance for d in debts if d.balance > 0)
    
    # Calculate total paid from DebtPayment records
    total_paid = sum(p.amount for p in DebtPayment.query.filter_by(customer_id=customer_id).all())
    
    return jsonify({
        'customer': {
            'id': customer.id,
            'name': customer.name,
            'phone': customer.phone,
            'email': customer.email,
            'address': customer.address
        },
        'summary': {
            'total_outstanding': money_float(total_debt),
            'total_paid': money_float(total_paid)
        },
        'debts': [serialize_debt(d) for d in debts]
    })

@app.route('/api/debts/<int:debt_id>/payment', methods=['POST'])
@manager_required
def make_debt_payment(debt_id):
    debt = db.session.get(Debt, debt_id)
    if not debt:
        return jsonify({'success': False, 'message': 'Debt record not found'}), 404

    current_balance = to_decimal(debt.balance)
    if current_balance <= 0:
        return jsonify({'success': False, 'message': 'This debt is already fully paid'}), 400
    
    data = request.get_json()
    if not data or 'amount' not in data:
        return jsonify({'success': False, 'message': 'Payment amount is required'}), 400

    try:
        payment_amount = to_decimal(data['amount'])
    except Exception:
        return jsonify({'success': False, 'message': 'Invalid payment amount'}), 400

    if payment_amount <= 0:
        return jsonify({'success': False, 'message': 'Payment amount must be greater than 0'}), 400

    if payment_amount > current_balance:
        return jsonify({'success': False, 'message': 'Payment amount exceeds remaining balance'}), 400

    try:
        # Calculate new balance
        remaining_balance = (current_balance - payment_amount).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        
        # Get notes safely - handle None values
        payment_notes = data.get('notes')
        if payment_notes is not None:
            payment_notes = str(payment_notes).strip() or None
        
        # Create a payment record for tracking
        payment = DebtPayment(
            debt_id=debt.id,
            customer_id=debt.customer_id,
            amount=round_money(payment_amount),
            notes=payment_notes,
            processed_by=session.get('user_id'),
            branch_id=debt.branch_id
        )
        db.session.add(payment)
        
        # Update the original debt balance.
        # remaining_balance is already a Decimal quantized to MONEY_QUANT, so
        # money_float() stores it as a plain float consistent with the Float
        # column type (see round_money()/money_float() docstrings).
        debt.balance = money_float(remaining_balance)
        debt.status = calculate_debt_status(debt)
        
        # Track payment in communication notes
        payment_note = f"Payment of {format_currency(payment_amount)} received"
        if payment_notes:
            payment_note += f" - {payment_notes}"
        
        existing_notes = debt.communication_notes or ''
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
        if existing_notes:
            debt.communication_notes = f"{existing_notes}\n[{timestamp}] {payment_note}"
        else:
            debt.communication_notes = f"[{timestamp}] {payment_note}"

        db.session.commit()
        
        # Calculate total paid from payment records
        total_paid = sum(p.amount for p in debt.payments) if debt.payments else 0
        
        # Return payment confirmation
        return jsonify({
            'success': True, 
            'message': f'Payment of {format_currency(payment_amount)} recorded successfully',
            'remaining_balance': debt.balance,
            'total_paid': total_paid,
            'debt_status': debt.status
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error recording debt payment: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to record payment'}), 500

@app.route('/api/debts/<int:debt_id>/print', methods=['GET'])
@manager_required
def print_debt_receipt(debt_id):
    """Generate PDF receipt for debt payment"""
    debt = db.session.get(Debt, debt_id)
    if not debt:
        return jsonify({'success': False, 'message': 'Debt record not found'}), 404
    
    buffer = io.BytesIO()
    page_width = 80 * mm
    left_margin = 4 * mm
    right_margin = 4 * mm
    content_width = page_width - left_margin - right_margin
    
    doc = SimpleDocTemplate(buffer, pagesize=(page_width, 150*mm), 
                           rightMargin=right_margin, leftMargin=left_margin,
                           topMargin=4*mm, bottomMargin=4*mm)
    styles = getSampleStyleSheet()
    elements = []
    
    elements.append(Paragraph("PARROT POS", styles['Heading4']))
    elements.append(Paragraph("DEBT RECEIPT", styles['Normal']))
    elements.append(Spacer(1, 6))
    
    info = [
        ["Date:", debt.date.strftime("%Y-%m-%d %H:%M")],
        ["Customer:", debt.customer.name if debt.customer else "N/A"],
        ["Amount:", format_currency(debt.amount)],
        ["Balance:", format_currency(debt.balance)],
        ["Status:", calculate_debt_status(debt).upper()],
    ]
    if debt.notes:
        info.append(["Notes:", debt.notes[:50]])
    
    t = Table(info, colWidths=[content_width * 0.35, content_width * 0.65])
    t.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 8))
    elements.append(Paragraph("Thank you!", styles['Normal']))
    
    doc.build(elements)
    buffer.seek(0)
    
    response = make_response(buffer.getvalue())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'inline; filename=debt_receipt_{debt_id}.pdf'
    return response

# ============================================
# AI Agent API Endpoints
# ============================================

# Model registry for the AI tool container. This is an explicit allowlist of the
# domain models tools may reach, and it is kept to business data only. AppSetting
# is intentionally omitted: it stores credentials (the account-creation barrier
# hash, the AI API key, ...) that no agent tool should ever read or rewrite, so
# the Loli agent has no path to the barrier even if a future tool is added.
AI_MODELS = {
    'User': User,
    'Branch': Branch,
    'Category': Category,
    'Product': Product,
    'Supplier': Supplier,
    'PurchaseOrder': PurchaseOrder,
    'PurchaseOrderItem': PurchaseOrderItem,
    'SupplierCommunication': SupplierCommunication,
    'SupplierPriceAgreement': SupplierPriceAgreement,
    'WarehouseInventory': WarehouseInventory,
    'WarehouseTransfer': WarehouseTransfer,
    'Sale': Sale,
    'SaleItem': SaleItem,
    'Promotion': Promotion,
    'Customer': Customer,
    'Debt': Debt,
    'DebtPayment': DebtPayment,
    'Delivery': Delivery,
    'ReturnExchange': ReturnExchange,
    'ReturnExchangeItem': ReturnExchangeItem
}


def get_ai_orchestrator():
    """Get the current user's isolated AI conversation."""
    orchestrator = get_orchestrator(
        db, AI_MODELS, get_setting, app,
        conversation_id=session.get('user_id')
    )
    orchestrator.set_request_context({
        'branch_id': get_current_branch_id(),
        'user_id': session.get('user_id'),
        'role': session.get('role')
    })
    # SQLite-backed memory is always available; Mem0 upgrades it to semantic
    # retrieval when a local embedding backend is configured.
    orchestrator.memory_service = get_persistent_memory_service()
    return orchestrator


MEMORY_SCOPES = {'private', 'branch_shared'}


def get_persistent_memory_service():
    """Load the optional local-memory integration without making app startup depend on it."""
    try:
        from ai_memory_service import get_memory_service
        service = get_memory_service(
            db=db, registry_model=MemoryRegistry, audit_model=MemoryAudit
        )
        if service is None:
            return None
        return service
    except (ImportError, ModuleNotFoundError) as exc:
        app.logger.info('Persistent memory is unavailable: %s', exc)
    except Exception as exc:
        app.logger.warning('Persistent memory failed to initialize: %s', exc)
    return None


def memory_scope_from_request(data=None):
    value = ((data or {}).get('scope') or request.args.get('scope') or 'private').strip().lower()
    return value if value in MEMORY_SCOPES else None


def may_manage_memory_scope(scope):
    return scope != 'branch_shared' or session.get('role') in ('manager', 'boss')


def record_memory_audit(action, memory_id=None, scope=None, details=None):
    """Audit metadata only; submitted and recalled memory text must not enter this log."""
    db.session.add(MemoryAudit(
        actor_user_id=session.get('user_id'),
        branch_id=get_current_branch_id(),
        memory_id=str(memory_id)[:191] if memory_id else None,
        action=action[:30],
        scope=scope,
        details=(details or '')[:500] or None,
    ))


def visible_memory_registry_query(scope=None):
    branch_id = get_current_branch_id()
    user_id = session.get('user_id')
    query = MemoryRegistry.query.filter_by(branch_id=branch_id)
    if scope == 'private':
        return query.filter_by(user_id=user_id, scope='private')
    if scope == 'branch_shared':
        return query.filter_by(scope='branch_shared')
    return query.filter(or_(MemoryRegistry.user_id == user_id, MemoryRegistry.scope == 'branch_shared'))


def capture_low_risk_memory(command):
    """Allow Loli to learn only explicit, preference-like statements after a successful turn."""
    service = get_persistent_memory_service()
    if not service or not getattr(service, 'should_auto_save', lambda _: False)(command):
        return
    try:
        result = service.remember(
            content=command, user_id=session['user_id'], branch_id=get_current_branch_id(),
            scope='private', source='automatic', explicit=False, allow_auto=True,
            db=db, registry_model=MemoryRegistry, audit_model=MemoryAudit,
        )
        if isinstance(result, dict) and result.get('saved'):
            db.session.commit()
    except Exception as exc:
        db.session.rollback()
        app.logger.info('Optional low-risk AI memory was not saved: %s', exc)


@app.route('/api/agent/memories', methods=['GET', 'POST'])
@login_required
def api_agent_memories():
    """List or explicitly save memory, bounded to the active user and branch."""
    # Authorization must precede optional-backend availability so a disabled
    # feature cannot reveal whether a caller was permitted to share memory.
    if request.method == 'POST':
        requested_scope = memory_scope_from_request(request.get_json(silent=True) or {})
        if requested_scope == 'branch_shared' and not may_manage_memory_scope(requested_scope):
            return jsonify({'success': False, 'error': 'Manager access required for branch-shared memory'}), 403
    service = get_persistent_memory_service()
    if not service:
        return jsonify({'success': False, 'error': 'Memory service is unavailable'}), 503

    if request.method == 'GET':
        scope = memory_scope_from_request()
        if not scope and request.args.get('scope'):
            return jsonify({'success': False, 'error': 'Invalid memory scope'}), 400
        limit = min(max(request.args.get('limit', 50, type=int) or 50, 1), 100)
        memories = [entry.to_dict() for entry in visible_memory_registry_query(scope)
                    .order_by(MemoryRegistry.updated_at.desc()).limit(limit).all()]
        return jsonify({'success': True, 'memories': memories})

    data = request.get_json(silent=True) or {}
    content = data.get('content')
    scope = memory_scope_from_request(data)
    if not scope:
        return jsonify({'success': False, 'error': 'Invalid memory scope'}), 400
    if not isinstance(content, str) or not content.strip():
        return jsonify({'success': False, 'error': 'Memory content is required'}), 400
    if len(content) > 4000:
        return jsonify({'success': False, 'error': 'Memory content is too long'}), 400
    if not may_manage_memory_scope(scope):
        return jsonify({'success': False, 'error': 'Manager access required for branch-shared memory'}), 403

    try:
        result = service.remember(
            content=content.strip(), user_id=session['user_id'], branch_id=get_current_branch_id(),
            scope=scope, source='manual', db=db,
            registry_model=MemoryRegistry, audit_model=MemoryAudit,
        )
        if result is False or result is None:
            return jsonify({'success': False, 'error': 'Memory service did not save the memory'}), 503
        memory_id = str((result.get('id') or result.get('memory_id')) if isinstance(result, dict) else result)
        if not memory_id or memory_id == 'None':
            return jsonify({'success': False, 'error': 'Memory service returned no memory identifier'}), 502
        entry = MemoryRegistry.query.filter_by(memory_id=memory_id).first()
        if not entry:
            # Do not duplicate raw user input in the SQL registry.  A service
            # may return a separately sanitized label after its validation.
            safe_summary = (result.get('summary') if isinstance(result, dict) else None) or 'Manual memory'
            entry = MemoryRegistry(memory_id=memory_id, user_id=session['user_id'],
                                   branch_id=get_current_branch_id(), scope=scope,
                                   summary=str(safe_summary)[:500], source='manual')
            db.session.add(entry)
        record_memory_audit('created', memory_id, scope)
        db.session.commit()
        return jsonify({'success': True, 'memory': entry.to_dict()}), 201
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        app.logger.warning('Memory save failed: %s', exc)
        return jsonify({'success': False, 'error': 'Unable to save memory'}), 503


@app.route('/api/agent/memories/<string:memory_id>', methods=['DELETE'])
@login_required
def api_delete_agent_memory(memory_id):
    entry = visible_memory_registry_query().filter_by(memory_id=memory_id).first()
    if not entry:
        return jsonify({'success': False, 'error': 'Memory not found'}), 404
    if entry.scope == 'branch_shared' and not may_manage_memory_scope(entry.scope):
        return jsonify({'success': False, 'error': 'Manager access required for branch-shared memory'}), 403
    service = get_persistent_memory_service()
    if not service:
        return jsonify({'success': False, 'error': 'Memory service is unavailable'}), 503
    try:
        result = service.forget(memory_id=entry.memory_id, user_id=session['user_id'],
                                branch_id=entry.branch_id, scope=entry.scope, db=db,
                                registry_model=MemoryRegistry, audit_model=MemoryAudit)
        if result is False or (isinstance(result, dict) and not result.get('deleted', False)):
            return jsonify({'success': False, 'error': 'Memory service did not remove the memory'}), 503
        db.session.delete(entry)
        record_memory_audit('deleted', memory_id, entry.scope)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Memory deleted'})
    except Exception as exc:
        db.session.rollback()
        app.logger.warning('Memory deletion failed: %s', exc)
        return jsonify({'success': False, 'error': 'Unable to delete memory'}), 503


@app.route('/api/agent/memories/forget', methods=['POST'])
@login_required
def api_forget_agent_memory():
    """Compatibility endpoint for a UI forget action; delegates to the same guarded delete path."""
    memory_id = (request.get_json(silent=True) or {}).get('memory_id')
    if not isinstance(memory_id, str) or not memory_id.strip():
        return jsonify({'success': False, 'error': 'memory_id is required'}), 400
    return api_delete_agent_memory(memory_id.strip())


@app.route('/api/agent/memories/forget-all', methods=['POST'])
@login_required
def api_forget_all_agent_memories():
    """Forget only the current user's private memories in the active branch."""
    entries = visible_memory_registry_query('private').all()
    service = get_persistent_memory_service()
    if not service:
        return jsonify({'success': False, 'error': 'Memory service is unavailable'}), 503
    try:
        for entry in entries:
            result = service.forget(
                memory_id=entry.memory_id, user_id=session['user_id'], branch_id=entry.branch_id,
                scope='private', db=db, registry_model=MemoryRegistry, audit_model=MemoryAudit,
            )
            if not isinstance(result, dict) or not result.get('deleted'):
                raise RuntimeError('Memory service did not remove a private memory')
            db.session.delete(entry)
            record_memory_audit('deleted', entry.memory_id, 'private', 'bulk forget')
        db.session.commit()
        return jsonify({'success': True, 'message': f'Forgot {len(entries)} private memories'})
    except Exception as exc:
        db.session.rollback()
        app.logger.warning('Bulk memory deletion failed: %s', exc)
        return jsonify({'success': False, 'error': 'Unable to forget private memories'}), 503


@app.route('/api/agent/chat', methods=['POST'])
@login_required
def agent_chat():
    """
    Process a chat command through the AI Agent
    Expects JSON: {"command": "your command here"}
    """
    try:
        data = request.get_json()
        if not data or 'command' not in data:
            return jsonify({
                'success': False,
                'error': 'Missing required field: command'
            }), 400

        command = data['command'].strip()
        if not command:
            return jsonify({
                'success': False,
                'error': 'Command cannot be empty'
            }), 400

        # Process the command through the agent
        orchestrator = get_ai_orchestrator()
        result = orchestrator.process_command(command, session.get('user_id'))
        if result.get('success'):
            capture_low_risk_memory(command)

        # GOAL 1: persist plan-bearing results so proposals can be approved later.
        if isinstance(result.get('plan'), list) and isinstance(result.get('step_results'), list):
            try:
                has_proposals = any(sr.get('status') == 'proposal' for sr in result['step_results'])
                task = AgentTask(
                    user_id=session.get('user_id'),
                    command=command,
                    plan_json=json.dumps(result.get('plan')),
                    status='pending_approval' if has_proposals else (
                        'completed' if result.get('success') else 'failed'),
                    step_results_json=json.dumps(result.get('step_results')),
                )
                db.session.add(task)
                db.session.commit()
                result['task_id'] = task.id
            except Exception as persist_error:
                db.session.rollback()
                app.logger.warning(f"AgentTask persistence failed: {persist_error}")

        return jsonify(result)

    except Exception as e:
        app.logger.error(f"AI Agent error: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e),
            'message': 'An error occurred while processing your request.'
        }), 500


@app.route('/api/agent/status', methods=['GET'])
@manager_required
def agent_status():
    """Get the current status of the AI Agent"""
    try:
        orchestrator = get_ai_orchestrator()
        status = orchestrator.get_status()
        return jsonify({
            'success': True,
            'status': status
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/agent/history', methods=['GET'])
@manager_required
def agent_history():
    """Get the conversation history (truncated for display)"""
    try:
        orchestrator = get_ai_orchestrator()
        history = orchestrator.get_conversation_history()
        return jsonify({
            'success': True,
            'history': history
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/agent/clear', methods=['POST'])

# ---------------------------------------------------------------------------
# GOAL 1: Agent task persistence + approval flow
#
# APPROVAL EXECUTION (implemented): POST /api/agent/approve/<task_id>/<step_no>
# records the approval on the persisted task (step_results_json marks the step
# "approved"). POST /api/agent/task/<id>/advance then calls
# orchestrator.run_approved_plan() with the persisted plan and the approved
# step numbers: the plan is re-run deterministically (fresh read data, zero
# LLM calls) and ONLY the approved mutating steps touch the database, tagged
# "executed_by": "approved". Unapproved proposals stay proposals; proposals
# expire after AI_APPROVAL_TTL_HOURS (default 24h).
# ---------------------------------------------------------------------------

def _agent_task_step_results(task):
    """Load step_results list from a task's JSON blob (never mutates)."""
    try:
        results = json.loads(task.step_results_json) if task.step_results_json else []
    except (ValueError, TypeError):
        results = []
    return results if isinstance(results, list) else []


@app.route('/api/agent/approve/<int:task_id>/<int:step_no>', methods=['POST'])
@login_required
def api_agent_approve_step(task_id, step_no):
    """Record approval for ONE proposal step of a persisted agent task."""
    task = AgentTask.query.get_or_404(task_id)
    if task.user_id != session.get('user_id'):
        return jsonify({'success': False, 'error': 'Forbidden'}), 403

    step_results = _agent_task_step_results(task)
    if not (0 <= step_no < len(step_results)):
        return jsonify({'success': False, 'error': 'Invalid step number'}), 400

    step = step_results[step_no]
    if step.get('status') != 'proposal':
        return jsonify({'success': False,
                        'error': f"Step {step_no} is not a pending proposal "
                                 f"(status={step.get('status')})"}), 409

    # Safety rail: do not accept approvals for expired proposals.
    ttl_hours = _agent_approval_ttl_hours()
    if ttl_hours > 0:
        age_hours = (datetime.utcnow() - (task.created_at or datetime.utcnow())
                     ).total_seconds() / 3600.0
        if age_hours > ttl_hours:
            task.status = 'expired'
            task.updated_at = datetime.utcnow()
            db.session.commit()
            return jsonify({
                'success': False,
                'error': (f'This proposal expired after {int(ttl_hours)}h. '
                          'Ask Loli again and approve the fresh plan.'),
            }), 410

    # Store approval state; actual execution happens via /advance.
    step['approved'] = True
    task.step_results_json = json.dumps(step_results)
    task.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True, 'task': task.to_dict(),
                    'message': f"Step {step_no} approved. Send /advance to execute."})


def _agent_approval_ttl_hours():
    """Proposal expiry window in hours (AI_APPROVAL_TTL_HOURS env, default 24).

    A value <= 0 disables expiry entirely.
    """
    try:
        return float(os.environ.get('AI_APPROVAL_TTL_HOURS', '24'))
    except (TypeError, ValueError):
        return 24.0


@app.route('/api/agent/reject/<int:task_id>/<int:step_no>', methods=['POST'])
@login_required
def api_agent_reject_step(task_id, step_no):
    """Record rejection for ONE proposal step of a persisted agent task."""
    task = AgentTask.query.get_or_404(task_id)
    if task.user_id != session.get('user_id'):
        return jsonify({'success': False, 'error': 'Forbidden'}), 403

    step_results = _agent_task_step_results(task)
    if not (0 <= step_no < len(step_results)):
        return jsonify({'success': False, 'error': 'Invalid step number'}), 400

    step = step_results[step_no]
    if step.get('status') != 'proposal':
        return jsonify({'success': False,
                        'error': f"Step {step_no} is not a pending proposal "
                                 f"(status={step.get('status')})"}), 409

    step['rejected'] = True
    task.step_results_json = json.dumps(step_results)
    approved_left = any(sr.get('approved') for sr in step_results
                        if sr.get('status') == 'proposal')
    undecided_left = any(not sr.get('approved') and not sr.get('rejected')
                         for sr in step_results
                         if sr.get('status') == 'proposal')
    if not approved_left and not undecided_left:
        task.status = 'rejected'
    task.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True, 'task': task.to_dict(),
                    'message': f"Step {step_no} rejected."})


@app.route('/api/agent/task/<int:task_id>/advance', methods=['POST'])
@login_required
def api_agent_advance_task(task_id):
    """Deterministically execute the approved proposal steps of a stored plan.

    Zero LLM involvement: the persisted plan's own arguments are re-run with
    fresh read data; only the human-approved mutating steps execute.
    """
    task = AgentTask.query.get_or_404(task_id)
    if task.user_id != session.get('user_id'):
        # Fail closed: a missing user_id must never open access to a task.
        return jsonify({'success': False, 'error': 'Forbidden'}), 403

    step_results = _agent_task_step_results(task)
    approved_nos = sorted({sr.get('step') for sr in step_results
                           if sr.get('status') == 'proposal' and sr.get('approved')
                           and isinstance(sr.get('step'), int)})
    if not approved_nos:
        return jsonify({'success': False, 'error': 'No approved steps to advance'}), 409

    # Safety rail: stale proposals must not run against changed data.
    ttl_hours = _agent_approval_ttl_hours()
    age_hours = (datetime.utcnow() - (task.created_at or datetime.utcnow())
                 ).total_seconds() / 3600.0
    if ttl_hours > 0 and age_hours > ttl_hours:
        task.status = 'expired'
        task.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({
            'success': False,
            'error': (f'This proposal expired after {int(ttl_hours)}h. '
                      'Ask Loli again and approve the fresh plan.'),
        }), 410

    try:
        plan = json.loads(task.plan_json) if task.plan_json else None
    except (ValueError, TypeError):
        plan = None

    # Atomic claim: only one concurrent request may flip this task into
    # 'executing' — prevents double execution of approved writes.
    claimed = AgentTask.query.filter(
        AgentTask.id == task.id,
        AgentTask.status != 'executing',
    ).update({'status': 'executing', 'updated_at': datetime.utcnow()})
    db.session.commit()
    if not claimed:
        return jsonify({'success': False, 'error': 'Task is already executing'}), 409

    try:
        orchestrator = get_ai_orchestrator()
        result = orchestrator.run_approved_plan(task.command, plan or {}, approved_nos)

        if isinstance(result.get('step_results'), list):
            task.step_results_json = json.dumps(result['step_results'])
        still_pending = any(sr.get('status') == 'proposal'
                            for sr in _agent_task_step_results(task))
        if result.get('success') and not still_pending:
            task.status = 'completed'
        elif result.get('success'):
            task.status = 'pending_approval'
        else:
            task.status = 'failed'
        db.session.commit()
    except Exception as exc:
        app.logger.error(f"Agent task advance error: {exc}")
        # Never leave the task bricked in 'executing'.
        task.status = 'failed'
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 500

    result['task_id'] = task.id
    return jsonify(result)


@app.route('/api/agent/task/<int:task_id>', methods=['GET'])
@login_required
def api_agent_get_task(task_id):
    """Return plan + step statuses + pending proposals for the widget."""
    task = AgentTask.query.get_or_404(task_id)
    if task.user_id != session.get('user_id'):
        return jsonify({'success': False, 'error': 'Forbidden'}), 403
    data = task.to_dict()
    data['success'] = True
    data['pending_proposals'] = [
        {'step_no': i, **sr}
        for i, sr in enumerate(data.get('step_results') or [])
        if sr.get('status') == 'proposal' and not sr.get('approved')
    ]
    return jsonify(data)

@app.route('/api/agent/autonomy', methods=['GET'])
@login_required
def api_agent_autonomy_get():
    """Report whether agent autonomy (kill switch) is enabled."""
    return jsonify({'success': True, 'enabled': get_agent_autonomy_enabled()})


@app.route('/api/agent/autonomy', methods=['POST'])
@manager_required
def api_agent_autonomy_set():
    """Enable/disable the agent autonomy kill switch (manager only)."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get('enabled'), bool):
        return jsonify({'success': False,
                        'error': "Request body must be JSON with a boolean 'enabled' field"}), 400
    enabled = payload['enabled']
    try:
        set_setting('agent_autonomy_enabled', 'true' if enabled else 'false')
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'enabled': enabled})


@manager_required
def agent_clear():
    """Clear the conversation history"""
    try:
        orchestrator = get_ai_orchestrator()
        orchestrator.clear_conversation()
        return jsonify({
            'success': True,
            'message': 'Conversation history cleared'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/healthz', methods=['GET'])
def healthcheck():
    """Lightweight container health endpoint without database side effects."""
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8888, debug=os.environ.get('FLASK_DEBUG') == '1')
