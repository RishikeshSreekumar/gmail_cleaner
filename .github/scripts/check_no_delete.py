#!/usr/bin/env python3
"""Fail if any executable code in mailcleaner/ can permanently delete mail.

The project's first invariant is that nothing here ever issues EXPUNGE, sets
the \\Deleted flag, or emulates MOVE with copy-then-delete. Comments and
docstrings say so in those words, so this checks tokens rather than raw text:
string literals and comments are stripped before matching.
"""

from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

FORBIDDEN = re.compile(r"\bEXPUNGE\b|\bexpunge\b|\\\\?Deleted", re.IGNORECASE)
ROOT = Path(__file__).resolve().parents[2] / "mailcleaner"


def code_lines(path: Path):
    """Yield (lineno, text) for the file with comments and strings blanked out."""
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    blanked = {}
    with io.BytesIO(src.encode("utf-8")) as buf:
        for tok in tokenize.tokenize(buf.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                for n in range(tok.start[0], tok.end[0] + 1):
                    blanked[n] = ""
    for n, text in enumerate(lines, start=1):
        yield n, blanked.get(n, text)


def main() -> int:
    hits = []
    for path in sorted(ROOT.rglob("*.py")):
        for lineno, text in code_lines(path):
            if FORBIDDEN.search(text):
                hits.append(f"{path.relative_to(ROOT.parent)}:{lineno}: {text.strip()}")
    if hits:
        print("permanent-delete primitives found in executable code:")
        for hit in hits:
            print("  " + hit)
        print("\nSee CONTRIBUTING.md: this tool never permanently deletes mail.")
        return 1
    print(f"clean: no delete primitives in {ROOT.name}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
