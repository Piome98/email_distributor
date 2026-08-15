"""Application paths and persisted settings.

Everything lives under %LOCALAPPDATA% so the app never needs write access to
Program Files or the registry, and therefore never needs administrator rights.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

APP_NAME = "EmailDistributor"


def data_dir() -> Path:
    """Per-user writable directory for the database, settings and logs."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "identity.db"


def rules_path() -> Path:
    return data_dir() / "rules.json"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def log_path() -> Path:
    return data_dir() / "distributor.log"


@dataclass
class Settings:
    """User-tunable knobs, persisted as JSON next to the database."""

    # Folder to watch, as a path relative to the account root, e.g. "Inbox".
    watch_folder: str = "Inbox"

    # Root folder under which company folders are created. Empty string means
    # "create them directly under the account root" rather than inside Inbox.
    filing_root: str = "Inbox"

    # Seconds between polls of the watched folder.
    poll_interval: int = 60

    # How many messages a manual run examines. 0 means every message.
    #
    # Defaults to unlimited because a cap makes two very different situations
    # look identical: "the rules file nothing" and "nothing in the newest 200
    # happened to be eligible".
    run_limit: int = 0

    # How many the background watcher looks at per poll. This one stays capped:
    # it runs every minute and only needs to catch mail that has just arrived.
    poll_limit: int = 200

    # When True, actions are logged but never applied to the mailbox. This is
    # the default so a first run can never surprise anyone by moving mail.
    dry_run: bool = True

    # Apply an Outlook colour category naming the company/group.
    apply_categories: bool = True

    # Move messages into per-company folders.
    move_to_folders: bool = True

    # Re-examine mail the app has already handled.
    #
    # Off by default, because the normal job is new mail. Turn it on after
    # changing rules or confirming a company, when mail that was already seen
    # should be re-filed. It applies to a manual run only - the background
    # watcher always leaves handled mail alone, since re-moving a message into
    # the folder it already sits in just makes Outlook refuse.
    reprocess_handled: bool = False

    # Domains treated as "us" - mail from these is internal, never filed as a
    # customer/vendor. Populated on first run from the user's own address.
    internal_domains: list[str] = field(default_factory=list)

    # How many messages the learner reads per folder when building the DB.
    learn_limit: int = 2000

    def save(self) -> None:
        settings_path().write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def load(cls) -> "Settings":
        path = settings_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        settings = cls(**{k: v for k, v in raw.items() if k in known})

        # A public provider can never be an "internal" domain: it would make
        # every Gmail or Naver sender on earth a colleague. Saved settings are
        # filtered as well as new ones, so a value stored before this check
        # existed is corrected rather than honoured forever.
        settings.internal_domains = [
            d for d in settings.internal_domains
            if d.strip().lower().lstrip("@") not in PUBLIC_DOMAINS
        ]
        return settings


# Free / public mail providers. A sender at one of these tells us nothing about
# which company they belong to, so domain-based company inference must skip them
# and fall back to signature parsing or a manual mapping.
PUBLIC_DOMAINS: frozenset[str] = frozenset(
    {
        # Korean
        "naver.com", "hanmail.net", "daum.net", "nate.com", "kakao.com",
        "korea.com", "empas.com", "paran.com", "dreamwiz.com", "chol.com",
        "hanmir.com", "netsgo.com", "lycos.co.kr", "freechal.com",
        # Global
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
        "live.com", "msn.com", "yahoo.com", "yahoo.co.jp", "ymail.com",
        "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
        "proton.me", "gmx.com", "mail.com", "zoho.com", "yandex.com",
        "qq.com", "163.com", "126.com", "sina.com", "foxmail.com",
    }
)
