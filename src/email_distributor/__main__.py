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
from .actions.folders import FolderBuilder
from .config import PUBLIC_DOMAINS, Settings, data_dir, log_path, rules_path
from .identity.learner import Learner
from .identity.models import SOURCE_MANUAL
from .identity.store import IdentityStore
from .outlook.client import COM_AVAILABLE, OutlookClient, OutlookError
from .rules.engine import RuleSet
from .service.watcher import Watcher


def configure_console() -> None:
    """Make the console able to print Korean, however we were launched.

    The batch files set PYTHONIOENCODING and run `chcp 65001`, but a
    double-clicked .exe inherits neither: it lands in whatever codepage the
    console happens to use (949 on a Korean Windows), and every 회사 name comes
    out as mojibake. Fixing it here means the program is correct on its own
    rather than only when launched through a wrapper.
    """
    try:
        import ctypes

        # 65001 is UTF-8. Only affects this console, not the system.
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001 - no console (pythonw), or not Windows
        pass

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            # errors="replace" so an unprintable character degrades to "?"
            # instead of killing the run with a UnicodeEncodeError.
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


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

    def progress(folder: str, done: int, _total: int) -> None:
        print(f"  {folder}: {done} messages read", end="\r", flush=True)

    with OutlookClient() as client, IdentityStore() as store:
        if not settings.internal_domains:
            address = client.current_user_address()
            domain = address.rsplit("@", 1)[1].lower() if "@" in address else ""
            if domain and domain in PUBLIC_DOMAINS:
                # Your own address is at a public provider, so its domain says
                # nothing about who your colleagues are. Treating gmail.com as
                # "internal" would mark every Gmail sender on earth a colleague.
                print(
                    f"Your address is at {domain}, a public provider, so no internal\n"
                    "domain was set. On a work PC this is detected from your company\n"
                    "address; set it by hand on the Settings tab if you need it."
                )
            elif domain:
                settings.internal_domains = [domain]
                settings.save()
                print(f"Detected internal domain: {domain}")

        # Applied after any save, so a one-off --limit stays one-off. Setting
        # it before would persist a throwaway value into settings.json and
        # quietly cap every later run.
        if args.limit:
            settings.learn_limit = args.limit

        stats = Learner(client, store, settings).learn_all(progress=progress)
        print("\n" + stats.describe())
        print(f"database now holds: {store.stats()}")

        if stats.unresolved_senders:
            print(
                "\nSome senders could not be resolved to an email address. On an "
                "Exchange profile this usually means the address-book lookup is "
                "failing.\nCheck the People tab: addresses should look like "
                "name@company.com, never /O=EXCHANGELABS/...\n"
                "Messages whose sender cannot be resolved are skipped, never filed."
            )
        elif stats.exchange_senders:
            print(
                f"\nAll {stats.exchange_senders} Exchange sender(s) resolved to real "
                "SMTP addresses."
            )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.load()
    settings.dry_run = not args.live

    with OutlookClient() as client, IdentityStore() as store:
        distributor = Distributor(client, store, RuleSet.load(), settings)
        mode = "LIVE - mail will be moved" if args.live else "DRY RUN - nothing will change"
        target = args.folder or settings.watch_folder
        scope = " and its subfolders" if args.recurse else ""
        print(f"Filing '{target}'{scope} ({mode})\n")

        report = lambda r: print(r.describe()) if (  # noqa: E731
            (r.decision.has_effect and not r.skipped_reason) or r.error
        ) else None

        if args.folder or args.recurse:
            folder = client.get_folder(target)
            if folder is None:
                print(f"Folder not found: {target!r}")
                return 4
            if args.recurse:
                summary = distributor.process_tree(
                    folder, limit=args.limit, reprocess=args.reprocess,
                    on_result=report,
                )
            else:
                summary = distributor.process_folder(
                    folder, limit=args.limit, reprocess=args.reprocess,
                    on_result=report,
                )
        else:
            summary = distributor.process_watch_folder(
                limit=args.limit, on_result=report
            )
        print("\n" + summary.describe())
        if not args.live and summary.planned:
            print("\nRe-run with --live to apply these changes.")
    return 0


