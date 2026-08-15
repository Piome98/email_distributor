# Progress notes

Where the project stands, what was learned building it, and what is left.
Written after the first full round of testing against a real mailbox.

---

## What works today

The tool reads an Outlook mailbox, works out **who** each message is from —
the individual, their company, and the group that company belongs to — and
files it into `거래처/{업체}/{담당자}`.

| Area | State |
|---|---|
| Outlook COM bridge | Working. Verified against a live 5,000-message mailbox. |
| Exchange sender resolution | Written, **not yet exercised** — see Open questions. |
| Signature parsing | Working; reads labels first, then falls back to heuristics. |
| Identity database | Working. 127 companies, 233 people learned from a real mailbox. |
| Folder creation | Working, syncs to the server. |
| Filing (move + categorise) | Working. Verified live on real messages. |
| Desktop UI / CLI / .bat / .exe | All working, all verified by running them. |
| Tests | 145, all passing, no Outlook required. |

---

## The decision that shapes everything

**Only companies you have confirmed are filed.** Two things count as
confirmation:

1. **You have written to them.** Read straight from Sent Items — no setup.
2. **You gave them a group** on the Companies tab — the manual override.

What is deliberately *not* enough is "the learner knows this company". That was
tried and was badly wrong: the learner invents a company for every domain it
meets, so a plain "is this known?" test wanted to move **1,493 of 1,500** inbox
messages into folders like `거래처/Instagram/Instagram` and
`거래처/Redditmail/Reddit`.

Correspondence is the honest signal. Nobody writes back to a newsletter.

Everything unconfirmed stays in the Inbox and is merely tagged `미분류`. Moving
mail the tool cannot explain only relocates the problem into a folder nobody
reads.

---

## Bugs found by running it, not by reading it

Every one of these passed code review and unit tests first. They were only
found by pointing the tool at a real mailbox.

### Identity

- **Exchange senders have no email address.** `SenderEmailAddress` returns an
  X.500 DN (`/O=EXCHANGELABS/...`), so filing on it would give every colleague
  a fake domain. Recovered via MAPI properties with three fallbacks.
- **Newsletters were being given job titles** lifted out of marketing prose.
  Person-level fields now need evidence of a real sign-off.
- **A recruitment mailshot renamed a company.** Mail from `saramin.co.kr`
  advertising jobs at `㈜카카오페이` made 카카오페이 the *sender's* company, and
  the domain was re-linked to it. One bad parse rewrote a whole company.
- **Korean word boundaries.** 국, 처, 단 and 과 all had to be dropped as
  department suffixes — they match far more ordinary words than departments
  (한국, 거래처, 재단, and 과 is simply "and"). `한국고등교육재단`, a
  foundation, was being stored as somebody's team.
- **`공인회계사`** was split into the person 공인 holding the rank 회계사.
- **`gmail.com` was set as the internal domain**, making every Gmail sender on
  earth a "colleague".

### Filing

- **A failed move was reported as success**, because "changed" was true if the
  *category* had been applied. The log said filed; the mail had not moved.
- **Outlook issues a new EntryID when a message changes folder**, so the ledger
  never recognised moved mail. A second pass re-filed everything and Outlook
  refused each move.
- **Tag-only decisions were recorded as handled**, which permanently consumed
  the message: once its company was later confirmed, it would be skipped
  forever.
- **The ledger only asked "seen before?"** — so a corrected ruleset could never
  reach mail already processed. It now records *what was done* and compares.

### Plumbing

- **`python -m email_distributor` did not work** in a fresh shell. Every
  command during development had `PYTHONPATH` set by the shell that ran it, so
  the gap stayed invisible until the documented command was typed by hand.
  `cli.py` and `run_tests.py` now put `src/` on the path themselves.
- **Batch files must be pure ASCII.** `cmd.exe` parses a `.bat` using the
  console codepage, so Korean text shifted the parser and split later tokens —
  `echo` became `ho`. Korean lives in the Python layer.
- **The `.exe` printed mojibake**, because the `.bat` files had been setting
  `PYTHONIOENCODING` and `chcp` for it. The program now sets its own console
  to UTF-8.
- **`learn --limit N` persisted N**, silently capping every later run at 250.
- **A stale `rules.json` was used forever.** `load()` only wrote the file when
  it was missing, so none of the rule fixes ever reached an existing install:
  the code was right, the behaviour was not, and nothing said why. The file now
  carries a version and is upgraded when it is still an untouched default.

---

## Open questions

1. **The Exchange X.500 path has still never executed.** The test profile is
   IMAP/Gmail, so every sender was already `SMTP` type. This is the branch the
   office PC depends on most. After `learn` there, check the line reporting
   Exchange senders resolved, and confirm the People tab shows real addresses
   rather than `/O=EXCHANGELABS/...`.
2. **Correspondence cannot fire on the test profile.** Its Sent folder holds
   0 items — Gmail's sent mail is not synced into Outlook — so manual grouping
   is the only path there. A corporate profile has real sent mail, where this
   works unattended.
3. **Outlook's "programmatic access" warning** is unconfirmed on a managed
   build. `PropertyAccessor` is used in preference to guarded properties to
   avoid it.
4. **Contacts with no display name** produce a folder named after the raw
   address, e.g. `거래처/Claude/no-reply@email.claude.com`. Collapsing those
   into one folder per company is an open choice.
5. **195 messages sit in the old `거래처/미분류/{company}` layout** from an
   early run. They can be re-filed with
   `python cli.py run --folder "Inbox/거래처" --recurse --reprocess --live`,
   but mail from ungrouped companies will stay where it is rather than
   returning to the Inbox.

---

## Working practice that paid off

- **Dry run by default.** Every destructive path was previewed before it ran.
- **Test live, in a sandbox.** The filing path was proved on 8 real messages
  copied into a temporary folder, which was deleted afterwards. The Inbox
  originals were never touched — and that run found two real bugs.
- **Measure, don't assume.** "Only one mail was distributed" was actually 195
  in 38 folders; "nothing moves" was 200 of 200 skipped by the ledger. Each
  time, counting first pointed straight at the cause.
