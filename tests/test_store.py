"""Identity database: storage, source precedence, and sender resolution."""

import tempfile
import unittest
from pathlib import Path

from email_distributor.identity.learner import company_name_from_domain
from email_distributor.identity.models import (
    SOURCE_INFERRED,
    SOURCE_MANUAL,
    SOURCE_SIGNATURE,
    outranks,
)
from email_distributor.identity.store import IdentityStore, split_domain


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = IdentityStore(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class TestDomainHelpers(unittest.TestCase):
    def test_split_domain(self):
        self.assertEqual(split_domain("A.B@Hanguk.CO.KR"), "hanguk.co.kr")
        self.assertEqual(split_domain("not-an-address"), "")
        self.assertEqual(split_domain(""), "")

    def test_company_name_from_domain(self):
        self.assertEqual(company_name_from_domain("hanguk.co.kr"), "Hanguk")
        self.assertEqual(company_name_from_domain("hanguk-elec.co.kr"), "Hanguk Elec")
        self.assertEqual(company_name_from_domain("acme.com"), "Acme")
        self.assertEqual(company_name_from_domain("mail.acme.com"), "Acme")

    def test_public_providers_identify_no_company(self):
        for domain in ("gmail.com", "naver.com", "hanmail.net", "outlook.com"):
            self.assertEqual(company_name_from_domain(domain), "", domain)


class TestSourcePrecedence(unittest.TestCase):
    def test_ranking(self):
        self.assertTrue(outranks(SOURCE_MANUAL, SOURCE_SIGNATURE))
        self.assertTrue(outranks(SOURCE_SIGNATURE, SOURCE_INFERRED))
        self.assertFalse(outranks(SOURCE_INFERRED, SOURCE_MANUAL))
        self.assertTrue(outranks(SOURCE_MANUAL, SOURCE_MANUAL))


class TestCompanies(StoreTestCase):
    def test_upsert_is_idempotent_and_case_insensitive(self):
        first = self.store.upsert_company("한국전자", group="고객사")
        second = self.store.upsert_company("한국전자")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.list_companies()), 1)

    def test_group_is_recorded_and_returned(self):
        cid = self.store.upsert_company("한국전자", group="고객사")
        self.store.link_domain("hanguk.co.kr", cid)
        company = self.store.company_by_domain("hanguk.co.kr")
        self.assertIsNotNone(company)
        self.assertEqual(company.group_name, "고객사")

    def test_public_domain_cannot_be_linked_to_a_company(self):
        """Mapping gmail.com to one company would mis-file every gmail sender."""
        cid = self.store.upsert_company("한국전자")
        self.store.link_domain("gmail.com", cid)
        self.assertIsNone(self.store.company_by_domain("gmail.com"))

    def test_manual_edit_survives_a_later_inferred_rescan(self):
        cid = self.store.upsert_company("Hanguk", source=SOURCE_INFERRED)
        self.store.upsert_company("Hanguk", group="고객사", source=SOURCE_MANUAL)
        self.store.upsert_company("Hanguk", group="협력사", source=SOURCE_INFERRED)
        self.assertEqual(self.store.company_by_id(cid).group_name, "고객사")

    def test_domain_relink_respects_source_rank(self):
        manual = self.store.upsert_company("Manual Co")
        other = self.store.upsert_company("Other Co")
        self.store.link_domain("shared.co.kr", manual, SOURCE_MANUAL)
        self.store.link_domain("shared.co.kr", other, SOURCE_INFERRED)
        self.assertEqual(self.store.company_by_domain("shared.co.kr").name, "Manual Co")


class TestPeople(StoreTestCase):
    def test_upsert_normalises_email_and_counts_messages(self):
        self.store.upsert_person("A.B@Hanguk.co.kr", bump_count=True)
        self.store.upsert_person("a.b@hanguk.co.kr", bump_count=True)
        people = self.store.list_people()
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0].message_count, 2)

    def test_blank_values_never_erase_known_details(self):
        self.store.upsert_person(
            "a@x.co.kr", display_name="홍길동", title="부장", source=SOURCE_SIGNATURE
        )
        self.store.upsert_person("a@x.co.kr", display_name="", title="", source=SOURCE_MANUAL)
        person = self.store.person_by_email("a@x.co.kr")
        self.assertEqual(person.display_name, "홍길동")
        self.assertEqual(person.title, "부장")

    def test_invalid_address_is_rejected(self):
        self.assertIsNone(self.store.upsert_person("no-at-sign"))


class TestResolve(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.cid = self.store.upsert_company("한국전자", group="고객사")
        self.store.link_domain("hanguk.co.kr", self.cid)

    def test_resolves_company_by_domain(self):
        identity = self.store.resolve("someone.new@hanguk.co.kr")
        self.assertTrue(identity.known)
        self.assertEqual(identity.company_name, "한국전자")
        self.assertEqual(identity.group_name, "고객사")

    def test_person_company_link_beats_domain_map(self):
        """Someone at a public provider can still be attached to a company."""
        other = self.store.upsert_company("특별거래처", group="협력사")
        self.store.upsert_person("freelancer@gmail.com", company_id=other)
        identity = self.store.resolve("freelancer@gmail.com")
        self.assertEqual(identity.company_name, "특별거래처")
        self.assertTrue(identity.is_public_domain)

    def test_unknown_sender_is_reported_as_unknown(self):
        identity = self.store.resolve("stranger@nowhere.co.kr")
        self.assertFalse(identity.known)
        self.assertEqual(identity.company_name, "")

    def test_internal_domain_flag(self):
        identity = self.store.resolve("me@mycorp.com", internal_domains=["mycorp.com"])
        self.assertTrue(identity.is_internal)

    def test_company_marked_internal_makes_sender_internal(self):
        cid = self.store.upsert_company("우리회사", is_internal=True)
        self.store.link_domain("mycorp.com", cid)
        self.assertTrue(self.store.resolve("someone@mycorp.com").is_internal)

    def test_describe_includes_the_useful_parts(self):
        self.store.upsert_person(
            "hong@hanguk.co.kr",
            display_name="홍길동",
            title="부장",
            department="영업1팀",
            company_id=self.cid,
        )
        text = self.store.resolve("hong@hanguk.co.kr").describe()
        for part in ("홍길동", "부장", "영업1팀", "한국전자", "고객사"):
            self.assertIn(part, text)


class TestProcessedLedger(StoreTestCase):
    def test_marking_and_checking(self):
        self.assertFalse(self.store.is_processed("ENTRY1"))
        self.store.mark_processed("ENTRY1", "rule", "moved")
        self.assertTrue(self.store.is_processed("ENTRY1"))

    def test_marking_twice_does_not_raise(self):
        self.store.mark_processed("ENTRY1", "rule", "moved")
        self.store.mark_processed("ENTRY1", "rule2", "categorised")
        self.assertEqual(self.store.stats()["processed"], 1)


if __name__ == "__main__":
    unittest.main()
