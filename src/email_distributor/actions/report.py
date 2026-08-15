"""Explains, across a whole folder, why each message has not been filed.

A capped filing run only ever examines the newest slice of a large mailbox, so
"nothing is moving" and "nothing in the first 200 is eligible" look identical
from the outside. This walks every message and says, for each one, which of the
handful of possible reasons applies - and then ranks the companies that are
holding back the most mail, since confirming those is the fastest way to make
the rest of the inbox sort itself.

Reading is deliberately body-free: only the sender matters here, and the body
is by far the most expensive property to pull over COM.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import Settings
from ..identity.store import IdentityStore
from ..outlook.client import OutlookClient
from ..rules.engine import RuleSet

# Why a message is sitting where it is.
FILED = "이동 대상 (will be filed)"
ALREADY = "이미 같은 위치로 처리됨 (already filed the same way)"
INTERNAL = "사내 메일 (internal - tagged, not moved)"
NO_SENDER = "발신자 주소를 확인할 수 없음 (sender could not be resolved)"
UNKNOWN_COMPANY = "회사를 알 수 없음 (sender not matched to any company)"
NOT_CONFIRMED = "거래처로 확정되지 않음 (company not confirmed)"
NO_RULE = "해당되는 규칙 없음 (no rule produced an action)"

# The order results are shown in: the actionable reasons first.
REASON_ORDER = [
    FILED,
    NOT_CONFIRMED,
    UNKNOWN_COMPANY,
    INTERNAL,
    ALREADY,
    NO_SENDER,
    NO_RULE,
]


@dataclass
class Report:
    examined: int = 0
    reasons: Counter = field(default_factory=Counter)
    # Company -> how many messages are waiting on it being confirmed.
    blocked_by_company: Counter = field(default_factory=Counter)
    # Sender domain -> message count, for mail we could not place at all.
    unknown_domains: Counter = field(default_factory=Counter)
    destinations: Counter = field(default_factory=Counter)

    @property
    def movable(self) -> int:
        return self.reasons[FILED]

    def describe(self) -> str:
        return f"{self.examined} message(s) examined, {self.movable} would be filed"


class InboxReport:
    def __init__(
        self,
        client: OutlookClient,
        store: IdentityStore,
        ruleset: RuleSet,
        settings: Settings,
    ) -> None:
        self.client = client
        self.store = store
        self.ruleset = ruleset
        self.settings = settings

    def run(
        self,
        folder: Any,
        limit: int = 0,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> Report:
        report = Report()
        internal = self.settings.internal_domains

        for _item, message in self.client.iter_messages(
            folder, limit=limit, with_body=False
        ):
            report.examined += 1
            if on_progress and report.examined % 250 == 0:
                on_progress(report.examined)

            if not message.sender_email:
                report.reasons[NO_SENDER] += 1
                continue

            identity = self.store.resolve(
                message.sender_email, message.sender_name, internal_domains=internal
            )
            decision = self.ruleset.evaluate(message, identity)

            if decision.move_to:
                previous = self.store.processed_action(message.entry_id)
                if previous is not None and previous == decision.describe():
                    report.reasons[ALREADY] += 1
                else:
                    report.reasons[FILED] += 1
                    report.destinations[decision.move_to] += 1
                continue

            if identity.is_internal:
                report.reasons[INTERNAL] += 1
            elif not identity.known:
                report.reasons[UNKNOWN_COMPANY] += 1
                report.unknown_domains[identity.domain or "(no domain)"] += 1
            elif not (identity.group_name or identity.has_correspondence):
                report.reasons[NOT_CONFIRMED] += 1
                report.blocked_by_company[identity.company_name] += 1
            else:
                report.reasons[NO_RULE] += 1

        return report
