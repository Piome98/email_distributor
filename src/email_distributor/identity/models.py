"""The three-level identity model: Group -> Company -> Person.

A *Group* is the coarse bucket an organisation sits in from your side of the
relationship ("고객사", "협력사", "그룹사", "사내"). A *Company* is a single
organisation, recognised by one or more email domains. A *Person* is one
individual, recognised by their email address.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Where a piece of information came from, and how much we trust it. Anything
# the user typed by hand outranks anything the learner inferred, so a nightly
# re-scan can never quietly undo a manual correction.
SOURCE_MANUAL = "manual"
SOURCE_SIGNATURE = "signature"
SOURCE_INFERRED = "inferred"

SOURCE_RANK: dict[str, int] = {
    SOURCE_INFERRED: 1,
    SOURCE_SIGNATURE: 2,
    SOURCE_MANUAL: 3,
}


def outranks(new_source: str, existing_source: str) -> bool:
    """True if `new_source` is trusted at least as much as `existing_source`."""
    return SOURCE_RANK.get(new_source, 0) >= SOURCE_RANK.get(existing_source, 0)


@dataclass
class Group:
    name: str
    description: str = ""
    id: Optional[int] = None


@dataclass
class Company:
    name: str
    group_id: Optional[int] = None
    group_name: str = ""
    is_internal: bool = False
    notes: str = ""
    source: str = SOURCE_INFERRED
    address: str = ""
    website: str = ""
    domains: list[str] = field(default_factory=list)
    id: Optional[int] = None


@dataclass
class Person:
    email: str
    display_name: str = ""
    company_id: Optional[int] = None
    department: str = ""
    title: str = ""
    phone: str = ""
    mobile: str = ""
    fax: str = ""
    address: str = ""
    source: str = SOURCE_INFERRED
    message_count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    id: Optional[int] = None


@dataclass
class Identity:
    """The resolved answer to "who sent this?", handed to the rules engine."""

    email: str
    display_name: str = ""
    domain: str = ""
    person: Optional[Person] = None
    company: Optional[Company] = None
    group_name: str = ""
    is_internal: bool = False
    is_public_domain: bool = False

    @property
    def company_name(self) -> str:
        return self.company.name if self.company else ""

    @property
    def known(self) -> bool:
        """True when we matched the sender to a company we already know."""
        return self.company is not None

    def describe(self) -> str:
        bits = [self.display_name or self.email]
        if self.person and self.person.title:
            bits.append(self.person.title)
        if self.person and self.person.department:
            bits.append(self.person.department)
        if self.company:
            bits.append(self.company.name)
        if self.group_name:
            bits.append(f"[{self.group_name}]")
        return " / ".join(b for b in bits if b)
