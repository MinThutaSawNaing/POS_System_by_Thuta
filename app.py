from flask import Flask, render_template, request, jsonify, session, redirect, url_for, make_response, send_from_directory, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
import os
import uuid
import io
from sqlalchemy import inspect, text, func
from decimal import Decimal, ROUND_HALF_UP
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib import colors
import pandas as pd
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing
from datetime import datetime, timedelta
import pytz
from functools import wraps
from reportlab.graphics.barcode import createBarcodeDrawing

# Import AI Agent modules
from agent_orchestrator import get_orchestrator

app = Flask(__name__)
app.secret_key = 'your_super_secret_key_here'  # Change this in production!
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'uploads', 'products')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB per request
db = SQLAlchemy(app)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

MONEY_QUANT = Decimal('0.01')
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
CURRENCY_OPTIONS = {
    'USD': '$',
    'MMK': 'MMK',
    'THB': 'THB'
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

def round_money(value):
    return float(to_decimal(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP))

def get_setting(key, default=None):
    setting = AppSetting.query.filter_by(key=key).first()
    return setting.value if setting else default

def set_setting(key, value):
    setting = AppSetting.query.filter_by(key=key).first()
    if setting:
        setting.value = value
    else:
        setting = AppSetting(key=key, value=value)
        db.session.add(setting)
    db.session.commit()

def get_currency_code():
    code = get_setting('currency_code', 'USD')
    return code if code in CURRENCY_OPTIONS else 'USD'

def get_currency_suffix(currency_code=None):
    code = currency_code or get_currency_code()
    return CURRENCY_OPTIONS.get(code, '$')

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

def build_inventory_alert_payload():
    low_stock_items = []
    out_of_stock_count = 0

    products = Product.query.order_by(Product.name.asc()).all()
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

def save_product_image(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_image_file(file_storage.filename):
        raise ValueError('Only image files are allowed (png, jpg, jpeg, gif, webp)')

    original = secure_filename(file_storage.filename)
    extension = original.rsplit('.', 1)[1].lower()
    unique_name = f"{uuid.uuid4().hex}.{extension}"
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
    file_storage.save(file_path)
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
        'category': product.category,
        'tax_rate': product.tax_rate,
        'reorder_point': product.reorder_point,
        'reorder_quantity': product.reorder_quantity,
        'reorder_enabled': bool(product.reorder_enabled),
        'photo_filename': product.photo_filename,
        'photo_url': product_photo_url(product.photo_filename)
    }

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
    if debt.balance < debt.amount:
        return 'partial'
    if debt.due_date and datetime.utcnow() > debt.due_date:
        return 'overdue'
    return 'pending'

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

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(50), unique=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    cost = db.Column(db.Float)
    stock = db.Column(db.Integer, default=0)
    category = db.Column(db.String(50))
    tax_rate = db.Column(db.Float, default=0.0)
    photo_filename = db.Column(db.String(255))
    reorder_point = db.Column(db.Integer, default=10)
    reorder_quantity = db.Column(db.Integer, default=50)
    reorder_enabled = db.Column(db.Boolean, default=True)

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
    category = db.Column(db.String(50))
    tax_id = db.Column(db.String(50))
    website = db.Column(db.String(200))
    bank_name = db.Column(db.String(100))
    bank_account = db.Column(db.String(50))
    quality_rating = db.Column(db.Float, default=0.0)
    delivery_rating = db.Column(db.Float, default=0.0)
    total_orders = db.Column(db.Integer, default=0)
    on_time_deliveries = db.Column(db.Integer, default=0)
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    product = db.relationship('Product', backref='transfers')
    performer = db.relationship('User', backref='warehouse_transfers')

class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.String(36), unique=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    total = db.Column(db.Float, nullable=False)
    tax = db.Column(db.Float, nullable=False)
    cash_received = db.Column(db.Float)
    refund_amount = db.Column(db.Float, default=0.0)
    payment_method = db.Column(db.String(20))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
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
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sale = db.relationship('Sale', backref=db.backref('delivery', uselist=False))
    customer = db.relationship('Customer', backref='deliveries')
    creator = db.relationship('User', backref='created_deliveries')

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
    db.create_all()
    inspector = inspect(db.engine)
    product_columns = [col['name'] for col in inspector.get_columns('product')]
    if 'photo_filename' not in product_columns:
        db.session.execute(text('ALTER TABLE product ADD COLUMN photo_filename VARCHAR(255)'))
        db.session.commit()
    product_migrations = [
        ('reorder_point', 'ALTER TABLE product ADD COLUMN reorder_point INTEGER DEFAULT 10'),
        ('reorder_quantity', 'ALTER TABLE product ADD COLUMN reorder_quantity INTEGER DEFAULT 50'),
        ('reorder_enabled', 'ALTER TABLE product ADD COLUMN reorder_enabled BOOLEAN DEFAULT 1')
    ]
    for column_name, migration_sql in product_migrations:
        if column_name not in product_columns:
            db.session.execute(text(migration_sql))
            db.session.commit()

    db.session.execute(text('UPDATE product SET reorder_point = 10 WHERE reorder_point IS NULL'))
    db.session.execute(text('UPDATE product SET reorder_quantity = 50 WHERE reorder_quantity IS NULL'))
    db.session.execute(text('UPDATE product SET reorder_enabled = 1 WHERE reorder_enabled IS NULL'))
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
        ('refund_amount', 'ALTER TABLE sale ADD COLUMN refund_amount FLOAT DEFAULT 0')
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

    if not AppSetting.query.filter_by(key='currency_code').first():
        db.session.add(AppSetting(key='currency_code', value='USD'))
        db.session.commit()

    # Performance indexes (safe for repeated startup)
    performance_indexes = [
        'CREATE INDEX IF NOT EXISTS idx_product_name ON product(name)',
        'CREATE INDEX IF NOT EXISTS idx_product_category ON product(category)',
        'CREATE INDEX IF NOT EXISTS idx_sale_date ON sale(date)',
        'CREATE INDEX IF NOT EXISTS idx_sale_user_date ON sale(user_id, date)',
        'CREATE INDEX IF NOT EXISTS idx_sale_item_sale_id ON sale_item(sale_id)',
        'CREATE INDEX IF NOT EXISTS idx_sale_item_product_id ON sale_item(product_id)',
        'CREATE INDEX IF NOT EXISTS idx_debt_customer_balance ON debt(customer_id, balance)',
        'CREATE INDEX IF NOT EXISTS idx_debt_status_date ON debt(status, date)',
        'CREATE INDEX IF NOT EXISTS idx_purchase_order_status_created ON purchase_order(status, created_at)',
        'CREATE INDEX IF NOT EXISTS idx_delivery_stage_priority ON delivery(stage, priority)',
        'CREATE INDEX IF NOT EXISTS idx_delivery_created_at ON delivery(created_at)',
        'CREATE INDEX IF NOT EXISTS idx_warehouse_product_qty ON warehouse_inventory(product_id, quantity)'
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
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = user.role
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
            'ai_api_key_configured': bool(ai_api_key and len(ai_api_key) > 10)
        })

    if session.get('role') != 'manager':
        return jsonify({'success': False, 'message': 'Manager access required'}), 403

    data = request.get_json() or {}
    
    # Handle currency code update
    currency_code = data.get('currency_code')
    if currency_code is not None:
        if currency_code not in CURRENCY_OPTIONS:
            return jsonify({'success': False, 'message': 'Invalid currency code'}), 400
        set_setting('currency_code', currency_code)
        return jsonify({
            'success': True,
            'message': 'Settings updated',
            'currency_code': currency_code,
            'currency_suffix': get_currency_suffix(currency_code)
        })
    
    # Handle AI API key update
    ai_api_key = data.get('ai_api_key')
    if ai_api_key is not None:
        if ai_api_key == "":
            # Clear the API key
            set_setting('ai_api_key', '')
            # Also update environment variable for current session
            os.environ.pop('APIFREE_API_KEY', None)
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

    with open(db_file_path, 'rb') as f:
        db_bytes = f.read()

    backup_filename = f"pos_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.db"
    return send_file(
        io.BytesIO(db_bytes),
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name=backup_filename
    )

