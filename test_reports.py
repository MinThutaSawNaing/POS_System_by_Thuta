"""Tests for the professional PDF/Excel reporting on the Warehouse and
Purchase & Receiving tabs.

Three layers are covered: the report builders (rows, KPI summary, totals and
currency handling), the renderers (valid PDF/Excel bytes carrying the expected
content) and the export endpoints (manager gate, tab filters, content type and
filenames).
"""

import io
import unittest
import uuid
import zipfile
from datetime import datetime

import reports
from app import (DEFAULT_RECEIPT_BRAND_NAME, Branch, Product, PurchaseOrder,
                 PurchaseOrderItem, Supplier, User, WarehouseInventory, app, db)


class WarehouseReportBuilderTests(unittest.TestCase):
    @staticmethod
    def _records():
        return [
            {
                "id": 1, "product_id": 1, "product_name": "Coffee Beans",
                "barcode": "BC-1", "location": "Shelf A1", "batch_number": "PO-100",
                "quantity": 3, "unit_cost": 2.5, "total_value": 7.5,
                "main_stock": 12, "received_date": "2026-09-01T08:00:00",
                "expiry_date": None,
            },
            {
                "id": 2, "product_id": 2, "product_name": "Green Tea",
                "barcode": "BC-2", "location": "Shelf B2", "batch_number": "PO-101",
                "quantity": 40, "unit_cost": 1.25, "total_value": 50.0,
                "main_stock": 0, "received_date": "2026-09-02T08:00:00",
                "expiry_date": "2027-01-31T00:00:00",
            },
        ]

    def _report(self, **overrides):
        kwargs = {
            "brand": {
                "name": "Parrot POS", "address": "1 Main St\nYangon",
                "phone": "555", "email": "shop@example.com",
            },
            "branch_name": "Main Branch",
            "generated_by": "manager",
            "filters_text": "All items",
            "currency_suffix": "MMK",
            "generated_at": datetime(2026, 9, 21, 10, 30),
        }
        kwargs.update(overrides)
        return reports.build_warehouse_stock_report(self._records(), **kwargs)

    def test_rows_carry_the_tab_columns(self):
        report = self._report()
        self.assertEqual(
            [entry["label"] for entry in report["columns"]],
            ["Product", "Barcode", "Location", "Batch", "Qty", "Unit cost",
             "Stock value", "Shop stock", "Received", "Expiry"],
        )
        first = report["rows"][0]
        self.assertEqual(first["product_name"], "Coffee Beans")
        self.assertEqual(first["quantity"], 3)
        self.assertEqual(first["unit_cost"], 2.5)
        self.assertEqual(first["total_value"], 7.5)
        self.assertEqual(first["main_stock"], 12)

    def test_summary_and_totals_cover_units_value_and_low_stock(self):
        report = self._report()
        self.assertEqual(report["totals"]["quantity"], 43)
        self.assertEqual(report["totals"]["total_value"], 57.5)
        summary = {entry["label"]: entry["value"] for entry in report["summary"]}
        self.assertEqual(summary["Products in stock"], "2")
        self.assertEqual(summary["Stock lines"], "2")
        self.assertEqual(summary["Units in warehouse"], "43")
        self.assertEqual(summary["Stock value"], "57.50 MMK")
        self.assertEqual(summary[f"Low stock lines (≤ {reports.LOW_STOCK_THRESHOLD})"], "1")

    def test_value_is_derived_when_the_api_omits_it(self):
        records = [{
            "product_id": 7, "product_name": "No value field", "quantity": 4,
            "unit_cost": 3.25, "main_stock": 1,
        }]
        report = reports.build_warehouse_stock_report(records, currency_suffix="MMK")
        self.assertEqual(report["rows"][0]["total_value"], 13.0)
        self.assertEqual(report["totals"]["total_value"], 13.0)

    def test_meta_carries_branch_author_time_and_filters(self):
        report = self._report()
        meta = {entry["label"]: entry["value"] for entry in report["meta"]}
        self.assertEqual(meta["Branch"], "Main Branch")
        self.assertEqual(meta["Generated"], "2026-09-21 10:30")
        self.assertEqual(meta["Prepared by"], "manager")
        self.assertEqual(meta["Filters"], "All items")
        self.assertEqual(report["file_stem"], "warehouse_stock_list")
        self.assertEqual(report["brand"]["name"], "Parrot POS")
        self.assertEqual(report["brand"]["address_lines"], ["1 Main St", "Yangon"])

    def test_empty_stock_list_is_still_a_valid_report(self):
        report = reports.build_warehouse_stock_report([], branch_name="Main")
        self.assertEqual(report["rows"], [])
        self.assertEqual(report["totals"], {"quantity": 0, "total_value": 0.0})
        summary = {entry["label"]: entry["value"] for entry in report["summary"]}
        self.assertEqual(summary["Stock value"], "0.00 $")


