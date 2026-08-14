"""Builds the identity database out of mail you already have.

The premise of the whole tool is that your mailbox already contains the answer
to "who is this person and which company do they belong to" - it is just
scattered across thousands of signature blocks. The learner reads that history
once and turns it into a queryable database.

Nothing here contacts the network. Every fact comes from your own mailbox.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import PUBLIC_DOMAINS, Settings
from ..outlook.client import OutlookClient
from ..outlook.message import Message
from . import signature
from .models import SOURCE_INFERRED, SOURCE_SIGNATURE
from .store import IdentityStore, split_domain

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, int], None]

# Second-level suffixes that carry no company information. Needed so that
# "hanguk.co.kr" yields "Hanguk" rather than "Co".
MULTI_LEVEL_SUFFIXES = {
    "co.kr", "or.kr", "ne.kr", "re.kr", "go.kr", "ac.kr", "pe.kr", "hs.kr",
    "ms.kr", "es.kr", "sc.kr", "kg.kr",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk",
    "com.cn", "net.cn", "org.cn", "gov.cn",
    "com.au", "net.au", "org.au", "edu.au",
    "com.tw", "com.hk", "com.sg", "com.br", "com.mx", "co.in", "co.id",
    "co.th", "com.vn", "com.my", "co.nz", "co.za",
}


def company_name_from_domain(domain: str) -> str:
    """Derive a first-guess company name from an email domain.

    "hanguk-elec.co.kr" -> "Hanguk Elec". It is only a placeholder: the user
    renames it in the UI, and a signature block will override it if one is
    found. Returns "" for public providers, which identify no company at all.
    """
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain or domain in PUBLIC_DOMAINS or "." not in domain:
        return ""

    parts = domain.split(".")
    label = parts[0]
    if len(parts) >= 3 and ".".join(parts[-2:]) in MULTI_LEVEL_SUFFIXES:
        label = parts[-3]
    elif len(parts) == 2:
        label = parts[0]
    else:
        # e.g. mail.hanguk.com -> hanguk
        label = parts[-2]

    words = [w for w in label.replace("_", "-").split("-") if w]
    return " ".join(w.capitalize() for w in words) or domain


@dataclass
class LearnStats:
    scanned: int = 0
    mail_read: int = 0
    people_seen: int = 0
    companies_created: int = 0
    signatures_parsed: int = 0
    skipped_public: int = 0
    errors: int = 0
    folders: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"{self.mail_read} messages read from {len(self.folders)} folder(s); "
            f"{self.people_seen} people, {self.companies_created} new companies, "
            f"{self.signatures_parsed} signatures parsed"
        )


class Learner:
    def __init__(
        self,
        client: OutlookClient,
        store: IdentityStore,
        settings: Optional[Settings] = None,
    ) -> None:
        self.client = client
        self.store = store
        self.settings = settings or Settings()
        self._known_companies: set[str] = set()

    # ------------------------------------------------------------------
    def _company_for(
        self, domain: str, sig: Optional[signature.SignatureInfo]
    ) -> Optional[int]:
        """Find or create the company that owns `domain`.

        A signature-supplied company name is preferred because it is the real
        trading name; the domain-derived guess is only a fallback.
        """
        if not domain or domain in PUBLIC_DOMAINS:
            return None

        existing = self.store.company_by_domain(domain)
        if existing is not None:
            # Upgrade a domain-derived placeholder once a signature reveals the
            # organisation's actual name.
            if (
                sig
                and sig.company
                and existing.source == SOURCE_INFERRED
                and existing.name != sig.company
            ):
                company_id = self.store.upsert_company(
                    sig.company, source=SOURCE_SIGNATURE
                )
                self.store.link_domain(domain, company_id, SOURCE_SIGNATURE)
                return company_id
            return existing.id

        if sig and sig.company:
            name, source = sig.company, SOURCE_SIGNATURE
        else:
            name, source = company_name_from_domain(domain), SOURCE_INFERRED
        if not name:
            return None

        company_id = self.store.upsert_company(name, source=source)
        if name.lower() not in self._known_companies:
            self._known_companies.add(name.lower())
        self.store.link_domain(domain, company_id, source)
        return company_id

    def learn_message(self, message: Message, stats: LearnStats) -> None:
        """Extract every identity fact one message has to offer."""
        stats.scanned += 1
        email = (message.sender_email or "").strip().lower()
        if not email or "@" not in email:
            return

        domain = split_domain(email)
        sig = signature.parse(message.body, message.sender_name)
        if not sig.is_empty():
            stats.signatures_parsed += 1

        # Only trust a signature when it plausibly belongs to the sender. On a
        # forwarded chain the tail of the body may hold somebody else's block;
        # if it carries an address and that address is not the sender's, the
        # block is not theirs and must not be attributed to them.
        sig_is_senders = not sig.email or sig.email == email
        usable = sig if sig_is_senders else None

        if domain in PUBLIC_DOMAINS:
            stats.skipped_public += 1

        before = self.store.stats()["companies"]
        company_id = self._company_for(domain, usable)
        if self.store.stats()["companies"] > before:
            stats.companies_created += 1

        stamp = message.received.isoformat(timespec="seconds") if message.received else ""
        seen_before = self.store.person_by_email(email) is not None

        self.store.upsert_person(
            email,
            display_name=(usable.name if usable and usable.name else message.sender_name),
            company_id=company_id,
            department=(usable.department if usable else ""),
            title=(usable.title if usable else ""),
            phone=(usable.phone if usable else ""),
            mobile=(usable.mobile if usable else ""),
            source=SOURCE_SIGNATURE if usable and not usable.is_empty() else SOURCE_INFERRED,
            seen_at=stamp,
            bump_count=True,
        )
        if not seen_before:
            stats.people_seen += 1

    def learn_correspondents(self, message: Message, stats: LearnStats) -> None:
        """Record the people a sent message was addressed to.

        Sent mail is the strongest signal of who actually matters to you: you
        chose to write to them.
        """
        for email in list(message.to) + list(message.cc):
            email = email.strip().lower()
            if not email or "@" not in email:
                continue
            domain = split_domain(email)
            company_id = self._company_for(domain, None)
            seen_before = self.store.person_by_email(email) is not None
            self.store.upsert_person(
                email,
                company_id=company_id,
                source=SOURCE_INFERRED,
                seen_at=(
                    message.received.isoformat(timespec="seconds")
                    if message.received
                    else ""
                ),
                bump_count=True,
            )
            if not seen_before:
                stats.people_seen += 1

    # ------------------------------------------------------------------
    def learn_folder(
        self,
        folder: Any,
        limit: int,
        stats: LearnStats,
        from_recipients: bool = False,
        progress: Optional[ProgressFn] = None,
    ) -> LearnStats:
        name = str(getattr(folder, "Name", "?"))
        stats.folders.append(name)

        for index, (_item, message) in enumerate(
            self.client.iter_messages(folder, limit=limit), start=1
        ):
            try:
                if from_recipients:
                    self.learn_correspondents(message, stats)
                else:
                    self.learn_message(message, stats)
                stats.mail_read += 1
            except Exception as exc:  # noqa: BLE001 - one bad item must not stop the scan
                stats.errors += 1
                log.debug("learn failed on an item in %s: %s", name, exc)
            if progress and index % 25 == 0:
                progress(name, index, limit)

        if progress:
            progress(name, stats.mail_read, limit)
        return stats

    def learn_all(self, progress: Optional[ProgressFn] = None) -> LearnStats:
        """Scan Inbox (senders) and Sent Items (recipients)."""
        stats = LearnStats()
        limit = self.settings.learn_limit

        inbox = self.client.inbox()
        self.learn_folder(inbox, limit, stats, from_recipients=False, progress=progress)

        # Subfolders of the Inbox usually hold already-filed correspondence,
        # which is exactly the labelled history we want to learn from.
        for sub in getattr(inbox, "Folders", []):
            self.learn_folder(
                sub, max(limit // 4, 100), stats, from_recipients=False, progress=progress
            )

        try:
            self.learn_folder(
                self.client.sent_folder(),
                limit,
                stats,
                from_recipients=True,
                progress=progress,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("could not scan Sent Items: %s", exc)

        return stats
