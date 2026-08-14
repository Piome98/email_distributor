"""SQLite-backed repository for the identity database.

The database is a plain file under %LOCALAPPDATA%; nothing leaves the machine.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from ..config import PUBLIC_DOMAINS, db_path
from .models import (
    SOURCE_INFERRED,
    SOURCE_MANUAL,
    Company,
    Group,
    Identity,
    Person,
    outranks,
)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    group_id    INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    is_internal INTEGER NOT NULL DEFAULT 0,
    notes       TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'inferred',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_name
    ON companies (name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS domains (
    domain     TEXT PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    source     TEXT NOT NULL DEFAULT 'inferred'
);
CREATE INDEX IF NOT EXISTS idx_domains_company ON domains (company_id);

CREATE TABLE IF NOT EXISTS people (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL DEFAULT '',
    company_id    INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    department    TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    mobile        TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'inferred',
    message_count INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL DEFAULT '',
    last_seen     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_people_company ON people (company_id);

-- Messages we have already acted on, so a restart never double-files mail.
CREATE TABLE IF NOT EXISTS processed (
    entry_id     TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    rule_name    TEXT NOT NULL DEFAULT '',
    action       TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def split_domain(email: str) -> str:
    """Lowercased domain part of an address, or '' if it isn't one."""
    if not email or "@" not in email:
        return ""
    return email.rsplit("@", 1)[1].strip().lower()


