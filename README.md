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
  - Branch-aware natural language operations queries
  - Inventory, procurement, warehouse, sales, customer, debt, delivery, promotion, and return summaries
  - Smart reorder recommendations and sales trend analysis
  - Secure Markdown-formatted responses backed by live database data

- **Windows Setup Script**
  - `SetupTheSoftware.bat` creates a virtual environment and installs dependencies

---

## 🤖 AI Agent Assistant (Loli)

Parrot POS features an intelligent AI assistant named **Loli** that helps manage inventory, procurement, and business operations through natural language conversations.

Loli always answers from the **currently active branch**. It uses read-only live-data tools, so it can summarize operations accurately without creating or changing records.

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

- **Customers, Debts & Deliveries**
  - Find customers and outstanding balances
  - Review overdue debt and aging status
  - Check delivery stages, priorities, and open delivery work

- **Promotions, Categories & Returns**
  - Review active, upcoming, and expired promotions
  - Summarize categories and product coverage
  - Review recent returns and exchanges

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
- "Which customer debts are overdue?"
- "Show urgent deliveries"
- "What promotions are active?"
- "What branch am I working in?"

### AI Agent Features

- **Smart Tool Selection**: Automatically selects the right tools based on your query
- **Multi-step Tasks**: Handles complex workflows like "check low stock and create purchase orders"
- **No Invented Data**: Data questions are always answered from live database results. If the AI does not call a tool, Loli retries with forced tool calling and falls back to built-in real-data lookups before answering — it never shows guessed figures.
- **Fallback Handling**: Even if the AI service is unavailable, built-in fallback logic ensures core queries still work
- **Conversation History**: Maintains context across multiple questions
- **Real-time Data**: Always works with live database information
- **Branch Awareness**: Operational results are scoped to the currently selected branch
- **Professional Answers**: Supports safe Markdown headings, lists, code, and tables in Loli responses

### AI Agent Configuration

The AI agent uses APIFree.ai (Gemini 2.5 Flash Lite) for natural language processing. To configure:

1. Go to **Settings** in the dashboard
2. Enter your API key in the AI Agent section
3. Save settings

> **Note**: The AI agent works with real database data and can perform actual operations like creating purchase orders. Always verify important actions.

### Optional Loli long-term memory

