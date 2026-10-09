# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The browser app is the product: `mclean` with no command opens it (even with
  no mailbox yet), and everything that needed a terminal is in the page —
  actions on/off, sync window, re-entering a password or signing in again,
  removing a mailbox, a connection check, keyword packs, a classifier accuracy
  check, the Assistant (Ollama or Claude, with consent), background sync,
  applying auto rules, CSV / JSON export and MCP setup. No message in the app
  tells anyone to run a command.
- Rebuilt page layout: grouped navigation, one page-header pattern, a single
  1120px content column, shared table component with consistent alignment,
  a two-column Settings page, an in-page confirmation instead of browser
  dialogs, Undo straight from the toast after an action.
- The app picks the next free port when 8765 is taken.
- The terminal dashboard moved to `mclean ui`.
- Brand: a new mark (an envelope whose flap is a tick leaving it), the name set
  as a Geist SemiBold wordmark, and a generated asset set in `assets/brand/` -
  SVG mark and lockups for light and dark, PNG sizes, `.ico` (now on the Windows
  executable), `.icns`, a social preview card and a short brand guide. Built by
  `packaging/brand/build.py`. The app header, favicon and a new welcome screen use it.

## [0.2.0] - 2026-10-09

### Added

- Compound rules (`mclean rules`): a filter over a fixed field list plus an
  archive / mark-read / label / star action. They suggest until marked auto;
  trash is never a rule action and protected mail is always excluded.
- One-click unsubscribe (RFC 8058) from the List-Unsubscribe header, with the
  link or mailto opened for senders that do not support it. Requests are https
  only, pinned to a checked public address, cookie-less, never follow
  redirects, and log only the host.
- Optional LLM advisor (`mclean llm`), off by default: Ollama locally or Claude
  via the Anthropic SDK. It proposes protect / ignore / category rules per
  sender for the user to accept, can never remove protection, and powers
  plain-English search that compiles to a validated filter.
- Read-only MCP server (`mclean mcp`).
- `--json` on the reporting commands, and `mclean export` to CSV / JSON.
- Background sync (`mclean schedule`) via launchd, systemd or Task Scheduler,
  with a digest and desktop notifications; `mclean digest`.
- Trend alerts for senders whose volume jumped, in both dashboards.
- `mclean doctor`, also run automatically when `mclean add` cannot connect, and
  an offer to open the provider's app-password page during setup.
- Classifier evaluation (`mclean eval`) that reports missed protection first.
- Keyword packs for en, in, us, uk, de, fr and es (`mclean locale`).
- Rules, Unsubscribe and Suggestions screens plus an Ask box in the browser GUI;
  Rules, Unsubscribe and Suggestions screens in the terminal dashboard.
- A Homebrew formula and winget manifest renderer for releases.
- macOS binaries are signed with a Developer ID certificate and notarized by
  Apple when the signing secrets are configured, and fall back to an ad-hoc
  signature when they are not.

### Changed

- Anyone you have sent mail to is treated as a correspondent and protected
  (mailing lists excepted).
- Unread mail with a deadline stays protected until read, unless it is marketing.
- Mail-system bounces are classified as automated, not as orders.
- Existing indexes gain the new columns in place; no re-sync is required, but
  unsubscribe options appear only for mail synced from now on (`mclean sync
  --full` fills them in for older mail).

## [0.1.0] - 2026-08-31

### Added

- Open-source scaffolding: MIT license, contributing guide, code of conduct,
  security policy, issue and pull-request templates, and CI across Python
  3.11-3.13 on Linux and macOS — including a check that rejects permanent-delete
  primitives in executable code.
- One-line installers (`install.sh`, `install.ps1`) that fetch a standalone
  `mclean` from the latest GitHub release and verify its SHA-256.
- Tag-driven release workflow publishing the wheel, the sdist and standalone
  binaries for Linux, macOS and Windows on every release.
- Multi-provider support: Gmail, Outlook.com / Microsoft 365, Proton Mail via
  Bridge, Fastmail, iCloud, Yahoo, Zoho and generic IMAP, with device-code
  sign-in where app passwords are not available.
- Multiple accounts side by side, each with its own index, rules, settings and
  audit log.
- Metadata-only SQLite index with incremental sync.
- Deterministic offline classification across category, attention and retention.
- Terminal dashboard and a local browser GUI over the same index.
- Bulk archive / trash / label / mark-read with preview, protected-mail
  exclusion, full audit log and undo.
- Automatic migration from `~/.gmail_cleaner` to `~/.mailcleaner`.
