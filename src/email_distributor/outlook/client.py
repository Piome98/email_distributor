"""COM bridge to the classic Outlook desktop client.

This drives the Outlook application the user is already signed in to, which is
what makes the whole tool viable on a locked-down corporate laptop: there is no
Azure app registration, no OAuth consent screen, no admin install, and no
credential handling of any kind. Outlook has already authenticated; we simply
ask it to do things.

Threading note: COM is apartment-threaded. Every thread that touches these
objects must call CoInitialize first, and COM objects must not be shared
between threads. `OutlookClient` therefore owns its connection and is expected
to be used from the single thread that created it - see `service/watcher.py`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterator, Optional

from .message import Message

log = logging.getLogger(__name__)

try:  # pragma: no cover - import guard exercised only on non-Windows
    import pythoncom
    import win32com.client
    from win32com.client import constants as _c  # noqa: F401
    COM_AVAILABLE = True
except ImportError:  # pragma: no cover
    pythoncom = None  # type: ignore[assignment]
    win32com = None  # type: ignore[assignment]
    COM_AVAILABLE = False


# MAPI property tags, addressed through PropertyAccessor.
# PR_SMTP_ADDRESS on an AddressEntry, and the "sent representing" variant on
# the message itself. These are the only reliable way to turn an Exchange
# sender into a real SMTP address.
PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
PR_SENT_REPRESENTING_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"

OL_MAIL_ITEM = 43          # olMail
OL_FOLDER_INBOX = 6        # olFolderInbox
OL_FOLDER_SENT = 5         # olFolderSentMail

# Outlook's 25 category colours; 0 means "no colour".
CATEGORY_COLOR_COUNT = 24


class OutlookError(RuntimeError):
    """Raised when Outlook cannot be reached or a request to it fails."""


def _safe(getter: Any, default: Any = "") -> Any:
    """Read one COM property, swallowing the many ways that can fail.

    A property read can raise for entirely routine reasons: the item is a
    meeting response with no sender, the security guard blocked an address
    lookup, or the message was moved by the user mid-iteration. None of those
    should stop a batch run.
    """
    try:
        value = getter()
        return default if value is None else value
    except Exception:  # noqa: BLE001 - COM raises a wide, undocumented variety
        return default


class OutlookClient:
    """A connection to the running Outlook client."""

    def __init__(self) -> None:
        self._app: Any = None
        self._ns: Any = None
        self._com_initialised = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    def connect(self) -> "OutlookClient":
        if not COM_AVAILABLE:
            raise OutlookError(
                "pywin32 is not installed. Run:  pip install --user pywin32"
            )
        if self._app is not None:
            return self

        try:
            pythoncom.CoInitialize()
            self._com_initialised = True
        except Exception:  # noqa: BLE001 - already initialised on this thread
            pass

        try:
            # Dispatch attaches to the running instance when there is one, and
            # starts Outlook otherwise.
            self._app = win32com.client.Dispatch("Outlook.Application")
            self._ns = self._app.GetNamespace("MAPI")
        except Exception as exc:  # noqa: BLE001
            raise OutlookError(
                "Could not attach to Outlook. Make sure the classic Outlook "
                "desktop client is installed and can be started.\n"
                f"Underlying error: {exc}"
            ) from exc
        return self

    def close(self) -> None:
        self._ns = None
        self._app = None
        if self._com_initialised and pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
            self._com_initialised = False

    def __enter__(self) -> "OutlookClient":
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def namespace(self) -> Any:
        if self._ns is None:
            self.connect()
        return self._ns

    def current_user_address(self) -> str:
        """The signed-in user's own SMTP address.

        Used to seed the list of internal domains, so the tool knows which
        mail is from colleagues rather than from an outside organisation.
        """
        try:
            account = self.namespace.Accounts.Item(1)
            addr = _safe(lambda: account.SmtpAddress)
            if addr:
                return str(addr).lower()
        except Exception:  # noqa: BLE001
            pass
        try:
            entry = self.namespace.CurrentUser.AddressEntry
            addr = _safe(
                lambda: entry.GetExchangeUser().PrimarySmtpAddress
            ) or _safe(lambda: entry.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS))
            return str(addr).lower() if addr else ""
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------
    def inbox(self) -> Any:
        return self.namespace.GetDefaultFolder(OL_FOLDER_INBOX)

    def sent_folder(self) -> Any:
        return self.namespace.GetDefaultFolder(OL_FOLDER_SENT)

    def _root(self) -> Any:
        """The mailbox root - the parent of Inbox, Sent Items, and friends."""
        return self.inbox().Parent

    @staticmethod
    def _split_path(path: str) -> list[str]:
        return [p for p in path.replace("\\", "/").split("/") if p.strip()]

    def get_folder(self, path: str) -> Optional[Any]:
        """Resolve a folder path such as "Inbox/거래처/한국전자"."""
        parts = self._split_path(path)
        if not parts:
            return self._root()

        # "Inbox" is localised in a Korean Outlook ("받은 편지함"), so the
        # first segment is matched against the default folder as well as by
        # name; otherwise a hard-coded "Inbox" would never resolve.
        current = self._root()
        if parts[0].lower() in {"inbox", "받은 편지함", "받은편지함"}:
            current = self.inbox()
            parts = parts[1:]

        for part in parts:
            found = None
            for folder in current.Folders:
                if str(_safe(lambda f=folder: f.Name)).strip().lower() == part.lower():
                    found = folder
                    break
            if found is None:
                return None
            current = found
        return current

    def ensure_folder(self, path: str) -> Any:
        """Resolve a folder path, creating any missing levels."""
        parts = self._split_path(path)
        if not parts:
            return self._root()

        current = self._root()
        if parts[0].lower() in {"inbox", "받은 편지함", "받은편지함"}:
            current = self.inbox()
            parts = parts[1:]

        for part in parts:
            found = None
            for folder in current.Folders:
                if str(_safe(lambda f=folder: f.Name)).strip().lower() == part.lower():
                    found = folder
                    break
            if found is None:
                try:
                    found = current.Folders.Add(part)
                except Exception as exc:  # noqa: BLE001
                    raise OutlookError(
                        f"Could not create folder '{part}' under "
                        f"'{_safe(lambda: current.Name)}': {exc}"
                    ) from exc
            current = found
        return current

    def folder_path(self, folder: Any) -> str:
        return str(_safe(lambda: folder.FolderPath, ""))

    def store_info(self, folder: Any = None) -> dict[str, Any]:
        """Describe the store a folder lives in, and whether it syncs.

        This matters for folder creation: a folder made inside a local .pst
        exists only on this PC. It will not appear on the web client, on a
        phone, or after the machine is rebuilt - which is not what anyone means
        by "make the folders in Outlook". Exchange (.ost) and IMAP stores both
        sync to the server; a .pst does not.
        """
        folder = folder if folder is not None else self._root()
        store = _safe(lambda: folder.Store, None)
        if store is None:
            return {"name": "?", "path": "", "syncs": True, "kind": "unknown"}

        path = str(_safe(lambda: store.FilePath, ""))
        # olExchangeStoreType: 0 primary mailbox, 1 delegate, 2 public folder,
        # 3 not Exchange (IMAP, POP or a standalone .pst).
        exchange_type = _safe(lambda: store.ExchangeStoreType, 3)
        is_pst = path.lower().endswith(".pst")

        if is_pst:
            kind = "local .pst - does NOT sync"
        elif exchange_type in (0, 1):
            kind = "Exchange mailbox - syncs"
        elif path.lower().endswith(".ost"):
            kind = "cached account (IMAP/Exchange) - syncs"
        else:
            kind = "account store - syncs"

        return {
            "name": str(_safe(lambda: store.DisplayName, "?")),
            "path": path,
            "syncs": not is_pst,
            "kind": kind,
        }

    def list_folder_tree(self, folder: Any = None, depth: int = 0) -> list[tuple[int, str]]:
        """Flat (depth, name) listing, for populating the UI's folder picker."""
        folder = folder if folder is not None else self._root()
        out: list[tuple[int, str]] = []
        for sub in _safe(lambda: folder.Folders, []):
            name = str(_safe(lambda: sub.Name))
            out.append((depth, name))
            if depth < 3:
                out.extend(self.list_folder_tree(sub, depth + 1))
        return out

    # ------------------------------------------------------------------
    # Reading messages
    # ------------------------------------------------------------------
    def resolve_sender_smtp(self, item: Any) -> str:
        """Turn a message's sender into a real SMTP address.

        Inside an Exchange organisation `SenderEmailAddress` is not an email
        address at all - it is an X.500 distinguished name that looks like
        `/O=EXCHANGELABS/OU=.../CN=RECIPIENTS/CN=a1b2c3...`. Filing on that
        string would give every internal colleague their own bogus "domain",
        so the real address has to be recovered through MAPI properties.
        """
        sender_type = str(_safe(lambda: item.SenderEmailType)).upper()
        raw = str(_safe(lambda: item.SenderEmailAddress))

        # Already a normal address - the common case for external mail.
        if sender_type == "SMTP" and "@" in raw:
            return raw.lower()

        # The message's own "sent representing" SMTP property.
        addr = _safe(
            lambda: item.PropertyAccessor.GetProperty(PR_SENT_REPRESENTING_SMTP)
        )
        if addr and "@" in str(addr):
            return str(addr).lower()

        # Ask the address book for the sender's mailbox.
        addr = _safe(lambda: item.Sender.GetExchangeUser().PrimarySmtpAddress)
        if addr and "@" in str(addr):
            return str(addr).lower()

        addr = _safe(
            lambda: item.Sender.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS)
        )
        if addr and "@" in str(addr):
            return str(addr).lower()

        # Give up gracefully: return the raw value if it is at least an
        # address, otherwise empty so callers treat the sender as unknown.
        return raw.lower() if "@" in raw else ""

    def _recipient_addresses(self, item: Any, recipient_type: int) -> list[str]:
        """SMTP addresses of the To: (1) or CC: (2) recipients."""
        out: list[str] = []
        recipients = _safe(lambda: item.Recipients, None)
        if recipients is None:
            return out
        try:
            count = int(_safe(lambda: recipients.Count, 0) or 0)
        except (TypeError, ValueError):
            return out

        for i in range(1, count + 1):
            recipient = _safe(lambda i=i: recipients.Item(i), None)
            if recipient is None:
                continue
            if int(_safe(lambda: recipient.Type, 0) or 0) != recipient_type:
                continue
            addr = _safe(
                lambda: recipient.AddressEntry.GetExchangeUser().PrimarySmtpAddress
            ) or _safe(
                lambda: recipient.AddressEntry.PropertyAccessor.GetProperty(
                    PR_SMTP_ADDRESS
                )
            ) or _safe(lambda: recipient.Address)
            if addr and "@" in str(addr):
                out.append(str(addr).lower())
        return out

    def read_item(
        self, item: Any, folder_path: str = "", with_body: bool = True
    ) -> Optional[Message]:
        """Snapshot one COM item into a `Message`, or None if unusable.

        `with_body=False` skips the message body, which is by far the most
        expensive property to read over COM. Callers that only need to know who
        sent something - a report over thousands of messages, say - are several
        times faster without it.
        """
        if int(_safe(lambda: item.Class, 0) or 0) != OL_MAIL_ITEM:
            return None  # calendar invite, delivery report, note, ...

        received = _safe(lambda: item.ReceivedTime, None)
        if received is not None:
            try:
                # pywin32 hands back a pywintypes.datetime, which is tz-aware;
                # drop the tzinfo so comparisons against naive datetimes work.
                received = datetime.fromtimestamp(received.timestamp())
            except Exception:  # noqa: BLE001
                received = None

        return Message(
            entry_id=str(_safe(lambda: item.EntryID)),
            subject=str(_safe(lambda: item.Subject)),
            body=str(_safe(lambda: item.Body)) if with_body else "",
            sender_email=self.resolve_sender_smtp(item),
            sender_name=str(_safe(lambda: item.SenderName)),
            sender_type=str(_safe(lambda: item.SenderEmailType)).upper(),
            received=received,
            to=self._recipient_addresses(item, 1),
            cc=self._recipient_addresses(item, 2),
            unread=bool(_safe(lambda: item.UnRead, False)),
            has_attachments=int(_safe(lambda: item.Attachments.Count, 0) or 0) > 0,
            importance=int(_safe(lambda: item.Importance, 1) or 1),
            folder_path=folder_path,
            conversation_id=str(_safe(lambda: item.ConversationID)),
        )

    def iter_messages(
        self,
        folder: Any,
        limit: int = 500,
        unread_only: bool = False,
        newest_first: bool = True,
        with_body: bool = True,
    ) -> Iterator[tuple[Any, Message]]:
        """Yield (com_item, snapshot) pairs from a folder.

        The COM item comes along so callers can act on it (move, categorise)
        without a second lookup by EntryID.

        A `limit` of 0 or less means "every message in the folder". A capped
        run only ever sees the newest slice, which silently leaves the rest of
        a large mailbox untouched.
        """
        items = _safe(lambda: folder.Items, None)
        if items is None:
            return

        path = self.folder_path(folder)
        try:
            items.Sort("[ReceivedTime]", newest_first)
        except Exception:  # noqa: BLE001 - some stores refuse to sort
            pass
        if unread_only:
            try:
                items = items.Restrict("[UnRead] = True")
            except Exception:  # noqa: BLE001
                pass

        count = 0
        # Index-based iteration: a `for item in items` loop misbehaves when the
        # collection changes underneath it, which is exactly what happens when
        # we move messages out of the folder we are reading.
        try:
            total = int(_safe(lambda: items.Count, 0) or 0)
        except (TypeError, ValueError):
            return

        unlimited = limit <= 0
        for i in range(1, total + 1):
            if not unlimited and count >= limit:
                return
            item = _safe(lambda i=i: items.Item(i), None)
            if item is None:
                continue
            snapshot = self.read_item(item, path, with_body=with_body)
            if snapshot is None:
                continue
            count += 1
            yield item, snapshot

    # ------------------------------------------------------------------
    # Acting on messages
    # ------------------------------------------------------------------
    def ensure_category(self, name: str) -> None:
        """Make sure a colour category exists in the master category list.

        Assigning an unknown category still works, but Outlook shows it
        without a colour, which defeats the point of colour-coding by company.
        """
        if not name:
            return
        try:
            categories = self.namespace.Categories
            for i in range(1, int(categories.Count) + 1):
                if str(categories.Item(i).Name).lower() == name.lower():
                    return
            # Deterministic colour so a company keeps the same colour forever.
            color = (abs(hash(name)) % CATEGORY_COLOR_COUNT) + 1
            categories.Add(name, color)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not register category %r: %s", name, exc)

    def add_categories(self, item: Any, names: list[str]) -> bool:
        """Add categories to an item without dropping the ones already on it."""
        names = [n.strip() for n in names if n and n.strip()]
        if not names:
            return False
        existing_raw = str(_safe(lambda: item.Categories, ""))
        existing = [c.strip() for c in existing_raw.split(",") if c.strip()]
        merged = list(existing)
        for name in names:
            self.ensure_category(name)
            if name.lower() not in {e.lower() for e in existing}:
                merged.append(name)
        if merged == existing:
            return False
        try:
            item.Categories = ", ".join(merged)
            item.Save()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not set categories on item: %s", exc)
            return False

    def move_item(self, item: Any, target_folder: Any) -> Optional[Any]:
        """Move an item, returning the relocated item, or None on failure.

        The relocated item matters: Outlook issues a *new* EntryID when a
        message changes folder, so the caller needs the new one to record that
        this message has been handled.
        """
        try:
            return item.Move(target_folder)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not move item: %s", exc)
            return None

    def mark_read(self, item: Any, read: bool = True) -> bool:
        try:
            item.UnRead = not read
            item.Save()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not change read state: %s", exc)
            return False
