# email_distributor

Automatically files Outlook mail by **who sent it** — the individual, the company
they belong to, and the group that company sits in.

It reads the mail you already have, learns which people belong to which
organisations, and then keeps your inbox sorted by applying colour categories
and moving messages into per-company folders.

---

## Why it works on a locked-down laptop

This drives the **classic Outlook desktop client over COM**, using the session
Outlook has already signed in to.

| | |
|---|---|
| Azure app registration | not needed |
| OAuth consent / IT ticket | not needed |
| Administrator rights | not needed |
| Passwords handled by the app | none, ever |
| Network calls | none — all data stays on your PC |
| Dependencies | `pywin32` only |

Everything else it uses (SQLite, Tkinter, JSON) ships inside Python.

---

## Install

Double-click the batch files, in this order:

| File | What it does |
|---|---|
| **`setup.bat`** | Installs `pywin32` (user-level, no admin) and checks Outlook is reachable |
| **`learn.bat`** | Reads your mailbox and builds the database. **Read-only.** |
| **`start.bat`** | Opens the desktop app |
| **`preview.bat`** | Dry run — shows what would be filed, changes nothing |

That is the whole installation: Python plus one package.

> The `.bat` files are deliberately **ASCII-only**. `cmd.exe` parses a batch
> file using the console codepage, so a multi-byte character shifts the parser
> and splits later tokens — `echo` becomes `ho`. Korean text belongs in the
> Python layer, which handles UTF-8 correctly.

### Without the batch files

```bash
python cli.py status
```

`cli.py`, `run_tests.py` and `run.pyw` each put `src/` on the path themselves,
so nothing needs installing and `PYTHONPATH` never has to be set.

> `python -m email_distributor` works **only** after `pip install --user -e .`
> or with `PYTHONPATH=src`. Use `python cli.py` instead — it works from a
> freshly opened terminal in a copied folder, which is the point of the
> no-install design.

### A standalone .exe

If the target PC has no Python and you cannot install it, run **`build_exe.bat`**
on a machine where you can, then copy the resulting `dist\EmailDistributor`
folder (~30 MB) across. It bundles its own Python — the target needs nothing.

```
EmailDistributor.exe            double-click, opens the app
EmailDistributor.exe status     what the database knows
EmailDistributor.exe learn      build the database (read-only)
EmailDistributor.exe run        dry run, changes nothing
```

Two deliberate build choices:

- **A folder, not a single file.** `--onefile` unpacks itself into `%TEMP%` at
  every launch, and managed PCs commonly forbid executing anything from there.
- **A console build, not a windowed one.** With no arguments it still opens the
  GUI, so double-clicking works — but a console build can be driven from a
  command line and, more importantly, *shows its errors*. A windowed build
  swallows them, which is the last thing you want on a locked-down PC.

Be aware the .exe is **unsigned**. SmartScreen warns on first run
("자세히" → "실행"), and some corporate antivirus quarantines PyInstaller
output as a false positive. **Where Python can be installed, the `.bat` route
is the more reliable one.**

---

## How to use it

### 1. Learn from your mailbox

Open the **실행 / Run** tab and press **사서함 학습 시작 (Learn)**.

This reads your Inbox, its subfolders, and Sent Items. For every message it
resolves the sender's real SMTP address, parses the signature block for name,
rank, department and company, and maps each email domain to an organisation.
**It only reads — no message is modified.**

### 2. Correct what it guessed

On the **회사 / Companies** tab you'll see what it found. A company inferred
from a domain gets a placeholder name (`hanguk.co.kr` → "Hanguk"); double-click
to rename it to the real trading name and assign it to a group such as
`고객사`, `협력사` or `그룹사`.

Your edits are stored as `manual` and **a later re-scan can never overwrite
them.**

### 3. Preview, then apply

Press **한 번 실행 (Run once)** with **미리보기 (dry run)** left on. Nothing is
touched; the log shows exactly what *would* happen to each message:

```
[2026-03-15 09:30] hong@hanguk.co.kr - 견적서 송부의 건
        who: 홍길동 / 부장 / 영업1팀 / 한국전자 [고객사]
        do : 그룹별 분류: move -> Inbox/거래처/고객사/한국전자; categories: 고객사, 한국전자
```

Once the plan looks right, turn dry run off and run again. Then
**자동 감시 시작 (Start watching)** keeps it running on a timer.

