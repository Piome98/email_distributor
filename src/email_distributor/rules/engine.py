"""The rules engine: turns "who is this from?" into "where does it go?".

Rules live in a JSON file under %LOCALAPPDATA% so they can be edited by hand,
version-controlled, or shared with a colleague. They are evaluated top to
bottom; the first match wins unless a rule sets `stop_on_match: false`, in
which case evaluation continues and later actions are merged over earlier ones.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..config import rules_path
from ..identity.models import Identity
from ..outlook.message import Message

# Characters Outlook refuses in a folder name.
RE_ILLEGAL_FOLDER_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_folder_name(name: str) -> str:
    """Make a string safe to use as a single Outlook folder name."""
    cleaned = RE_ILLEGAL_FOLDER_CHARS.sub("-", name).strip(" .")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned[:100] or "기타"


def sanitize_folder_path(path: str) -> str:
    """Sanitize each segment of a folder path, preserving the separators."""
    parts = [p for p in path.replace("\\", "/").split("/") if p.strip()]
    return "/".join(sanitize_folder_name(p) for p in parts)


def expand(
    template: str, message: Message, identity: Identity, for_path: bool = False
) -> str:
    """Fill placeholders such as {company} and {group} from the sender.

    When `for_path` is set, each substituted *value* is sanitised before it is
    inserted. This matters: a company legitimately named "A/B Corp" would
    otherwise inject a path separator into "Inbox/{company}" and quietly create
    a nested "A" folder containing "B Corp". Separators may come from the
    template, never from the data.
    """
    person = identity.person
    now = message.received or datetime.now()
    values = {
        "company": identity.company_name or "기타",
        "group": identity.group_name or "미분류",
        "person": (identity.display_name or identity.email.split("@")[0] or "unknown"),
        "email": identity.email,
        "domain": identity.domain or "unknown",
        "department": (person.department if person and person.department else "기타"),
        "title": (person.title if person and person.title else ""),
        "year": now.strftime("%Y"),
        "month": now.strftime("%m"),
        "yyyymm": now.strftime("%Y-%m"),
    }
    out = template
    for key, value in values.items():
        text = str(value)
        if for_path:
            text = sanitize_folder_name(text)
        out = out.replace("{" + key + "}", text)
    return out


@dataclass
class Match:
    """Conditions on a message. Empty/None fields are simply not tested."""

    # When True every populated condition must hold; when False any one will do.
    all: bool = True

    group: list[str] = field(default_factory=list)
    company: list[str] = field(default_factory=list)
    sender_domain: list[str] = field(default_factory=list)
    sender_email: list[str] = field(default_factory=list)
    department: list[str] = field(default_factory=list)
    subject_contains: list[str] = field(default_factory=list)
    body_contains: list[str] = field(default_factory=list)

    is_internal: Optional[bool] = None
    is_unknown: Optional[bool] = None      # sender not matched to any company
    is_public_domain: Optional[bool] = None

    # True only when the company has been put into a group by the user.
    #
    # This is the difference between "the learner saw this domain" and "I have
    # confirmed this is a 거래처". Every newsletter domain becomes a company
    # automatically, so without this test the rules would file Instagram and
    # Reddit alongside real customers.
    has_group: Optional[bool] = None
    has_attachments: Optional[bool] = None
    unread_only: Optional[bool] = None
    importance_min: Optional[int] = None

    def _tests(self, message: Message, identity: Identity) -> list[bool]:
        """Evaluate every condition that was actually configured."""
        results: list[bool] = []

        def any_ci(needles: list[str], haystack: str) -> bool:
            low = haystack.lower()
            return any(n.strip().lower() in low for n in needles if n.strip())

        def equals_ci(needles: list[str], value: str) -> bool:
            low = value.lower()
            return any(n.strip().lower() == low for n in needles if n.strip())

        if self.group:
            results.append(equals_ci(self.group, identity.group_name))
        if self.company:
            results.append(equals_ci(self.company, identity.company_name))
        if self.sender_domain:
            results.append(equals_ci(self.sender_domain, identity.domain))
        if self.sender_email:
            results.append(equals_ci(self.sender_email, identity.email))
        if self.department:
            dept = identity.person.department if identity.person else ""
            results.append(equals_ci(self.department, dept))
        if self.subject_contains:
            results.append(any_ci(self.subject_contains, message.subject))
        if self.body_contains:
            results.append(any_ci(self.body_contains, message.body))

        if self.is_internal is not None:
            results.append(identity.is_internal == self.is_internal)
        if self.is_unknown is not None:
            results.append((not identity.known) == self.is_unknown)
        if self.is_public_domain is not None:
            results.append(identity.is_public_domain == self.is_public_domain)
        if self.has_group is not None:
            results.append(bool(identity.group_name) == self.has_group)
        if self.has_attachments is not None:
            results.append(message.has_attachments == self.has_attachments)
        if self.unread_only is not None:
            results.append(message.unread == self.unread_only)
        if self.importance_min is not None:
            results.append(message.importance >= self.importance_min)

        return results

    def matches(self, message: Message, identity: Identity) -> bool:
        results = self._tests(message, identity)
        if not results:
            return True  # a rule with no conditions is a catch-all
        return all(results) if self.all else any(results)


@dataclass
class Actions:
    move_to: str = ""                                  # folder path template
    categories: list[str] = field(default_factory=list)  # category templates
    mark_read: Optional[bool] = None


@dataclass
class Rule:
    name: str = "unnamed"
    enabled: bool = True
    stop_on_match: bool = True
    match: Match = field(default_factory=Match)
    actions: Actions = field(default_factory=Actions)


@dataclass
class Decision:
    """What the engine concluded for one message."""

    matched: bool = False
    rule_names: list[str] = field(default_factory=list)
    move_to: str = ""
    categories: list[str] = field(default_factory=list)
    mark_read: Optional[bool] = None

    def describe(self) -> str:
        if not self.matched:
            return "no rule matched"
        bits = []
        if self.move_to:
            bits.append(f"move -> {self.move_to}")
        if self.categories:
            bits.append(f"categories: {', '.join(self.categories)}")
        if self.mark_read is not None:
            bits.append("mark read" if self.mark_read else "mark unread")
        return f"{' + '.join(self.rule_names)}: {'; '.join(bits) or 'no action'}"

    @property
    def has_effect(self) -> bool:
        return bool(self.move_to or self.categories or self.mark_read is not None)


class RuleSet:
    def __init__(self, rules: Optional[list[Rule]] = None) -> None:
        self.rules: list[Rule] = rules if rules is not None else []

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def evaluate(self, message: Message, identity: Identity) -> Decision:
        decision = Decision()
        for rule in self.rules:
            if not rule.enabled:
                continue
            if not rule.match.matches(message, identity):
                continue

            decision.matched = True
            decision.rule_names.append(rule.name)

            if rule.actions.move_to:
                decision.move_to = sanitize_folder_path(
                    expand(rule.actions.move_to, message, identity, for_path=True)
                )
            for template in rule.actions.categories:
                value = expand(template, message, identity).strip()
                # Outlook uses "," to separate categories, so a category name
                # may not contain one.
                value = value.replace(",", " ").strip()
                if value and value not in decision.categories:
                    decision.categories.append(value)
            if rule.actions.mark_read is not None:
                decision.mark_read = rule.actions.mark_read

            if rule.stop_on_match:
                break
        return decision

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "rules": [
                {
                    "name": r.name,
                    "enabled": r.enabled,
                    "stop_on_match": r.stop_on_match,
                    "match": {
                        k: v
                        for k, v in vars(r.match).items()
                        if v not in ([], None) or k == "all"
                    },
                    "actions": {
                        k: v for k, v in vars(r.actions).items() if v not in ([], None, "")
                    },
                }
                for r in self.rules
            ],
        }

    def save(self, path: Optional[Path] = None) -> Path:
        target = Path(path) if path else rules_path()
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuleSet":
        rules: list[Rule] = []
        match_fields = set(Match.__dataclass_fields__)
        action_fields = set(Actions.__dataclass_fields__)

        for entry in raw.get("rules", []):
            match_raw = {
                k: v for k, v in (entry.get("match") or {}).items() if k in match_fields
            }
            action_raw = {
                k: v
                for k, v in (entry.get("actions") or {}).items()
                if k in action_fields
            }
            rules.append(
                Rule(
                    name=str(entry.get("name", "unnamed")),
                    enabled=bool(entry.get("enabled", True)),
                    stop_on_match=bool(entry.get("stop_on_match", True)),
                    match=Match(**match_raw),
                    actions=Actions(**action_raw),
                )
            )
        return cls(rules)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "RuleSet":
        target = Path(path) if path else rules_path()
        if not target.exists():
            ruleset = default_ruleset()
            ruleset.save(target)
            return ruleset
        try:
            return cls.from_dict(json.loads(target.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return default_ruleset()


def default_ruleset() -> RuleSet:
    """A safe, useful starting point.

    Ordering matters: internal mail is claimed first so colleagues never end up
    filed as an external company, then confirmed 거래처 are filed under
    거래처/{업체}/{담당자}.

    Only companies **you have put into a group** are filed. The learner turns
    every domain it meets into a company, newsletters included, so without that
    condition the rules would file Instagram and Reddit next to real customers
    and empty the Inbox into folders that mean nothing. Assigning a group on
    the Companies tab is the act of saying "this really is a 거래처".

    Everything else is deliberately **left in the Inbox** and merely tagged.
    Moving mail we cannot explain only relocates the problem into a folder
    nobody reads; the person reading it knows what it is, and can file it.
    """
    return RuleSet(
        [
            Rule(
                name="사내 메일 (internal)",
                match=Match(is_internal=True),
                actions=Actions(categories=["사내"]),
            ),
            Rule(
                name="거래처/담당자별 분류 (confirmed companies only)",
                match=Match(is_unknown=False, has_group=True),
                actions=Actions(
                    move_to="Inbox/거래처/{company}/{person}",
                    categories=["{group}", "{company}"],
                ),
            ),
            Rule(
                name="미확인 - 받은 편지함에 그대로 둠 (not yet grouped: leave in Inbox)",
                match=Match(),
                actions=Actions(categories=["미분류"]),
            ),
        ]
    )
