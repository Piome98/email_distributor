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

```bash
pip install --user pywin32
```

Then double-click **`run.pyw`**, or:

```bash
python -m email_distributor
```

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

```bash
python -m email_distributor status      # what the database knows
python -m email_distributor learn       # build the identity DB
python -m email_distributor run         # preview one pass
python -m email_distributor run --live  # actually apply it
python -m email_distributor watch       # poll until Ctrl+C
```

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
python -m unittest discover -s tests
```

62 tests covering signature parsing, identity resolution, source precedence,
rule matching, template expansion and folder-name safety. They use fixture data
and don't need Outlook.

---

## Status

### Done

- **Outlook COM bridge** — connect, folder navigation and creation (handles the
  localised `받은 편지함`), colour categories, move, read-state.
- **Exchange sender resolution** — X.500 DNs turned into real SMTP addresses
  through `PR_SMTP_ADDRESS` / `PR_SENT_REPRESENTING_SMTP_ADDRESS`, with
  fallbacks.
- **Identity model** — Group → Company → Person on SQLite, with
  `manual > signature > inferred` source precedence enforced on every write.
- **Signature parser** — mixed Korean/English blocks; extracts name, rank,
  department, company, landline and mobile. Skips quoted reply chains, legal
  disclaimers, and fax numbers.
- **Learner** — builds the database from Inbox, Inbox subfolders and Sent Items.
- **Rules engine** — JSON rules, `all`/`any` matching, ordered evaluation with
  `stop_on_match`, placeholder expansion.
- **Distributor** — dry run by default, per-message ledger so restarts never
  re-file, categories applied before moves.
- **Watcher** — interruptible polling loop, own COM and SQLite handles per
  thread, re-reads rules each pass.
- **Desktop UI** — five tabs (Run / Companies / People / Rules / Settings),
  worker threads that never touch widgets.
- **CLI** — `status`, `learn`, `run [--live]`, `watch`.
- **62 tests**, all passing, no Outlook required.

### Verified

- Full test suite passes.
- CLI runs (`status`, `--help`).
- UI builds, renders both tree views, loads rules, closes cleanly.

### Not yet done

- **Live Outlook run against a real corporate mailbox.** The COM layer has not
  been exercised against a production Exchange profile — that is the next step,
  on the work laptop.
- **Outlook "programmatic access" prompt.** Some managed configurations warn
  when external code reads mail properties. `PropertyAccessor` is used in
  preference to guarded properties to avoid this, but it is unconfirmed in the
  target environment.
- Signature parsing is heuristic and will need vocabulary added as real
  footers turn up — `KO_TITLES` and `DEPT_SUFFIXES` in
  `identity/signature.py` are the places to extend.
- No packaging into a single .exe yet; the app runs from source.
