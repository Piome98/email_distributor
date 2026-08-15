"""Building the 업체/담당자 folder tree.

The paths must come from the same rules that do the filing, so that what is
created is exactly what filing will use. These tests pin that down.
"""

import tempfile
import unittest
from pathlib import Path

from email_distributor.actions.folders import FolderBuilder
from email_distributor.config import Settings
from email_distributor.identity.store import IdentityStore
from email_distributor.rules.engine import (
    Actions,
    Match,
    Rule,
    RuleSet,
    default_ruleset,
)


class FakeClient:
    """Records folder creation; pretends a given set already exists."""

    def __init__(self, existing=(), store_syncs=True, fail_on=()):
        self.existing = set(existing)
        self.created: list[str] = []
        self.fail_on = set(fail_on)
        self._store_syncs = store_syncs

    def get_folder(self, path):
        return object() if path in self.existing else None

    def ensure_folder(self, path):
        if path in self.fail_on:
            raise RuntimeError("folder name rejected by Outlook")
        self.created.append(path)
        self.existing.add(path)
        return path

    def store_info(self, folder=None):
        return {
            "name": "test@example.com",
            "path": "C:\\x.ost" if self._store_syncs else "C:\\x.pst",
            "syncs": self._store_syncs,
            "kind": "cached account - syncs" if self._store_syncs else "local .pst - does NOT sync",
        }


class FolderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = IdentityStore(Path(self._tmp.name) / "t.db")
        self.settings = Settings()

        cid = self.store.upsert_company("한국전자", group="고객사")
        self.store.link_domain("hanguk.co.kr", cid)
        self.store.upsert_person(
            "hong@hanguk.co.kr", display_name="홍길동", company_id=cid, bump_count=True
        )
        self.store.upsert_person(
            "kim@hanguk.co.kr", display_name="김철수", company_id=cid, bump_count=True
        )

        other = self.store.upsert_company("미래상사", group="협력사")
        self.store.link_domain("mirae.co.kr", other)

        self.ruleset = default_ruleset()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def builder(self, client):
        return FolderBuilder(client, self.store, self.ruleset, self.settings)


class TestPlan(FolderTestCase):
    def test_person_level_folders_are_planned(self):
        report = self.builder(FakeClient()).build(dry_run=True)
        paths = [p.path for p in report.plans]
        self.assertIn("Inbox/거래처/한국전자/홍길동", paths)
        self.assertIn("Inbox/거래처/한국전자/김철수", paths)

    def test_company_without_contacts_stops_at_the_company_folder(self):
        """No contact means no contact folder - not a folder called "unknown".

        Inventing one creates a folder no mail will ever be filed into.
        """
        report = self.builder(FakeClient()).build(dry_run=True)
        paths = [p.path for p in report.plans]
        self.assertIn("Inbox/거래처/미래상사", paths)
        self.assertFalse(
            any(p.startswith("Inbox/거래처/미래상사/") for p in paths), paths
        )

    def test_companies_only_stops_at_the_company_level(self):
        self.ruleset = RuleSet([
            Rule(
                name="회사만",
                match=Match(is_unknown=False),
                actions=Actions(move_to="Inbox/거래처/{company}"),
            )
        ])
        report = self.builder(FakeClient()).build(dry_run=True, include_people=False)
        paths = [p.path for p in report.plans]
        self.assertIn("Inbox/거래처/한국전자", paths)
        self.assertNotIn("Inbox/거래처/한국전자/홍길동", paths)

    def test_min_messages_filters_out_one_off_contacts(self):
        cid = self.store.company_by_domain("hanguk.co.kr").id
        for _ in range(4):
            self.store.upsert_person("hong@hanguk.co.kr", company_id=cid, bump_count=True)

        report = self.builder(FakeClient()).build(dry_run=True, min_messages=3)
        paths = [p.path for p in report.plans]
        self.assertIn("Inbox/거래처/한국전자/홍길동", paths)
        self.assertNotIn("Inbox/거래처/한국전자/김철수", paths)

    def test_paths_match_what_filing_would_use(self):
        """The whole point: no drift between planned and actual folders."""
        from datetime import datetime

        from email_distributor.outlook.message import Message

        report = self.builder(FakeClient()).build(dry_run=True)
        planned = {p.path for p in report.plans}

        message = Message(
            entry_id="E1", sender_email="hong@hanguk.co.kr", received=datetime.now()
        )
        identity = self.store.resolve("hong@hanguk.co.kr", "홍길동")
        filing_target = self.ruleset.evaluate(message, identity).move_to
        self.assertIn(filing_target, planned)

    def test_existing_folders_are_reported_not_recreated(self):
        client = FakeClient(existing={"Inbox/거래처/한국전자/홍길동"})
        report = self.builder(client).build(dry_run=False)
        self.assertNotIn("Inbox/거래처/한국전자/홍길동", client.created)
        self.assertEqual(report.existing, 1)

    def test_duplicate_paths_are_collapsed(self):
        report = self.builder(FakeClient()).build(dry_run=True)
        paths = [p.path for p in report.plans]
        self.assertEqual(len(paths), len(set(paths)))


class TestBuild(FolderTestCase):
    def test_dry_run_creates_nothing(self):
        client = FakeClient()
        report = self.builder(client).build(dry_run=True)
        self.assertEqual(client.created, [])
        self.assertTrue(report.dry_run)
        self.assertGreater(report.missing, 0)

    def test_live_creates_the_folders(self):
        client = FakeClient()
        report = self.builder(client).build(dry_run=False)
        self.assertGreater(len(client.created), 0)
        self.assertEqual(report.created, len(client.created))
        self.assertIn("Inbox/거래처/한국전자/홍길동", client.created)

    def test_one_bad_name_does_not_stop_the_rest(self):
        client = FakeClient(fail_on={"Inbox/거래처/한국전자/홍길동"})
        report = self.builder(client).build(dry_run=False)
        self.assertEqual(report.errors, 1)
        self.assertGreater(report.created, 0)


class TestStoreSyncWarning(FolderTestCase):
    def test_syncing_store_is_reported_as_such(self):
        report = self.builder(FakeClient(store_syncs=True)).build(dry_run=True)
        self.assertTrue(report.store_syncs)

    def test_local_pst_is_flagged(self):
        """Folders in a .pst never reach the server, so the user must be told."""
        report = self.builder(FakeClient(store_syncs=False)).build(dry_run=True)
        self.assertFalse(report.store_syncs)
        self.assertIn("pst", report.store_kind.lower())


if __name__ == "__main__":
    unittest.main()