---

## Rules

Rules live in `%LOCALAPPDATA%\EmailDistributor\rules.json` and are editable
from the **규칙 / Rules** tab. They are evaluated top to bottom; the first match
wins unless a rule sets `"stop_on_match": false`.

```json
{
  "name": "고객사 발주 메일",
  "match": {
    "group": ["고객사"],
    "subject_contains": ["발주", "PO"]
  },
  "actions": {
    "move_to": "Inbox/거래처/{group}/{company}/{yyyymm}",
    "categories": ["{group}", "{company}"]
  }
}
```

**Placeholders** — `{group}` `{company}` `{person}` `{department}` `{title}`
`{domain}` `{email}` `{year}` `{month}` `{yyyymm}`

**Conditions** — `group` `company` `sender_domain` `sender_email` `department`
`subject_contains` `body_contains` `is_internal` `is_unknown`
`is_public_domain` `has_attachments` `importance_min`

**Actions** — `move_to` `categories` `mark_read`

The shipped default: tag internal mail, file known companies under
`Inbox/거래처/{group}/{company}`, and set unrecognised senders aside in
`Inbox/거래처/_미분류` for review.

---

## Command line

Run these from the project folder:

```bash
python cli.py status      # what the database knows
```

```bash
python cli.py learn       # build the identity DB from your mailbox
```

```bash
python cli.py run         # preview one pass, changing nothing
```

```bash
python cli.py run --live  # actually apply it
```

```bash
python cli.py watch       # poll until Ctrl+C
```

After `pip install --user -e .` the same commands are available as
`email-distributor status`, and `python -m email_distributor` starts working.

---

## Design notes

**Exchange senders don't have email addresses.** Inside an Exchange org,
`SenderEmailAddress` returns an X.500 distinguished name
(`/O=EXCHANGELABS/OU=.../CN=RECIPIENTS/CN=a1b2c3`), not an address. Filing on
that would give every colleague their own bogus domain, so the real address is
recovered through MAPI properties (`PR_SMTP_ADDRESS`,
`PR_SENT_REPRESENTING_SMTP_ADDRESS`) with fallbacks — see
`outlook/client.py:resolve_sender_smtp`.

**Public providers identify nobody.** A sender at `gmail.com` or `naver.com`
tells you nothing about their employer, so those domains are refused as
company mappings. Such a person can still be attached to a company by hand, and
that link takes priority over the domain map.

**Source precedence.** Every fact is tagged `manual` > `signature` > `inferred`.
Lower-ranked data never overwrites higher-ranked data, and blank values never
erase what's already known — so re-learning is always safe.

**Dry run by default.** Nothing touches the mailbox until you explicitly turn it
off and confirm.

**Nothing is deleted.** The app only moves, categorises, and marks read.

---

## Data

Everything lives in `%LOCALAPPDATA%\EmailDistributor\`:

| File | Contents |
|---|---|
| `identity.db` | groups, companies, domains, people, processed-message ledger |
| `rules.json` | your filing rules |
| `settings.json` | watch folder, interval, internal domains |
| `distributor.log` | activity log |

The database holds real colleague and customer names, addresses and phone
numbers read from your mailbox. It is **git-ignored** and never leaves the PC.

---

## Tests

```bash
python run_tests.py
```

145 tests covering signature parsing, identity resolution, source precedence,
schema migration, rule matching, template expansion, folder-name safety, and
the filing path (against a fake Outlook client). They use fixture data and
don't need Outlook.

---

## Status

Working end to end, and verified against a real 5,000-message mailbox: the COM
bridge, signature parsing, folder creation, and filing (moves and colour
categories) have all been run for real, not only unit-tested.

**Only companies you have confirmed get filed** — either because you have
written to them, or because you gave them a group on the Companies tab.
Everything else stays in the Inbox, tagged `미분류`. This matters: without that
rule, a plain "is this company known?" test wanted to move 1,493 of 1,500 inbox
messages into folders like `거래처/Instagram/Instagram`.

The main thing still unproven is **Exchange sender resolution**. The test
profile is IMAP/Gmail, so the X.500 branch has never executed — and that is the
branch a corporate mailbox depends on.

See **[docs/PROGRESS.md](docs/PROGRESS.md)** for the full picture: what works,
the bugs that only a live mailbox exposed, and the open questions.

