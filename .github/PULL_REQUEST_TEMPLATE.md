## What this changes

<!-- and why -->

## Checklist

- [ ] `python tests/test_smoke.py`, `tests/test_web.py` and `tests/test_providers.py` all pass
- [ ] No permanent delete, no `EXPUNGE`, no `\Deleted`, no copy-then-delete
- [ ] No message bodies or attachments are fetched or stored beyond an explicit preview
- [ ] No credentials outside the OS keychain; nothing new is logged
- [ ] No new outbound network destination other than the user's mail provider
- [ ] README updated if user-visible behaviour changed
