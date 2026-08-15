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


# Mailbox names that belong to a system or a department rather than a person.
# Bulk mail from these carries no personal signature, so recording a name,
# rank or department against them produces noise that pollutes the People view.
ROLE_ACCOUNT_PREFIXES = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "notification", "notifications", "alert", "alerts", "mailer", "mail",
    "newsletter", "news", "marketing", "promo", "info", "support", "help",
    "helpdesk", "service", "admin", "webmaster", "postmaster", "billing",
    "invoice", "accounts", "sales", "contact", "hello", "team", "auto",
    "system", "bounce", "notice", "official", "cs", "customer",
)


def is_role_account(email: str) -> bool:
    """True when an address is a system/role mailbox rather than a person."""
    local = (email or "").split("@", 1)[0].strip().lower()
    if not local:
        return False
    return any(
        local == prefix or local.startswith(prefix + ".") or local.startswith(prefix + "-")
        or local.startswith(prefix + "_") or local.startswith(prefix)
        for prefix in ROLE_ACCOUNT_PREFIXES
    )


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
    unresolved_senders: int = 0
    exchange_senders: int = 0
    errors: int = 0
    folders: list[str] = field(default_factory=list)

    def describe(self) -> str:
        text = (
            f"{self.mail_read} messages read from {len(self.folders)} folder(s); "
            f"{self.people_seen} people, {self.companies_created} new companies, "
            f"{self.signatures_parsed} signatures parsed"
        )
        if self.exchange_senders:
            text += f"; {self.exchange_senders} Exchange sender(s) resolved"
        if self.unresolved_senders:
            text += (
                f"; WARNING {self.unresolved_senders} sender(s) could not be "
                "resolved to an email address"
            )
        return text


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
            # Still record contact details we did not have before, without
            # touching the name.
            if sig and (sig.address or sig.website):
                self.store.upsert_company(
                    existing.name,
                    address=sig.address,
                    website=sig.website,
                    source=existing.source,
                )
            return existing.id

        if sig and sig.company:
            name, source = sig.company, SOURCE_SIGNATURE
        else:
            name, source = company_name_from_domain(domain), SOURCE_INFERRED
        if not name:
            return None

        # The office address in a footer describes the organisation, so it is
        # recorded against the company rather than against one employee.
        company_id = self.store.upsert_company(
            name,
            address=(sig.address if sig else ""),
            website=(sig.website if sig else ""),
            source=source,
        )
        if name.lower() not in self._known_companies:
            self._known_companies.add(name.lower())
        self.store.link_domain(domain, company_id, source)
        return company_id

    def learn_message(self, message: Message, stats: LearnStats) -> None:
        """Extract every identity fact one message has to offer."""
        stats.scanned += 1
        email = (message.sender_email or "").strip().lower()

        # Track how often the Exchange X.500 path had to run and whether it
        # worked. On a corporate profile this is the number to watch: a high
        # unresolved count means the address book lookup is failing and no
        # internal colleague will be identified correctly.
        if message.sender_type == "EX":
            stats.exchange_senders += 1
        if not email or "@" not in email:
            stats.unresolved_senders += 1
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

        # A role mailbox still identifies its organisation, so the company name
        # is kept; only the person-level fields are discarded.
        role_account = is_role_account(email)

        # Only a genuine personal sign-off may name the sender's company.
        #
        # Newsletters quote other organisations constantly - a recruitment
        # mailshot from saramin.co.kr advertises jobs at ㈜카카오페이 - and
        # trusting those turned the sender's employer into whichever company
        # the marketing copy happened to mention. Bulk mail therefore keeps
        # the name derived from its own domain, which is always about right.
        company_sig = usable if (usable and usable.personal and not role_account) else None

        if domain in PUBLIC_DOMAINS:
            stats.skipped_public += 1

        before = self.store.stats()["companies"]
        company_id = self._company_for(domain, company_sig)
        if self.store.stats()["companies"] > before:
            stats.companies_created += 1

        # An office address in a footer describes the sending organisation even
        # when the mail is a newsletter, so it is recorded whatever we decided
        # about the name.
        if company_id and usable and (usable.address or usable.website):
            company = self.store.company_by_id(company_id)
            if company is not None:
                self.store.upsert_company(
                    company.name,
                    address=usable.address,
                    website=usable.website,
                    source=company.source,
                )

        stamp = message.received.isoformat(timespec="seconds") if message.received else ""
        seen_before = self.store.person_by_email(email) is not None

        # Person-level details need the same evidence: without a real sign-off
        # a "department" is just a noun lifted out of marketing copy.
        person_fields = (
            usable if (usable and usable.personal and not role_account) else None
        )
        self.store.upsert_person(
            email,
            display_name=(
                person_fields.name
                if person_fields and person_fields.name
                else message.sender_name
            ),
            company_id=company_id,
            department=(person_fields.department if person_fields else ""),
            title=(person_fields.title if person_fields else ""),
            phone=(person_fields.phone if person_fields else ""),
            mobile=(person_fields.mobile if person_fields else ""),
            fax=(person_fields.fax if person_fields else ""),
            address=(person_fields.address if person_fields else ""),
            source=(
                SOURCE_SIGNATURE
                if person_fields and not person_fields.is_empty()
                else SOURCE_INFERRED
            ),
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
