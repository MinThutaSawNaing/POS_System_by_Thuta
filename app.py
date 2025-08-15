from flask import Flask, render_template, request, jsonify, session, redirect, url_for, make_response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import os
import uuid
import io
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
import pandas as pd
from decimal import Decimal, ROUND_HALF_UP

app = Flask(__name__)
app.secret_key = 'your_super_secret_key_here'  # Change this in production!
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Database Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), default='cashier')

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(50), unique=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    cost = db.Column(db.Float)
    stock = db.Column(db.Integer, default=0)
    category = db.Column(db.String(50))
    tax_rate = db.Column(db.Float, default=0.0)

class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.String(36), unique=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    total = db.Column(db.Float, nullable=False)
    tax = db.Column(db.Float, nullable=False)
    payment_method = db.Column(db.String(20))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))

class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    tax = db.Column(db.Float, nullable=False)

# Create database tables and admin user
with app.app_context():
    db.create_all()
    if not User.query.filter_by(username='admin').first():
        admin_user = User(
            username='admin',
            password=generate_password_hash('admin123'),
            role='manager'
        )
        db.session.add(admin_user)
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
    return render_template('dashboard.html')

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
            'tax_rate': p.tax_rate
        } for p in products])
    elif request.method == 'POST':
        data = request.get_json()
        if not data or not all(k in data for k in ['name', 'price', 'stock']):
            return jsonify({'success': False, 'message': 'Missing required fields'}), 400
        product = Product(
            barcode=data.get('barcode'),
            name=data['name'],
            price=data['price'],
            cost=data.get('cost', 0),
            stock=data['stock'],
            category=data.get('category'),
            tax_rate=data.get('tax_rate', 0)
        )
        db.session.add(product)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Product added'}), 201

@app.route('/api/products/<int:product_id>', methods=['GET', 'PUT', 'DELETE'])
def api_single_product(product_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    product = Product.query.get(product_id)
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
            'tax_rate': product.tax_rate
        })
    elif request.method == 'PUT':
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400
        product.barcode = data.get('barcode', product.barcode)
        product.name = data.get('name', product.name)
        product.price = data.get('price', product.price)
        product.cost = data.get('cost', product.cost)
        product.stock = data.get('stock', product.stock)
        product.category = data.get('category', product.category)
        product.tax_rate = data.get('tax_rate', product.tax_rate)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Product updated'})
    elif request.method == 'DELETE':
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
        'stock': p.stock
    } for p in products])

# Sales API Endpoints
@app.route('/api/sales', methods=['POST'])
def api_sales():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    if not data or not all(k in data for k in ['items', 'payment_method']):
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400
    subtotal = Decimal('0.00')
    tax = Decimal('0.00')
    items = []
    for item in data['items']:
        product = Product.query.get(item['product_id'])
        if not product:
            return jsonify({'success': False, 'message': f"Product {item['product_id']} not found"}), 404
        if product.stock < item['quantity']:
            return jsonify({'success': False, 'message': f'Insufficient stock for {product.name}'}), 400
        price = Decimal(str(product.price))
        qty = int(item['quantity'])
        item_total = (price * qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        item_tax = (item_total * Decimal(str(product.tax_rate)) / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        subtotal += item_total
        tax += item_tax
        items.append({
            'product_id': product.id,
            'price': float(price),
            'quantity': qty,
            'tax': float(item_tax)
        })
    total = (subtotal + tax).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    transaction_id = str(uuid.uuid4())
    sale = Sale(
        transaction_id=transaction_id,
        total=float(total),
        tax=float(tax),
        payment_method=data['payment_method'],
        user_id=session['user_id']
    )
    db.session.add(sale)
    db.session.commit()
    for item in items:
        sale_item = SaleItem(
            sale_id=sale.id,
            product_id=item['product_id'],
            quantity=item['quantity'],
            price=item['price'],
            tax=item['tax']
        )
        db.session.add(sale_item)
        # Decrease stock
        product = Product.query.get(item['product_id'])
        product.stock -= item['quantity']
    db.session.commit()
    sale_data = {
        'transaction_id': sale.transaction_id,
        'date': sale.date.isoformat(),
        'total': sale.total,
        'tax': sale.tax,
        'payment_method': sale.payment_method,
        'items': []
    }
    for si in SaleItem.query.filter_by(sale_id=sale.id).all():
        product = Product.query.get(si.product_id)
        sale_data['items'].append({
            'product_id': si.product_id,
            'name': product.name,
            'price': si.price,
            'quantity': si.quantity,
            'tax': si.tax,
            'tax_rate': product.tax_rate
        })
    return jsonify(sale_data)

# Fixed PDF receipt endpoint
@app.route('/api/sales/<string:transaction_id>/print', methods=['GET'])
def print_receipt(transaction_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    sale = Sale.query.filter_by(transaction_id=transaction_id).first()
    if not sale:
        return jsonify({'success': False, 'message': 'Sale not found'}), 404
    items = SaleItem.query.filter_by(sale_id=sale.id).all()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=72, leftMargin=72,
                           topMargin=72, bottomMargin=18)
    styles = getSampleStyleSheet()
    elements = []
    elements.append(Paragraph("POS SYSTEM RECEIPT", styles['Title']))
    elements.append(Spacer(1, 12))
    transaction_info = [
        ["Transaction ID:", sale.transaction_id],
        ["Date:", sale.date.strftime("%Y-%m-%d %H:%M:%S")],
        ["Cashier:", session.get('username', '')],
        ["Payment Method:", sale.payment_method.capitalize()]
    ]
    t = Table(transaction_info, colWidths=[120, 200])
    t.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 24))
    items_data = [["Item", "Price", "Qty", "Tax", "Total"]]
    subtotal = 0
    for item in items:
        product = Product.query.get(item.product_id)
        item_total = item.price * item.quantity
        subtotal += item_total
        items_data.append([
            product.name,
            f"${item.price:.2f}",
            str(item.quantity),
            f"{product.tax_rate:.0f}%",
            f"${item_total:.2f}"
        ])
    t = Table(items_data, colWidths=[200, 60, 40, 40, 60])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('FONT', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 12))
    totals_data = [
        ["Subtotal:", f"${subtotal:.2f}"],
        ["Tax:", f"${sale.tax:.2f}"],
        ["Total:", f"${sale.total:.2f}"]
    ]
    t = Table(totals_data, colWidths=[100, 60])
    t.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 24))
    elements.append(Paragraph("Thank you for your business!", styles['Normal']))
    elements.append(Paragraph("Please visit us again soon!", styles['Normal']))
    doc.build(elements)
    buffer.seek(0)
    response = make_response(buffer.getvalue())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'inline; filename=receipt_{transaction_id}.pdf'
    return response

# Fixed Excel export endpoint
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
    return jsonify([{
        'id': s.id,
        'transaction_id': s.transaction_id,
        'date': s.date.isoformat(),
        'total': s.total,
        'tax': s.tax,
        'payment_method': s.payment_method,
        'user_id': s.user_id
    } for s in sales])

if __name__ == '__main__':
    app.run(debug=True)