class PurchaseOrderReportBuilderTests(unittest.TestCase):
    @staticmethod
    def _records():
        return [
            {
                "po_number": "PO-1", "supplier_name": "Acme Supplies",
                "status": "partially_received", "total_amount": 500.0,
                "items_count": 2, "total_ordered": 10, "total_received": 4,
                "expected_delivery_date": "2026-10-01T00:00:00",
                "created_at": "2026-09-10T09:00:00", "created_by": "manager",
                "approved_by": "boss",
                "items": [
                    {"product_name": "Coffee Beans", "ordered_qty": 6,
                     "received_qty": 4, "unit_cost": 50.0, "line_total": 300.0},
                    {"product_name": "Green Tea", "ordered_qty": 4,
                     "received_qty": 0, "unit_cost": 50.0, "line_total": 200.0},
                ],
            },
            {
                "po_number": "PO-2", "supplier_name": "Acme Supplies",
                "status": "pending", "total_amount": 120.5,
                "items_count": 1, "total_ordered": 5, "total_received": 0,
                "expected_delivery_date": None, "created_at": "2026-09-12T09:00:00",
                "created_by": "manager", "approved_by": None,
                "items": [
                    {"product_name": "Green Tea", "ordered_qty": 5,
                     "received_qty": 0, "unit_cost": 24.1, "line_total": 120.5},
                ],
            },
        ]

    def _report(self, **overrides):
        kwargs = {
            "brand": {"name": "Parrot POS"},
            "branch_name": "Main Branch",
            "generated_by": "manager",
            "filters_text": "From: 2026-09-01",
            "currency_suffix": "MMK",
            "generated_at": datetime(2026, 9, 21, 11, 0),
        }
        kwargs.update(overrides)
        return reports.build_purchase_order_report(self._records(), **kwargs)

    def test_rows_use_readable_status_labels(self):
        report = self._report()
        self.assertEqual(report["rows"][0]["status_label"], "Partially received")
        self.assertEqual(report["rows"][1]["status_label"], "Pending approval")
        self.assertEqual(report["rows"][0]["supplier_name"], "Acme Supplies")
        self.assertEqual(report["rows"][0]["total_ordered"], 10)
        self.assertEqual(report["rows"][0]["total_received"], 4)

    def test_summary_tracks_statuses_and_order_value(self):
        report = self._report()
        summary = {entry["label"]: entry["value"] for entry in report["summary"]}
        self.assertEqual(summary["Purchase orders"], "2")
        self.assertEqual(summary["Pending approval"], "1")
        self.assertEqual(summary["Partially received"], "1")
        self.assertEqual(summary["Received"], "0")
        self.assertEqual(summary["Ordered units"], "15")
        self.assertEqual(summary["Order value"], "620.50 MMK")
        self.assertEqual(report["totals"]["total_amount"], 620.5)

    def test_item_sheet_flattens_lines_with_progress(self):
        sheet = reports.build_purchase_order_item_sheet(
            self._records(), currency_suffix="MMK"
        )
        self.assertEqual(sheet["name"], "PO Line Items")
        self.assertEqual(len(sheet["rows"]), 3)
        first = sheet["rows"][0]
        self.assertEqual(first["po_number"], "PO-1")
        self.assertEqual(first["product_name"], "Coffee Beans")
        self.assertEqual(first["progress_percent"], 67)
        self.assertEqual(first["line_total"], 300.0)
        self.assertEqual(sheet["totals"]["ordered_qty"], 15)
        self.assertEqual(sheet["totals"]["received_qty"], 4)
        self.assertEqual(sheet["totals"]["line_total"], 620.5)

    def test_unordered_lines_report_zero_progress(self):
        sheet = reports.build_purchase_order_item_sheet([{
            "po_number": "PO-9",
            "items": [{"product_name": "Odd", "ordered_qty": 0, "received_qty": 0,
                       "unit_cost": 1.0}],
        }])
        self.assertEqual(sheet["rows"][0]["progress_percent"], 0)
        self.assertEqual(sheet["rows"][0]["line_total"], 0.0)


