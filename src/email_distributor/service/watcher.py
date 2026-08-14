"""Background polling service.

Outlook can push events over COM, but those callbacks are fragile: they die
silently when Outlook is busy, when the profile reconnects, or when a modal
dialog is open. Polling on a timer is unglamorous and completely reliable, and
at one pass per minute the cost is irrelevant.

The whole COM stack lives inside this thread. COM objects are apartment-
threaded and must not cross thread boundaries, so the worker builds its own
client, store and distributor and never shares them with the UI thread.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ..actions.filing import Distributor, FileResult, RunSummary
from ..config import Settings
from ..identity.store import IdentityStore
from ..outlook.client import COM_AVAILABLE, OutlookClient, OutlookError
from ..rules.engine import RuleSet

log = logging.getLogger(__name__)

EventFn = Callable[[str, str], None]  # (level, text)


class Watcher:
    """Polls the watch folder on an interval and files what it finds."""

    def __init__(self, settings: Settings, on_event: Optional[EventFn] = None) -> None:
        self.settings = settings
        self.on_event = on_event or (lambda level, text: None)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_summary: Optional[RunSummary] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_summary(self) -> Optional[RunSummary]:
        with self._lock:
            return self._last_summary

    def start(self) -> None:
        if self.running:
            return
        if not COM_AVAILABLE:
            self.on_event("error", "pywin32 not installed - cannot reach Outlook.")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="distributor-watcher", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def poll_now(self) -> None:
        """Ask the worker to run a pass immediately instead of waiting."""
        self._wake.set()

    # ------------------------------------------------------------------
    def _emit(self, level: str, text: str) -> None:
        try:
            self.on_event(level, text)
        except Exception:  # noqa: BLE001 - a broken UI callback must not kill the loop
            log.debug("event callback failed", exc_info=True)

    def _run(self) -> None:
        client = OutlookClient()
        store: Optional[IdentityStore] = None

        try:
            client.connect()
            # SQLite connections are also thread-bound, so this one belongs to
            # the worker just as the COM objects do.
            store = IdentityStore()
            ruleset = RuleSet.load()
            distributor = Distributor(client, store, ruleset, self.settings)

            mode = "DRY RUN - nothing will be moved" if self.settings.dry_run else "LIVE"
            self._emit(
                "info",
                f"Watching '{self.settings.watch_folder}' every "
                f"{self.settings.poll_interval}s ({mode})",
            )

            while not self._stop.is_set():
                self._poll_once(distributor)

                # Interruptible sleep: stop() and poll_now() both wake it.
                self._wake.wait(timeout=max(5, self.settings.poll_interval))
                self._wake.clear()

        except OutlookError as exc:
            self._emit("error", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("watcher crashed")
            self._emit("error", f"Watcher stopped unexpectedly: {exc}")
        finally:
            if store is not None:
                store.close()
            client.close()
            self._emit("info", "Watcher stopped.")

    def _poll_once(self, distributor: Distributor) -> None:
        def report(result: FileResult) -> None:
            if result.error:
                self._emit("error", result.describe())
            elif result.decision.has_effect and not result.skipped_reason:
                self._emit("action", result.describe())

        try:
            # Re-read the ruleset each pass so edits in the UI take effect
            # without restarting the watcher.
            distributor.ruleset = RuleSet.load()
            summary = distributor.process_folder(
                distributor.client.get_folder(self.settings.watch_folder)
                or distributor.client.inbox(),
                limit=200,
                on_result=report,
            )
            with self._lock:
                self._last_summary = summary

            if summary.planned or summary.errors:
                self._emit("info", summary.describe())
        except Exception as exc:  # noqa: BLE001 - keep polling through transient faults
            log.warning("poll failed: %s", exc)
            self._emit("error", f"Poll failed: {exc}")
