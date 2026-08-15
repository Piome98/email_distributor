"""Filing behaviour, exercised against a fake Outlook client.

These cover the two defects a live run exposed: a failed move being reported
as success, and the processed-ledger missing the new EntryID that Outlook
issues when a message changes folder.
"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from email_distributor.actions.filing import Distributor, OutlookActionError
from email_distributor.config import Settings
from email_distributor.identity.store import IdentityStore
from email_distributor.outlook.message import Message
from email_distributor.rules.engine import Actions, Match, Rule, RuleSet


class FakeItem:
    """Stands in for an Outlook MailItem."""

    def __init__(self, entry_id: str):
        self.EntryID = entry_id
        self.Categories = ""
        self.UnRead = True
        self.saved = False

    def Save(self):
        self.saved = True


class FakeClient:
    """Records what was asked of Outlook, and can be told to fail a move."""

    def __init__(self, move_succeeds: bool = True):
        self.move_succeeds = move_succeeds
        self.moved: list[tuple[str, str]] = []
        self.categorised: list[tuple[str, list[str]]] = []
        self.folders_made: list[str] = []

    def ensure_folder(self, path):
        self.folders_made.append(path)
        return path

    def add_categories(self, item, names):
        self.categorised.append((item.EntryID, list(names)))
        item.Categories = ", ".join(names)
        return True

    def mark_read(self, item, read=True):
        item.UnRead = not read
        return True

    def move_item(self, item, folder):
        if not self.move_succeeds:
            return None
        self.moved.append((item.EntryID, folder))
        # Outlook issues a fresh EntryID on a folder change.
        return FakeItem(item.EntryID + "-MOVED")


def make_message(entry_id="E1", sender="hong@hanguk.co.kr"):
    return Message(
        entry_id=entry_id,
        subject="테스트",
        body="본문",
        sender_email=sender,
        sender_name="홍길동",
        received=datetime(2026, 3, 15, 9, 0),
    )


class FilingTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = IdentityStore(Path(self._tmp.name) / "t.db")
        cid = self.store.upsert_company("한국전자", group="고객사")
        self.store.link_domain("hanguk.co.kr", cid)

        self.settings = Settings()
        self.settings.dry_run = False
        self.settings.move_to_folders = True
        self.settings.apply_categories = True

        self.ruleset = RuleSet([
            Rule(
                name="회사별",
                match=Match(is_unknown=False),
                actions=Actions(move_to="Inbox/{company}", categories=["{company}"]),
            )
        ])

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def distributor(self, client):
        return Distributor(client, self.store, self.ruleset, self.settings)


class TestSuccessfulFiling(FilingTestCase):
    def test_message_is_moved_and_categorised(self):
        client = FakeClient()
        dist = self.distributor(client)
        result = dist._process_one(FakeItem("E1"), make_message(), reprocess=False)

        self.assertTrue(result.applied)
        self.assertEqual(result.error, "")
        self.assertEqual(client.moved, [("E1", "Inbox/한국전자")])
        self.assertEqual(client.categorised, [("E1", ["한국전자"])])

    def test_both_old_and_new_entry_ids_are_recorded(self):
        """Outlook renumbers a moved message; the ledger must know both."""
        client = FakeClient()
        dist = self.distributor(client)
        dist._process_one(FakeItem("E1"), make_message(), reprocess=False)

        self.assertTrue(self.store.is_processed("E1"))
        self.assertTrue(self.store.is_processed("E1-MOVED"))

    def test_a_moved_message_is_not_filed_twice(self):
        """Re-scanning the destination folder must skip what is already filed.

        Without the post-move id this re-filed every message, and Outlook then
        refused the move because the message was already in that folder.
        """
        client = FakeClient()
        dist = self.distributor(client)
        dist._process_one(FakeItem("E1"), make_message(), reprocess=False)

        moved_again = dist._process_one(
            FakeItem("E1-MOVED"), make_message("E1-MOVED"), reprocess=False
        )
        self.assertEqual(moved_again.skipped_reason, "already processed")
        self.assertEqual(len(client.moved), 1)

    def test_already_processed_is_skipped(self):
        client = FakeClient()
        dist = self.distributor(client)
        self.store.mark_processed("E1", "rule", "action")
        result = dist._process_one(FakeItem("E1"), make_message(), reprocess=False)
        self.assertEqual(result.skipped_reason, "already processed")
        self.assertEqual(client.moved, [])

    def test_reprocess_overrides_the_ledger(self):
        client = FakeClient()
        dist = self.distributor(client)
        self.store.mark_processed("E1", "rule", "action")
        result = dist._process_one(FakeItem("E1"), make_message(), reprocess=True)
        self.assertTrue(result.applied)


class TestFailedMove(FilingTestCase):
    def test_a_failed_move_is_an_error_not_a_success(self):
        """A category applied to a message that never moved is not success."""
        client = FakeClient(move_succeeds=False)
        dist = self.distributor(client)
        result = dist._process_one(FakeItem("E1"), make_message(), reprocess=False)

        self.assertFalse(result.applied)
        self.assertIn("could not move", result.error)

    def test_a_failed_move_is_not_recorded_as_processed(self):
        """Otherwise the message would never be retried."""
        client = FakeClient(move_succeeds=False)
        dist = self.distributor(client)
        dist._process_one(FakeItem("E1"), make_message(), reprocess=False)
        self.assertFalse(self.store.is_processed("E1"))

    def test_apply_raises_on_a_failed_move(self):
        client = FakeClient(move_succeeds=False)
        dist = self.distributor(client)
        with self.assertRaises(OutlookActionError):
            dist.apply(FakeItem("E1"), self.ruleset.evaluate(
                make_message(), self.store.resolve("hong@hanguk.co.kr")
            ))

    def test_one_failure_does_not_stop_the_batch(self):
        client = FakeClient(move_succeeds=False)
        dist = self.distributor(client)
        results = [
            dist._process_one(FakeItem(f"E{i}"), make_message(f"E{i}"), reprocess=False)
            for i in range(3)
        ]
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.error for r in results))


class TestUnresolvedSender(FilingTestCase):
    """An address-book failure must not be mistaken for an unknown company.

    On Exchange, SenderEmailAddress is an X.500 DN and the real address has to
    be looked up. If that lookup fails the sender is empty - and filing it as
    "unknown" would sweep internal mail into a review folder on the strength
    of a transient failure.
    """

    def test_a_message_with_no_resolvable_sender_is_skipped(self):
        client = FakeClient()
        dist = self.distributor(client)
        message = make_message(sender="")
        result = dist._process_one(FakeItem("E1"), message, reprocess=False)

        self.assertIn("could not be resolved", result.skipped_reason)
        self.assertEqual(client.moved, [])
        self.assertEqual(client.categorised, [])

    def test_it_is_not_recorded_as_processed(self):
        """It must be retried once the address book is reachable again."""
        client = FakeClient()
        dist = self.distributor(client)
        dist._process_one(FakeItem("E1"), make_message(sender=""), reprocess=False)
        self.assertFalse(self.store.is_processed("E1"))

    def test_the_unknown_rule_does_not_claim_it(self):
        self.ruleset = RuleSet([
            Rule(
                name="미분류",
                match=Match(is_unknown=True),
                actions=Actions(move_to="Inbox/_미분류", categories=["미분류"]),
            )
        ])
        client = FakeClient()
        result = self.distributor(client)._process_one(
            FakeItem("E1"), make_message(sender=""), reprocess=False
        )
        self.assertTrue(result.skipped_reason)
        self.assertEqual(client.moved, [])


class TestTagOnlyIsNotConsumed(FilingTestCase):
    """A message that was only tagged must stay eligible for filing later.

    Recording it as processed would consume it permanently: once its company
    is confirmed as a 거래처, the message would be skipped forever and never
    reach its folder.
    """

    def setUp(self):
        super().setUp()
        self.ruleset = RuleSet([
            Rule(
                name="tag only",
                match=Match(),
                actions=Actions(categories=["미분류"]),
            )
        ])

    def test_tag_only_does_not_enter_the_ledger(self):
        client = FakeClient()
        dist = self.distributor(client)
        dist._process_one(FakeItem("E1"), make_message(), reprocess=False)

        self.assertEqual(client.categorised, [("E1", ["미분류"])])
        self.assertEqual(client.moved, [])
        self.assertFalse(self.store.is_processed("E1"))

    def test_it_is_filed_once_the_rules_start_moving_it(self):
        client = FakeClient()
        self.distributor(client)._process_one(
            FakeItem("E1"), make_message(), reprocess=False
        )

        # The company is confirmed later, so the rules now move it.
        self.ruleset = RuleSet([
            Rule(
                name="file it",
                match=Match(),
                actions=Actions(move_to="Inbox/거래처/{company}"),
            )
        ])
        result = self.distributor(client)._process_one(
            FakeItem("E1"), make_message(), reprocess=False
        )
        self.assertEqual(result.skipped_reason, "")
        self.assertEqual(client.moved, [("E1", "Inbox/거래처/한국전자")])


class TestDryRun(FilingTestCase):
    def test_dry_run_changes_nothing(self):
        self.settings.dry_run = True
        client = FakeClient()
        dist = self.distributor(client)
        result = dist._process_one(FakeItem("E1"), make_message(), reprocess=False)

        self.assertFalse(result.applied)
        self.assertTrue(result.dry_run)
        self.assertEqual(client.moved, [])
        self.assertEqual(client.categorised, [])
        self.assertFalse(self.store.is_processed("E1"))

    def test_dry_run_still_reports_the_plan(self):
        self.settings.dry_run = True
        dist = self.distributor(FakeClient())
        result = dist._process_one(FakeItem("E1"), make_message(), reprocess=False)
        self.assertEqual(result.decision.move_to, "Inbox/한국전자")


class TestSettingsGates(FilingTestCase):
    def test_moves_can_be_disabled(self):
        self.settings.move_to_folders = False
        client = FakeClient()
        dist = self.distributor(client)
        result = dist._process_one(FakeItem("E1"), make_message(), reprocess=False)
        self.assertEqual(client.moved, [])
        self.assertTrue(result.applied)  # the category was still applied

    def test_categories_can_be_disabled(self):
        self.settings.apply_categories = False
        client = FakeClient()
        dist = self.distributor(client)
        dist._process_one(FakeItem("E1"), make_message(), reprocess=False)
        self.assertEqual(client.categorised, [])
        self.assertEqual(len(client.moved), 1)


if __name__ == "__main__":
    unittest.main()