class ReportFormattingTests(unittest.TestCase):
    def test_describe_filters_skips_blank_values(self):
        self.assertEqual(
            reports.describe_filters(
                {"Search": "coffee", "Status": "", "Low stock only": False}
            ),
            "Search: coffee",
        )
        self.assertEqual(
            reports.describe_filters({}),
            "No filters applied — all records",
        )

    def test_format_cell_handles_missing_and_broken_money(self):
        self.assertEqual(reports.format_report_cell(None, "money", "MMK"), "0.00 MMK")
        self.assertEqual(reports.format_report_cell("abc", "money", "$"), "0.00 $")
        self.assertEqual(reports.format_report_cell(float("nan"), "int"), "0")
        self.assertEqual(
            reports.format_report_cell("2026-09-21T08:00:00", "date"), "2026-09-21"
        )
        self.assertEqual(reports.format_report_cell("", "text"), "—")

    def test_format_normalization_and_download_headers(self):
        self.assertEqual(reports.normalize_report_format("XLSX"), "xlsx")
        self.assertEqual(reports.normalize_report_format("csv"), "pdf")
        generated = datetime(2026, 9, 21, 12, 0)
        self.assertEqual(
            reports.report_filename("warehouse_stock_list", "xlsx", generated),
            "warehouse_stock_list_2026-09-21.xlsx",
        )
        self.assertEqual(
            reports.report_disposition("stock.pdf", "pdf"),
            "inline; filename=stock.pdf",
        )
        self.assertEqual(
            reports.report_disposition("stock.xlsx", "xlsx"),
            "attachment; filename=stock.xlsx",
        )
        self.assertEqual(
            reports.report_content_type("xlsx"), reports.XLSX_CONTENT_TYPE
        )
        self.assertEqual(reports.report_content_type("pdf"), reports.PDF_CONTENT_TYPE)



