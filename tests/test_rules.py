"""Rule matching, template expansion and folder-name safety."""

import unittest
from datetime import datetime

from email_distributor.identity.models import Company, Identity, Person
from email_distributor.outlook.message import Message
from email_distributor.rules.engine import (
    Actions,
    Match,
    Rule,
    RuleSet,
    default_ruleset,
    expand,
    sanitize_folder_name,
    sanitize_folder_path,
)


def make_message(**kwargs) -> Message:
    defaults = dict(
        entry_id="E1",
        subject="견적서 송부의 건",
        body="안녕하세요",
        sender_email="hong@hanguk.co.kr",
        sender_name="홍길동",
        received=datetime(2026, 3, 15, 9, 30),
    )
    defaults.update(kwargs)
    return Message(**defaults)


def make_identity(**kwargs) -> Identity:
    company = kwargs.pop("company", Company(name="한국전자", group_name="고객사", id=1))
    defaults = dict(
        email="hong@hanguk.co.kr",
        display_name="홍길동",
        domain="hanguk.co.kr",
        company=company,
        group_name=company.group_name if company else "",
    )
    defaults.update(kwargs)
    return Identity(**defaults)


class TestSanitize(unittest.TestCase):
    def test_illegal_characters_replaced(self):
        self.assertEqual(sanitize_folder_name('a/b\\c:d*e?f"g<h>i|j'), "a-b-c-d-e-f-g-h-i-j")

    def test_empty_name_gets_a_fallback(self):
        self.assertEqual(sanitize_folder_name("..."), "기타")

    def test_path_separators_survive_sanitising(self):
        self.assertEqual(
            sanitize_folder_path("Inbox/거래처/한국:전자"), "Inbox/거래처/한국-전자"
        )

    def test_length_is_capped(self):
        self.assertLessEqual(len(sanitize_folder_name("가" * 300)), 100)


class TestExpand(unittest.TestCase):
    def test_placeholders_filled(self):
        identity = make_identity(
            person=Person(email="hong@hanguk.co.kr", department="영업1팀", title="부장")
        )
        result = expand("Inbox/{group}/{company}/{department}/{yyyymm}", make_message(), identity)
        self.assertEqual(result, "Inbox/고객사/한국전자/영업1팀/2026-03")

    def test_unknown_sender_gets_readable_defaults(self):
        identity = Identity(email="x@y.com", domain="y.com")
        self.assertEqual(expand("{group}/{company}", make_message(), identity), "미분류/기타")


class TestMatch(unittest.TestCase):
    def test_empty_match_is_a_catch_all(self):
        self.assertTrue(Match().matches(make_message(), make_identity()))

    def test_all_semantics_require_every_condition(self):
        match = Match(all=True, company=["한국전자"], subject_contains=["발주"])
        self.assertFalse(match.matches(make_message(), make_identity()))

    def test_any_semantics_require_only_one(self):
        match = Match(all=False, company=["한국전자"], subject_contains=["발주"])
        self.assertTrue(match.matches(make_message(), make_identity()))

    def test_company_match_is_case_insensitive_and_exact(self):
        self.assertTrue(Match(company=["한국전자"]).matches(make_message(), make_identity()))
        self.assertFalse(Match(company=["한국"]).matches(make_message(), make_identity()))

    def test_subject_match_is_a_substring(self):
        self.assertTrue(
            Match(subject_contains=["견적"]).matches(make_message(), make_identity())
        )

    def test_is_unknown_distinguishes_matched_senders(self):
        known, unknown = make_identity(), Identity(email="x@y.com", domain="y.com")
        self.assertTrue(Match(is_unknown=False).matches(make_message(), known))
        self.assertTrue(Match(is_unknown=True).matches(make_message(), unknown))
        self.assertFalse(Match(is_unknown=True).matches(make_message(), known))

    def test_attachment_and_importance_conditions(self):
        message = make_message(has_attachments=True, importance=2)
        self.assertTrue(Match(has_attachments=True).matches(message, make_identity()))
        self.assertTrue(Match(importance_min=2).matches(message, make_identity()))
        self.assertFalse(Match(importance_min=2).matches(make_message(), make_identity()))

    def test_has_group_distinguishes_confirmed_companies(self):
        grouped = make_identity()
        ungrouped = make_identity(company=Company(name="Reddit", group_name=""))
        self.assertTrue(Match(has_group=True).matches(make_message(), grouped))
        self.assertFalse(Match(has_group=True).matches(make_message(), ungrouped))
        self.assertTrue(Match(has_group=False).matches(make_message(), ungrouped))

    def test_false_conditions_are_tested_not_ignored(self):
        """`is_internal=False` must be a real test, not treated as 'unset'."""
        internal = make_identity(is_internal=True)
        self.assertFalse(Match(is_internal=False).matches(make_message(), internal))


