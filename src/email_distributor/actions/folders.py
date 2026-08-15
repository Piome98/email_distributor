"""Creates the 업체/담당자 folder tree in Outlook, ahead of any filing.

The paths are not invented here. They are produced by running the *real* rules
over the *real* identities in the database, so the tree that gets created is
exactly the tree filing will use. If the two were computed separately they
would drift, and mail would quietly land somewhere other than the folder the
user was shown.

Folders are created through Outlook itself, so on an Exchange or IMAP account
they sync to the server like any folder made by hand - they appear on the web
client and on a phone. A folder created inside a local .pst does not sync, so
the store is checked and reported before anything is created.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from ..config import Settings
from ..identity.store import IdentityStore
from ..outlook.client import OutlookClient
from ..outlook.message import Message
from ..rules.engine import RuleSet

log = logging.getLogger(__name__)


@dataclass
class FolderPlan:
    """One folder that should exist, and why."""

    path: str
    company: str = ""
    person: str = ""
    message_count: int = 0
    exists: bool = False
    created: bool = False
    error: str = ""

    def describe(self) -> str:
        if self.error:
            return f"ERROR  {self.path} - {self.error}"
        if self.exists:
            return f"exists {self.path}"
        if self.created:
            return f"CREATED {self.path}"
        return f"would create {self.path}"


@dataclass
class FolderReport:
    plans: list[FolderPlan] = field(default_factory=list)
    store_name: str = ""
    store_kind: str = ""
    store_syncs: bool = True
    dry_run: bool = True

    @property
    def existing(self) -> int:
        return sum(1 for p in self.plans if p.exists)

    @property
    def created(self) -> int:
        return sum(1 for p in self.plans if p.created)

    @property
    def missing(self) -> int:
        return sum(1 for p in self.plans if not p.exists and not p.created)

    @property
    def errors(self) -> int:
        return sum(1 for p in self.plans if p.error)

    def describe(self) -> str:
        verb = "would create" if self.dry_run else "created"
        return (
            f"{len(self.plans)} folder(s) in the plan: {self.existing} already "
            f"exist, {self.created if not self.dry_run else self.missing} "
            f"{verb}, {self.errors} error(s)"
        )


class FolderBuilder:
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

    # ------------------------------------------------------------------
    def _sample_message(self) -> Message:
        """A stand-in message, so the rules can be evaluated without real mail.

        Only the sender matters for folder naming; subject and body are left
        empty so that content-based conditions never match and invent a folder
        that real mail would not actually use.
        """
        return Message(entry_id="", subject="", body="", received=datetime.now())

    def _company_level_path(self, message: Message, company: Any, internal: Any) -> str:
        """The part of a company's folder path that does not depend on a person.

        Rather than assume the template ends with {person}, the rules are
        evaluated twice with two different synthetic contacts at the company's
        own domain. Whatever the two paths share is company-level; the first
        segment that differs is where the contact name begins. That keeps this
        correct for any template the user writes, not just the shipped one.
        """
        domain = company.domains[0] if company.domains else ""
        if not domain:
            return ""

        first = self.store.resolve(f"aaa@{domain}", "AAA", internal_domains=internal)
        second = self.store.resolve(f"bbb@{domain}", "BBB", internal_domains=internal)
        if first.company is None:
            return ""

        left = self.ruleset.evaluate(message, first).move_to.split("/")
        right = self.ruleset.evaluate(message, second).move_to.split("/")

        shared: list[str] = []
        for a, b in zip(left, right):
            if a != b:
                break
            shared.append(a)
        return "/".join(shared)

    def plan(
        self,
        min_messages: int = 1,
        include_people: bool = True,
    ) -> list[FolderPlan]:
        """Work out every folder the current rules and database imply."""
        message = self._sample_message()
        internal = self.settings.internal_domains
        seen: dict[str, FolderPlan] = {}

        def add(path: str, company: str, person: str, count: int) -> None:
            if not path:
                return
            existing = seen.get(path)
            if existing is None:
                seen[path] = FolderPlan(
                    path=path, company=company, person=person, message_count=count
                )
            else:
                # Several people can map to one folder; keep the busiest as
                # the representative and total the traffic.
                existing.message_count += count
                if count > 0 and not existing.person:
                    existing.person = person

        for company in self.store.list_companies():
            people = self.store.list_people(company.id)
            qualifying = [p for p in people if p.message_count >= min_messages]

            if include_people and qualifying:
                for person in qualifying:
                    identity = self.store.resolve(
                        person.email, person.display_name, internal_domains=internal
                    )
                    decision = self.ruleset.evaluate(message, identity)
                    add(
                        decision.move_to,
                        company.name,
                        person.display_name or person.email,
                        person.message_count,
                    )
            else:
                # No qualifying contact, so make the company's own folder and
                # stop there - inventing a contact called "unknown" would
                # create a folder no mail will ever be filed into.
                path = self._company_level_path(message, company, internal)
                add(path, company.name, "", 0)

        plans = sorted(seen.values(), key=lambda p: p.path)
        for plan in plans:
            plan.exists = self.client.get_folder(plan.path) is not None
        return plans

    # ------------------------------------------------------------------
    def build(
        self,
        min_messages: int = 1,
        include_people: bool = True,
        dry_run: bool = True,
        on_progress: Optional[Callable[[FolderPlan], None]] = None,
    ) -> FolderReport:
        info = self.client.store_info()
        report = FolderReport(
            store_name=str(info.get("name", "")),
            store_kind=str(info.get("kind", "")),
            store_syncs=bool(info.get("syncs", True)),
            dry_run=dry_run,
        )
        report.plans = self.plan(
            min_messages=min_messages, include_people=include_people
        )

        if dry_run:
            if on_progress:
                for plan in report.plans:
                    on_progress(plan)
            return report

        for plan in report.plans:
            if plan.exists:
                if on_progress:
                    on_progress(plan)
                continue
            try:
                self.client.ensure_folder(plan.path)
                plan.created = True
            except Exception as exc:  # noqa: BLE001 - one bad name must not stop the rest
                plan.error = str(exc)
                log.warning("could not create %s: %s", plan.path, exc)
            if on_progress:
                on_progress(plan)

        return report
