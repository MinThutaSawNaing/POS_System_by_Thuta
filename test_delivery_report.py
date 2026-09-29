"""Tests for delivery performance reporting: pure metrics, builders and routes."""

import unittest
import uuid
from datetime import datetime, timedelta

from reports import (
    DELIVERY_OVERDUE_HOURS,
    UNASSIGNED_COURIER,
    build_delivery_performance_report,
    build_delivery_performance_rows,
    build_report_pdf,
    build_report_xlsx,
    delivery_courier_performance,
    delivery_duration_hours,
    parse_report_datetime,
    summarize_delivery_performance,
)


def _iso(moment):
    return moment.isoformat()


def _record(**overrides):
    now = datetime(2026, 9, 29, 12, 0, 0)
    data = {
        "id": 1,
        "delivery_number": "DLV-1",
        "stage": "to_deliver",
        "stage_label": "To Deliver",
        "priority": "normal",
        "recipient_name": "Recipient",
        "township": "Kamayut",
        "courier_name": None,
        "delivery_fee": 500,
        "created_at": _iso(now - timedelta(hours=2)),
    }
    data.update(overrides)
    return data


class DeliveryDurationTests(unittest.TestCase):
    def test_duration_between_iso_stamps(self):
        start = "2026-09-28T10:00:00"
        end = "2026-09-29T12:30:00"
        self.assertEqual(delivery_duration_hours(start, end), 26.5)

    def test_duration_is_none_for_missing_or_bad_values(self):
        self.assertIsNone(delivery_duration_hours(None, "2026-09-29T12:00:00"))
        self.assertIsNone(delivery_duration_hours("2026-09-29T12:00:00", ""))
        self.assertIsNone(delivery_duration_hours("not-a-date", "2026-09-29T12:00:00"))

    def test_negative_duration_clamps_to_zero(self):
        self.assertEqual(delivery_duration_hours("2026-09-29T12:00:00", "2026-09-28T12:00:00"), 0.0)

    def test_mixed_timezone_awareness_is_normalized(self):
        naive = "2026-09-29T12:00:00"
        aware = "2026-09-29T18:30:00+06:30"  # 12:00 UTC
        self.assertEqual(delivery_duration_hours(naive, aware), 0.0)

    def test_parse_report_datetime_handles_z_suffix_and_junk(self):
        self.assertEqual(parse_report_datetime("2026-09-29T12:00:00Z"), datetime(2026, 9, 29, 12, 0, 0))
        self.assertIsNone(parse_report_datetime("junk"))
        self.assertIsNone(parse_report_datetime(None))


class DeliveryPerformanceRowTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 29, 12, 0, 0)

    def test_delivered_row_computes_leg_times_and_flag(self):
        rows = build_delivery_performance_rows([_record(
            stage="delivered", stage_label="Delivered",
            created_at=_iso(self.now - timedelta(hours=10)),
            packaged_at=_iso(self.now - timedelta(hours=9)),
            out_for_delivery_at=_iso(self.now - timedelta(hours=8)),
            delivered_at=_iso(self.now - timedelta(hours=6)),
        )], now=self.now)
        row = rows[0]
        self.assertEqual(row["timing_flag"], "Delivered")
        self.assertEqual(row["fulfillment_hours"], 4.0)
        self.assertEqual(row["packing_hours"], 1.0)
        self.assertEqual(row["dispatch_hours"], 1.0)
        self.assertEqual(row["final_leg_hours"], 2.0)
        self.assertIsNone(row["age_hours"])

    def test_open_row_ages_and_turns_overdue(self):
        fresh = build_delivery_performance_rows([_record()], now=self.now)[0]
        self.assertEqual(fresh["timing_flag"], "On track")
        self.assertEqual(fresh["age_hours"], 2.0)

        stale = build_delivery_performance_rows([_record(
            created_at=_iso(self.now - timedelta(hours=DELIVERY_OVERDUE_HOURS + 1)),
        )], now=self.now)[0]
        self.assertEqual(stale["timing_flag"], "Overdue")

    def test_cancelled_rows_are_never_overdue(self):
        row = build_delivery_performance_rows([_record(
            stage="cancelled", stage_label="Cancelled",
            created_at=_iso(self.now - timedelta(hours=100)),
        )], now=self.now)[0]
        self.assertEqual(row["timing_flag"], "Cancelled")
        self.assertIsNone(row["age_hours"])

    def test_missing_courier_becomes_unassigned(self):
        row = build_delivery_performance_rows([_record()], now=self.now)[0]
        self.assertEqual(row["courier_name"], UNASSIGNED_COURIER)