class TestRuleSetEvaluation(unittest.TestCase):
    def test_first_match_wins_by_default(self):
        ruleset = RuleSet(
            [
                Rule(name="first", actions=Actions(move_to="A")),
                Rule(name="second", actions=Actions(move_to="B")),
            ]
        )
        decision = ruleset.evaluate(make_message(), make_identity())
        self.assertEqual(decision.move_to, "A")
        self.assertEqual(decision.rule_names, ["first"])

    def test_stop_on_match_false_lets_later_rules_add(self):
        ruleset = RuleSet(
            [
                Rule(name="tag", stop_on_match=False, actions=Actions(categories=["urgent"])),
                Rule(name="file", actions=Actions(move_to="Inbox/{company}")),
            ]
        )
        decision = ruleset.evaluate(make_message(), make_identity())
        self.assertEqual(decision.rule_names, ["tag", "file"])
        self.assertEqual(decision.categories, ["urgent"])
        self.assertEqual(decision.move_to, "Inbox/한국전자")

    def test_disabled_rules_are_skipped(self):
        ruleset = RuleSet(
            [
                Rule(name="off", enabled=False, actions=Actions(move_to="A")),
                Rule(name="on", actions=Actions(move_to="B")),
            ]
        )
        self.assertEqual(ruleset.evaluate(make_message(), make_identity()).move_to, "B")

    def test_no_match_produces_no_effect(self):
        ruleset = RuleSet([Rule(name="x", match=Match(company=["없는회사"]))])
        decision = ruleset.evaluate(make_message(), make_identity())
        self.assertFalse(decision.matched)
        self.assertFalse(decision.has_effect)

    def test_commas_are_stripped_from_category_names(self):
        """Outlook separates categories with commas, so one may not contain one."""
        identity = make_identity(company=Company(name="Acme, Inc.", group_name="고객사"))
        ruleset = RuleSet([Rule(name="c", actions=Actions(categories=["{company}"]))])
        decision = ruleset.evaluate(make_message(), identity)
        self.assertNotIn(",", decision.categories[0])

    def test_move_target_is_sanitised(self):
        identity = make_identity(company=Company(name="A/B: Corp", group_name="고객사"))
        ruleset = RuleSet([Rule(name="m", actions=Actions(move_to="Inbox/{company}"))])
        self.assertEqual(
            ruleset.evaluate(make_message(), identity).move_to, "Inbox/A-B- Corp"
        )


class TestDefaultRuleset(unittest.TestCase):
    def test_internal_mail_is_claimed_before_company_filing(self):
        decision = default_ruleset().evaluate(
            make_message(), make_identity(is_internal=True)
        )
        self.assertEqual(decision.rule_names, ["사내 메일 (internal)"])
        self.assertEqual(decision.move_to, "")  # colleagues are tagged, not moved

    def test_company_you_have_written_to_is_filed(self):
        """The main path, and it needs no setup: Sent Items proves the tie."""
        identity = make_identity(
            display_name="홍길동",
            company=Company(name="한국전자", group_name=""),
            has_correspondence=True,
        )
        decision = default_ruleset().evaluate(make_message(), identity)
        self.assertEqual(decision.move_to, "Inbox/거래처/한국전자/홍길동")
        self.assertIn("한국전자", decision.categories)

    def test_manually_grouped_company_is_filed_even_without_correspondence(self):
        identity = make_identity(display_name="홍길동")  # group 고객사, no sent mail
        decision = default_ruleset().evaluate(make_message(), identity)
        self.assertEqual(decision.move_to, "Inbox/거래처/한국전자/홍길동")

    def test_contact_without_a_display_name_uses_the_mailbox_name(self):
        identity = make_identity(display_name="", has_correspondence=True)
        decision = default_ruleset().evaluate(make_message(), identity)
        self.assertEqual(decision.move_to, "Inbox/거래처/한국전자/hong")

    def test_newsletter_sender_is_left_in_the_inbox(self):
        """The decisive case.

        The learner invents a company for every domain it meets, so simply
        being "known" proves nothing. Instagram has never been written to and
        has no group, so its mail stays where the user can see it.
        """
        newsletter = make_identity(company=Company(name="Instagram", group_name=""))
        decision = default_ruleset().evaluate(make_message(), newsletter)
        self.assertEqual(decision.move_to, "")
        self.assertEqual(decision.categories, ["미분류"])

    def test_unknown_sender_stays_in_the_inbox(self):
        unknown = Identity(email="x@nowhere.kr", domain="nowhere.kr")
        decision = default_ruleset().evaluate(make_message(), unknown)
        self.assertEqual(decision.move_to, "")
        self.assertEqual(decision.categories, ["미분류"])

    def test_internal_mail_is_tagged_but_never_moved(self):
        decision = default_ruleset().evaluate(
            make_message(), make_identity(is_internal=True)
        )
        self.assertEqual(decision.move_to, "")


class TestPersistence(unittest.TestCase):
    def test_round_trip_preserves_rules(self):
        original = RuleSet(
            [
                Rule(
                    name="테스트",
                    stop_on_match=False,
                    match=Match(company=["한국전자"], has_attachments=True),
                    actions=Actions(move_to="Inbox/{company}", categories=["{group}"]),
                )
            ]
        )
        restored = RuleSet.from_dict(original.to_dict())
        self.assertEqual(len(restored.rules), 1)
        rule = restored.rules[0]
        self.assertEqual(rule.name, "테스트")
        self.assertFalse(rule.stop_on_match)
        self.assertEqual(rule.match.company, ["한국전자"])
        self.assertTrue(rule.match.has_attachments)
        self.assertEqual(rule.actions.move_to, "Inbox/{company}")

    def test_default_ruleset_round_trips(self):
        restored = RuleSet.from_dict(default_ruleset().to_dict())
        self.assertEqual(
            [r.name for r in restored.rules],
            [r.name for r in default_ruleset().rules],
        )

    def test_unknown_keys_are_ignored(self):
        restored = RuleSet.from_dict(
            {"rules": [{"name": "x", "match": {"bogus": 1}, "actions": {"nope": 2}}]}
        )
        self.assertEqual(restored.rules[0].name, "x")


if __name__ == "__main__":
    unittest.main()