@app.route('/api/inventory/alerts', methods=['GET'])
@manager_required
def api_inventory_alerts():
    return jsonify(build_inventory_alert_payload())

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

    payload = build_inventory_alert_payload()
    suggested_items = payload.get('suggested_purchase_order', {}).get('items', [])
    if not suggested_items:
        return jsonify({'success': False, 'message': 'No low-stock items to generate purchase order'}), 400

    try:
        po = PurchaseOrder(
            po_number=generate_po_number(),
            supplier_id=supplier.id,
            status='draft',
            notes=(data.get('notes') or '').strip() or 'System generated from inventory alerts',
            created_by=session.get('user_id')
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

@app.route('/public/<path:filename>')
def public_file(filename):
    return send_from_directory(os.path.join(app.root_path, 'public'), filename)

# Product API Endpoints
@app.route('/api/products', methods=['GET', 'POST'])
def api_products():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        q = (request.args.get('q') or '').strip()
        page = request.args.get('page', type=int)
        per_page = request.args.get('per_page', type=int)

        query = Product.query
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
            price = float(price)
            stock = int(stock)
            cost = float(data.get('cost', 0) or 0)
            tax_rate = float(data.get('tax_rate', 0) or 0)
            reorder_point = max(int(data.get('reorder_point', 10) or 0), 0)
            reorder_quantity = max(int(data.get('reorder_quantity', 50) or 0), 0)
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': 'Invalid numeric values'}), 400

        reorder_enabled = to_bool(data.get('reorder_enabled'), True)

        photo_filename = None
        photo_file = request.files.get('photo') if is_multipart else None
        if photo_file and photo_file.filename:
            try:
                photo_filename = save_product_image(photo_file)
            except ValueError as e:
                return jsonify({'success': False, 'message': str(e)}), 400

        product = Product(
            barcode=data.get('barcode'),
            name=name,
            price=price,
            cost=cost,
            stock=stock,
            category=data.get('category'),
            tax_rate=tax_rate,
            reorder_point=reorder_point,
            reorder_quantity=reorder_quantity,
            reorder_enabled=reorder_enabled,
            photo_filename=photo_filename
        )
        db.session.add(product)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Product added'}), 201

# --- Single Product Endpoint (GET, PUT, DELETE) ---
@app.route('/api/products/<int:product_id>', methods=['GET', 'PUT', 'DELETE'])
def api_single_product(product_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    product = db.session.get(Product, product_id)
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
            'category': product.category,
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

        # Check for duplicate barcode
        if 'barcode' in data and data['barcode']:
            existing = Product.query.filter(Product.barcode == data['barcode'], Product.id != product_id).first()
            if existing:
                return jsonify({'success': False, 'message': 'Barcode already in use'}), 400
            product.barcode = data['barcode']

        if 'name' in data:
            product.name = data.get('name', product.name)
        if 'price' in data:
            try:
                product.price = float(data.get('price'))
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': 'Invalid price value'}), 400
        if 'cost' in data:
            try:
                product.cost = float(data.get('cost') or 0)
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': 'Invalid cost value'}), 400
        if 'stock' in data:
            try:
                product.stock = int(data.get('stock'))
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': 'Invalid stock value'}), 400

        product.category = data.get('category', product.category)
        if 'tax_rate' in data:
            try:
                product.tax_rate = float(data.get('tax_rate') or 0)
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': 'Invalid tax rate value'}), 400
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
            except ValueError as e:
                return jsonify({'success': False, 'message': str(e)}), 400

        db.session.commit()
        return jsonify({'success': True, 'message': 'Product updated'})

    elif request.method == 'DELETE':
        if product.photo_filename:
            delete_product_image(product.photo_filename)
        db.session.delete(product)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Product deleted'})

@app.route('/api/products/search', methods=['GET'])
def api_search_products():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    products = Product.query.filter(
        (Product.name.ilike(f'%{query}%')) | 
        (Product.barcode.ilike(f'%{query}%')) |
        (Product.category.ilike(f'%{query}%'))
    ).limit(10).all()
    return jsonify([{
        'id': p.id,
        'barcode': p.barcode,
        'name': p.name,
        'price': p.price,
        'stock': p.stock,
        'photo_url': product_photo_url(p.photo_filename)
    } for p in products])

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

    try:
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

        payment_method = data.get('payment_method', 'cash')
        cash_received_raw = data.get('cash_received')
        cash_received = None
        refund_amount = 0.0

        if payment_method == 'cash':
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
            transaction_id=str(uuid.uuid4()),
            date=sale_time,
            total=total_rounded,
            tax=round_money(tax_total),
            cash_received=cash_received,
            refund_amount=refund_amount,
            payment_method=payment_method,
            user_id=session['user_id']
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
            # Update product stock
            item['product'].stock -= item['quantity']

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
                notes=f'Sale transaction {sale.transaction_id}'
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
                created_by=session.get('user_id')
            )
            db.session.add(delivery)

        db.session.commit()

        return jsonify({
            'success': True,
            'message': 'Sale completed',
            'transaction_id': sale.transaction_id,
            'delivery_number': sale.delivery.delivery_number if getattr(sale, 'delivery', None) else None
        }), 201

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error creating sale: {str(e)}")
        return jsonify({'success': False, 'message': f'Error creating sale: {str(e)}'}), 500

# --- Get Single Sale with Items ---
@app.route('/api/sales/<string:transaction_id>', methods=['GET'])
def api_single_sale(transaction_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    sale = Sale.query.filter_by(transaction_id=transaction_id).first()
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
        product = Product.query.get(item.product_id)
        already_returned = returned_qty_map.get(item.id, 0)
        available_to_return = max(item.quantity - already_returned, 0)
        sale_data['items'].append({
            'sale_item_id': item.id,
            'product_id': item.product_id,
            'name': product.name,
            'price': item.price,
            'quantity': item.quantity,
            'tax': item.tax,
            'tax_rate': product.tax_rate,
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
            'product_name': item.product.name if item.product else 'Unknown',
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

    if request.method == 'GET':
        query = Delivery.query
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
        created_by=session.get('user_id')
    )
    db.session.add(delivery)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Delivery created', 'delivery': serialize_delivery(delivery)}), 201

@app.route('/api/deliveries/<int:delivery_id>', methods=['GET', 'PUT'])
def api_single_delivery(delivery_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    delivery = db.session.get(Delivery, delivery_id)
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

    deliveries = Delivery.query.all()
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

# --- PDF Receipt ---
@app.route('/api/sales/<string:transaction_id>/print', methods=['GET'])
def print_receipt(transaction_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    sale = Sale.query.filter_by(transaction_id=transaction_id).first()
    if not sale:
        return jsonify({'success': False, 'message': 'Sale not found'}), 404

    items = SaleItem.query.filter_by(sale_id=sale.id).all()
    buffer = io.BytesIO()

    # 80mm thermal slip layout
    page_width = 80 * mm
    left_margin = 4 * mm
    right_margin = 4 * mm
    top_margin = 4 * mm
    bottom_margin = 4 * mm
    content_width = page_width - left_margin - right_margin

    extra_info_lines = 4 + (2 if sale.payment_method == 'cash' else 0)
    estimated_height_mm = max(120, 42 + (len(items) * 8) + (extra_info_lines * 4))
    page_height = estimated_height_mm * mm

    doc = SimpleDocTemplate(
        buffer,
        pagesize=(page_width, page_height),
        rightMargin=right_margin,
        leftMargin=left_margin,
        topMargin=top_margin,
        bottomMargin=bottom_margin
    )
    styles = getSampleStyleSheet()
    wrap_style = styles['Normal'].clone('receipt_wrap')
    wrap_style.fontName = 'Helvetica'
    wrap_style.fontSize = 7
    wrap_style.leading = 9
    wrap_style.wordWrap = 'CJK'

    def wrap_cell(value):
        return Paragraph(str(value), wrap_style)

    elements = []
    elements.append(Paragraph("PARROT POS RECEIPT", styles['Heading4']))
    elements.append(Spacer(1, 4))
    cashier_name = sale.user.username if sale.user else 'Unknown'
    transaction_info = [
        [wrap_cell("Transaction ID:"), wrap_cell(sale.transaction_id)],
        [wrap_cell("Date:"), wrap_cell(sale.date.strftime("%Y-%m-%d %H:%M:%S"))],
        [wrap_cell("Cashier:"), wrap_cell(cashier_name)],
        [wrap_cell("Payment Method:"), wrap_cell(sale.payment_method.capitalize())]
    ]
    if sale.payment_method == 'cash':
        transaction_info.append([wrap_cell("Cash Received:"), wrap_cell(format_currency(sale.cash_received or 0))])
        transaction_info.append([wrap_cell("Refund Given:"), wrap_cell(format_currency(sale.refund_amount or 0))])
    t = Table(transaction_info, colWidths=[content_width * 0.42, content_width * 0.58])
    t.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 1),
        ('RIGHTPADDING', (0, 0), (-1, -1), 1),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 6))
    items_data = [["Item", "Price", "Qty", "Tax", "Total"]]
    subtotal_calc = Decimal('0.00')
    for item in items:
        product = Product.query.get(item.product_id)
        item_total = Decimal(str(item.price)) * Decimal(str(item.quantity))
        subtotal_calc += item_total
        items_data.append([
            wrap_cell(product.name),
            wrap_cell(format_currency(item.price)),
            wrap_cell(str(item.quantity)),
            wrap_cell(f"{product.tax_rate:.0f}%"),
            wrap_cell(format_currency(item_total))
        ])
    t = Table(items_data, colWidths=[
        content_width * 0.38,
        content_width * 0.17,
        content_width * 0.10,
        content_width * 0.12,
        content_width * 0.23
    ])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (0, 1), (0, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('WORDWRAP', (0, 0), (-1, -1), 'CJK'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 4),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('FONT', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 7),
        ('LEFTPADDING', (0, 0), (-1, -1), 1),
        ('RIGHTPADDING', (0, 0), (-1, -1), 1),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 5))
    totals_data = [
        [wrap_cell("Subtotal:"), wrap_cell(format_currency(subtotal_calc))],
        [wrap_cell("Tax:"), wrap_cell(format_currency(sale.tax))],
        [wrap_cell("Total:"), wrap_cell(format_currency(sale.total))]
    ]
    t = Table(totals_data, colWidths=[content_width * 0.55, content_width * 0.45])
    t.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 1),
        ('RIGHTPADDING', (0, 0), (-1, -1), 1),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 8))
    elements.append(Paragraph("Thank you for your business!", styles['Normal']))
    elements.append(Paragraph("Please visit us again soon!", styles['Normal']))
    doc.build(elements)
    buffer.seek(0)
    response = make_response(buffer.getvalue())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'inline; filename=receipt_{transaction_id}.pdf'
    return response

