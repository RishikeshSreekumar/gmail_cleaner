#!/bin/sh
# mailcleaner installer.
#
#   curl -fsSL https://raw.githubusercontent.com/RishikeshSreekumar/gmail_cleaner/main/install.sh | sh
#
# Downloads the standalone `mclean` binary for this machine from the latest
# GitHub release, verifies its checksum, and puts it on your PATH. If no binary
# is published for this platform it falls back to installing the wheel with uv,
# pipx or a private virtualenv -- so the same command works everywhere.
#
# Environment:
#   MAILCLEANER_VERSION   tag to install (default: latest release)
#   MAILCLEANER_BIN_DIR   where to put the command (default: ~/.local/bin)
set -eu

REPO="RishikeshSreekumar/gmail_cleaner"
BIN_DIR="${MAILCLEANER_BIN_DIR:-$HOME/.local/bin}"
VERSION="${MAILCLEANER_VERSION:-}"
TMP=""

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

cleanup() { [ -n "$TMP" ] && rm -rf "$TMP"; }
trap cleanup EXIT INT TERM

detect_platform() {
    case "$(uname -s)" in
        Linux) OS=linux ;;
        Darwin) OS=macos ;;
        *) die "unsupported OS: $(uname -s). Install from source: pip install mailcleaner" ;;
    esac
    case "$(uname -m)" in
        x86_64 | amd64) ARCH=x86_64 ;;
        arm64 | aarch64) ARCH=arm64 ;;
        *) die "unsupported architecture: $(uname -m)" ;;
    esac
}

fetch() {
    # fetch URL OUTFILE
    if have curl; then
        curl -fsSL "$1" -o "$2"
    elif have wget; then
        wget -qO "$2" "$1"
    else
        die "need curl or wget"
    fi
}

fetch_stdout() {
    if have curl; then
        curl -fsSL "$1"
    elif have wget; then
        wget -qO- "$1"
    else
        die "need curl or wget"
    fi
}

resolve_version() {
    [ -n "$VERSION" ] && return 0
    VERSION=$(fetch_stdout "https://api.github.com/repos/$REPO/releases/latest" \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1) || true
    [ -n "$VERSION" ] || die "could not find the latest release; set MAILCLEANER_VERSION"
}

verify_checksum() {
    # verify_checksum FILE NAME CHECKSUMS
    expected=$(sed -n "s/^\([0-9a-f]\{64\}\)  *$2\$/\1/p" "$3" | head -n 1)
    [ -n "$expected" ] || die "no checksum published for $2"
    if have sha256sum; then
        actual=$(sha256sum "$1" | cut -d' ' -f1)
    elif have shasum; then
        actual=$(shasum -a 256 "$1" | cut -d' ' -f1)
    else
        say "warning: no sha256 tool found, skipping checksum verification"
        return 0
    fi
    [ "$actual" = "$expected" ] || die "checksum mismatch for $2"
}

install_binary() {
    asset="mclean-$VERSION-$OS-$ARCH.tar.gz"
    base="https://github.com/$REPO/releases/download/$VERSION"
    fetch "$base/$asset" "$TMP/$asset" 2>/dev/null || return 1
    fetch "$base/checksums.txt" "$TMP/checksums.txt" || return 1
    verify_checksum "$TMP/$asset" "$asset" "$TMP/checksums.txt"
    tar -xzf "$TMP/$asset" -C "$TMP"
    [ -f "$TMP/mclean" ] || die "release archive did not contain mclean"
    mkdir -p "$BIN_DIR"
    install -m 755 "$TMP/mclean" "$BIN_DIR/mclean"
    ln -sf "$BIN_DIR/mclean" "$BIN_DIR/gclean"
    METHOD="standalone binary"
}

# Falls back to the wheel when there is no binary for this platform. Needs a
# Python 3.11+ interpreter; uv brings its own, so it is tried first.
install_wheel() {
    spec="mailcleaner @ https://github.com/$REPO/releases/download/$VERSION/mailcleaner-${VERSION#v}-py3-none-any.whl"
    if have uv; then
        uv tool install --force --python 3.13 "$spec"
        METHOD="uv tool"
        BIN_DIR="$HOME/.local/bin"
        return 0
    fi
    if have pipx; then
        pipx install --force "$spec"
        METHOD="pipx"
        BIN_DIR="$HOME/.local/bin"
        return 0
    fi
    python=""
    for candidate in python3.13 python3.12 python3.11 python3; do
        if have "$candidate" && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
            python="$candidate"
            break
        fi
    done
    [ -n "$python" ] || die "need uv, pipx, or Python 3.11+ to install from the wheel"
    venv="${MAILCLEANER_HOME:-$HOME/.local/share/mailcleaner}/venv"
    "$python" -m venv "$venv"
    "$venv/bin/python" -m pip install --quiet --upgrade pip
    "$venv/bin/python" -m pip install --quiet "$spec"
    mkdir -p "$BIN_DIR"
    ln -sf "$venv/bin/mclean" "$BIN_DIR/mclean"
    ln -sf "$venv/bin/mclean" "$BIN_DIR/gclean"
    METHOD="virtualenv at $venv"
}

main() {
    detect_platform
    resolve_version
    TMP=$(mktemp -d)
    say "Installing mailcleaner $VERSION for $OS/$ARCH..."
    if ! install_binary; then
        say "No standalone build for $OS/$ARCH; installing the wheel instead."
        install_wheel
    fi
    say ""
    say "Installed mclean ($METHOD) to $BIN_DIR"
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *)
            say ""
            say "$BIN_DIR is not on your PATH. Add this to your shell profile:"
            say "    export PATH=\"$BIN_DIR:\$PATH\""
            ;;
    esac
    say ""
    say "Next:  mclean add      # connect a mailbox"
    say "       mclean sync     # build the local index"
    say "       mclean          # open the dashboard"
}

main "$@"
