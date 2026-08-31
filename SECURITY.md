# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report it privately through GitHub's
[Security Advisories](https://github.com/RishikeshSreekumar/gmail_cleaner/security/advisories/new)
form, or by email to rishikesh.sreekumar@mando.work.

Include what you did, what happened, and how bad you think it is. You should get
an acknowledgement within a few days. Please give a reasonable window for a fix
before disclosing publicly.

## Scope

Things worth reporting:

- Any path that permanently deletes mail, or that reaches `EXPUNGE` / `\Deleted`.
- Credentials leaking anywhere other than the OS keychain — into the index,
  logs, the audit trail, an HTTP response, or a subprocess argument.
- The local GUI being reachable from another origin, another host, or without
  its token; or a request that widens a selection beyond what the UI offers.
- Undo failing to restore what an action changed, or the audit log missing a
  mutation.
- Any outbound network call to something other than the user's own mail
  provider.

Out of scope: an attacker who already has your unlocked machine and keychain,
and issues in Proton Mail Bridge or a provider's own IMAP service.

## What this tool does with your data

- The index in `~/.mailcleaner/` holds **metadata only** — from, to, subject,
  date, list headers, size, flags and labels. No bodies, no attachments.
- Credentials live in the OS keychain via `keyring`, never in `config.json` and
  never in the index.
- The only network destination is your mail provider (and, for device-code
  sign-in, that provider's identity endpoint).
- There is no telemetry of any kind.