# --- Excel Export ---
@app.route('/api/reports/sales/export', methods=['GET'])
def export_sales_report():
    if 'user_id' not in session or session['role'] != 'manager':
        return jsonify({'error': 'Unauthorized'}), 401

    start_date = request.args.get('start')
    end_date = request.args.get('end')
    query = Sale.query
    try:
        if start_date:
            start_date_obj = datetime.strptime(start_date, '%Y-%m-%d')
            query = query.filter(Sale.date >= start_date_obj)
        if end_date:
            end_date_obj = datetime.strptime(end_date, '%Y-%m-%d')
            query = query.filter(Sale.date <= end_date_obj)
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid date format'}), 400

    sales = query.order_by(Sale.date).all()
    data = []
    for sale in sales:
        data.append({
            'Transaction ID': sale.transaction_id,
            'Date': sale.date.strftime('%Y-%m-%d %H:%M:%S'),
            'Total': sale.total,
            'Tax': sale.tax,
            'Cash Received': sale.cash_received,
            'Refund Given': sale.refund_amount or 0,
            'Payment Method': sale.payment_method,
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

    query = Sale.query
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
                'total': s.total,
                'tax': s.tax,
                'cash_received': s.cash_received,
                'refund_amount': s.refund_amount or 0,
                'payment_method': s.payment_method,
                'user_id': s.user_id,
                'username': s.user.username if s.user else 'Unknown',
                'has_delivery': hasattr(s, 'delivery') and s.delivery is not None
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

    # Get sales for the last 7 days
    end_date = datetime.now(pytz.timezone('Asia/Yangon'))
    start_date = end_date - timedelta(days=7)
    
    sales = Sale.query.filter(
        Sale.date >= start_date,
        Sale.date <= end_date
    ).order_by(Sale.date).all()

    # Group sales by day
    sales_by_day = {}
    for sale in sales:
        sale_date = sale.date.strftime('%Y-%m-%d')
        if sale_date not in sales_by_day:
            sales_by_day[sale_date] = 0
        sales_by_day[sale_date] += sale.total

    # Fill in missing days with 0
    result = []
    current_date = start_date
    while current_date <= end_date:
        date_str = current_date.strftime('%Y-%m-%d')
        result.append({
            'date': date_str,
            'total': sales_by_day.get(date_str, 0)
        })
        current_date += timedelta(days=1)

    return jsonify(result)

@app.route('/api/dashboard/top_products', methods=['GET'])
def api_dashboard_top_products():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

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
            'sales_amount': round_money(row.sales_amount or 0)
        }
        for row in rows
    ])

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
            
        # Check if username already exists (excluding current user)
        if 'username' in data and data['username'] != user.username:
            existing_user = User.query.filter_by(username=data['username']).first()
            if existing_user and existing_user.id != user.id:
                return jsonify({'success': False, 'message': 'Username already exists'}), 400
                
        user.username = data.get('username', user.username)
        user.role = data.get('role', user.role)
        
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