class DeliverySummaryTests(unittest.TestCase):
    def rows(self):
        now = datetime(2026, 9, 29, 12, 0, 0)
        return build_delivery_performance_rows([
            _record(id=1, stage="delivered", courier_name="Aung",
                    created_at=_iso(now - timedelta(hours=20)),
                    delivered_at=_iso(now - timedelta(hours=16))),
            _record(id=2, stage="delivered", courier_name="Aung",
                    created_at=_iso(now - timedelta(hours=50)),
                    delivered_at=_iso(now - timedelta(hours=10))),
            _record(id=3, stage="to_deliver", courier_name="Kyaw",
                    created_at=_iso(now - timedelta(hours=30))),
            _record(id=4, stage="packaged",
                    created_at=_iso(now - timedelta(hours=1))),
            _record(id=5, stage="cancelled",
                    created_at=_iso(now - timedelta(hours=5))),
        ], now=now)

    def test_kpis_counts_rates_and_averages(self):
        kpis = summarize_delivery_performance(self.rows())
        self.assertEqual(kpis["total"], 5)
        self.assertEqual(kpis["delivered"], 2)
        self.assertEqual(kpis["open"], 2)
        self.assertEqual(kpis["overdue"], 1)
        self.assertEqual(kpis["cancelled"], 1)
        # completion ignores cancelled: 2 delivered of 4 expected
        self.assertEqual(kpis["completion_rate"], 50.0)
        # one of two delivered within 24h
        self.assertEqual(kpis["on_time_rate"], 50.0)
        self.assertEqual(kpis["avg_fulfillment_hours"], 22.0)  # (4 + 40) / 2
        self.assertEqual(kpis["fastest_fulfillment_hours"], 4.0)
        self.assertEqual(kpis["slowest_fulfillment_hours"], 40.0)

    def test_empty_rows_yield_safe_nulls(self):
        kpis = summarize_delivery_performance([])
        self.assertEqual(kpis["total"], 0)
        self.assertIsNone(kpis["completion_rate"])
        self.assertIsNone(kpis["on_time_rate"])
        self.assertIsNone(kpis["avg_fulfillment_hours"])

    def test_courier_grouping_and_sorting(self):
        couriers = delivery_courier_performance(self.rows())
        by_name = {entry["courier_name"]: entry for entry in couriers}
        self.assertEqual(by_name["Aung"]["assigned"], 2)
        self.assertEqual(by_name["Aung"]["delivered"], 2)
        self.assertEqual(by_name["Aung"]["avg_fulfillment_hours"], 22.0)
        self.assertEqual(by_name["Kyaw"]["open"], 1)
        self.assertEqual(by_name["Kyaw"]["overdue"], 1)
        self.assertEqual(by_name[UNASSIGNED_COURIER]["assigned"], 2)
        self.assertEqual(couriers[0]["courier_name"], "Aung")  # most assigned first


class DeliveryReportBuilderTests(unittest.TestCase):
    def test_report_builds_pdf_and_xlsx_payloads(self):
        rows = build_delivery_performance_rows([
            _record(stage="delivered", courier_name="Aung",
                    created_at="2026-09-28T10:00:00",
                    delivered_at="2026-09-28T18:00:00"),
            _record(id=2, delivery_number="DLV-2", stage="to_deliver",
                    created_at="2026-09-26T10:00:00"),
        ], now=datetime(2026, 9, 29, 12, 0, 0))
        report = build_delivery_performance_report(
            rows, brand={"name": "Parrot POS"}, branch_name="Main",
            generated_by="admin", filters_text="From: 2026-09-01",
            currency_suffix="MMK",
        )
        self.assertEqual(report["title"], "Delivery Performance Report")
        self.assertEqual(len(report["rows"]), 2)
        self.assertEqual(report["totals"]["delivery_fee"], 1000.0)
        summary_labels = [entry["label"] for entry in report["summary"]]
        self.assertTrue(any("Overdue" in label for label in summary_labels))

        pdf = build_report_pdf(report)
        self.assertTrue(pdf.startswith(b"%PDF"))
        workbook = build_report_xlsx(report)
        self.assertGreater(len(workbook), 0)