class ReportRendererTests(unittest.TestCase):
    """The renderers must produce documents a customer could be handed."""

    @staticmethod
    def _stock_report(records=None):
        records = records if records is not None else [
            {"product_id": 1, "product_name": "Coffee Beans", "barcode": "BC-1",
             "location": "A1", "batch_number": "PO-100", "quantity": 3,
             "unit_cost": 2.5, "total_value": 7.5, "main_stock": 4,
             "received_date": "2026-09-01T08:00:00", "expiry_date": None},
            {"product_id": 2, "product_name": "Green Tea", "barcode": "BC-2",
             "location": "B2", "batch_number": "PO-101", "quantity": 40,
             "unit_cost": 1.25, "total_value": 50.0, "main_stock": 0,
             "received_date": "2026-09-02T08:00:00", "expiry_date": None},
        ]
        return reports.build_warehouse_stock_report(
            records,
            brand={"name": "Parrot POS", "address": "1 Main St", "phone": "555",
                   "email": "shop@example.com"},
            branch_name="Main Branch",
            generated_by="manager",
            filters_text="All items",
            currency_suffix="MMK",
            generated_at=datetime(2026, 9, 21, 10, 0),
        )

    def test_pdf_is_branded_and_page_numbered(self):
        pdf = reports.build_report_pdf(self._stock_report(), compress=False)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        for expected in (b"Parrot POS", b"Main St", b"Tel 555", b"shop@example.com",
                         b"Warehouse Stock List", b"Coffee Beans", b"Stock value",
                         b"TOTAL", b"Page 1 of 1"):
            self.assertIn(expected, pdf)

    def test_pdf_numbers_every_page_of_a_long_report(self):
        records = [
            {"product_id": index, "product_name": f"Stock line {index}",
             "quantity": index % 7, "unit_cost": 1.0, "total_value": float(index % 7),
             "main_stock": 0}
            for index in range(160)
        ]
        report = self._stock_report(records)
        pdf = reports.build_report_pdf(report, compress=False)
        self.assertIn(b"Page 2 of ", pdf)
        self.assertIn(b"Page", pdf)

    def test_pdf_treats_hostile_names_as_text(self):
        records = [{
            "product_id": 1, "product_name": "Coffee <b>bold</b> & <i>Co</i>",
            "quantity": 1, "unit_cost": 1.0, "total_value": 1.0, "main_stock": 0,
        }]
        pdf = reports.build_report_pdf(self._stock_report(records), compress=False)
        self.assertIn(b"Coffee <", pdf)

    def test_pdf_states_when_nothing_matches(self):
        report = reports.build_warehouse_stock_report(
            [], brand={"name": "Parrot POS"}, generated_at=datetime(2026, 9, 21)
        )
        pdf = reports.build_report_pdf(report, compress=False)
        self.assertIn(b"No records match the selected filters.", pdf)

    def _workbook(self, payload):
        self.assertTrue(payload.startswith(b"PK"))
        archive = zipfile.ZipFile(io.BytesIO(payload))
        return archive, {
            name: archive.read(name).decode("utf-8")
            for name in ("xl/workbook.xml", "xl/worksheets/sheet1.xml", "xl/sharedStrings.xml")
        }

    def test_excel_workbook_is_filterable_and_numeric(self):
        payload = reports.build_report_xlsx(self._stock_report())
        archive, parts = self._workbook(payload)
        self.assertIn("Coffee Beans", parts["xl/sharedStrings.xml"])
        self.assertIn("Warehouse Stock List", parts["xl/sharedStrings.xml"])
        self.assertIn("TOTAL", parts["xl/sharedStrings.xml"])
        self.assertIn("Stock List", parts["xl/workbook.xml"])
        self.assertIn("Report Info", parts["xl/workbook.xml"])
        sheet = parts["xl/worksheets/sheet1.xml"]
        # Header on row 4, two data rows, then the totals row: the filter must
        # cover the data rows only, and the totals must not overwrite them.
        self.assertIn('autoFilter ref="A4:J6"', sheet)
        self.assertIn('<row r="7"', sheet)
        self.assertIn("<v>7.5</v>", sheet)     # first line stock value
        self.assertIn("<v>40</v>", sheet)      # second line quantity (data row)
        self.assertIn("<v>50</v>", sheet)      # second line stock value
        self.assertIn("<v>43</v>", sheet)      # total units (totals row)
        self.assertIn("<v>57.5</v>", sheet)    # total stock value

    def test_excel_extras_render_extra_sheets(self):
        report = reports.build_purchase_order_report(
            [{
                "po_number": "PO-77", "supplier_name": "Acme", "status": "received",
                "total_amount": 90.0, "items_count": 1, "total_ordered": 3,
                "total_received": 3, "created_by": "manager",
                "items": [{"product_name": "Coffee Beans", "ordered_qty": 3,
                           "received_qty": 3, "unit_cost": 30.0, "line_total": 90.0}],
            }],
            brand={"name": "Parrot POS"},
            currency_suffix="MMK",
            generated_at=datetime(2026, 9, 21),
        )
        payload = reports.build_report_xlsx(report, extra_sheets=[
            reports.build_purchase_order_item_sheet(
                [{
                    "po_number": "PO-77",
                    "items": [{"product_name": "Coffee Beans", "ordered_qty": 3,
                               "received_qty": 3, "unit_cost": 30.0,
                               "line_total": 90.0}],
                }],
                currency_suffix="MMK",
            )
        ])
        archive, parts = self._workbook(payload)
        self.assertIn("PO Line Items", parts["xl/workbook.xml"])
        shared = parts["xl/sharedStrings.xml"]
        self.assertIn("Purchase Order Report", shared)
        self.assertIn("Coffee Beans", shared)
        item_sheet = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
        self.assertIn('autoFilter ref="A4:G5"', item_sheet)
        # Progress and money are numeric cells so the columns can be summed.
        self.assertIn("<v>100</v>", item_sheet)   # 3 of 3 received
        self.assertIn("<v>90</v>", item_sheet)    # line total + line total total
        self.assertIn('<row r="6"', item_sheet)   # totals row below the single line