def cmd_folders(args: argparse.Namespace) -> int:
    settings = Settings.load()

    with OutlookClient() as client, IdentityStore() as store:
        builder = FolderBuilder(client, store, RuleSet.load(), settings)
        report = builder.build(
            min_messages=args.min_messages,
            include_people=not args.companies_only,
            dry_run=not args.live,
        )

        print(f"Store : {report.store_name}  ({report.store_kind})")
        if not report.store_syncs:
            print(
                "\nWARNING: this is a local .pst. Folders created here exist only\n"
                "         on this PC - they will not appear on the web client or\n"
                "         on your phone, and are lost if the machine is rebuilt.\n"
            )
        print(f"Rules : {rules_path()}\n")

        for plan in report.plans:
            print(f"  {plan.describe()}")

        print("\n" + report.describe())
        if not args.live and report.missing:
            print("\nRe-run with --live to create these folders in Outlook.")
    return 0


def cmd_group(args: argparse.Namespace) -> int:
    """Confirm companies as 거래처 in bulk.

    Only grouped companies are filed, and a real mailbox produces a hundred or
    more companies, so there has to be a way to confirm them without opening a
    dialog for each one.
    """
    with IdentityStore() as store:
        companies = store.list_companies()
        chosen = [
            c
            for c in companies
            if len(store.list_people(c.id)) >= args.min_contacts
            and sum(p.message_count for p in store.list_people(c.id)) >= args.min_messages
            and (not args.match or args.match.lower() in c.name.lower())
        ]

        if not chosen:
            print("No company matched. Loosen --min-messages or --match.")
            return 0

        print(f"{'Would assign' if not args.live else 'Assigning'} group "
              f"{args.name!r} to {len(chosen)} company(ies):\n")
        for c in chosen:
            total = sum(p.message_count for p in store.list_people(c.id))
            print(f"   {c.name[:40]:42} {total:5} messages   {c.group_name or '-'}")
            if args.live:
                store.upsert_company(c.name, group=args.name, source=SOURCE_MANUAL)

        if args.live:
            print(f"\nDone. {len(chosen)} company(ies) are now filed as {args.name!r}.")
        else:
            print("\nRe-run with --live to apply.")
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

    group = sub.add_parser(
        "group", help="confirm companies as 거래처 in bulk (only these get filed)"
    )
    group.add_argument("name", help="group to assign, e.g. 고객사")
    group.add_argument(
        "--min-messages", type=int, default=1, help="only companies with at least this much traffic"
    )
    group.add_argument(
        "--min-contacts", type=int, default=1, help="only companies with at least this many contacts"
    )
    group.add_argument("--match", default="", help="only companies whose name contains this")
    group.add_argument("--live", action="store_true", help="apply (default: preview)")
    group.set_defaults(func=cmd_group)

    folders = sub.add_parser(
        "folders", help="create the 업체/담당자 folder tree in Outlook"
    )
    folders.add_argument(
        "--live", action="store_true", help="actually create them (default: preview)"
    )
    folders.add_argument(
        "--min-messages",
        type=int,
        default=1,
        help="only make a 담당자 folder for contacts with at least this many messages",
    )
    folders.add_argument(
        "--companies-only",
        action="store_true",
        help="stop at the 업체 level, no per-contact subfolders",
    )
    folders.set_defaults(func=cmd_folders)

    run = sub.add_parser("run", help="one filing pass over the watch folder")
    run.add_argument(
        "--live", action="store_true", help="actually apply changes (default: dry run)"
    )
    run.add_argument("--limit", type=int, default=200, help="max messages to examine")
    run.add_argument(
        "--folder", default="", help="file this folder instead of the watch folder"
    )
    run.add_argument(
        "--recurse", action="store_true", help="include every subfolder as well"
    )
    run.add_argument(
        "--reprocess",
        action="store_true",
        help="re-file messages already recorded as done (use when rules changed)",
    )
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console()
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
