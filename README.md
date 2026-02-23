# 🦜 Parrot POS

Professional, full-featured **Point of Sale (POS)** system built with **Flask** for retail and small business operations.

Parrot POS helps teams run day-to-day store workflows from one dashboard: product management, checkout, inventory, promotions, sales reporting, customer/supplier records, debt management, barcode labels, and role-based user access.

---

## ✨ Highlights

- **Modern POS Interface**
  - Fast product search and cart workflow
  - Multiple payment methods
  - Cash received and refund/change support

- **Configurable Business Settings**
  - POS branding support
  - Currency switching between:
    - Dollar (`$`)
    - Myanmar Kyat (`MMK`)
   - Thai Baht (`THB`)
- Currency display format uses **suffix style** (e.g., `100$`, `100MMK`, `100THB`)

- **Inventory & Product Management**
  - Product CRUD
  - Barcode support
  - Cost, category, tax rate, stock control
  - Product photo upload and preview

- **Sales, Analytics & Reporting**
  - Transaction history and sale details
  - Dashboard metrics and charts
  - Date-range reporting with export support
  - Printable receipts and barcode labels

- **Business Modules**
  - Promotions (fixed and percentage discounts)
  - Customer management
  - Debt & payment tracking
  - Supplier management
  - User roles (`manager`, `cashier`)

---

## 🧱 Tech Stack

- **Backend:** Python, Flask
- **Database:** SQLite + SQLAlchemy
- **Frontend:** HTML, Bootstrap 5, Vanilla JavaScript
- **Reporting & Documents:** ReportLab, Pandas, XlsxWriter

---

## Project Structure

```text
POS_System_by_Thuta/
├── Dockerfile
├── .dockerignore
├── app.py
├── requirements.txt
├── templates/
│   ├── login.html
│   └── dashboard.html
├── uploads/
│   └── products/
├── instance/
│   └── pos.db
└── README.md
```

---

## ⚙️ Quick Start

### 1) Clone the repository

```bash
git clone https://github.com/MinThutaSawNaing/POS_System_by_Thuta.git
cd POS_System_by_Thuta
```

### 2) Create and activate a virtual environment

```bash
python -m venv .venv
```

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\Activate.ps1
```

### 3) Install dependencies

```bash
pip install -r requirements.txt
```

### 4) Run the application

```bash
python app.py
```

Application URL:

```text
http://127.0.0.1:8888
```

---

## 🐳 Run with Docker

### 1) Build the image

```bash
docker build -t parrot-pos .
```

### 2) Run the container

```bash
docker run --name parrot-pos \
  -p 8888:8888 \
  -v "${PWD}/instance:/app/instance" \
  -v "${PWD}/uploads:/app/uploads" \
  parrot-pos
```

> Windows CMD example:

```cmd
docker run --name parrot-pos -p 8888:8888 -v "%cd%\instance:/app/instance" -v "%cd%\uploads:/app/uploads" parrot-pos
```

Using volume mounts keeps your SQLite database and uploaded product images persistent between container restarts.

Application URL:

```text
http://127.0.0.1:8888
```

### 3) Stop and remove

```bash
docker stop parrot-pos && docker rm parrot-pos
```

---

## 🔐 Default Access

An admin account is auto-created if missing:

- **Username:** `admin`
- **Password:** `admin123`
- **Role:** `manager`

> ⚠️ Security note: For deployment, change default credentials and secret configuration immediately.

---

## 🛠 Operational Notes

- Database initialization runs automatically at startup.
- Uploaded product images are stored under `uploads/products/`.
- Existing databases are migrated automatically for compatible schema updates.

---

## Maintainer

**Min Thuta Saw Naing**  
GitHub: [@MinThutaSawNaing](https://github.com/MinThutaSawNaing)

---

## 📄 License

This project is available for learning, customization, and business adaptation.
