# POS System by Thuta

A modern **Point of Sale (POS) web application** built with Flask for small and medium businesses.  
This project provides an all-in-one workflow for daily store operations including product management, sales processing, reporting, user roles, promotions, supplier/customer records, debt tracking, and barcode label generation.

---

## 🚀 Key Features

- **Authentication & Role Access**
  - Secure login
  - Role-based access (`manager`, `cashier`)

- **Product Management**
  - Add, edit, delete products
  - Barcode support
  - Stock, cost, category, and tax management
  - **Product photo upload and preview**

- **POS Checkout Flow**
  - Fast product search and cart system
  - Stock validation
  - Multiple payment methods
  - Promotion-aware pricing

- **Sales & Receipts**
  - Transaction history
  - Sale detail view
  - Printable PDF receipts

- **Reports & Export**
  - Sales reporting by date range
  - Dashboard charts and summaries
  - Excel export

- **Promotions Module**
  - Percentage/fixed discounts
  - Date/time controlled campaigns

- **Customers, Debts & Payments**
  - Customer records
  - Debt tracking
  - Payment settlement workflow

- **Supplier Management**
  - Supplier profile CRUD

- **Barcode Labels**
  - Generate printable barcode label PDFs

- **Improved UI/UX**
  - Resizable sidebar
  - Sidebar tabs/buttons expand properly with sidebar width

---

## 🧱 Tech Stack

- **Backend:** Python, Flask
- **Database:** SQLite (via Flask-SQLAlchemy)
- **Frontend:** HTML, Bootstrap 5, Vanilla JavaScript
- **Reporting:** ReportLab, Pandas, XlsxWriter

---

## 📂 Project Structure

```text
POS_System_by_Thuta/
├── app.py
├── requirements.txt
├── templates/
│   ├── login.html
│   └── dashboard.html
└── README.md
```

---

## ⚙️ Installation & Setup

### 1) Clone repository

```bash
git clone https://github.com/MinThutaSawNaing/POS_System_by_Thuta.git
cd POS_System_by_Thuta
```

### 2) Create virtual environment

```bash
python -m venv .venv
```

### 3) Activate virtual environment

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\Activate.ps1
```

### 4) Install dependencies

```bash
pip install -r requirements.txt
```

### 5) Run the app

```bash
python app.py
```

By default, the app starts at:

```text
http://127.0.0.1:8888
```

---

## 🔐 Default Admin Account

The app auto-creates an admin account if it does not exist:

- **Username:** `admin`
- **Password:** `admin123`
- **Role:** `manager`

> ⚠️ For production, change the secret key and default credentials immediately.

---

## 🧠 Notes

- Database is initialized automatically on startup.
- Product images are stored under `uploads/products`.
- Existing databases are auto-migrated for newly added `photo_filename` field.

---

## 👨‍💻 Author

**Min Thuta Saw Naing**  
GitHub: [@MinThutaSawNaing](https://github.com/MinThutaSawNaing)

---

## 📄 License

This project is open for learning and personal/business customization.