Loli can use embedded [Mem0 OSS](https://github.com/mem0ai/mem0) long-term memory for approved user preferences and branch-specific aliases. Memory is disabled by default. It is private by user and branch, auditable, and never stores passwords, API keys, payment data, customer contact data, or raw business results.

To enable it, run a local Ollama instance with `llama3.2` and `nomic-embed-text`, then configure `AI_MEMORY_ENABLED=true` and the local-only `AI_MEMORY_MEM0_CONFIG` example in `.env.example`. The memory vector data persists under the existing `/app/instance` volume. If Mem0 or the local models are unavailable, normal Loli chat continues without persistent recall.

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
├── compose.yaml              # Resource-limited VPS deployment
├── .env.example              # Safe environment template (copy to .env)
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

## 🐳 Docker Deployment (Resource-Limited VPS)

Docker containers have no CPU or memory limit by default. For a VPS, the recommended deployment is `compose.yaml`, which applies conservative limits while keeping the SQLite database and uploads persistent.

### Default resource profile

| Resource | Limit | Notes |
| --- | ---: | --- |
| CPU | `0.75` CPU | The container can use at most 75% of one CPU core. |
| Memory | `768 MiB` | Hard limit; Docker may restart the app if it exceeds this limit. |
| Memory reservation | `384 MiB` | Soft target used when the host is under memory pressure. |
| Memory + swap | `1 GiB` | Allows a small swap buffer instead of unlimited swap usage. |
| Processes/threads | `128` PIDs | Protects the host from process/thread exhaustion. |
| Application workers | `2` threads | Conservative Waitress concurrency for SQLite and small VPS hosts. |
| Temporary storage | `64 MiB` | `/tmp` is a size-limited in-memory filesystem. |
| Container logs | `3 × 10 MiB` | Log rotation prevents Docker logs from filling the VPS disk. |

The `768 MiB` memory limit leaves room for legitimate temporary spikes from Pandas, Excel exports, and PDF generation. If Docker reports OOM kills during large exports, increase it to `1g` rather than disabling the limit.

### Recommended: Docker Compose

Requirements: Docker Engine with the Compose plugin (`docker compose version`).

1. Create the environment file and generate a persistent session secret:

```bash
cp .env.example .env
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" .env
chmod 600 .env
```

Do not commit `.env`. Keep the same `SECRET_KEY` across restarts; changing it signs out all existing sessions.

2. Build and start the service:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 app
```

3. Verify it:

```bash
curl --fail http://127.0.0.1:8888/healthz
docker stats parrot-pos --no-stream
docker inspect parrot-pos --format '{{.State.Health.Status}}'
```

By default, `POS_BIND_ADDRESS=127.0.0.1`, so the application is reachable only from the VPS itself. This is recommended when Nginx, Caddy, or Cloudflare Tunnel is the public entry point. Set `POS_BIND_ADDRESS=0.0.0.0` in `.env` only if port `8888` must be exposed directly, and restrict it with the VPS firewall.

Application URL from the VPS itself:

```text
http://127.0.0.1:8888
```

### Alternative: resource-limited `docker run`

Build the image:

```bash
docker build -t parrot-pos:latest .
docker volume create parrot-pos-instance
docker volume create parrot-pos-uploads
```

Run it with limits equivalent to the Compose profile:

```bash
docker run -d \
  --name parrot-pos \
  --restart unless-stopped \
  --init \
  --cpus="0.75" \
  --memory="768m" \
  --memory-reservation="384m" \
  --memory-swap="1g" \
  --pids-limit=128 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=1777 \
  --security-opt no-new-privileges=true \
  --cap-drop ALL \
  --log-driver json-file \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  -p 127.0.0.1:8888:8888 \
  --env-file .env \
  -v parrot-pos-instance:/app/instance \
  -v parrot-pos-uploads:/app/uploads \
  parrot-pos:latest
```

Named volumes are recommended because the image runs as non-root UID/GID `10001`. If you prefer Linux bind mounts, create and assign them first:

```bash
mkdir -p instance uploads/products
sudo chown -R 10001:10001 instance uploads
```

On Windows, use Docker Compose rather than the Linux `docker run` example; Compose handles path and command differences more reliably.

### Operations

View status, resource usage, health, and logs:

```bash
docker compose ps
docker stats parrot-pos
docker inspect parrot-pos --format 'health={{.State.Health.Status}} oom={{.State.OOMKilled}} restarts={{.RestartCount}}'
docker compose logs -f --tail=100 app
```

Restart or stop without deleting persistent data:

```bash
docker compose restart app
docker compose down
```

Update to the latest source and rebuild:

```bash
git pull --ff-only
docker compose up -d --build
docker image prune -f
```

Back up the named volumes before an update:

```bash
mkdir -p backups
docker compose stop app
docker run --rm -v parrot-pos_pos_instance:/data:ro -v "${PWD}/backups:/backup" alpine sh -c 'tar czf /backup/instance-$(date +%Y%m%d-%H%M%S).tgz -C /data .'
docker run --rm -v parrot-pos_pos_uploads:/data:ro -v "${PWD}/backups:/backup" alpine sh -c 'tar czf /backup/uploads-$(date +%Y%m%d-%H%M%S).tgz -C /data .'
docker compose start app
```

Stopping the app briefly ensures the SQLite backup is consistent. Compose prefixes named volumes with the project name (`parrot-pos`), producing `parrot-pos_pos_instance` and `parrot-pos_pos_uploads`. Confirm names with `docker volume ls` before backup or restore. The manager database-backup function in the dashboard is an alternative that does not require downtime.

Remove the containers **and all persistent POS data** only when you intentionally want a full reset:

```bash
docker compose down --volumes
```

> Warning: `--volumes` permanently deletes the SQLite database and uploaded product images.

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
