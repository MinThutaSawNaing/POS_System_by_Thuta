import unittest
from pathlib import Path

from ai_tools import AITools, TOOL_SCHEMAS, get_all_tools


class AiRefreshContractTests(unittest.TestCase):
    def test_agent_registry_excludes_mutation_tools(self):
        read_only_tools = get_all_tools(read_only=True)

        self.assertIn('get_current_branch_context', read_only_tools)
        self.assertIn('get_debt_summary', read_only_tools)
        self.assertIn('get_delivery_summary', read_only_tools)
        self.assertIn('get_promotion_summary', read_only_tools)
        self.assertNotIn('create_purchase_order', read_only_tools)
        self.assertNotIn('approve_purchase_order', read_only_tools)
        self.assertNotIn('cancel_purchase_order', read_only_tools)
        self.assertNotIn('create_warehouse_transfer', read_only_tools)

    def test_legacy_registry_remains_available_to_non_agent_callers(self):
        self.assertIn('create_purchase_order', TOOL_SCHEMAS)
        self.assertIn('create_purchase_order', get_all_tools())

    def test_every_read_only_schema_has_an_implemented_tool_method(self):
        tool_container = AITools(db=None, models={})

        for tool_name in get_all_tools(read_only=True):
            self.assertTrue(callable(getattr(tool_container, tool_name, None)), tool_name)

    def test_dashboard_branch_loader_has_no_undefined_pos_selector(self):
        dashboard = Path('templates/dashboard.html').read_text(encoding='utf-8')

        self.assertNotIn('if (posSelector)', dashboard)
        self.assertIn('updateBranchIndicators(current);', dashboard)
        self.assertIn('Unable to load branches', dashboard)


if __name__ == '__main__':
    unittest.main()