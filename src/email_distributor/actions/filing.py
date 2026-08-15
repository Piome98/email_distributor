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


class OutlookActionError(RuntimeError):
    """A requested change to the mailbox could not be carried out."""


def _safe_entry_id(item: Any) -> str:
    try:
        return str(item.EntryID)
    except Exception:  # noqa: BLE001
        return ""


def _safe_folders(folder: Any) -> list[Any]:
    """Subfolders as a plain list, snapshotted before anything is moved."""
    try:
        return list(folder.Folders)
    except Exception:  # noqa: BLE001
        return []


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

    def apply(self, item: Any, decision: Decision) -> tuple[bool, str]:
        """Carry out a decision against a live Outlook item.

        Returns (changed, new_entry_id). `new_entry_id` is non-empty only when
        the message was moved, because Outlook issues a fresh EntryID on a
        folder change and the ledger has to record the new one.

        A failed move raises rather than returning quietly: applying a category
        but leaving the message where it was is not the requested outcome, and
        reporting it as success would be a lie.
        """
        changed = False

        # Categories and read-state are set before the move, because after
        # Move() the original reference points at an item that no longer
        # exists in its old folder.
        if self.settings.apply_categories and decision.categories:
            changed |= self.client.add_categories(item, decision.categories)

        if decision.mark_read is not None:
            changed |= self.client.mark_read(item, decision.mark_read)

        new_entry_id = ""
        if self.settings.move_to_folders and decision.move_to:
            folder = self._target_folder(decision.move_to)
            moved = self.client.move_item(item, folder)
            if moved is None:
                raise OutlookActionError(
                    f"could not move message into {decision.move_to!r}"
                )
            changed = True
            new_entry_id = str(_safe_entry_id(moved))

        return changed, new_entry_id

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
        # If the sender could not be resolved to a real address we know nothing
        # about this message, so the safe action is none at all. Filing it as
        # "unknown" would sweep mail into a review folder on the strength of a
        # lookup failure - which is exactly what an Exchange address-book
        # hiccup looks like.
        if not message.sender_email:
            result.skipped_reason = "sender address could not be resolved"
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
            result.applied, new_entry_id = self.apply(item, decision)
            rules = " + ".join(decision.rule_names)

            # Only a message that was actually filed counts as handled.
            #
            # A tag-only decision leaves the message exactly where it was, so
            # recording it as processed would permanently consume it: once the
            # rules improve - a company gets confirmed as a 거래처 - the message
            # would be skipped forever and never reach its folder. Re-tagging on
            # a later pass is harmless, because adding a category the message
            # already has changes nothing.
            if not decision.move_to:
                return result

            self.store.mark_processed(message.entry_id, rules, decision.describe())
            # Outlook issues a new EntryID when a message changes folder, so
            # the pre-move id alone would not recognise this message again.
            # Without the new one, a later pass over the destination folder
            # re-files mail that is already filed - and Outlook then refuses
            # the move because the message is already there.
            if new_entry_id and new_entry_id != message.entry_id:
                self.store.mark_processed(new_entry_id, rules, decision.describe())
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the run
            result.applied = False
            result.error = str(exc)
            log.warning("failed to file %s: %s", message.summary(), exc)

        return result

    def process_tree(
        self,
        folder: Any,
        limit: int = 200,
        reprocess: bool = False,
        on_result: Optional[Callable[[FileResult], None]] = None,
    ) -> RunSummary:
        """Process a folder and every folder beneath it.

        Used to re-file mail that an earlier ruleset already sorted: the
        messages are no longer in the watch folder, so only a recursive pass
        can reach them.
        """
        combined = RunSummary(dry_run=self.settings.dry_run)

        def walk(current: Any) -> None:
            if len(combined.results) >= limit:
                return
            remaining = limit - len(combined.results)
            summary = self.process_folder(
                current, limit=remaining, reprocess=reprocess, on_result=on_result
            )
            combined.results.extend(summary.results)
            for sub in _safe_folders(current):
                walk(sub)

        walk(folder)
        return combined

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
