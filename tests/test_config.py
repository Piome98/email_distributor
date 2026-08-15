"""Settings persistence and the guards applied when loading them."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from email_distributor.config import Settings


class SettingsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json"
        patcher = mock.patch(
            "email_distributor.config.settings_path", return_value=self.path
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, payload):
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestDefaults(SettingsTestCase):
    def test_a_manual_run_is_unlimited_by_default(self):
        """A cap makes "files nothing" and "nothing in the newest 200 was
        eligible" indistinguishable."""
        self.assertEqual(Settings().run_limit, 0)

    def test_the_watcher_stays_capped(self):
        self.assertGreater(Settings().poll_limit, 0)

    def test_dry_run_is_on_until_deliberately_disabled(self):
        self.assertTrue(Settings().dry_run)


class TestPublicDomainGuard(SettingsTestCase):
    def test_a_saved_public_domain_is_dropped_on_load(self):
        """A value stored before this check existed must be corrected, not
        honoured forever - gmail.com as "internal" makes every Gmail sender a
        colleague."""
        self._write({"internal_domains": ["gmail.com", "mycorp.co.kr"]})
        self.assertEqual(Settings.load().internal_domains, ["mycorp.co.kr"])

    def test_several_public_providers_are_all_dropped(self):
        self._write({"internal_domains": ["naver.com", "hanmail.net", "outlook.com"]})
        self.assertEqual(Settings.load().internal_domains, [])

    def test_a_real_company_domain_survives(self):
        self._write({"internal_domains": ["te.com"]})
        self.assertEqual(Settings.load().internal_domains, ["te.com"])


class TestRoundTrip(SettingsTestCase):
    def test_values_survive_a_save_and_load(self):
        settings = Settings()
        settings.run_limit = 500
        settings.reprocess_handled = True
        settings.internal_domains = ["mycorp.co.kr"]
        settings.save()

        loaded = Settings.load()
        self.assertEqual(loaded.run_limit, 500)
        self.assertTrue(loaded.reprocess_handled)
        self.assertEqual(loaded.internal_domains, ["mycorp.co.kr"])

    def test_unknown_keys_are_ignored(self):
        self._write({"watch_folder": "Inbox", "bogus": 1})
        self.assertEqual(Settings.load().watch_folder, "Inbox")

    def test_a_corrupt_file_falls_back_to_defaults(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(Settings.load().watch_folder, "Inbox")


if __name__ == "__main__":
    unittest.main()
