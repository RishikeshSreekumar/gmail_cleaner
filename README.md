<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/lockup-dark.svg">
    <img src="assets/brand/lockup.svg" alt="mailcleaner" width="360">
  </picture>
</p>

<p align="center"><b>See who fills your inbox. Clean it up safely.</b><br>
Runs on your computer · reads headers only · never deletes</p>

[![CI](https://github.com/RishikeshSreekumar/gmail_cleaner/actions/workflows/ci.yml/badge.svg)](https://github.com/RishikeshSreekumar/gmail_cleaner/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Latest release](https://img.shields.io/github/v/release/RishikeshSreekumar/gmail_cleaner?label=download)](https://github.com/RishikeshSreekumar/gmail_cleaner/releases/latest)

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
- **Automation never trashes.** Compound rules can archive, mark read, label or
  star. They only suggest until you mark one *auto*, and even then they run only
  when you apply them (or from a schedule you installed), never on protected
  mail.
- **The LLM advisor is optional and can only propose.** It is off until you turn
  it on per account in the terminal. Suggestions wait for you to accept them, and
  it can add protection but never remove it.

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

**macOS / Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/RishikeshSreekumar/gmail_cleaner/main/install.sh | sh
```

**Windows** (PowerShell)

```powershell
irm https://raw.githubusercontent.com/RishikeshSreekumar/gmail_cleaner/main/install.ps1 | iex
```

The installer downloads the standalone `mclean` for your machine from the
[latest release](https://github.com/RishikeshSreekumar/gmail_cleaner/releases/latest),
checks its SHA-256, and puts it in `~/.local/bin`. There is nothing to
configure and no Python to install — the build bundles its own interpreter.
Uninstalling is `rm ~/.local/bin/mclean` plus `rm -rf ~/.mailcleaner` for the
local indexes.

<details>
<summary>Other ways to install</summary>

**A binary, by hand.** Every release publishes one per platform. Download,
unpack, `chmod +x mclean`, move it somewhere on your PATH.

**With pipx or uv**, if you already have Python 3.11+:

```bash
pipx install https://github.com/RishikeshSreekumar/gmail_cleaner/releases/latest/download/mailcleaner-0.1.0-py3-none-any.whl
```

**From source:**

```bash
git clone https://github.com/RishikeshSreekumar/gmail_cleaner
cd gmail_cleaner
uv venv --python 3.13
uv pip install -e .
```

The `mclean` command lands in `.venv/bin/mclean` (add `.venv/bin` to your PATH,
or use `uv run mclean`).

</details>

`gclean` still works as an alias for `mclean` everywhere.

## Use

Run `mclean`. Your browser opens on a page served from your own computer, and
everything happens there: no other commands to learn.

1. **Add a mailbox.** Pick your provider; the page shows its setup steps and
   links to the page where you create an app password (or, for Outlook, a code
   to sign in with). Add as many mailboxes as you like from the account menu.
2. **Look around.** It syncs headers right away. Overview, Attention, Senders,
   Cleanup, Domains, Categories and Age show where your mail comes from.
3. **Turn on actions when you are ready.** The *Read-only* pill in the header,
   or Settings → Actions. Every bulk action still shows a preview first.
4. **Automate.** Rules, Unsubscribe and the optional Assistant.
5. **Settings** holds everything else: the sync window, re-entering a
   password, a step-by-step connection check, keyword packs, a classifier
   accuracy check, the Assistant, background sync, exports, connecting
   Claude (MCP) and removing a mailbox.

Keep the terminal window open while you use it; closing it stops the app.

Everything below also works from a terminal, for scripts and for anyone who
prefers it. The terminal is never required.

```bash
mclean add              # connect a mailbox (repeat for each one)
mclean sync             # index the last 365 days of the active account
mclean ui               # the terminal dashboard
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

## How the app stays local

```bash
mclean                  # same as: mclean gui
mclean gui --port 9000 --no-browser
```

If port 8765 is taken (another copy is running, say), it picks the next free
one. Click any sender, domain, category, age band, rule or cleanup suggestion
to get the preview, and act from there. History undoes a batch with one click.

It is a local page, not a service:

- Bound to `127.0.0.1` only, and it stops when you press Ctrl-C.
- A one-time token in the launch URL is required on every request, so no other
  page in your browser can drive it.
- The page never sends SQL. It names a target (`sender`, `domain`, `category`,
  `suggestion`, ...) and the server builds the query, so the browser cannot
  widen a selection beyond what the UI offers.
- Actions are off until you turn them on (Settings → Actions, with a
  confirmation); they still skip protected mail, still show the preview first,
  and still cannot permanently delete anything. Turning them off needs no
  confirmation.
- The Assistant's Claude option is only saved with a ticked consent box that
  says what is sent; an API key goes to the keychain and no endpoint returns it.
- Connecting a mailbox from the page sends an app password across that loopback
  socket. It goes straight to the keychain — the page never stores it, no
  endpoint echoes it back, and nothing is logged. Providers that use OAuth never
  see a password at all: the browser shows a device code and the sign-in happens
  on the provider's own site. If you would rather not type a password into a
  local web page, `mclean add` in the terminal does the same thing.

## Terminal dashboard (optional)

`mclean ui` opens the same views in the terminal.

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
A      next account          U       unsubscribe (sender / Unsubscribe screen)
y / n  accept / reject an LLM suggestion (Suggestions screen)
g      rule: auto on/off     d       delete rule (Rules screen)
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
starred/important, age — and **who you write to**: anyone you have sent mail to
(read from your indexed Sent mail) counts as a correspondent and is protected,
mailing lists excepted. Unread mail with a deadline in it ("expires", "due",
"final notice") stays protected until you have read it, unless it is plainly
marketing.

`p` / `i` on a sender or domain writes a rule that overrides the heuristics for
every message from them; `r` re-runs classification over the index.

### Keyword packs

Subject keywords come in packs: `en`, `in` (India), `us`, `uk`, `de`, `fr`,
`es`. Every pack is on by default; narrow it per account if one misfires, in
Settings → Classification or from a terminal:

```bash
mclean locale                 # show
mclean locale --set en,uk     # choose (reclassifies)
mclean locale --set all
```

### Measuring it

In the app: Settings → Classification → *Check accuracy* shows thirty random
messages; correct what is wrong and it scores itself. From a terminal:

```bash
mclean eval export -n 300 -o labels.csv   # random sample, label columns blank
# fill in true_category and true_protected (1/0)
mclean eval score labels.csv              # missed protection first, then accuracy
```

**Missed protection** — mail you marked important that the classifier left
unprotected — is reported first and on its own, because it is the only kind of
classifier mistake that can cost you mail. `tests/classifier_cases.csv` is a
small hand-labelled set the test suite holds the classifier to.

## Rules

In the app: **Rules → New rule**, or *Save as rule* on any plain-English search.
*Always protect* and *Treat as clutter* on a sender's panel are overrides: they
change how that sender's mail is *classified*. Compound rules are a filter plus
a mailbox action. From a terminal:

```bash
mclean rules add "Old newsletters" --category newsletter --older-than 180 --action archive
mclean rules add "Quora" --domain quora.com --action label --label Later --auto
mclean rules add "Big" --where '{"larger_than_kb": 5000, "starred": false}' --action label --label Big
mclean rules fields        # every filter field
mclean rules list          # what each rule matches right now
mclean rules run           # preview them all; changes nothing
mclean rules apply         # apply the --auto ones (asks first; each batch is undoable)
mclean rules apply --rule 3
```

A rule **suggests** until you mark it auto. Opening a rule previews it and lets
you act on it like any other selection; *Apply auto rules* on the Rules page
(or the background schedule) runs the auto ones. Rule actions are archive, mark read, label and star — never
trash — and protected mail is excluded as always.

## Unsubscribe

In the app: the **Unsubscribe** page, or *Unsubscribe…* on a sender's panel —
with *Unsubscribe & archive* to clear the backlog in one go. From a terminal:

```bash
mclean unsubscribe list                       # senders that offer it, loudest first
mclean unsubscribe sender news@example.com --archive
```

Where the sender supports RFC 8058 **one-click**, one POST is sent from your
machine and that is it. Otherwise the sender's page (or a `mailto:`) is opened
for you to finish. Because the link comes from the sender, the one-click request
is handled defensively: https only, the host must resolve to a public address and
the connection is pinned to it, no cookies, no redirects followed, and only the
host is logged — the URL usually contains a token that identifies you.
Unsubscribing needs actions enabled, touches no mail, and cannot be undone here;
`--archive` (or *Unsubscribe & archive backlog* in the GUI) then archives their
unprotected inbox mail as an ordinary, undoable batch. Don't unsubscribe from
spam: it confirms your address is live.

## Assistant (optional LLM advisor)

In the app: **Settings → Assistant** to choose *On this computer* (Ollama; the
page lists the models you have pulled) or *Claude* (paste an API key, tick the
consent box), then the **Assistant** page to ask for and review suggestions.

Off by default. When on, it looks at the senders the classifier is unsure about
(or that are loud and unprotected) and proposes a rule for each: protect,
ignore, or a better category. One request covers twenty senders, so a large
mailbox costs a handful of calls.

From a terminal:

```bash
mclean llm setup -p ollama                 # local: nothing leaves your machine
mclean llm setup -p ollama -m qwen2.5:7b --host http://127.0.0.1:11434
mclean llm setup -p anthropic              # Claude; asks before enabling
mclean llm setup -p anthropic --no-share-subjects   # sender statistics only
mclean llm suggest -n 40
mclean llm review
mclean llm accept 3 7 9
mclean llm reject 4
mclean llm ask "unread newsletters older than a year"
mclean llm setup -p off
```

What is sent per sender: the address, message count, weekly rate, read rate,
whether it is a mailing list, the classifier's category and — unless you pass
`--no-share-subjects` — up to five recent subject lines. Never bodies. The
Anthropic provider needs `pip install 'mailcleaner[claude]'` (the standalone
binaries include it) and an API key: stored in the keychain by `llm setup`, or
taken from `ANTHROPIC_API_KEY` / an `ant auth login` profile. It defaults to
`claude-opus-5-5`; pick another with `-m`.

Guard rails, enforced in code rather than in the prompt:

- Nothing applies until you accept it; an accepted suggestion is an ordinary rule.
- A suggestion to ignore — or recategorise into a bulk category — a sender with
  any protected, starred or finance/security/travel/personal mail is dropped.
- Senders the model was not shown are discarded; nobody is asked about twice.
- Plain-English search (`llm ask`, or the *Ask* box in the GUI) turns into a filter
  over the fixed field list, validated server-side — never SQL.
- Claude is only enabled with an explicit consent flag, which the page sends
  only after you tick the box describing what leaves your machine.

## Integrations

In the app, **Settings** covers all of these: Export (CSV / JSON downloads),
Connect to Claude (a ready-to-paste MCP config), and Background sync (on/off,
how often, whether to apply auto rules). From a terminal:

**JSON everywhere.** `accounts`, `status`, `top`, `digest`, `doctor`,
`rules list`, `unsubscribe list`, `llm review`, `llm ask`, `eval score` and
`schedule status` take `--json`.

```bash
mclean export senders -o senders.csv
mclean export messages --format json --where '{"category": ["promotion"], "unread": true}'
```

**MCP server.** `mclean mcp` speaks the Model Context Protocol over stdio, so
Claude (or any MCP client) can answer "who is filling my inbox?" from your
index. It is **read-only**: overview, senders, domains, categories, attention,
cleanup suggestions, trend alerts, digest, message search and action previews —
no tool can archive, trash, label, unsubscribe or write a rule. Whatever it
returns goes to the client you registered it with.

```bash
claude mcp add mailcleaner -- mclean mcp
```

Or in a client's JSON config: `{"mcpServers": {"mailcleaner": {"command": "mclean", "args": ["mcp"]}}}`.

**Background sync and a digest.**

```bash
mclean schedule install --every 6            # launchd / systemd timer / Task Scheduler
mclean schedule install --every 6 --apply-rules   # also apply auto rules
mclean schedule status
mclean schedule remove
mclean digest --all                          # new mail, what wants you, who got louder
```

Each run syncs every account, writes `digest.json` next to its index, logs to
`~/.mailcleaner/background.log`, and (unless `--no-notify`) raises a desktop
notification when something wants attention or a sender suddenly got louder.
The dashboards show those **trend alerts** too.

**Setup help.** Settings → Mailbox → *Check connection* (or `mclean doctor`)
checks DNS, the port, TLS, the login, MOVE support and folder listing one by
one and says which setup step to redo. A failed *Add mailbox* says the same.

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
  classify.py     the deterministic classifier and its keyword packs
  stats.py        every query behind every screen, trend alerts, the digest
  actions.py      archive/trash/label/mark-read + preview + audit log + undo
  policies.py     the filter language (the only filter-to-SQL path) + compound rules
  unsubscribe.py  List-Unsubscribe: one-click POST, or the link for you to open
  llm.py          the optional LLM advisor (Ollama / Anthropic) and its guard rails
  mcp_server.py   read-only MCP server on stdio
  schedule.py     background sync via launchd / systemd / Task Scheduler
  doctor.py       step-by-step connection diagnosis
  evaluate.py     classifier scoring against hand-labelled mail
  tui.py          the terminal dashboard
  web.py          local HTTP server behind `mclean gui`
  static/         the one-page browser GUI
  cli.py          mclean
tests/
  make_fixture.py synthetic mailbox + a fake backend
  test_smoke.py   headless run of every screen + action round trips, both shapes
  test_web.py     the GUI's HTTP surface, token/host guards, account switching
  test_providers.py folder discovery, message identity, MOVE guard, migration
  test_features.py rules, unsubscribe, LLM advisor, MCP, eval, schedule, doctor, CLI
  classifier_cases.csv hand-labelled cases the classifier is held to
packaging/
  package_managers.py renders a Homebrew formula + winget manifests per release
```

Data lives in `~/.mailcleaner/`: `config.json` plus one directory per account
holding that account's `index.db`.

```bash
.venv/bin/python tests/test_smoke.py
.venv/bin/python tests/test_web.py
.venv/bin/python tests/test_providers.py
.venv/bin/python tests/test_features.py
```

## Contributing

Bug reports, provider quirks and pull requests are all welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) — it lists the invariants this project will
not trade away (no permanent delete, metadata only, deterministic classification, no
telemetry) and how to run the three test suites.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). For
security issues, follow [SECURITY.md](SECURITY.md) rather than the issue
tracker. Notable changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) © Rishikesh Sreekumar.

Provided as is, without warranty. It talks to your real mailbox: keep the
default read-only mode until you have looked at what a preview selects.
