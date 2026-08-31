# Contributing to mailcleaner

Thanks for taking the time. This is a small tool that touches people's real
mailboxes, so the bar for changes is less "does it work" and more "can it never
lose mail".

## Getting set up

```bash
uv venv --python 3.13
uv pip install -e .
.venv/bin/mclean add        # connect a throwaway or secondary mailbox
```

You do not need a real mailbox to work on most of the code — the test fixture
builds a synthetic one with a fake IMAP backend.

## Running the tests

The suites are plain scripts, not a pytest run:

```bash
.venv/bin/python tests/test_smoke.py       # every TUI screen + action round trips
.venv/bin/python tests/test_web.py         # the GUI's HTTP surface and guards
.venv/bin/python tests/test_providers.py   # folder discovery, MOVE guard, migration
```

All three must pass before a pull request is ready. CI runs the same three on
Python 3.11, 3.12 and 3.13.

## The invariants

These are the rules the project exists to keep. A change that breaks one will
not be merged, however convenient it is.

1. **No permanent delete. Ever.** No `EXPUNGE`, no setting `\Deleted`, no
   copy-then-delete emulation of `MOVE`. "Trash" means the provider's own Trash
   and nothing more. If a server lacks `MOVE`, refuse the action.
2. **Metadata only.** Sync fetches headers, flags, labels and size. Bodies and
   attachments are fetched only when a user explicitly asks for one preview,
   and are never stored.
3. **Read-only by default**, per account, until `mclean enable-actions`.
4. **Bulk actions preview first** and exclude protected mail, and every batch is
   written to the audit log so it can be undone.
5. **Accounts never mix.** One index file per account; no query spans two
   mailboxes.
6. **The classifier stays deterministic and offline.** No network calls, no
   model, no LLM in the classification path.
7. **The GUI stays local.** Bound to `127.0.0.1`, token-guarded, and the browser
   names a target rather than sending SQL.
8. **Nothing phones home.** No telemetry, no analytics, no crash reporting.

## Where things live

`README.md` has the layout map. In short: `providers/` is IMAP and per-provider
behaviour, `classify.py` is the heuristics, `stats.py` is every query, `actions.py`
is every mutation, `tui.py` / `web.py` + `static/` are the two front ends.

## Adding a provider

Most providers need only a registry entry in `providers/__init__.py` — host,
port, TLS mode, sign-in style, and the setup steps shown to the user. Reach for
a new backend module only when the mailbox is not folder-shaped (Gmail's labels
are the existing example). Add coverage to `tests/test_providers.py`, and make
sure `mclean providers` and the README table describe the sign-in accurately,
including exactly what the user is granting and how they revoke it.

## Pull requests

- One concern per PR, with the reasoning in the description.
- Match the surrounding style: standard library first, no new dependency unless
  it earns itself, comments only where the code is genuinely non-obvious.
- Say in the description which of the invariants above your change touches, if
  any, and how it stays inside them.
- Update `README.md` when you change behaviour a user can see.
- Never include real mail, addresses, tokens or app passwords in code, tests,
  fixtures, screenshots or issue reports.

## Releasing

Releases are tag-driven. Bump `version` in `pyproject.toml` and `__version__`
in `mailcleaner/__init__.py`, move the `Unreleased` entries into a new section
of `CHANGELOG.md`, then:

```bash
git tag v0.2.0 && git push origin v0.2.0
```

`.github/workflows/release.yml` builds the wheel, the sdist and a standalone
`mclean` for Linux, macOS and Windows (PyInstaller, `packaging/mclean.spec`),
then publishes them all on the GitHub release with a `checksums.txt` that the
installer scripts verify. To test a build without tagging, run the workflow
manually from the Actions tab.

### Signing the macOS builds

The macOS binaries are signed with a Developer ID certificate and notarized by
Apple, so people who download them through a browser are not told the app is
from an unidentified developer. Both steps are skipped when the secrets below
are missing, and the build falls back to an ad-hoc signature -- so a fork with
no Apple Developer account still produces a working release.

Notarization needs a paid Apple Developer Program membership. Set these as
repository secrets (Settings -> Secrets and variables -> Actions):

| Secret | What it is |
|---|---|
| `MACOS_CERTIFICATE` | Base64 of your *Developer ID Application* certificate exported as a `.p12` |
| `MACOS_CERTIFICATE_PASSWORD` | The password you set when exporting that `.p12` |
| `APPLE_API_KEY` | Base64 of an App Store Connect API key (`.p8`) |
| `APPLE_API_KEY_ID` | That key's Key ID |
| `APPLE_API_ISSUER_ID` | The issuer ID shown above the key list |

To produce them:

1. In Xcode (Settings -> Accounts -> Manage Certificates) or on
   [developer.apple.com](https://developer.apple.com/account/resources/certificates),
   create a **Developer ID Application** certificate.
2. In Keychain Access, export it *with its private key* as `certificate.p12`,
   then `base64 -i certificate.p12 | pbcopy` for `MACOS_CERTIFICATE`.
3. At [App Store Connect -> Users and Access -> Integrations -> Keys](https://appstoreconnect.apple.com/access/integrations/api),
   create a key with the **Developer** role. Download the `.p8` once, then
   `base64 -i AuthKey_XXXX.p8 | pbcopy` for `APPLE_API_KEY`.

The identity itself is not a secret: the workflow reads it back out of the
imported certificate. The certificate lives in a throwaway keychain that is
deleted when the job ends, whether or not it succeeded.

A bare executable cannot be stapled -- `stapler` only accepts bundles, disk
images and installer packages -- so Gatekeeper resolves the notarization ticket
online the first time someone runs a browser-downloaded binary. Nothing is
needed for the `install.sh` path, which never sets the quarantine flag.

## Reporting bugs

Open an issue with your OS, Python version, provider, and the command you ran.
Redact addresses and subjects. For anything security-sensitive, see
[SECURITY.md](SECURITY.md) instead of the issue tracker.