class DeliveryReportRouteTests(unittest.TestCase):
    """Route-level checks against the live schema (test rows are cleaned up)."""

    def setUp(self):
        from app import Delivery, Sale, User, app, db, get_default_branch_id
        self.app = app
        self.db = db
        app.config.update(TESTING=True)
        self.context = app.app_context()
        self.context.push()
        self.user = User.query.filter_by(username='admin').first()
        self.branch_id = get_default_branch_id()
        self.assertIsNotNone(self.branch_id)

        now = datetime.utcnow()
        suffix = uuid.uuid4().hex[:8]
        self.sales = []
        self.deliveries = []
        specs = [
            # (stage, created offset, extra timestamps)
            ('delivered', timedelta(days=2), {
                'packaged_at': now - timedelta(days=2) + timedelta(hours=1),
                'out_for_delivery_at': now - timedelta(days=2) + timedelta(hours=2),
                'delivered_at': now - timedelta(days=2) + timedelta(hours=4),
                'courier_name': 'Report Courier',
            }),
            ('to_deliver', timedelta(days=3), {}),
        ]
        for index, (stage, created_offset, extra) in enumerate(specs):
            sale = Sale(
                transaction_id=f'report-test-{suffix}-{index}',
                total=1000, tax=0, payment_method='cash',
                user_id=self.user.id, branch_id=self.branch_id,
            )
            db.session.add(sale)
            db.session.commit()
            delivery = Delivery(
                delivery_number=f'DLV-RPT-{suffix}-{index}',
                sale_id=sale.id, stage=stage, priority='normal',
                recipient_name='Report Recipient', recipient_phone='09111111111',
                delivery_address='Report Street', delivery_fee=300,
                branch_id=self.branch_id, created_by=self.user.id,
                created_at=now - created_offset, **extra,
            )
            db.session.add(delivery)
            db.session.commit()
            self.sales.append(sale)
            self.deliveries.append(delivery)
        self.date_from = (now - timedelta(days=4)).strftime('%Y-%m-%d')
        self.date_to = now.strftime('%Y-%m-%d')

    def tearDown(self):
        from app import Sale
        db = self.db
        for delivery in self.deliveries:
            db.session.delete(db.session.get(type(delivery), delivery.id))
        for sale in self.sales:
            db.session.delete(db.session.get(Sale, sale.id))
        db.session.commit()
        self.context.pop()

    def client(self, role='manager'):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.user.id
            session['role'] = role
            session['branch_id'] = self.branch_id
            session['username'] = 'admin'
        return client

    def test_report_requires_login(self):
        response = self.app.test_client().get('/api/deliveries/report')
        self.assertEqual(response.status_code, 401)

    def test_report_rejects_bad_dates(self):
        response = self.client().get('/api/deliveries/report?date_from=not-a-date')
        self.assertEqual(response.status_code, 400)

    def test_report_returns_kpis_couriers_and_attention(self):
        response = self.client().get(
            f'/api/deliveries/report?date_from={self.date_from}&date_to={self.date_to}')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        for key in ('total', 'delivered', 'open', 'overdue', 'cancelled',
                    'completion_rate', 'on_time_rate', 'avg_fulfillment_hours'):
            self.assertIn(key, data['kpis'])
        self.assertIn('to_deliver', data['by_stage'])
        couriers = {entry['courier_name'] for entry in data['courier_performance']}
        self.assertIn('Report Courier', couriers)
        numbers = {entry['delivery_number'] for entry in data['attention']}
        open_number = self.deliveries[1].delivery_number
        # The watchlist is capped at 50 rows, so only assert membership when complete.
        if len(data['attention']) < 50:
            self.assertIn(open_number, numbers)
        flagged = [entry for entry in data['attention']
                   if entry['delivery_number'] == open_number]
        if flagged:
            self.assertEqual(flagged[0]['timing_flag'], 'Overdue')

    def test_date_range_excludes_out_of_range_deliveries(self):
        future = (datetime.utcnow() + timedelta(days=2)).strftime('%Y-%m-%d')
        response = self.client().get(f'/api/deliveries/report?date_from={future}')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['total'], 0)

    def test_export_requires_manager_or_boss(self):
        self.assertEqual(self.client(role='cashier').get('/api/deliveries/export').status_code, 403)
        self.assertEqual(self.client(role='manager').get('/api/deliveries/export').status_code, 200)
        self.assertEqual(self.client(role='boss').get('/api/deliveries/export').status_code, 200)

    def test_export_downloads_pdf_and_xlsx(self):
        params = f'date_from={self.date_from}&date_to={self.date_to}'
        pdf = self.client().get(f'/api/deliveries/export?format=pdf&{params}')
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.headers['Content-Type'], 'application/pdf')
        self.assertTrue(pdf.data.startswith(b'%PDF'))
        self.assertIn('inline', pdf.headers['Content-Disposition'])

        xlsx = self.client().get(f'/api/deliveries/export?format=xlsx&{params}')
        self.assertEqual(xlsx.status_code, 200)
        self.assertIn('spreadsheetml', xlsx.headers['Content-Type'])
        self.assertTrue(xlsx.data.startswith(b'PK'))
        self.assertIn('attachment', xlsx.headers['Content-Disposition'])

if __name__ == '__main__':
    unittest.main()
