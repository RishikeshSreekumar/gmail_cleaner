## Install

macOS / Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/RishikeshSreekumar/gmail_cleaner/main/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/RishikeshSreekumar/gmail_cleaner/main/install.ps1 | iex
```

Or grab a binary below and put it on your PATH. No Python needed — the
standalone builds bundle their own interpreter.

With Python 3.11+ already installed:

```sh
pipx install https://github.com/RishikeshSreekumar/gmail_cleaner/releases/download/{TAG}/mailcleaner-{VERSION}-py3-none-any.whl
```

Then:

```sh
mclean add      # connect a mailbox
mclean sync     # build the local metadata index
mclean          # open the dashboard  (or: mclean gui)
```

The macOS binaries are signed with a Developer ID certificate and notarized by
Apple, so a browser download opens without a Gatekeeper warning -- the first run
checks the notarization ticket online. If you are offline and macOS refuses to
open it, `xattr -d com.apple.quarantine ./mclean` clears the quarantine flag.
The installer script never sets that flag, so it is unaffected either way.

## Verifying a download

`checksums.txt` lists the SHA-256 of every asset; the installers check it for
you.

## Assets

| File | For |
|---|---|
| `mclean-{TAG}-macos-arm64.tar.gz` | Apple Silicon Macs |
| `mclean-{TAG}-macos-x86_64.tar.gz` | Intel Macs |
| `mclean-{TAG}-linux-x86_64.tar.gz` | Linux (x86-64) |
| `mclean-{TAG}-linux-arm64.tar.gz` | Linux (ARM64) |
| `mclean-{TAG}-windows-x86_64.zip` | Windows |
| `mailcleaner-*.whl` / `.tar.gz` | pip / pipx / uv |
