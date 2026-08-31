# mailcleaner

[![CI](https://github.com/RishikeshSreekumar/gmail_cleaner/actions/workflows/ci.yml/badge.svg)](https://github.com/RishikeshSreekumar/gmail_cleaner/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A local mailbox observability + cleanup dashboard. It builds a **metadata-only**
SQLite index of your mail, shows you who is actually filling it up, and lets you
act in bulk — safely.

Works with **Gmail, Outlook.com / Microsoft 365, Proton Mail (via Bridge),
Fastmail, iCloud, Yahoo, Zoho, and any other IMAP server**, and tracks **as many
mailboxes as you like** side by side.

Your mail server stays the source of truth. Deleting an index loses nothing;
sync again and it rebuilds.

## Safety model

- **No permanent delete anywhere in the code, for any provider.** "Trash" means
  your provider's own Trash (Gmail's `\Trash` label, or an IMAP `MOVE` into the
  Trash folder). The app never sends `EXPUNGE` and never sets `\Deleted`.
- **Archive, trash and label need the IMAP `MOVE` extension** on folder-based
  servers. Where a server does not offer it, those actions are refused rather
  than emulated with copy-then-delete — because that is a delete.
- **Read-only until you say otherwise**, per account: `mclean enable-actions`.
- **Protected mail is excluded from bulk actions.** Finance, security, travel,
  orders, human conversation and anything starred are protected by default.
- **Every bulk action shows a sample first** — counts, date span, category
  breakdown, how many protected messages were excluded, and example messages.
- **Everything is logged and undoable** (History screen, `u`).
- Only headers are fetched: from/to/subject/date/list headers/size/labels.
  No bodies, no attachments. A body preview is fetched only if you ask for one.
- **Accounts are independent.** Each has its own index file, its own rules, its
  own history and its own credential. No query in this app spans two mailboxes.

## Providers

```bash
mclean providers        # what is supported and how each one signs in
```

| Provider | Sign-in | Notes |
|---|---|---|
| Gmail | app password | Label-shaped: archive/trash never move a message |
| Outlook.com / Microsoft 365 | browser device code | Microsoft turned off IMAP basic auth; there is no app password |
| Proton Mail | Bridge password | Needs [Proton Mail Bridge](https://proton.me/mail/bridge) running locally (paid plan) |
| Fastmail / iCloud / Yahoo / Zoho | app password | Standard IMAP |
| Any other IMAP server | password | You supply host, port and TLS mode |

`mclean add` guesses the provider from your address, then prints that provider's
exact setup steps before asking for anything.

### Gmail

1. Turn on 2-Step Verification: https://myaccount.google.com/signinoptions/two-step-verification
2. Create an app password: https://myaccount.google.com/apppasswords
3. Enable IMAP: Gmail → Settings → Forwarding and POP/IMAP

No Google Cloud project, OAuth client or consent screen is involved. Revoke the
app password on that page and this tool loses all access.

### Outlook / Microsoft 365

Microsoft retired IMAP basic authentication, so an app password cannot reach
these mailboxes. `mclean add` shows a short code to enter at
https://microsoft.com/devicelogin; after that a refresh token lives in your
keychain and renews itself. The only permission requested is IMAP access to your
own mailbox. Revoke it at https://myaccount.microsoft.com/settings.

If your tenant blocks third-party clients, register your own app and set
`MAILCLEANER_MS_CLIENT_ID`.

### Proton Mail

Proton does not expose IMAP directly — Bridge decrypts locally. Install Bridge,
open *Mailbox details*, and use the host, port and **Bridge password** it shows.
Bridge presents a self-signed certificate on loopback, so certificate
verification is relaxed for Proton accounts only.

## Install

```bash
uv venv --python 3.13
uv pip install -e .
```

The `mclean` command lands in `.venv/bin/mclean` (add `.venv/bin` to your PATH,
or use `uv run mclean`). `gclean` still works as an alias.

## Use

```bash
mclean add              # connect a mailbox (repeat for each one)
mclean sync             # index the last 365 days of the active account
mclean                  # open the terminal dashboard
mclean gui              # ...or the browser one
```

### Several mailboxes

```bash
mclean accounts                     # list them; * marks the active one
mclean use work                     # switch by id, address or label
mclean sync --all                   # sync every account
mclean status -a personal-gmail     # any command takes -a/--account
mclean remove old-yahoo             # forget an account and its local index
```

In the terminal dashboard the **Accounts** screen lists every mailbox — Enter
switches, or shift-`A` cycles. In the browser GUI it is the dropdown next to the
logo, which also has **＋ Add mailbox…** — the same connect flow as `mclean add`,
including the device-code sign-in for Outlook. Switching only changes which
index the next query reads.

Everything else is per account: the sync window, whether actions are enabled,
protect/ignore rules, and the audit log.

```bash
mclean sync --days 1825      # last 5 years
mclean sync --days 9999      # everything
```

Later syncs are incremental. For each indexed folder the app tracks UIDVALIDITY
and the highest UID it has seen, and re-checks the newest 500 known messages so
flag and folder changes land too. Gmail is indexed as one virtual folder (All
Mail); other providers index every folder except Junk, Drafts and Trash.

To allow archive / trash / label / mark-read:

```bash
mclean enable-actions            # for the active account only
```

### Upgrading from gmail-cleaner

The first run migrates `~/.gmail_cleaner` into `~/.mailcleaner` automatically:
your address, settings and app password become an account named
`gmail-<your-address>`. The old index is not carried over — it used a different
schema and `mclean sync` rebuilds it in full. Nothing in `~/.gmail_cleaner` is
modified or removed.

## The browser GUI

```bash
mclean gui              # opens http://127.0.0.1:8765 in your browser
mclean gui --port 9000 --no-browser
```

Same index, same queries, same guard rails as the terminal dashboard — just
easier to read. Click any sender, domain, category, age band or cleanup
suggestion to get the preview, and act from there. History undoes a batch with
one click.

It is a local page, not a service:

- Bound to `127.0.0.1` only, and it stops when you press Ctrl-C.
- A one-time token in the launch URL is required on every request, so no other
  page in your browser can drive it.
- The page never sends SQL. It names a target (`sender`, `domain`, `category`,
  `suggestion`, ...) and the server builds the query, so the browser cannot
  widen a selection beyond what the UI offers.
- Actions still need `mclean enable-actions`, still skip protected mail, still
  show the preview first, and still cannot permanently delete anything.
- Connecting a mailbox from the page sends an app password across that loopback
  socket. It goes straight to the keychain — the page never stores it, no
  endpoint echoes it back, and nothing is logged. Providers that use OAuth never
  see a password at all: the browser shows a device code and the sign-in happens
  on the provider's own site. If you would rather not type a password into a
  local web page, `mclean add` in the terminal does the same thing.

## The dashboard

| Screen | What it answers |
|---|---|
| Dashboard | Size of the problem: totals, top domains, categories, suggested cleanups |
| Accounts | Every configured mailbox, its size and last sync. Enter switches |
| Attention | Unread mail that looks like it matters — invoices, security, deadlines, humans |
| Domains / Senders | Who writes to you, how often, 7/30/90-day counts, per-week rate, trend, dormancy |
| Categories | Finance, security, orders, travel, developer, newsletter, promotion, social, human, automated |
| Frequency | Senders bucketed by how noisy they are |
| Age / Storage | What is old, what is heavy |
| Cleanup | Ready-made safe selections: ignored newsletters, old promos, ancient unread, large mail |
| Rules | Your overrides (protect / ignore a sender or domain) |
| History | Audit log, with undo |

### Keys

```
enter  drill down            escape  back           q  quit
s      sync                  r       reclassify
a      archive               t       trash          m  mark read
l      label                 f       flag (star)
p      protect this sender   i       ignore this sender
u      undo last batch       o       open in webmail
A      next account
```

Actions apply to whatever the cursor is on: a domain, a sender, an age band, a
cleanup suggestion, or a single message. You always get the confirmation
screen first.

### Labels vs folders

On Gmail, `l` adds a label and undo removes it — the message does not move.
On every other provider a message lives in exactly one folder, so `l` **files**
it into the folder you name and undo moves it back where it came from. The
confirmation dialog and the prompt wording follow whichever applies.

## Classification

Deterministic, offline, no LLM. Three independent dimensions per message —
**category** (what it is), **attention** (what it wants now), **retention**
(what should happen to it) — plus a protected flag. Signals: List-Id /
List-Unsubscribe, sender local-part, known developer/social/finance domains,
subject keyword families, the provider's own labels or folder names,
starred/important, age.

`p` / `i` on a sender or domain writes a rule that overrides the heuristics for
every message from them; `r` re-runs classification over the index.

## Layout

```
mailcleaner/
  accounts.py     accounts, per-account settings, credential storage, migration
  providers/
    __init__.py   the provider registry (host, port, TLS, sign-in style)
    base.py       IMAP plumbing + the generic folder-shaped backend
    gmail.py      the label-shaped backend
    oauth.py      device-code sign-in for providers without app passwords
  db.py           SQLite schema: emails, rules, action_log, sync_state
  sync.py         fetch -> normalize -> classify -> upsert, per folder
  classify.py     the deterministic classifier
  stats.py        every query behind every screen
  actions.py      archive/trash/label/mark-read + preview + audit log + undo
  tui.py          the terminal dashboard
  web.py          local HTTP server behind `mclean gui`
  static/         the one-page browser GUI
  cli.py          mclean
tests/
  make_fixture.py synthetic mailbox + a fake backend
  test_smoke.py   headless run of every screen + action round trips, both shapes
  test_web.py     the GUI's HTTP surface, token/host guards, account switching
  test_providers.py folder discovery, message identity, MOVE guard, migration
```

Data lives in `~/.mailcleaner/`: `config.json` plus one directory per account
holding that account's `index.db`.

```bash
.venv/bin/python tests/test_smoke.py
.venv/bin/python tests/test_web.py
.venv/bin/python tests/test_providers.py
```

## Contributing

Bug reports, provider quirks and pull requests are all welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) — it lists the invariants this project will
not trade away (no permanent delete, metadata only, offline classification, no
telemetry) and how to run the three test suites.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). For
security issues, follow [SECURITY.md](SECURITY.md) rather than the issue
tracker. Notable changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) © Rishikesh Sreekumar.

Provided as is, without warranty. It talks to your real mailbox: keep the
default read-only mode until you have looked at what a preview selects.
