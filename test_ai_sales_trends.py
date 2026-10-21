"""Sales tools must use the real ORM schema; run via run_tests.py."""

from datetime import datetime, timedelta

from app import Sale, SaleItem, db
from agent_orchestrator import AgentOrchestrator
from test_full_coverage_tools import FullCoverageToolsTestBase


class SalesTrendsTests(FullCoverageToolsTestBase):
    def _sale(self, product, quantity, age=0, branch_id=None):
        sale = Sale(total=quantity * 2.5, tax=0,
                    date=datetime.utcnow() - timedelta(days=age),
                    branch_id=branch_id or self.branch_id)
        db.session.add(sale)
        db.session.flush()
        db.session.add(SaleItem(sale_id=sale.id, product_id=product.id,
                                quantity=quantity, price=2.5, tax=0))
        db.session.commit()
        return sale

    def test_nonempty_sales_use_real_models(self):
        product = self._product('Trend regression')
        self._sale(product, 3)
        result = self.tools.get_sales_trends()
        self.assertEqual(result['top_selling_products'][0]['product_name'], product.name)
        self.assertEqual(result['top_selling_products'][0]['total_quantity'], 3)

    def test_all_time_includes_old_sales_but_not_other_branches(self):
        product = self._product('Historical bestseller')
        self._sale(product, 2)
        self._sale(product, 8, age=800)
        other = self._sale(product, 100, branch_id=self.original_default.id)
        try:
            recent = self.tools.get_sales_trends()
            history = self.tools.get_sales_trends(all_time=True)
            self.assertEqual(recent['top_selling_products'][0]['total_quantity'], 2)
            self.assertEqual(history['top_selling_products'][0]['total_quantity'], 10)
            self.assertEqual(history['period'], 'all_time')
            self.assertIsNone(history['period_days'])
        finally:
            SaleItem.query.filter_by(sale_id=other.id).delete()
            db.session.delete(other)
            db.session.commit()

    def test_product_filter_and_top_n(self):
        low = self._product('Low seller')
        high = self._product('High seller')
        self._sale(low, 1)
        self._sale(high, 5)
        result = self.tools.get_sales_trends(top_n=1)
        self.assertEqual(result['total_products_sold'], 2)
        self.assertEqual([p['product_id'] for p in result['top_selling_products']], [high.id])
        result = self.tools.get_sales_trends(product_id=low.id)
        self.assertEqual([p['product_id'] for p in result['top_selling_products']], [low.id])

    def test_empty_sales(self):
        self.assertEqual(self.tools.get_sales_trends()['top_selling_products'], [])

    def test_deleted_product_history_is_not_lost(self):
        product = self._product('Deleted product')
        sale = self._sale(product, 4)
        SaleItem.query.filter_by(sale_id=sale.id).update({'product_id': None})
        db.session.commit()
        result = self.tools.get_sales_trends()
        self.assertEqual(result['top_selling_products'], [])
        self.assertEqual(result['total_products_sold'], 0)
        row = result['unknown_product_sales']
        self.assertEqual(row['total_quantity'], 4)
        self.assertEqual(row['total_revenue'], '10.00')
        orch = AgentOrchestrator(db, self.tools.models)
        for text in (orch._format_sales_trend_result(result),
                     orch._format_tool_results_for_user([
                         {'function_name': 'get_sales_trends', 'result': result, 'error': None}
                     ], 'Best sellers?')):
            self.assertIn('Unknown/deleted product sales (not ranked)', text)
            self.assertNotIn('No sales data', text)
            self.assertNotIn('1. **Unknown', text)

    def test_unknown_aggregate_does_not_displace_identifiable_bestseller(self):
        known = self._product('Known bestseller')
        removed = self._product('Removed seller')
        self._sale(known, 5)
        for quantity in (20, 30):
            sale = self._sale(removed, quantity)
            SaleItem.query.filter_by(sale_id=sale.id).update({'product_id': None})
        db.session.commit()
        result = self.tools.get_sales_trends(top_n=1)
        self.assertEqual(result['total_products_sold'], 1)
        self.assertEqual(result['top_selling_products'][0]['product_id'], known.id)
        self.assertEqual(result['unknown_product_sales'], {
            'total_quantity': 50, 'total_revenue': '125.00', 'sale_count': 2})
        filtered = self.tools.get_sales_trends(product_id=known.id)
        self.assertEqual(filtered['unknown_product_sales']['total_quantity'], 0)

    def test_all_time_summary_totals_are_not_limited_by_recent_rows(self):
        product = self._product('Summary product')
        self._sale(product, 2)
        self._sale(product, 8, age=800)
        recent = self.tools.get_sales_summary()
        history = self.tools.get_sales_summary(all_time=True, limit=1)
        self.assertEqual(recent['total_sales'], '5.00')
        self.assertEqual(history['total_sales'], '25.00')
        self.assertEqual(history['transaction_count'], 2)
        self.assertEqual(len(history['recent_sales']), 1)

    def test_unknown_history_respects_period_and_branch(self):
        product = self._product('Unknown scoped history')
        recent = self._sale(product, 2)
        old = self._sale(product, 8, age=800)
        other = self._sale(product, 100, branch_id=self.original_default.id)
        try:
            for sale in (recent, old, other):
                SaleItem.query.filter_by(sale_id=sale.id).update({'product_id': None})
            db.session.commit()
            rolling = self.tools.get_sales_trends()['unknown_product_sales']
            history = self.tools.get_sales_trends(all_time=True)['unknown_product_sales']
            self.assertEqual(rolling['total_quantity'], 2)
            self.assertEqual(history['total_quantity'], 10)
            self.assertEqual(history['sale_count'], 2)
            self.assertEqual(history['total_revenue'], '25.00')
        finally:
            SaleItem.query.filter_by(sale_id=other.id).delete()
            db.session.delete(other)
            db.session.commit()

    def test_plan_executes_real_all_time_trend_and_formats_money(self):
        product = self._product('Plan bestseller')
        self._sale(product, 8, age=800)
        orch = AgentOrchestrator(db, self.tools.models)
        orch.set_request_context(self.tools.context)
        steps, error, _ = orch._validate_plan([
            {'tool': 'get_sales_trends', 'args': {'all_time': True}}
        ], orch._get_tool_registry())
        self.assertIsNone(error)
        results, pending = orch._execute_plan({'steps': steps})
        self.assertEqual(results[0]['status'], 'ok')
        self.assertEqual(pending, [])
        result = results[0]['result']
        text = orch._format_tool_results_for_user([
            {'function_name': 'get_sales_trends', 'result': result, 'error': None}
        ], 'Which product sells best of all time?')
        self.assertIn('All time', text)
        self.assertIn('Plan bestseller', text)
        self.assertIn('20.00', text)
        self.assertIn('All time', orch._format_sales_trend_result(result))

    def test_direct_invalid_trend_arguments_return_errors(self):
        for args in ({'days': 0}, {'days': True}, {'top_n': -1},
                     {'all_time': 'true'}, {'product_id': -1}):
            with self.subTest(args=args):
                self.assertIn('error', self.tools.get_sales_trends(**args))