@app.route('/api/promotions/<int:promo_id>', methods=['PUT', 'DELETE'])
@manager_required
def api_single_promotion(promo_id):
    promo = Promotion.query.get_or_404(promo_id)
    myanmar_tz = pytz.timezone('Asia/Yangon')

    if request.method == 'DELETE':
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
    if request.method == 'GET':
        customers = Customer.query.all()
        return jsonify([{
            'id': c.id,
            'name': c.name,
            'phone': c.phone,
            'email': c.email,
            'address': c.address,
            'created_at': c.created_at.isoformat(),
            'total_debt': round_money(sum(max(to_decimal(d.balance), Decimal('0.00')) for d in c.debts if d.balance > 0))
        } for c in customers])
    
    elif request.method == 'POST':
        data = request.get_json()
        if not data or not 'name' in data:
            return jsonify({'success': False, 'message': 'Missing required fields'}), 400
        
        customer = Customer(
            name=data['name'],
            phone=data.get('phone'),
            email=data.get('email'),
            address=data.get('address')
        )
        db.session.add(customer)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Customer added'}), 201

# Purchase Order & Receiving API Endpoints
@app.route('/api/purchase_orders', methods=['GET', 'POST'])
@manager_required
def api_purchase_orders():
    if request.method == 'GET':
        # Get filter parameters
        search_query = (request.args.get('q') or '').strip()
        status_filter = (request.args.get('status') or '').strip()
        supplier_filter = (request.args.get('supplier_id') or '').strip()
        start_date = (request.args.get('start_date') or '').strip()
        end_date = (request.args.get('end_date') or '').strip()
        
        query = PurchaseOrder.query
        
        # Apply filters
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
        
        purchase_orders = query.order_by(PurchaseOrder.created_at.desc()).all()
        return jsonify([{
            'id': po.id,
            'po_number': po.po_number,
            'supplier_id': po.supplier_id,
            'supplier_name': po.supplier.name if po.supplier else 'Unknown',
            'status': po.status,
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
            'total_received': sum(i.received_qty for i in po.items)
        } for po in purchase_orders])

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
            created_by=session.get('user_id')
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

