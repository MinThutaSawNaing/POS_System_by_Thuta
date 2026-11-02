"""The floating Loli chat head and panel header must use the current avatar.

The assistant had two avatar <img> tags pointing at the old PNG
(public/photos/loli-logo.png). Replacing the artwork is only real if both
places point at the new asset AND the asset is actually served, so both halves
are asserted here.
"""

import unittest
from pathlib import Path

from app import app

ROOT = Path(__file__).resolve().parent
NEW_AVATAR_URL = "/public/AI%20LOGO/loliAiAssistant.svg"


class LoliAvatarTests(unittest.TestCase):
    def setUp(self):
        self.widget = (ROOT / 'templates' / 'ai_agent_widget.html').read_text(encoding='utf-8')

    def test_chat_head_and_header_use_the_new_avatar(self):
        self.assertEqual(self.widget.count(NEW_AVATAR_URL), 2)

    def test_old_logo_is_no_longer_referenced(self):
        self.assertNotIn('loli-logo.png', self.widget)

    def test_new_avatar_file_exists(self):
        asset = ROOT / 'public' / 'AI LOGO' / 'loliAiAssistant.svg'
        self.assertTrue(asset.is_file(), f"missing avatar asset: {asset}")
        self.assertIn('<svg', asset.read_text(encoding='utf-8'))

    def test_new_avatar_is_served(self):
        with app.test_client() as client:
            response = client.get(NEW_AVATAR_URL)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<svg', response.data[:400])


if __name__ == "__main__":
    unittest.main()
