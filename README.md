# gmail-cleaner

A local Gmail observability + cleanup dashboard. It builds a **metadata-only**
SQLite index of your mailbox, shows you who is actually filling it up, and lets
you act in bulk — safely.

Gmail stays the source of truth. `rm ~/.gmail_cleaner/index.db` loses nothing;
sync again and it rebuilds.

## Safety model

- **No OAuth, no Google Cloud project.** Access is over IMAP with a Gmail app
  password you create and can revoke in one click.
- **No permanent delete anywhere in the code.** "Trash" adds Gmail's `\Trash`
  label; the mail sits in Trash for 30 days and undo pulls it straight back.
  The app never sends `EXPUNGE` and never sets `\Deleted`.
- **Read-only until you say otherwise.** Actions are disabled until you run
  `gclean enable-actions`.
- **Protected mail is excluded from bulk actions.** Finance, security, travel,
  orders, human conversation and anything starred are protected by default.
- **Every bulk action shows a sample first** — counts, date span, category
  breakdown, how many protected messages were excluded, and example messages.
- **Everything is logged and undoable** (History screen, `u`).
- Only headers are fetched: from/to/subject/date/list headers/size/labels.
  No bodies, no attachments. A body preview is fetched only if you ask for one.

## What you need to do (one time, ~2 minutes)

1. **Turn on 2-Step Verification** (required before Google will issue an app
   password): https://myaccount.google.com/signinoptions/two-step-verification
2. **Create an app password**: https://myaccount.google.com/apppasswords
   Name it anything, e.g. `gmail cleaner`. Google shows a 16-character code —
   copy it.
3. **Check IMAP is enabled**: Gmail → Settings → *See all settings* →
   *Forwarding and POP/IMAP* → **Enable IMAP** → Save.

That is the whole manual part. To cut access later: delete the app password on
that page, or run `gclean logout`.

## Install

```bash
uv venv --python 3.13
uv pip install -e .
```

The `gclean` command lands in `.venv/bin/gclean` (add `.venv/bin` to your PATH,
or use `uv run gclean`).

## Use

```bash
gclean setup            # paste your Gmail address + app password
gclean sync             # index the last 365 days (metadata only)
gclean                  # open the terminal dashboard
gclean gui              # ...or the browser one
```

Widen the window when you want the old junk:

```bash
gclean sync --days 1825      # last 5 years
gclean sync --days 9999      # everything
```

Later syncs are incremental (new UIDs plus a re-check of the newest 500
messages, so flag and label changes land too).

To allow archive / trash / label / mark-read:

```bash
gclean enable-actions
```

## The browser GUI

```bash
gclean gui              # opens http://127.0.0.1:8765 in your browser
gclean gui --port 9000 --no-browser
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
- Actions still need `gclean enable-actions`, still skip protected mail, still
  show the preview first, and still cannot permanently delete anything.

## The dashboard

| Screen | What it answers |
|---|---|
| Dashboard | Size of the problem: totals, top domains, categories, suggested cleanups |
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
u      undo last batch       o       open in Gmail
```

Actions apply to whatever the cursor is on: a domain, a sender, an age band, a
cleanup suggestion, or a single message. You always get the confirmation
screen first.

## Classification

Deterministic, offline, no LLM. Three independent dimensions per message —
**category** (what it is), **attention** (what it wants now), **retention**
(what should happen to it) — plus a protected flag. Signals: List-Id /
List-Unsubscribe, sender local-part, known developer/social/finance domains,
subject keyword families, Gmail's own labels, starred/important, age.

`p` / `i` on a sender or domain writes a rule that overrides the heuristics for
every message from them; `r` re-runs classification over the index.

## Layout

```
gmail_cleaner/
  config.py       app password (system keychain) + settings
  db.py           SQLite schema: emails, rules, action_log, sync_state
  imapclient.py   Gmail IMAP: metadata fetch, label/flag mutation. No delete path.
  sync.py         fetch -> normalize -> classify -> upsert
  classify.py     the deterministic classifier
  stats.py        every query behind every screen
  actions.py      archive/trash/label/mark-read + preview + audit log + undo
  tui.py          the terminal dashboard
  web.py          local HTTP server behind `gclean gui`
  static/         the one-page browser GUI
  cli.py          gclean
tests/
  make_fixture.py synthetic mailbox
  test_smoke.py   headless run of every screen + fake-IMAP action round trip
  test_web.py     the GUI's HTTP surface, token/host guards, target validation
```

```bash
.venv/bin/python tests/test_smoke.py
.venv/bin/python tests/test_web.py
```
