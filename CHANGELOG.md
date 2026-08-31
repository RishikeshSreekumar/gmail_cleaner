# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Open-source scaffolding: MIT license, contributing guide, code of conduct,
  security policy, issue and pull-request templates, and CI across Python
  3.11-3.13 on Linux and macOS — including a check that rejects permanent-delete
  primitives in executable code.

## [0.1.0]

### Added

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