@app.route('/api/purchase_orders/summary', methods=['GET'])
@manager_required
def api_purchase_orders_summary():
    """Get purchase order summary statistics"""
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    total_pos = PurchaseOrder.query.count()
    pending_approval = PurchaseOrder.query.filter(PurchaseOrder.status == 'pending').count()
    approved = PurchaseOrder.query.filter(PurchaseOrder.status == 'approved').count()
    partially_received = PurchaseOrder.query.filter(PurchaseOrder.status == 'partially_received').count()
    received = PurchaseOrder.query.filter(PurchaseOrder.status == 'received').count()
    cancelled = PurchaseOrder.query.filter(PurchaseOrder.status == 'cancelled').count()
    
    # This month's totals
    monthly_received = PurchaseOrder.query.filter(
        PurchaseOrder.status == 'received',
        PurchaseOrder.updated_at >= month_start
    ).count()
    
    monthly_amount = db.session.query(db.func.sum(PurchaseOrder.total_amount)).filter(
        PurchaseOrder.created_at >= month_start,
        PurchaseOrder.status != 'cancelled'
    ).scalar() or 0
    
    return jsonify({
        'total': total_pos,
        'draft': PurchaseOrder.query.filter(PurchaseOrder.status == 'draft').count(),
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
    po = db.session.get(PurchaseOrder, po_id)
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
                unit_cost=po_item.unit_cost
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
    if request.method == 'GET':
        query = Supplier.query
        search_query = (request.args.get('q') or '').strip()
        active_filter = (request.args.get('active') or '').strip().lower()
        category_filter = (request.args.get('category') or '').strip()

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
        
        if category_filter:
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
            'category': s.category,
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
            category=(data.get('category') or '').strip() or None,
            tax_id=(data.get('tax_id') or '').strip() or None,
            website=(data.get('website') or '').strip() or None,
            bank_name=(data.get('bank_name') or '').strip() or None,
            bank_account=(data.get('bank_account') or '').strip() or None,
            quality_rating=float(data.get('quality_rating', 0) or 0),
            delivery_rating=float(data.get('delivery_rating', 0) or 0)
        )
        db.session.add(supplier)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Supplier added', 'supplier_id': supplier.id}), 201

@app.route('/api/suppliers/<int:supplier_id>', methods=['GET', 'PUT', 'DELETE'])
@manager_required
def api_single_supplier(supplier_id):
    supplier = db.session.get(Supplier, supplier_id)
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
            'category': supplier.category,
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
    
    orders = PurchaseOrder.query.filter_by(supplier_id=supplier_id).order_by(PurchaseOrder.created_at.desc()).all()
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

@app.route('/api/warehouse', methods=['GET'])
@manager_required
def api_warehouse_inventory():
    """Get all warehouse inventory with optional filters"""
    search_query = (request.args.get('q') or '').strip()
    low_stock = request.args.get('low_stock', '').strip().lower() == 'true'
    
    query = WarehouseInventory.query.filter(WarehouseInventory.quantity > 0)
    
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
            'total_value': item.quantity * (item.unit_cost or 0),
            'received_date': item.received_date.isoformat() if item.received_date else None,
            'expiry_date': item.expiry_date.isoformat() if item.expiry_date else None,
            'notes': item.notes,
            'created_at': item.created_at.isoformat(),
            'updated_at': item.updated_at.isoformat() if item.updated_at else None
        })
    
    if low_stock:
        result = [r for r in result if r['quantity'] <= 5]
    
    return jsonify(result)

