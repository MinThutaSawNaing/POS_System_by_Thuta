from flask import Flask, render_template, request, jsonify, session, redirect, url_for, make_response, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
import os
import uuid
import io
from sqlalchemy import inspect, text
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
    notes = db.Column(db.String(200))
    
    # Relationship with sale
    sale = db.relationship('Sale', backref='debt', lazy=True)

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

        db.session.commit()

        return jsonify({
            'success': True,
            'message': 'Sale completed',
            'transaction_id': sale.transaction_id
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
        'items': []
    }
    for item in items:
        product = Product.query.get(item.product_id)
        sale_data['items'].append({
            'product_id': item.product_id,
            'name': product.name,
            'price': item.price,
            'quantity': item.quantity,
            'tax': item.tax,
            'tax_rate': product.tax_rate
        })
    return jsonify(sale_data)

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

        # Fetch and return sales
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
            'username': s.user.username if s.user else 'Unknown'
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
        query = Debt.query.join(Customer)
        search_query = (request.args.get('q') or '').strip()
        debt_type = (request.args.get('type') or '').strip().lower()
        status = (request.args.get('status') or '').strip().lower()

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

        debts = query.order_by(Debt.date.desc(), Debt.id.desc()).all()
        return jsonify([{
            'id': d.id,
            'customer_id': d.customer_id,
            'customer_name': d.customer.name,
            'sale_id': d.sale_id,
            'amount': d.amount,
            'balance': d.balance,
            'type': d.type,
            'date': d.date.isoformat(),
            'notes': d.notes
        } for d in debts])
    
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

        try:
            debt = Debt(
                customer_id=customer.id,
                sale_id=sale_id,
                amount=round_money(amount),
                balance=round_money(amount),
                type='debt',
                notes=(data.get('notes') or '').strip() or None
            )
            db.session.add(debt)
            db.session.commit()
            return jsonify({'success': True, 'message': 'Debt record added'}), 201
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error creating debt: {str(e)}")
            return jsonify({'success': False, 'message': 'Failed to create debt record'}), 500

@app.route('/api/debts/<int:debt_id>', methods=['GET', 'PUT', 'DELETE'])
@manager_required
def api_single_debt(debt_id):
    debt = db.session.get(Debt, debt_id)
    if not debt:
        return jsonify({'success': False, 'message': 'Debt record not found'}), 404
    
    if request.method == 'GET':
        return jsonify({
            'id': debt.id,
            'customer_id': debt.customer_id,
            'customer_name': debt.customer.name,
            'sale_id': debt.sale_id,
            'amount': debt.amount,
            'balance': debt.balance,
            'type': debt.type,
            'date': debt.date.isoformat(),
            'notes': debt.notes
        })
    
    elif request.method == 'PUT':
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400
        
        debt.notes = data.get('notes', debt.notes)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Debt record updated'})
    
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
    return jsonify([{
        'id': d.id,
        'sale_id': d.sale_id,
        'amount': d.amount,
        'balance': d.balance,
        'type': d.type,
        'date': d.date.isoformat(),
        'notes': d.notes
    } for d in debts])

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
        # Create a payment record
        payment = Debt(
            customer_id=debt.customer_id,
            sale_id=debt.sale_id,
            amount=round_money(payment_amount),
            balance=0,  # Payment records have no balance
            type='payment',
            notes=(data.get('notes') or f'Payment towards debt #{debt.id}').strip()
        )
        db.session.add(payment)
        
        # Update the original debt balance
        remaining_balance = (current_balance - payment_amount).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        debt.balance = 0.0 if remaining_balance < MONEY_QUANT else round_money(remaining_balance)

        db.session.commit()
        return jsonify({'success': True, 'message': 'Payment recorded successfully'})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error recording debt payment: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to record payment'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8888, debug=True)
