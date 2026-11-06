"""Purchase order letterhead canvas regressions: layout, logo, print, Settings UI.

Run only this module: python run_tests.py test_po_template.py -q --tb=short
"""

import io
import json
import os
import re
import shutil
import unittest
from pathlib import Path

from PIL import Image

import test_delivery_operations as base


DASHBOARD = (Path(__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")


class PoTemplateLayoutTests(unittest.TestCase):
    """Pure layout validation: no database, no request context."""

    def test_default_layout_is_printable(self):
        from po_template import default_po_template, normalize_po_template

        template = normalize_po_template(None)
        self.assertEqual(template, default_po_template())
        self.assertEqual(template["page_inset_mm"], 15.0)
        self.assertEqual(template["header_height_mm"], 46.0)
        kinds = [element["kind"] for element in template["elements"]]
        self.assertIn("logo", kinds)
        self.assertIn("text", kinds)
        self.assertIn("rule", kinds)
        self.assertTrue(template["footer"]["show_page_number"])

    def test_normalizer_clamps_and_rejects_junk(self):
        from po_template import normalize_po_template

        template = normalize_po_template({
            "page_inset_mm": 999,
            "header_height_mm": -5,
            "elements": [
                {"kind": "text", "text": "ACME {{po_number}}", "x_pct": 500, "y_mm": -20,
                 "width_pct": 0, "font_size": 99, "align": "sideways", "color": "red"},
                {"kind": "totally-wrong", "text": "kept as text"},
                {"kind": "logo", "width_mm": 500, "height_mm": 1},
            ],
            "footer": {"text": "x" * 500, "font_size": 99, "show_page_number": "yes"},
        })
        self.assertEqual(template["page_inset_mm"], 30.0)
        self.assertEqual(template["header_height_mm"], 20.0)
        text, mislabelled, logo = template["elements"]
        self.assertEqual(text["kind"], "text")
        self.assertEqual(text["x_pct"], 100.0)
        self.assertEqual(text["y_mm"], 0.0)
        self.assertEqual(text["width_pct"], 5.0)
        self.assertEqual(text["font_size"], 40.0)
        self.assertEqual(text["align"], "left")
        self.assertEqual(text["color"], "#111827")
        self.assertEqual(mislabelled["kind"], "text")
        self.assertEqual(logo["width_mm"], 90.0)
        self.assertEqual(logo["height_mm"], 5.0)
        self.assertEqual(len(template["footer"]["text"]), 200)
        self.assertEqual(template["footer"]["font_size"], 14.0)
        self.assertTrue(template["footer"]["show_page_number"])

    def test_blocks_stay_inside_the_header_band(self):
        from po_template import normalize_po_template

        template = normalize_po_template({
            "header_height_mm": 40,
            "elements": [{"kind": "text", "text": "Deep", "y_mm": 100}],
        })
        self.assertEqual(template["elements"][0]["y_mm"], 38.0)

    def test_element_ids_stay_unique(self):
        from po_template import normalize_po_template

        template = normalize_po_template({
            "elements": [{"id": "same", "kind": "text"}, {"id": "same", "kind": "text"}],
        })
        self.assertEqual([element["id"] for element in template["elements"]], ["same", "same-2"])

    def test_tokens_render_real_values_and_keep_unknown_ones(self):
        from po_template import render_po_template_text

        self.assertEqual(
            render_po_template_text("PO {{po_number}} / {{missing}}", {"po_number": "PO-7"}),
            "PO PO-7 / {{missing}}",
        )
        self.assertEqual(render_po_template_text(None, {}), "")

    def test_header_flowable_produces_a_pdf(self):
        from reportlab.platypus import SimpleDocTemplate

        from po_template import (
            PurchaseOrderTemplateHeader, draw_po_template_footer, normalize_po_template,
        )

        template = normalize_po_template(None)
        buffer = io.BytesIO()
        document = SimpleDocTemplate(buffer)
        document.build(
            [PurchaseOrderTemplateHeader(template, {"po_number": "PO-1"})],
            onFirstPage=lambda canvas, doc: draw_po_template_footer(canvas, doc, template, {"po_number": "PO-1"}),
        )
        self.assertTrue(buffer.getvalue().startswith(b"%PDF-"))

    def test_foreign_glyphs_do_not_break_the_pdf(self):
        from reportlab.platypus import SimpleDocTemplate

        from po_template import PurchaseOrderTemplateHeader, normalize_po_template

        template = normalize_po_template({"elements": [
            {"kind": "text", "text": "Client \u1010\u1014\u103a header", "font_size": 12},
        ]})
        buffer = io.BytesIO()
        document = SimpleDocTemplate(buffer)
        document.build([PurchaseOrderTemplateHeader(template, {})])
        self.assertTrue(buffer.getvalue().startswith(b"%PDF-"))



class PoTemplateApiTests(unittest.TestCase):
    """Settings API, private logo upload and the printed letterhead."""

    setUpClass = classmethod(base.DeliveryOperationsTests.setUpClass.__func__)
    tearDownClass = classmethod(base.DeliveryOperationsTests.tearDownClass.__func__)
    setUp = base.DeliveryOperationsTests.setUp
    client = base.DeliveryOperationsTests.client

    def logo(self, image_format="PNG", size=(24, 12)):
        stream = io.BytesIO()
        Image.new("RGB", size, "white").save(stream, format=image_format)
        stream.seek(0)
        return stream

    def payload(self, **overrides):
        payload = {
            "page_inset_mm": 12,
            "header_height_mm": 50,
            "elements": [
                {"id": "logo", "kind": "logo", "x_pct": 0, "y_mm": 0,
                 "width_mm": 30, "height_mm": 14},
                {"id": "brand", "kind": "text", "text": "{{brand_name}}", "x_pct": 34,
                 "y_mm": 0, "width_pct": 66, "font_size": 16, "bold": True,
                 "align": "left", "color": "#123456"},
                {"id": "rule", "kind": "rule", "x_pct": 0, "y_mm": 45,
                 "width_pct": 100, "thickness": 2, "color": "#123456"},
            ],
            "footer": {"text": "Client letterhead footer", "font_size": 7,
                       "color": "#444444", "show_page_number": True},
        }
        payload.update(overrides)
        return payload

    def save_template(self, client=None, **overrides):
        return (client or self.client()).put(
            "/api/settings/po-template", json={"template": self.payload(**overrides)})

    def settings(self, client=None):
        response = (client or self.client()).get("/api/settings")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["po_template"]

    def test_requires_login_and_manager(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/api/settings").status_code, 401)
        self.assertEqual(
            anonymous.put("/api/settings/po-template", json={"template": {}}).status_code, 403)
        cashier = self.client(role="cashier")
        self.assertEqual(
            cashier.put("/api/settings/po-template", json={"template": self.payload()}).status_code,
            403)
        self.assertEqual(
            cashier.post("/api/settings/po-template-logo",
                         data={"logo": (self.logo(), "logo.png")}).status_code,
            403)

    def test_default_settings_expose_the_canvas_payload(self):
        settings = self.settings()
        self.assertEqual(settings["header_height_mm"], 46.0)
        self.assertEqual(len(settings["elements"]), 6)
        self.assertEqual(settings["page"], {"width_mm": 210, "height_mm": 297})
        self.assertIn("{{brand_name}}", [token["token"] for token in settings["tokens"]])
        self.assertIsNone(settings["logo_url"])

    def test_template_round_trip_is_normalized_and_branch_scoped(self):
        from po_template import PO_TEMPLATE_SETTING

        response = self.save_template(page_inset_mm=12, header_height_mm=50,
                                      elements=[{"kind": "text",
                                                 "text": "ACME {{po_number}}",
                                                 "color": "#123456", "font_size": 15}])
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["po_template"]["page_inset_mm"], 12.0)
        self.assertEqual(response.json["po_template"]["elements"][0]["color"], "#123456")

        saved = self.settings()
        self.assertEqual(saved["page_inset_mm"], 12.0)
        self.assertEqual(saved["header_height_mm"], 50.0)
        self.assertEqual(saved["elements"][0]["text"], "ACME {{po_number}}")

        with self.app.app_context():
            stored = self.module.AppSetting.query.filter_by(
                key=self.module.branch_setting_key(self.a, PO_TEMPLATE_SETTING)).first()
            self.assertIsNotNone(stored)
            self.assertEqual(json.loads(stored.value)["header_height_mm"], 50.0)

        # A second branch starts from the default letterhead and keeps its own.
        self.assertEqual(self.settings(self.client(branch=self.b))["page_inset_mm"], 15.0)
        other = self.save_template(self.client(branch=self.b), page_inset_mm=20)
        self.assertEqual(other.status_code, 200)
        self.assertEqual(self.settings(self.client(branch=self.b))["page_inset_mm"], 20.0)
        self.assertEqual(self.settings()["page_inset_mm"], 12.0)

    def test_rejects_missing_layout_branch_switch_and_too_many_blocks(self):
        client = self.client()
        self.assertEqual(client.put("/api/settings/po-template", json={}).status_code, 400)
        self.assertEqual(
            client.put("/api/settings/po-template", json={"template": "nope"}).status_code, 400)
        switched = client.put("/api/settings/po-template",
                              json={"branch_id": self.b, "template": self.payload()})
        self.assertEqual(switched.status_code, 409)
        self.assertEqual(self.settings()["page_inset_mm"], 15.0)
        too_many = client.put("/api/settings/po-template", json={"template": {
            "elements": [{"kind": "text", "text": str(index)} for index in range(15)],
        }})
        self.assertEqual(too_many.status_code, 400)

    def test_cashier_settings_payload_stays_free_of_commercial_terms(self):
        """The settings payload a cashier can read must not leak cost/profit keys."""
        payload = self.settings(self.client(role="cashier"))
        banned = ("cost", "profit", "supplier", "agreed_price", "margin")
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertFalse(any(term in key.lower() for term in banned),
                                     f"banned term in settings key {key!r}")
                    stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)

    def test_corrupt_stored_layout_falls_back_to_defaults(self):
        from po_template import PO_TEMPLATE_SETTING

        with self.app.app_context():
            self.module.set_branch_setting(PO_TEMPLATE_SETTING, "not-json{", self.a)
        self.assertEqual(self.settings()["page_inset_mm"], 15.0)

    def test_logo_upload_is_private_and_removable(self):
        client = self.client()
        response = client.post(
            "/api/settings/po-template-logo",
            data={"branch_id": self.a, "logo": (self.logo(), "client.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.json)
        url = response.json["logo_url"]
        self.assertTrue(url)
        self.assertEqual(self.settings()["logo_url"], url)

        fetched = client.get(url, buffered=True)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.headers["Cache-Control"], "private, max-age=86400")
        self.assertEqual(fetched.headers["X-Content-Type-Options"], "nosniff")
        self.assertGreater(len(fetched.data), 0)
        fetched.close()
        self.assertEqual(self.app.test_client().get(url).status_code, 401)

        fake = io.BytesIO(b'<svg onload="alert(1)"></svg>')
        self.assertEqual(
            client.post("/api/settings/po-template-logo",
                        data={"logo": (fake, "fake.png")}).status_code,
            400)
        self.assertEqual(
            client.post("/api/settings/po-template-logo",
                        data={"logo": (self.logo("WEBP"), "logo.webp")}).status_code,
            400)

        removed = client.delete(f"/api/settings/po-template-logo?branch_id={self.a}")
        self.assertEqual(removed.status_code, 200, removed.json)
        self.assertIsNone(self.settings()["logo_url"])
        self.assertEqual(client.get(url).status_code, 404)

        switched = client.post(
            "/api/settings/po-template-logo",
            data={"branch_id": self.b, "logo": (self.logo(), "client.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(switched.status_code, 409)

    def printed_order_id(self):
        module = self.module
        with self.app.app_context():
            supplier = module.Supplier(name="Golden Rice", contact_person="Daw Mya",
                                       phone="+95 9 111", branch_id=self.a)
            product = module.Product(name="Rice bag", price=10, cost=8, stock=2,
                                     branch_id=self.a)
            module.db.session.add_all([supplier, product])
            module.db.session.flush()
            order = module.PurchaseOrder(po_number="PO-TPL-1", supplier_id=supplier.id,
                                         status="draft", total_amount=16,
                                         created_by=self.user, branch_id=self.a)
            module.db.session.add(order)
            module.db.session.flush()
            module.db.session.add(module.PurchaseOrderItem(
                purchase_order_id=order.id, product_id=product.id,
                ordered_qty=2, unit_cost=8))
            module.db.session.commit()
            return order.id

    def test_printed_purchase_order_uses_the_saved_letterhead(self):
        po_id = self.printed_order_id()
        client = self.client()
        self.assertEqual(self.save_template(client).status_code, 200)

        response = client.get(f"/api/purchase_orders/{po_id}/print")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF-"))

        # A corrupt logo on disk must never break printing a purchase order.
        logo = client.post(
            "/api/settings/po-template-logo",
            data={"logo": (self.logo(), "client.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(logo.status_code, 200, logo.json)
        with open(os.path.join(self.module.app.config["PO_LOGO_FOLDER"],
                               logo.json["logo_filename"]), "wb") as handle:
            handle.write(b"not an image")
        again = client.get(f"/api/purchase_orders/{po_id}/print")
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.data.startswith(b"%PDF-"))

    def test_dashboard_renders_the_canvas_for_a_manager(self):
        response = self.client().get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="po-template-canvas"', html)
        self.assertIn('onclick="savePurchaseOrderTemplate()"', html)
        self.assertIn('id="po-template-token-bar"', html)

    def test_cashier_cannot_print_the_letterhead(self):
        po_id = self.printed_order_id()
        self.assertEqual(
            self.client(role="cashier").get(f"/api/purchase_orders/{po_id}/print").status_code,
            403)


class PoTemplateDashboardTests(unittest.TestCase):
    """The Settings tab ships the canvas, its inspector and a deliberate save."""

    def test_settings_nav_and_canvas_markup(self):
        self.assertIn('data-group="po-template"', DASHBOARD)
        self.assertIn('onclick="showSettingsGroup(\'po-template\')"', DASHBOARD)
        self.assertIn('id="settings-group-po-template"', DASHBOARD)
        self.assertIn('id="po-template-canvas"', DASHBOARD)
        self.assertIn('width="794" height="1123"', DASHBOARD)
        self.assertIn('id="save-po-template-btn"', DASHBOARD)
        self.assertIn('onclick="savePurchaseOrderTemplate()"', DASHBOARD)
        self.assertIn('id="po-template-access-note"', DASHBOARD)
        self.assertIn('id="po-logo-file"', DASHBOARD)
        self.assertIn('accept="image/png,image/jpeg"', DASHBOARD)
        self.assertIn('id="po-element-text"', DASHBOARD)
        self.assertIn('id="po-element-x"', DASHBOARD)

    def test_every_settings_nav_group_has_a_matching_pane(self):
        """showSettingsGroup() reveals `settings-group-<data-group>`; ids must line up."""
        groups = set(re.findall(r'data-group="([^"]+)"', DASHBOARD))
        panes = set(re.findall(r'id="settings-group-([^"]+)"', DASHBOARD))
        self.assertEqual(groups - panes, set(), "nav groups without a pane")
        self.assertEqual(panes - groups, set(), "panes without a nav group")
        for group in groups:
            self.assertIn(f'aria-controls="settings-group-{group}"', DASHBOARD)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_opening_po_template_reveals_pane_and_initializes_editor(self):
        from test_offline_sync_javascript import _function, _run_node_script

        panes = re.findall(r'id="(settings-group-[^"]+)"', DASHBOARD)
        groups = re.findall(r'data-group="([^"]+)"', DASHBOARD)
        result = _run_node_script(f"""
const panes = {json.dumps(panes)}.map(id => ({{id, style: {{display: 'none'}}}}));
const items = {json.dumps(groups)}.map(group => {{
  const item = {{dataset: {{group}}, active: false, attrs: {{}}}};
  item.classList = {{toggle: (_name, active) => {{item.active = active;}}}};
  item.setAttribute = (key, value) => {{item.attrs[key] = value;}};
  item.removeAttribute = key => {{delete item.attrs[key];}};
  return item;
}});
globalThis.document = {{querySelectorAll: selector =>
  selector === '#settings-section .settings-group' ? panes : items}};
const calls = [];
function bindPoTemplateEditor() {{calls.push('bind');}}
function loadPoTemplateLogo() {{calls.push('logo');}}
function renderPoTemplateCanvas() {{calls.push('render');}}
{_function(DASHBOARD, 'showSettingsGroup')}
showSettingsGroup('po-template');
console.log(JSON.stringify({{
  visible: panes.filter(pane => pane.style.display === '').map(pane => pane.id),
  active: items.filter(item => item.active).map(item => item.dataset.group),
  current: items.filter(item => item.attrs['aria-current'] === 'true')
    .map(item => item.dataset.group),
  calls,
}}));
""")
        self.assertEqual(result["visible"], ["settings-group-po-template"])
        self.assertEqual(result["active"], ["po-template"])
        self.assertEqual(result["current"], ["po-template"])
        self.assertEqual(result["calls"], ["bind", "logo", "render"])

    def test_canvas_edits_a_draft_and_only_save_posts(self):
        self.assertIn("function renderPoTemplateCanvas()", DASHBOARD)
        self.assertIn("function renderPoTemplateElementList()", DASHBOARD)
        self.assertIn("function stagePoTemplateLogo(", DASHBOARD)
        self.assertIn("/api/settings/po-template", DASHBOARD)
        self.assertIn("/api/settings/po-template-logo", DASHBOARD)

        save_block = DASHBOARD.split("async function savePurchaseOrderTemplate()", 1)[1].split(
            "function applyPoTemplateUI()", 1)[0]
        self.assertIn('method: "PUT"', save_block)
        self.assertIn("uploadPendingPoTemplateLogo", save_block)

        drag_block = DASHBOARD.split("function movePoTemplateElement(", 1)[1].split(
            "function endPoTemplateDrag()", 1)[0]
        self.assertNotIn("fetch(", drag_block)

    def test_tokens_and_branch_scope_are_visible_to_the_editor(self):
        self.assertIn("PO_TEMPLATE_FALLBACK_TOKENS", DASHBOARD)
        self.assertIn('id="po-template-token-bar"', DASHBOARD)
        for token in ("{{brand_name}}", "{{po_number}}", "{{total}}"):
            self.assertIn(token, DASHBOARD)
        self.assertIn("setPoTemplateFromSettings(data.po_template)", DASHBOARD)
        self.assertIn('if (groupId === "po-template")', DASHBOARD)

