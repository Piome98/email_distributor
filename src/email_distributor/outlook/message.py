"""A plain-Python snapshot of an Outlook mail item.

COM objects are awkward to pass around: they are apartment-threaded, every
attribute read is a cross-process call, and touching the wrong property can
trip Outlook's security guard. So each item is read exactly once, up front,
into this dataclass. Everything downstream (rules, filing, UI, tests) works
against plain data and never needs Outlook to be running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Message:
    entry_id: str = ""
    subject: str = ""
    body: str = ""
    sender_email: str = ""
    sender_name: str = ""
    sender_type: str = ""          # "EX" for Exchange, "SMTP" otherwise
    received: Optional[datetime] = None
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    unread: bool = False
    has_attachments: bool = False
    importance: int = 1            # 0 low, 1 normal, 2 high
    folder_path: str = ""
    conversation_id: str = ""

    @property
    def sender_domain(self) -> str:
        if "@" not in self.sender_email:
            return ""
        return self.sender_email.rsplit("@", 1)[1].lower()

    @property
    def recipient_count(self) -> int:
        return len(self.to) + len(self.cc)

    def addressed_directly(self) -> bool:
        """True when the user is on the To: line rather than merely CC'd.

        The caller decides who "the user" is; this only reports whether the
        To: line is non-empty and short enough to imply a direct ask.
        """
        return bool(self.to)

    def summary(self) -> str:
        when = self.received.strftime("%Y-%m-%d %H:%M") if self.received else "?"
        return f"[{when}] {self.sender_email} - {self.subject[:60]}"
