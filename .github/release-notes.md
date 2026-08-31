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

If macOS refuses to open a binary you downloaded through the browser, clear the
quarantine flag: `xattr -d com.apple.quarantine ./mclean`. The installer script
is not affected.

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