class IdentityStore:
    """All reads and writes against the identity database."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else db_path()
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "IdentityStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------
    def upsert_group(self, name: str, description: str = "") -> int:
        name = name.strip()
        cur = self.conn.execute("SELECT id FROM groups WHERE name = ?", (name,))
        row = cur.fetchone()
        if row:
            if description:
                self.conn.execute(
                    "UPDATE groups SET description = ? WHERE id = ?",
                    (description, row["id"]),
                )
                self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO groups (name, description) VALUES (?, ?)",
            (name, description),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_groups(self) -> list[Group]:
        rows = self.conn.execute("SELECT * FROM groups ORDER BY name").fetchall()
        return [
            Group(id=r["id"], name=r["name"], description=r["description"])
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Companies and their domains
    # ------------------------------------------------------------------
    def upsert_company(
        self,
        name: str,
        *,
        group: str = "",
        is_internal: bool = False,
        notes: str = "",
        source: str = SOURCE_INFERRED,
    ) -> int:
        name = name.strip()
        if not name:
            raise ValueError("company name must not be empty")

        group_id = self.upsert_group(group) if group.strip() else None
        row = self.conn.execute(
            "SELECT * FROM companies WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()

        if row is None:
            cur = self.conn.execute(
                """INSERT INTO companies
                       (name, group_id, is_internal, notes, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, group_id, int(is_internal), notes, source, _now(), _now()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

        # Only let the new data win if its source is trusted at least as much
        # as whatever is already recorded.
        if outranks(source, row["source"]):
            self.conn.execute(
                """UPDATE companies
                      SET group_id    = COALESCE(?, group_id),
                          is_internal = ?,
                          notes       = CASE WHEN ? <> '' THEN ? ELSE notes END,
                          source      = ?,
                          updated_at  = ?
                    WHERE id = ?""",
                (group_id, int(is_internal), notes, notes, source, _now(), row["id"]),
            )
            self.conn.commit()
        return int(row["id"])

    def link_domain(
        self, domain: str, company_id: int, source: str = SOURCE_INFERRED
    ) -> None:
        """Map an email domain to a company.

        Public providers (gmail, naver, ...) are refused: they are shared by
        millions of unrelated senders, so mapping one to a company would
        mis-file every subsequent message from that provider.
        """
        domain = domain.strip().lower().lstrip("@")
        if not domain or domain in PUBLIC_DOMAINS:
            return
        row = self.conn.execute(
            "SELECT company_id, source FROM domains WHERE domain = ?", (domain,)
        ).fetchone()
        if row and not outranks(source, row["source"]):
            return
        self.conn.execute(
            """INSERT INTO domains (domain, company_id, source) VALUES (?, ?, ?)
               ON CONFLICT(domain) DO UPDATE SET company_id = excluded.company_id,
                                                 source     = excluded.source""",
            (domain, company_id, source),
        )
        self.conn.commit()

    def company_by_domain(self, domain: str) -> Optional[Company]:
        domain = domain.strip().lower()
        if not domain or domain in PUBLIC_DOMAINS:
            return None
        row = self.conn.execute(
            """SELECT c.*, g.name AS group_name
                 FROM domains d
                 JOIN companies c ON c.id = d.company_id
            LEFT JOIN groups g    ON g.id = c.group_id
                WHERE d.domain = ?""",
            (domain,),
        ).fetchone()
        return self._company_from_row(row)

    def company_by_id(self, company_id: int) -> Optional[Company]:
        row = self.conn.execute(
            """SELECT c.*, g.name AS group_name
                 FROM companies c
            LEFT JOIN groups g ON g.id = c.group_id
                WHERE c.id = ?""",
            (company_id,),
        ).fetchone()
        return self._company_from_row(row)

    def _company_from_row(self, row: Optional[sqlite3.Row]) -> Optional[Company]:
        if row is None:
            return None
        domains = [
            r["domain"]
            for r in self.conn.execute(
                "SELECT domain FROM domains WHERE company_id = ? ORDER BY domain",
                (row["id"],),
            )
        ]
        return Company(
            id=row["id"],
            name=row["name"],
            group_id=row["group_id"],
            group_name=row["group_name"] or "",
            is_internal=bool(row["is_internal"]),
            notes=row["notes"],
            source=row["source"],
            domains=domains,
        )

    def list_companies(self) -> list[Company]:
        rows = self.conn.execute(
            """SELECT c.*, g.name AS group_name
                 FROM companies c
            LEFT JOIN groups g ON g.id = c.group_id
             ORDER BY c.name COLLATE NOCASE"""
        ).fetchall()
        return [self._company_from_row(r) for r in rows]  # type: ignore[misc]

    def delete_company(self, company_id: int) -> None:
        self.conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        self.conn.commit()

    # ------------------------------------------------------------------
    # People
    # ------------------------------------------------------------------
    def upsert_person(
        self,
        email: str,
        *,
        display_name: str = "",
        company_id: Optional[int] = None,
        department: str = "",
        title: str = "",
        phone: str = "",
        mobile: str = "",
        source: str = SOURCE_INFERRED,
        seen_at: str = "",
        bump_count: bool = False,
    ) -> Optional[int]:
        email = email.strip().lower()
        if not email or "@" not in email:
            return None

        row = self.conn.execute(
            "SELECT * FROM people WHERE email = ?", (email,)
        ).fetchone()
        stamp = seen_at or _now()

        if row is None:
            cur = self.conn.execute(
                """INSERT INTO people
                       (email, display_name, company_id, department, title, phone,
                        mobile, source, message_count, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    email, display_name, company_id, department, title, phone,
                    mobile, source, 1 if bump_count else 0, stamp, stamp,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)

        # Always widen the seen-window and the counter; those are facts about
        # observation, not claims that could conflict.
        first_seen = min(filter(None, [row["first_seen"], stamp]), default=stamp)
        last_seen = max(filter(None, [row["last_seen"], stamp]), default=stamp)
        count = row["message_count"] + (1 if bump_count else 0)

        if outranks(source, row["source"]):
            # Empty incoming values must not erase what we already know.
            self.conn.execute(
                """UPDATE people
                      SET display_name  = CASE WHEN ? <> '' THEN ? ELSE display_name END,
                          company_id    = COALESCE(?, company_id),
                          department    = CASE WHEN ? <> '' THEN ? ELSE department END,
                          title         = CASE WHEN ? <> '' THEN ? ELSE title END,
                          phone         = CASE WHEN ? <> '' THEN ? ELSE phone END,
                          mobile        = CASE WHEN ? <> '' THEN ? ELSE mobile END,
                          source        = ?,
                          message_count = ?,
                          first_seen    = ?,
                          last_seen     = ?
                    WHERE id = ?""",
                (
                    display_name, display_name, company_id,
                    department, department, title, title,
                    phone, phone, mobile, mobile,
                    source, count, first_seen, last_seen, row["id"],
                ),
            )
        else:
            self.conn.execute(
                """UPDATE people
                      SET message_count = ?, first_seen = ?, last_seen = ?
                    WHERE id = ?""",
                (count, first_seen, last_seen, row["id"]),
            )
        self.conn.commit()
        return int(row["id"])

    def person_by_email(self, email: str) -> Optional[Person]:
        row = self.conn.execute(
            "SELECT * FROM people WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
        if row is None:
            return None
        return Person(
            id=row["id"],
            email=row["email"],
            display_name=row["display_name"],
            company_id=row["company_id"],
            department=row["department"],
            title=row["title"],
            phone=row["phone"],
            mobile=row["mobile"],
            source=row["source"],
            message_count=row["message_count"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
        )

    def list_people(self, company_id: Optional[int] = None) -> list[Person]:
        if company_id is None:
            rows = self.conn.execute(
                "SELECT * FROM people ORDER BY message_count DESC, email"
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM people WHERE company_id = ?
                    ORDER BY message_count DESC, email""",
                (company_id,),
            ).fetchall()
        return [self.person_by_email(r["email"]) for r in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Resolution - the question the rest of the app actually asks
    # ------------------------------------------------------------------
    def resolve(
        self,
        email: str,
        display_name: str = "",
        internal_domains: Iterable[str] = (),
    ) -> Identity:
        """Work out who a sender is, from their address alone."""
        email = (email or "").strip().lower()
        domain = split_domain(email)
        internal = {d.strip().lower().lstrip("@") for d in internal_domains if d.strip()}

        person = self.person_by_email(email)
        company: Optional[Company] = None

        # A person explicitly attached to a company wins, because that link may
        # have been set by hand or read off a signature. Only fall back to the
        # domain map when the person is unknown or unattached.
        if person and person.company_id:
            company = self.company_by_id(person.company_id)
        if company is None:
            company = self.company_by_domain(domain)

        return Identity(
            email=email,
            display_name=display_name or (person.display_name if person else ""),
            domain=domain,
            person=person,
            company=company,
            group_name=company.group_name if company else "",
            is_internal=bool(domain and domain in internal)
            or bool(company and company.is_internal),
            is_public_domain=domain in PUBLIC_DOMAINS,
        )

    # ------------------------------------------------------------------
    # Processed-message bookkeeping
    # ------------------------------------------------------------------
    def is_processed(self, entry_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM processed WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            is not None
        )

    def mark_processed(self, entry_id: str, rule_name: str, action: str) -> None:
        self.conn.execute(
            """INSERT INTO processed (entry_id, processed_at, rule_name, action)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(entry_id) DO UPDATE SET processed_at = excluded.processed_at,
                                                   rule_name    = excluded.rule_name,
                                                   action       = excluded.action""",
            (entry_id, _now(), rule_name, action),
        )
        self.conn.commit()

    def stats(self) -> dict[str, int]:
        def count(table: str) -> int:
            return int(
                self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            )

        return {
            "groups": count("groups"),
            "companies": count("companies"),
            "domains": count("domains"),
            "people": count("people"),
            "processed": count("processed"),
        }
