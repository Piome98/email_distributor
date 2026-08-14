"""Command-line entry point.

    python -m email_distributor              # open the desktop UI
    python -m email_distributor learn        # build the identity DB from mail
    python -m email_distributor run          # one filing pass (dry run)
    python -m email_distributor run --live   # ... and actually apply it
    python -m email_distributor watch        # poll until interrupted
    python -m email_distributor status       # what the database knows
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .actions.filing import Distributor
from .config import Settings, data_dir, log_path
from .identity.learner import Learner
from .identity.store import IdentityStore
from .outlook.client import COM_AVAILABLE, OutlookClient, OutlookError
from .rules.engine import RuleSet
from .service.watcher import Watcher


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path(), encoding="utf-8"),
        ],
    )


def cmd_status(_args: argparse.Namespace) -> int:
    settings = Settings.load()
    with IdentityStore() as store:
        stats = store.stats()
        print(f"Data folder     : {data_dir()}")
        print(f"Watch folder    : {settings.watch_folder}")
        print(f"Dry run         : {settings.dry_run}")
        print(f"Internal domains: {', '.join(settings.internal_domains) or '(none)'}")
        print()
        print(f"Groups          : {stats['groups']}")
        print(f"Companies       : {stats['companies']}")
        print(f"Domains mapped  : {stats['domains']}")
        print(f"People          : {stats['people']}")
        print(f"Messages filed  : {stats['processed']}")

        companies = store.list_companies()
        if companies:
            print("\nTop companies:")
            ranked = sorted(
                companies,
                key=lambda c: len(store.list_people(c.id)),
                reverse=True,
            )
            for company in ranked[:15]:
                people = len(store.list_people(company.id))
                group = f" [{company.group_name}]" if company.group_name else ""
                print(f"  {company.name}{group} - {people} people, "
                      f"{', '.join(company.domains) or 'no domain'}")
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    settings = Settings.load()
    if args.limit:
        settings.learn_limit = args.limit

    def progress(folder: str, done: int, _total: int) -> None:
        print(f"  {folder}: {done} messages read", end="\r", flush=True)

    with OutlookClient() as client, IdentityStore() as store:
        if not settings.internal_domains:
            address = client.current_user_address()
            if "@" in address:
                settings.internal_domains = [address.rsplit("@", 1)[1]]
                settings.save()
                print(f"Detected internal domain: {settings.internal_domains[0]}")

        stats = Learner(client, store, settings).learn_all(progress=progress)
        print("\n" + stats.describe())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.load()
    settings.dry_run = not args.live

    with OutlookClient() as client, IdentityStore() as store:
        distributor = Distributor(client, store, RuleSet.load(), settings)
        mode = "LIVE - mail will be moved" if args.live else "DRY RUN - nothing will change"
        print(f"Filing '{settings.watch_folder}' ({mode})\n")

        summary = distributor.process_watch_folder(
            limit=args.limit,
            on_result=lambda r: print(r.describe())
            if (r.decision.has_effect and not r.skipped_reason) or r.error
            else None,
        )
        print("\n" + summary.describe())
        if not args.live and summary.planned:
            print("\nRe-run with --live to apply these changes.")
    return 0


def cmd_watch(_args: argparse.Namespace) -> int:
    settings = Settings.load()
    watcher = Watcher(settings, on_event=lambda level, text: print(f"[{level}] {text}"))
    watcher.start()
    print("Watching. Press Ctrl+C to stop.")
    try:
        while watcher.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
        watcher.stop()
    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    from .ui.app import main as gui_main

    gui_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="email_distributor",
        description="Automatically file Outlook mail by who sent it.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("gui", help="open the desktop UI (default)").set_defaults(func=cmd_gui)
    sub.add_parser("status", help="show what the database knows").set_defaults(
        func=cmd_status
    )
    sub.add_parser("watch", help="poll the watch folder until interrupted").set_defaults(
        func=cmd_watch
    )

    learn = sub.add_parser("learn", help="build the identity DB from your mailbox")
    learn.add_argument("--limit", type=int, default=0, help="max messages per folder")
    learn.set_defaults(func=cmd_learn)

    run = sub.add_parser("run", help="one filing pass over the watch folder")
    run.add_argument(
        "--live", action="store_true", help="actually apply changes (default: dry run)"
    )
    run.add_argument("--limit", type=int, default=200, help="max messages to examine")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if not COM_AVAILABLE:
        print("pywin32 is not installed. Run:  pip install --user pywin32")
        return 2

    func = getattr(args, "func", cmd_gui)
    try:
        return int(func(args))
    except OutlookError as exc:
        print(f"\nOutlook error: {exc}")
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