class ReportExportEndpointTests(unittest.TestCase):
    """PDF/Excel downloads for the Warehouse and Purchase tabs."""

    def setUp(self):
        app.config.update(TESTING=True)
        suffix = uuid.uuid4().hex[:8].upper()
        with app.app_context():
            self.user_id = User.query.filter_by(username='admin').first().id
            branch = (
                Branch.query.filter_by(is_default=True, is_active=True).first()
                or Branch.query.filter_by(is_active=True).first()
            )
            self.assertIsNotNone(branch, "a branch is required for report scoping")
            self.branch_id = branch.id

            self.low_product = Product(
                barcode=f'RPT-{suffix}-LO', name=f'Report Low Stock {suffix}',
                price=10.0, cost=4.0, stock=2, tax_rate=0.0, branch_id=self.branch_id,
            )
            self.full_product = Product(
                barcode=f'RPT-{suffix}-HI', name=f'Report Full Stock {suffix}',
                price=10.0, cost=4.0, stock=20, tax_rate=0.0, branch_id=self.branch_id,
            )
            db.session.add_all([self.low_product, self.full_product])
            db.session.commit()
            self.product_ids = [self.low_product.id, self.full_product.id]
            self.low_name = self.low_product.name
            self.full_name = self.full_product.name

            db.session.add_all([
                WarehouseInventory(
                    product_id=self.low_product.id, quantity=3, unit_cost=4.0,
                    location='Shelf A1', batch_number=f'B-{suffix}-1',
                    branch_id=self.branch_id,
                ),
                WarehouseInventory(
                    product_id=self.full_product.id, quantity=50, unit_cost=4.0,
                    location='Shelf B2', batch_number=f'B-{suffix}-2',
                    branch_id=self.branch_id,
                ),
            ])
            self.supplier = Supplier(
                name=f'Report Supplier {suffix}', branch_id=self.branch_id
            )
            db.session.add(self.supplier)
            db.session.commit()
            self.supplier_id = self.supplier.id
            self.supplier_name = self.supplier.name

            draft = PurchaseOrder(
                po_number=f'PO-{suffix}-DRAFT', supplier_id=self.supplier_id,
                status='draft', total_amount=100.0, created_by=self.user_id,
                branch_id=self.branch_id,
            )
            pending = PurchaseOrder(
                po_number=f'PO-{suffix}-PEND', supplier_id=self.supplier_id,
                status='pending', total_amount=250.0, created_by=self.user_id,
                branch_id=self.branch_id,
            )
            db.session.add_all([draft, pending])
            db.session.flush()
            db.session.add(PurchaseOrderItem(
                purchase_order_id=pending.id, product_id=self.full_product.id,
                ordered_qty=5, received_qty=2, unit_cost=50.0,
            ))
            db.session.commit()
            self.po_numbers = [draft.po_number, pending.po_number]
            self.draft_po_number = draft.po_number
            self.pending_po_number = pending.po_number

    def tearDown(self):
        with app.app_context():
            po_ids = [
                po.id for po in
                PurchaseOrder.query.filter(PurchaseOrder.po_number.in_(self.po_numbers)).all()
            ]
            if po_ids:
                PurchaseOrderItem.query.filter(
                    PurchaseOrderItem.purchase_order_id.in_(po_ids)
                ).delete(synchronize_session=False)
                PurchaseOrder.query.filter(
                    PurchaseOrder.id.in_(po_ids)
                ).delete(synchronize_session=False)
            WarehouseInventory.query.filter(
                WarehouseInventory.product_id.in_(self.product_ids)
            ).delete(synchronize_session=False)
            Product.query.filter(
                Product.id.in_(self.product_ids)
            ).delete(synchronize_session=False)
            Supplier.query.filter(
                Supplier.id == self.supplier_id
            ).delete(synchronize_session=False)
            db.session.commit()

    def _client(self, role='manager'):
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.user_id
            session['role'] = role
            session['branch_id'] = self.branch_id
            session['username'] = 'admin'
        return client

    @staticmethod
    def _workbook(payload):
        archive = zipfile.ZipFile(io.BytesIO(payload))
        return archive, archive.read("xl/sharedStrings.xml").decode("utf-8")



    def test_exports_are_manager_only(self):
        anonymous = app.test_client()
        for url in ("/api/warehouse/export", "/api/purchase_orders/export"):
            self.assertEqual(anonymous.get(url).status_code, 403)
            self.assertEqual(self._client(role='cashier').get(url).status_code, 403)

    def test_warehouse_pdf_download_headers(self):
        response = self._client().get('/api/warehouse/export?format=pdf')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Content-Type'], reports.PDF_CONTENT_TYPE)
        self.assertTrue(response.data.startswith(b'%PDF'))
        self.assertIn('inline; filename=warehouse_stock_list_', response.headers['Content-Disposition'])
        self.assertTrue(response.headers['Content-Disposition'].endswith('.pdf'))

    def test_warehouse_excel_honours_the_tab_filters(self):
        client = self._client()
        search = client.get(f'/api/warehouse/export?format=xlsx&q={self.low_name}')
        self.assertEqual(search.status_code, 200)
        self.assertEqual(
            search.headers['Content-Type'], reports.XLSX_CONTENT_TYPE
        )
        self.assertIn(
            'attachment; filename=warehouse_stock_list_',
            search.headers['Content-Disposition'],
        )
        _, strings = self._workbook(search.data)
        self.assertIn(self.low_name, strings)
        self.assertNotIn(self.full_name, strings)

        low_stock = client.get('/api/warehouse/export?format=xlsx&low_stock=true')
        _, low_strings = self._workbook(low_stock.data)
        self.assertIn(self.low_name, low_strings)
        self.assertNotIn(self.full_name, low_strings)
        self.assertIn("Low stock only: True", low_strings)

    def test_purchase_order_pdf_download_headers(self):
        response = self._client().get('/api/purchase_orders/export?format=pdf')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Content-Type'], reports.PDF_CONTENT_TYPE)
        self.assertTrue(response.data.startswith(b'%PDF'))
        self.assertIn('inline; filename=purchase_orders_', response.headers['Content-Disposition'])

    def test_purchase_order_excel_carries_register_items_and_cover_sheet(self):
        response = self._client().get(
            f'/api/purchase_orders/export?format=xlsx&supplier_id={self.supplier_id}'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Content-Type'], reports.XLSX_CONTENT_TYPE)
        archive, strings = self._workbook(response.data)
        workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
        self.assertIn(self.pending_po_number, strings)
        self.assertIn(self.draft_po_number, strings)
        self.assertIn(self.supplier_name, strings)
        self.assertIn(self.full_name, strings)      # line-item sheet
        self.assertIn("PO Line Items", workbook_xml)
        self.assertIn("Report Info", workbook_xml)
        self.assertIn("Purchase Order Report", strings)

    def test_purchase_order_status_filter_narrows_the_report(self):
        response = self._client().get('/api/purchase_orders/export?format=xlsx&status=draft')
        self.assertEqual(response.status_code, 200)
        archive, strings = self._workbook(response.data)
        self.assertIn(self.draft_po_number, strings)
        self.assertNotIn(self.pending_po_number, strings)
        self.assertIn("Status: Draft", strings)
        # The draft PO has no line items, so the detail sheet stays empty (header
        # only) but keeps the workbook layout predictable.
        item_sheet = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
        self.assertIn('<row r="4"', item_sheet)
        self.assertNotIn('<row r="5"', item_sheet)

    def test_unknown_format_falls_back_to_pdf(self):
        response = self._client().get('/api/warehouse/export?format=csv')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Content-Type'], reports.PDF_CONTENT_TYPE)
        self.assertTrue(response.data.startswith(b'%PDF'))