@app.route('/api/warehouse/summary', methods=['GET'])
@manager_required
def api_warehouse_summary():
    """Get warehouse summary statistics"""
    inventory = WarehouseInventory.query.filter(WarehouseInventory.quantity > 0).all()
    
    total_skus = len(set(item.product_id for item in inventory))
    total_units = sum(item.quantity for item in inventory)
    total_value = sum(item.quantity * (item.unit_cost or 0) for item in inventory)
    
    # Get recent transfers count (last 7 days)
    week_ago = datetime.utcnow() - timedelta(days=7)
    recent_transfers = WarehouseTransfer.query.filter(WarehouseTransfer.created_at >= week_ago).count()
    
    # Low stock items (quantity <= 5)
    low_stock_count = sum(1 for item in inventory if item.quantity <= 5)
    
    return jsonify({
        'total_skus': total_skus,
        'total_units': total_units,
        'total_value': round_money(total_value),
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
    notes = (data.get('notes') or '').strip() or None
    
    if not product_id or quantity <= 0:
        return jsonify({'success': False, 'message': 'Product and valid quantity are required'}), 400
    
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({'success': False, 'message': 'Product not found'}), 404
    
    # Get warehouse inventory for this product
    warehouse_query = WarehouseInventory.query.filter_by(product_id=product_id)
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
        
        # Update main product stock
        product.stock += quantity
        
        # Record the transfer
        transfer = WarehouseTransfer(
            product_id=product_id,
            quantity=quantity,
            from_warehouse=True,
            batch_number=batch_number,
            performed_by=session.get('user_id'),
            notes=notes
        )
        db.session.add(transfer)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Transferred {quantity} units to main stock',
            'new_main_stock': product.stock
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error transferring from warehouse: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to process transfer'}), 500

@app.route('/api/warehouse/transfers', methods=['GET'])
@manager_required
def api_warehouse_transfers():
    """Get transfer history"""
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    
    transfers = WarehouseTransfer.query.order_by(WarehouseTransfer.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    total = WarehouseTransfer.query.count()
    
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
    customer = db.session.get(Customer, customer_id)
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
    if request.method == 'GET':
        query = Debt.query.join(Customer, Debt.customer_id == Customer.id)
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
                created_by=session.get('user_id')
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
    # Get all outstanding debts (all debts are actual debts now, no type filter needed)
    outstanding_debts = Debt.query.filter(Debt.balance > 0).all()
    
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
        'total_outstanding': round_money(total_outstanding),
        'total_debts': total_debts,
        'customers_with_debt': len(customers_with_debt),
        'aging_breakdown': aging_breakdown,
        'aging_amounts': {k: round_money(v) for k, v in aging_amounts.items()},
        'total_payments_this_month': round_money(total_payments_this_month),
        'overdue_count': overdue_count
    })

@app.route('/api/debts/aging', methods=['GET'])
@manager_required
def api_debts_aging():
    """Get debt aging analysis"""
    outstanding_debts = Debt.query.filter(Debt.balance > 0).all()
    
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
    query = Debt.query.join(Customer, Debt.customer_id == Customer.id)
    
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
            'total_outstanding': round_money(total_debt),
            'total_paid': round_money(total_paid)
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
            processed_by=session.get('user_id')
        )
        db.session.add(payment)
        
        # Update the original debt balance
        debt.balance = 0.0 if remaining_balance < MONEY_QUANT else round_money(remaining_balance)
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

# Model registry for AI tools
AI_MODELS = {
    'User': User,
    'AppSetting': AppSetting,
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
    """Get or create AI orchestrator with database settings access"""
    return get_orchestrator(db, AI_MODELS, get_setting)


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8888, debug=True)
