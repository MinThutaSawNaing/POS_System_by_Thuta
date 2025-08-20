from flask import Flask, render_template, request, jsonify, session, redirect, url_for, make_response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import os
import uuid
import io
from decimal import Decimal, ROUND_HALF_UP
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import letter
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
db = SQLAlchemy(app)

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
    user = db.relationship('User', backref='sales')

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
                price = Paragraph(f"${product.price:.2f}", normal_style)

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
    if not data or 'items' not in data or 'payment_method' not in data:
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400

    try:
        # Calculate totals
        subtotal = 0
        tax_total = 0
        items = []

        for item in data['items']:
            product = Product.query.get(item['product_id'])
            if not product:
                return jsonify({'success': False, 'message': f'Product {item["product_id"]} not found'}), 404

            item_total = item['price'] * item['quantity']
            item_tax = item_total * (product.tax_rate / 100)
            
            subtotal += item_total
            tax_total += item_tax
            
            items.append({
                'product': product,
                'price': item['price'],
                'quantity': item['quantity'],
                'tax': item_tax
            })

        total = subtotal + tax_total
        
        myanmar_tz = pytz.timezone('Asia/Yangon')
        sale_time = datetime.now(myanmar_tz)

        # Create sale record
        sale = Sale(
            transaction_id=str(uuid.uuid4()),
            date=sale_time,
            total=total,
            tax=tax_total,
            payment_method=data['payment_method'],
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
                tax=item['tax']
            )
            db.session.add(sale_item)
            # Update product stock
            item['product'].stock -= item['quantity']

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
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=72, leftMargin=72,
                           topMargin=72, bottomMargin=18)
    styles = getSampleStyleSheet()
    elements = []
    elements.append(Paragraph("POS SYSTEM RECEIPT", styles['Title']))
    elements.append(Spacer(1, 12))
    cashier_name = sale.user.username if sale.user else 'Unknown'
    transaction_info = [
        ["Transaction ID:", sale.transaction_id],
        ["Date:", sale.date.strftime("%Y-%m-%d %H:%M:%S")],
        ["Cashier:", cashier_name],
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
    subtotal_calc = Decimal('0.00')
    for item in items:
        product = Product.query.get(item.product_id)
        item_total = Decimal(str(item.price)) * Decimal(str(item.quantity))
        subtotal_calc += item_total
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
        ["Subtotal:", f"${subtotal_calc:.2f}"],
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

    start_date = request.args.get('start')  # Format: 'YYYY-MM-DD'
    end_date = request.args.get('end')      # Format: 'YYYY-MM-DD'

    query = Sale.query
    myanmar_tz = pytz.timezone('Asia/Yangon')

    try:
        if start_date: 
            start_date_obj = datetime.strptime(start_date, '%Y-%m-%d')
            start_date_obj = myanmar_tz.localize(start_date_obj)
            query = query.filter(Sale.date >= start_date_obj)
        if end_date:
            end_date_obj = datetime.strptime(end_date, '%Y-%m-%d')
            end_date_obj = myanmar_tz.localize(end_date_obj).replace(hour=23, minute=59, second=59)
            query = query.filter(Sale.date <= end_date_obj)

        sales = query.order_by(Sale.date.desc()).all()

        return jsonify([{
            'id': s.id,
            'transaction_id': s.transaction_id,
            'date': s.date.isoformat(),
            'total': s.total,
            'tax': s.tax,
            'payment_method': s.payment_method,
            'user_id': s.user_id
        } for s in sales])

    except ValueError:
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
    user = User.query.get(user_id)
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
