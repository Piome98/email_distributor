"""Applies a filing decision to the mailbox.

This is the only module that changes anything in Outlook, which makes it the
only place that has to be careful. Two safeguards live here:

* `dry_run` (on by default) produces a full plan without touching the mailbox,
  so a first run can be inspected before anything moves.
* Every acted-on message is recorded by EntryID, so restarting the tool never
  re-files mail that has already been handled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import Settings
from ..identity.models import Identity
from ..identity.store import IdentityStore
from ..outlook.client import OutlookClient
from ..outlook.message import Message
from ..rules.engine import Decision, RuleSet

log = logging.getLogger(__name__)


@dataclass
class FileResult:
    message: Message
    identity: Identity
    decision: Decision
    applied: bool = False
    dry_run: bool = False
    skipped_reason: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.skipped_reason

    def describe(self) -> str:
        prefix = "[dry-run] " if self.dry_run else ""
        if self.error:
            return f"{prefix}ERROR {self.message.summary()} - {self.error}"
        if self.skipped_reason:
            return f"{prefix}skip  {self.message.summary()} - {self.skipped_reason}"
        return (
            f"{prefix}{self.message.summary()}\n"
            f"        who: {self.identity.describe()}\n"
            f"        do : {self.decision.describe()}"
        )


@dataclass
class RunSummary:
    results: list[FileResult] = field(default_factory=list)
    dry_run: bool = True

    @property
    def applied(self) -> int:
        return sum(1 for r in self.results if r.applied)

    @property
    def planned(self) -> int:
        return sum(1 for r in self.results if r.decision.has_effect and not r.error)

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.error)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.skipped_reason)

    def describe(self) -> str:
        mode = "planned (dry run)" if self.dry_run else "applied"
        return (
            f"{len(self.results)} message(s) examined, {self.planned} {mode}, "
            f"{self.skipped} skipped, {self.errors} error(s)"
        )


class Distributor:
    """Ties identity resolution, rules and mailbox actions together."""

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
        self._folder_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    def plan(self, message: Message) -> tuple[Identity, Decision]:
        """Resolve the sender and decide what should happen. No side effects."""
        identity = self.store.resolve(
            message.sender_email,
            message.sender_name,
            internal_domains=self.settings.internal_domains,
        )
        return identity, self.ruleset.evaluate(message, identity)

    def _target_folder(self, path: str) -> Any:
        if path not in self._folder_cache:
            self._folder_cache[path] = self.client.ensure_folder(path)
        return self._folder_cache[path]

    def apply(self, item: Any, decision: Decision) -> bool:
        """Carry out a decision against a live Outlook item."""
        changed = False

        # Categories and read-state are set before the move, because after
        # Move() the original reference points at an item that no longer
        # exists in its old folder.
        if self.settings.apply_categories and decision.categories:
            changed |= self.client.add_categories(item, decision.categories)

        if decision.mark_read is not None:
            changed |= self.client.mark_read(item, decision.mark_read)

        if self.settings.move_to_folders and decision.move_to:
            folder = self._target_folder(decision.move_to)
            changed |= self.client.move_item(item, folder)

        return changed

    # ------------------------------------------------------------------
    def process_folder(
        self,
        folder: Any,
        limit: int = 200,
        unread_only: bool = False,
        reprocess: bool = False,
        on_result: Optional[Callable[[FileResult], None]] = None,
    ) -> RunSummary:
        """Examine a folder and file everything the rules claim."""
        summary = RunSummary(dry_run=self.settings.dry_run)

        # Snapshot the whole batch before acting. Moving a message out of the
        # folder currently being iterated shifts every later index, which would
        # silently skip half the batch if we acted mid-iteration.
        batch = list(
            self.client.iter_messages(folder, limit=limit, unread_only=unread_only)
        )

        for item, message in batch:
            result = self._process_one(item, message, reprocess)
            summary.results.append(result)
            if on_result:
                on_result(result)

        return summary

    def _process_one(self, item: Any, message: Message, reprocess: bool) -> FileResult:
        identity, decision = self.plan(message)
        result = FileResult(
            message=message,
            identity=identity,
            decision=decision,
            dry_run=self.settings.dry_run,
        )

        if not message.entry_id:
            result.skipped_reason = "message has no EntryID"
            return result
        if not reprocess and self.store.is_processed(message.entry_id):
            result.skipped_reason = "already processed"
            return result
        if not decision.has_effect:
            result.skipped_reason = decision.describe()
            return result

        if self.settings.dry_run:
            return result  # planned, deliberately not applied

        try:
            result.applied = self.apply(item, decision)
            self.store.mark_processed(
                message.entry_id,
                " + ".join(decision.rule_names),
                decision.describe(),
            )
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the run
            result.error = str(exc)
            log.warning("failed to file %s: %s", message.summary(), exc)

        return result

    def process_watch_folder(
        self,
        limit: int = 200,
        unread_only: bool = False,
        on_result: Optional[Callable[[FileResult], None]] = None,
    ) -> RunSummary:
        folder = self.client.get_folder(self.settings.watch_folder)
        if folder is None:
            raise ValueError(
                f"Watch folder not found: {self.settings.watch_folder!r}"
            )
        return self.process_folder(
            folder, limit=limit, unread_only=unread_only, on_result=on_result
        )
