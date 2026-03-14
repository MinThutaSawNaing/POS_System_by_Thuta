# 🦜 Parrot POS

Professional, full-featured **Point of Sale (POS)** system built with **Flask** for retail and small business operations.

Parrot POS helps teams run day-to-day store workflows from one dashboard: product management, checkout, inventory, promotions, sales reporting, customer/supplier records, debt management, barcode labels, role-based user access, and an intelligent AI assistant.

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
  - Product CRUD with photo upload and preview
  - Barcode support with printable labels
  - Cost, category, tax rate, stock control
  - Centralized category management with color coding
  - Reorder points and automated low-stock alerts

- **Sales, Analytics & Reporting**
  - Transaction history and sale details
  - Dashboard metrics and charts
  - Date-range reporting with export support (Excel, PDF)
  - Printable receipts and barcode labels
  - Sales trends analysis

- **Business Modules**
  - Promotions (fixed and percentage discounts)
  - Customer management
  - Debt & payment tracking with aging analysis
  - Supplier management with price agreements
  - Purchase order system (create, approve, cancel)
  - Warehouse inventory management with transfers
  - User roles (`manager`, `cashier`)

- **Multi-Branch Support**
  - Manage multiple store locations from a single system
  - Complete data isolation between branches (products, sales, customers, etc.)
  - Easy branch switching via Settings
  - Default branch configuration with automatic fallback
  - Branch-specific reporting and analytics

- **AI Agent Assistant (Loli)**
  - Natural language inventory queries
  - Automated purchase order suggestions
  - Sales trend analysis
  - Smart reorder recommendations
  - Real-time database integration

- **Windows Setup Script**
  - `SetupTheSoftware.bat` creates a virtual environment and installs dependencies

---

## 🤖 AI Agent Assistant (Loli)

Parrot POS features an intelligent AI assistant named **Loli** that helps manage inventory, procurement, and business operations through natural language conversations.

### AI Agent Capabilities

The AI assistant can help with:

- **Inventory Management**
  - Check stock levels for all products
  - Identify low stock and out-of-stock items
  - Get product details and information
  - Receive automated reorder suggestions

- **Purchase Orders**
  - Create new purchase orders from suppliers
  - Approve or cancel pending orders
  - View purchase order history and status

- **Supplier Management**
  - List all suppliers with contact information
  - Get supplier details and price agreements
  - Compare supplier pricing

- **Warehouse Operations**
  - Check warehouse inventory levels
  - Create warehouse-to-store transfers
  - Manage unstocked items

- **Sales Analysis**
  - View sales trends over time
  - Identify top-selling products
  - Analyze revenue performance

### How to Use the AI Agent

1. Log in to the POS dashboard
2. Click the **AI Assistant** widget in the bottom-right corner
3. Type your question or command in natural language

**Example queries:**
- "Check low stock items"
- "Show me sales trends for last 30 days"
- "Create a purchase order for supplier ABC"
- "What products need reordering?"
- "Show inventory status"
- "Transfer 50 units of Product X from warehouse"

### AI Agent Features

- **Smart Tool Selection**: Automatically selects the right tools based on your query
- **Multi-step Tasks**: Handles complex workflows like "check low stock and create purchase orders"
- **Fallback Handling**: Even if the AI service is unavailable, built-in fallback logic ensures core queries still work
- **Conversation History**: Maintains context across multiple questions
- **Real-time Data**: Always works with live database information

### AI Agent Configuration

The AI agent uses APIFree.ai (Gemini 2.5 Flash Lite) for natural language processing. To configure:

1. Go to **Settings** in the dashboard
2. Enter your API key in the AI Agent section
3. Save settings

> **Note**: The AI agent works with real database data and can perform actual operations like creating purchase orders. Always verify important actions.

---

## 🏢 Multi-Branch Support

Parrot POS supports managing multiple store locations from a single installation. Each branch has complete data isolation while sharing the same system configuration.

### Features

- **Complete Data Isolation**: Products, sales, customers, suppliers, debts, purchase orders, and warehouse inventory are all scoped to individual branches
- **Easy Branch Switching**: Change active branch from the Settings page - all data updates automatically
- **Default Branch**: Set a default branch for automatic selection on login
- **Session Persistence**: Selected branch is maintained across page navigations
- **Branch Indicators**: Visual indicators show the current active branch in every section

### How to Use

1. **Switch Branches**:
   - Go to **Settings** in the dashboard
   - Find the **Branch Selection** section at the top
   - Select your desired branch from the dropdown
   - All data will automatically refresh for the selected branch

2. **Set Default Branch** (Manager only):
   - Navigate to **Branches** section
   - Click the star icon next to the desired branch
   - This branch will be automatically selected on future logins

3. **Manage Branches** (Manager only):
   - Create new branches with unique codes
   - Activate/deactivate branches as needed
   - View branch-specific reports and analytics

### Database Architecture

- Single database with `branch_id` foreign keys on all relevant tables
- Automatic filtering by current branch for all API endpoints
- Migration support for existing data (assigns to default branch)

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
├── app.py                    # Main Flask application
├── ai_agent.py               # AI Agent core module
├── agent_orchestrator.py     # AI Agent orchestration and tool management
├── ai_tools.py               # AI Agent database tools
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── SetupTheSoftware.bat      # Windows automated setup
├── templates/
│   ├── login.html
│   ├── dashboard.html        # Main dashboard with all modules
│   └── ai_agent_widget.html  # AI assistant chat interface
├── public/
│   └── photos/               # Static assets (logo, icons)
├── uploads/
│   └── products/             # Product photo uploads
├── instance/
│   └── pos.db                # SQLite database
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

## ðŸªŸ Windows Automated Setup

For new Windows machines, you can run the setup script to create the virtual environment and install dependencies:

```bat
SetupTheSoftware.bat
```

After it completes, run:

```bat
.\.venv\Scripts\python.exe app.py
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
- The AI agent requires an API key to be configured in Settings for full functionality.
- Barcode labels can be printed directly from the product management interface.
- Purchase orders go through a workflow: Draft → Pending → Approved → Received.
- Debt management includes aging analysis (0-30, 31-60, 61-90, 90+ days).
- Warehouse transfers automatically update main stock levels when confirmed.
- Multi-branch system isolates all data by branch - ensure you're on the correct branch before making changes.
- First branch is auto-created on initial setup; additional branches can be added from the Branches section.

---

## 📦 Key Modules

### Purchase Order System
- Create purchase orders from suppliers
- Multi-item support with automatic total calculation
- Approval workflow with status tracking
- Cancel orders with reason logging

### Warehouse Management
- Separate warehouse inventory tracking
- Transfer items to main store stock
- Manage unstocked products
- Batch transfer operations

### Debt Management
- Customer debt tracking with payment history
- Aging analysis reports
- Bulk payment processing
- Automated payment reminders

### Category Management
- Centralized category system for products and suppliers
- Color-coded categories for visual organization
- Hierarchical category support
- Automatic synchronization across the system

### Supplier Management
- Supplier profiles with contact information
- Price agreements per product
- Quality rating tracking
- Purchase history per supplier

---

## Maintainer

**Min Thuta Saw Naing**  
GitHub: [@MinThutaSawNaing](https://github.com/MinThutaSawNaing)
Phone: +95 977 144 320

---

## 📄 License

This project is available for learning, customization, and business adaptation.
#   C I / C D   T e s t 
 
 