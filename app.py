from flask import Flask, render_template, request, jsonify, session, redirect, url_for, make_response, send_from_directory
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
    'THB': 'Bh'
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
    if symbol.isalpha():
        return f"{symbol} {amount:.2f}"
    return f"{symbol}{amount:.2f}"

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
    if debt.type == 'payment':
        return 'payment'
    if debt.balance <= 0:
        return 'paid'
    if debt.balance < debt.amount:
        return 'partial'
    if debt.due_date and datetime.utcnow() > debt.due_date:
        return 'overdue'
    return 'pending'

def serialize_debt(debt):
    """Serialize debt record with all computed fields"""
    days_outstanding = calculate_debt_aging_days(debt.date) if debt.type == 'debt' else 0
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
    if total_paid == 0 and debt.type == 'debt':
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
        'type': debt.type,
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class PurchaseOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(40), unique=True, nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)
    status = db.Column(db.String(20), default='draft')  # draft, partial, received, cancelled
    notes = db.Column(db.String(300))
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    supplier = db.relationship('Supplier', backref='purchase_orders')
    creator = db.relationship('User', backref='created_purchase_orders')

class PurchaseOrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey('purchase_order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    ordered_qty = db.Column(db.Integer, nullable=False)
    received_qty = db.Column(db.Integer, default=0)
    unit_cost = db.Column(db.Float, default=0.0)

    purchase_order = db.relationship('PurchaseOrder', backref='items')
    product = db.relationship('Product', backref='purchase_order_items')

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
    type = db.Column(db.String(20), default='debt')  # 'debt' or 'payment'
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
                    db.session.execute(text("UPDATE debt SET status = CASE WHEN type = 'debt' AND balance > 0 THEN 'pending' WHEN type = 'debt' AND balance <= 0 THEN 'paid' ELSE 'payment' END WHERE status IS NULL OR status = 'pending'"))
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
        return jsonify({
            'pos_name': 'Parrot POS',
            'currency_code': get_currency_code(),
            'currency_suffix': get_currency_suffix()
        })

    if session.get('role') != 'manager':
        return jsonify({'success': False, 'message': 'Manager access required'}), 403

    data = request.get_json() or {}
    currency_code = data.get('currency_code')
    if currency_code not in CURRENCY_OPTIONS:
        return jsonify({'success': False, 'message': 'Invalid currency code'}), 400

    set_setting('currency_code', currency_code)
    return jsonify({
        'success': True,
        'message': 'Settings updated',
        'currency_code': currency_code,
        'currency_suffix': get_currency_suffix(currency_code)
    })

@app.route('/uploads/products/<path:filename>')
def product_image(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# Product API Endpoints
@app.route('/api/products', methods=['GET', 'POST'])
def api_products():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        products = Product.query.all()
        return jsonify([{
            'id': p.id,
            'barcode': p.barcode,
            'name': p.name,
            'price': p.price,
            'cost': p.cost,
            'stock': p.stock,
            'category': p.category,
            'tax_rate': p.tax_rate,
            'photo_filename': p.photo_filename,
            'photo_url': product_photo_url(p.photo_filename)
        } for p in products])

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
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': 'Invalid numeric values'}), 400

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
        products = Product.query.filter(Product.id.in_(data['product_ids'])).all()
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

        # ✅ Add products multiple times based on stock qty
        for product in products:
            qty = product.stock if hasattr(product, "stock") and product.stock else 1
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
                type='debt',
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
    elements = []
    elements.append(Paragraph("PARROT POS RECEIPT", styles['Heading4']))
    elements.append(Spacer(1, 4))
    cashier_name = sale.user.username if sale.user else 'Unknown'
    transaction_info = [
        ["Transaction ID:", sale.transaction_id],
        ["Date:", sale.date.strftime("%Y-%m-%d %H:%M:%S")],
        ["Cashier:", cashier_name],
        ["Payment Method:", sale.payment_method.capitalize()]
    ]
    if sale.payment_method == 'cash':
        transaction_info.append(["Cash Received:", format_currency(sale.cash_received or 0)])
        transaction_info.append(["Refund Given:", format_currency(sale.refund_amount or 0)])
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
            product.name,
            format_currency(item.price),
            str(item.quantity),
            f"{product.tax_rate:.0f}%",
            format_currency(item_total)
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
        ["Subtotal:", format_currency(subtotal_calc)],
        ["Tax:", format_currency(sale.tax)],
        ["Total:", format_currency(sale.total)]
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

        # Fetch and return sales (includes sales with and without delivery)
        sales = query.order_by(Sale.date.desc()).all()

        return jsonify([{
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
        } for s in sales])

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

# User API Endpoints
@app.route('/api/users', methods=['GET'])
@manager_required
def api_users():
    users = User.query.all()
    return jsonify([{
        'id': u.id,
        'username': u.username,
        'role': u.role
    } for u in users])

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
            'total_debt': round_money(sum(max(to_decimal(d.balance), Decimal('0.00')) for d in c.debts if d.type == 'debt'))
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
        purchase_orders = PurchaseOrder.query.order_by(PurchaseOrder.created_at.desc()).all()
        return jsonify([{
            'id': po.id,
            'po_number': po.po_number,
            'supplier_id': po.supplier_id,
            'supplier_name': po.supplier.name if po.supplier else 'Unknown',
            'status': po.status,
            'notes': po.notes,
            'created_at': po.created_at.isoformat(),
            'updated_at': po.updated_at.isoformat() if po.updated_at else None,
            'items_count': len(po.items),
            'received_items_count': sum(1 for i in po.items if i.received_qty >= i.ordered_qty)
        } for po in purchase_orders])

    data = request.get_json() or {}
    supplier_id = data.get('supplier_id')
    items = data.get('items') or []

    if not supplier_id or not items:
        return jsonify({'success': False, 'message': 'Supplier and at least one item are required'}), 400

    supplier = db.session.get(Supplier, supplier_id)
    if not supplier:
        return jsonify({'success': False, 'message': 'Supplier not found'}), 404

    try:
        po = PurchaseOrder(
            po_number=generate_po_number(),
            supplier_id=supplier_id,
            status='draft',
            notes=(data.get('notes') or '').strip() or None,
            created_by=session.get('user_id')
        )
        db.session.add(po)
        db.session.flush()

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

        db.session.commit()
        return jsonify({'success': True, 'message': 'Purchase order created', 'purchase_order_id': po.id}), 201
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error creating purchase order: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to create purchase order'}), 500

@app.route('/api/purchase_orders/<int:po_id>', methods=['GET'])
@manager_required
def api_single_purchase_order(po_id):
    po = db.session.get(PurchaseOrder, po_id)
    if not po:
        return jsonify({'success': False, 'message': 'Purchase order not found'}), 404

    return jsonify({
        'id': po.id,
        'po_number': po.po_number,
        'supplier_id': po.supplier_id,
        'supplier_name': po.supplier.name if po.supplier else 'Unknown',
        'status': po.status,
        'notes': po.notes,
        'created_at': po.created_at.isoformat(),
        'updated_at': po.updated_at.isoformat() if po.updated_at else None,
        'items': [{
            'id': item.id,
            'product_id': item.product_id,
            'product_name': item.product.name if item.product else 'Unknown',
            'ordered_qty': item.ordered_qty,
            'received_qty': item.received_qty,
            'unit_cost': item.unit_cost
        } for item in po.items]
    })

@app.route('/api/purchase_orders/<int:po_id>/receive', methods=['POST'])
@manager_required
def api_receive_purchase_order(po_id):
    po = db.session.get(PurchaseOrder, po_id)
    if not po:
        return jsonify({'success': False, 'message': 'Purchase order not found'}), 404

    if po.status == 'received':
        return jsonify({'success': False, 'message': 'Purchase order already fully received'}), 400

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
            po_item.product.stock += receive_qty

            if po_item.unit_cost and po_item.unit_cost > 0:
                po_item.product.cost = po_item.unit_cost

        all_received = all(item.received_qty >= item.ordered_qty for item in po.items)
        any_received = any(item.received_qty > 0 for item in po.items)
        po.status = 'received' if all_received else ('partial' if any_received else 'draft')

        db.session.commit()
        return jsonify({'success': True, 'message': 'Receiving recorded', 'status': po.status})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error receiving purchase order: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to process receiving'}), 500

# Supplier API Endpoints
@app.route('/api/suppliers', methods=['GET', 'POST'])
@manager_required
def api_suppliers():
    if request.method == 'GET':
        query = Supplier.query
        search_query = (request.args.get('q') or '').strip()
        active_filter = (request.args.get('active') or '').strip().lower()

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
            notes=(data.get('notes') or '').strip() or None
        )
        db.session.add(supplier)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Supplier added'}), 201

@app.route('/api/suppliers/<int:supplier_id>', methods=['GET', 'PUT', 'DELETE'])
@manager_required
def api_single_supplier(supplier_id):
    supplier = db.session.get(Supplier, supplier_id)
    if not supplier:
        return jsonify({'success': False, 'message': 'Supplier not found'}), 404

    if request.method == 'GET':
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
        supplier.updated_at = datetime.utcnow()

        db.session.commit()
        return jsonify({'success': True, 'message': 'Supplier updated'})

    elif request.method == 'DELETE':
        db.session.delete(supplier)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Supplier deleted'})

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
        total_debt = sum(d.balance for d in customer.debts if d.type == 'debt')
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

        if debt_type in ('debt', 'payment'):
            query = query.filter(Debt.type == debt_type)

        if status == 'open':
            query = query.filter(Debt.type == 'debt', Debt.balance > 0)
        elif status == 'closed':
            query = query.filter((Debt.type == 'payment') | ((Debt.type == 'debt') & (Debt.balance <= 0)))
        elif status == 'pending':
            query = query.filter(Debt.type == 'debt', Debt.balance == Debt.amount)
        elif status == 'partial':
            query = query.filter(Debt.type == 'debt', Debt.balance > 0, Debt.balance < Debt.amount)
        elif status == 'overdue':
            query = query.filter(Debt.type == 'debt', Debt.balance > 0, Debt.due_date != None, Debt.due_date < datetime.utcnow())

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
                if d.type == 'debt':
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
                type='debt',
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
    # Get all outstanding debts
    outstanding_debts = Debt.query.filter(Debt.type == 'debt', Debt.balance > 0).all()
    
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
    outstanding_debts = Debt.query.filter(Debt.type == 'debt', Debt.balance > 0).all()
    
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
    debt_type = request.args.get('type')
    status = request.args.get('status')
    customer_id = request.args.get('customer_id')
    
    if debt_type:
        query = query.filter(Debt.type == debt_type)
    if status == 'open':
        query = query.filter(Debt.type == 'debt', Debt.balance > 0)
    if customer_id:
        query = query.filter(Debt.customer_id == customer_id)
    
    debts = query.order_by(Debt.date.desc()).all()
    
    data = []
    for d in debts:
        days_outstanding = calculate_debt_aging_days(d.date) if d.type == 'debt' else 0
        data.append({
            'ID': d.id,
            'Customer': d.customer.name if d.customer else 'Unknown',
            'Phone': d.customer.phone if d.customer else '',
            'Type': d.type.upper(),
            'Amount': d.amount,
            'Balance': d.balance,
            'Days Outstanding': days_outstanding if d.type == 'debt' else 'N/A',
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
                if d.type == 'debt' and d.amount > d.balance:
                    continue  # Skip debts with payments
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
        if debt.type == 'debt' and debt.amount > debt.balance:
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
    
    # Calculate customer summary
    total_debt = sum(d.balance for d in debts if d.type == 'debt')
    total_paid = sum(d.amount for d in debts if d.type == 'payment')
    
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

    if debt.type != 'debt':
        return jsonify({'success': False, 'message': 'Payments can only be applied to debt records'}), 400

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
        ["Type:", debt.type.upper()],
        ["Amount:", format_currency(debt.amount)],
    ]
    if debt.type == 'debt':
        info.append(["Balance:", format_currency(debt.balance)])
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8888, debug=True)
