#!/usr/bin/env python3
"""Build every brand asset from one definition of the mark and the wordmark.

    uv run --no-project --with fonttools --with uharfbuzz \\
        python packaging/brand/build.py --fonts DIR [--out assets/brand]

DIR must hold Geist-SemiBold.ttf and Geist-Medium.ttf (SIL Open Font License,
https://github.com/vercel/geist-font). The fonts are only read here: the name
and taglines are converted to outlines, so no asset depends on a font being
installed and no font file is shipped.

Rasters need `rsvg-convert` (librsvg), and `iconutil` (macOS) for the .icns;
the .ico is written directly.

The mark: a rounded terracotta square holding an envelope whose flap is a tick
that leaves it through the open top-right corner - mail, sorted. One line
weight, no gradients, so it survives 16px and a single colour.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

ACCENT = "#9e4421"      # oklch(0.50 0.13 40), the app's --accent
ACCENT_LIGHT = "#f0a37f"  # the tick on a dark ground
INK = "#241e1a"         # oklch(0.24 0.012 60), --ink
PAPER = "#f7f4ee"       # oklch(0.968 0.009 85), --bg
NIGHT = "#1f1915"       # oklch(0.22 0.012 60), --dark
NIGHT_TEXT = "#f3efe8"
DIM = "#6e6762"         # --dim

NAME = "mailcleaner"
TAGLINE = "See who fills your inbox. Clean it up safely."
FOOTNOTE = "Runs on your computer  ·  reads headers only  ·  never deletes"


# -- the mark -----------------------------------------------------------------


def glyph(stroke: float = 5, colour: str = "#fff", dy: float = 2.5) -> str:
    """The envelope-and-tick on a 64-unit grid.

    The top edge runs into the tick where it crosses y=20, so the two strokes
    meet in one clean join instead of two round caps touching.
    """
    attrs = (f'fill="none" stroke="{colour}" stroke-width="{stroke}" '
             'stroke-linecap="round" stroke-linejoin="round"')
    return (f'<g transform="translate(0 {dy})">'
            f'<path d="M48 31v11a3.5 3.5 0 0 1-3.5 3.5h-27A3.5 3.5 0 0 1 14 42V23.5'
            f'A3.5 3.5 0 0 1 17.5 20H40.5" {attrs}/>'
            f'<path d="M16 22.5l10.5 8.75L49.5 13" {attrs}/></g>')


def mark(stroke: float = 5, ground: str = ACCENT, colour: str = "#fff") -> str:
    return f'<rect width="64" height="64" rx="15" fill="{ground}"/>' + glyph(stroke, colour)


def svg(view_w: float, view_h: float, body: str, title: str = "mailcleaner") -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {view_w:g} {view_h:g}" '
            f'role="img" aria-label="{title}"><title>{title}</title>{body}</svg>\n')


# -- text to outlines -----------------------------------------------------------


class Face:
    """One font, shaped with HarfBuzz (kerning included) and drawn as paths."""

    def __init__(self, path: Path):
        import uharfbuzz as hb
        from fontTools.ttLib import TTFont

        self.hb = hb
        self.font = TTFont(str(path))
        self.upem = self.font["head"].unitsPerEm
        self.glyphs = self.font.getGlyphSet()
        self.order = self.font.getGlyphOrder()
        blob = hb.Blob.from_file_path(str(path))
        self.hb_font = hb.Font(hb.Face(blob))
        os2 = self.font["OS/2"]
        self.x_height = os2.sxHeight
        self.cap_height = os2.sCapHeight

    def run(self, text: str, size: float, x: float, baseline: float,
            tracking: float = 0.0, colour: str = INK) -> tuple[str, float]:
        """Paths for `text` at `size` with its baseline at y=`baseline`.

        `tracking` is in em (e.g. -0.015). Returns (svg, advance width).
        """
        from fontTools.pens.svgPathPen import SVGPathPen
        from fontTools.pens.transformPen import TransformPen

        buf = self.hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        self.hb.shape(self.hb_font, buf, {"kern": True, "liga": True})
        scale = size / self.upem
        pen_x = 0.0
        parts = []
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            name = self.order[info.codepoint]
            sp = SVGPathPen(self.glyphs)
            # font units are y-up; flip into SVG's y-down at the baseline
            tp = TransformPen(sp, (scale, 0, 0, -scale,
                                   x + (pen_x + pos.x_offset) * scale,
                                   baseline - pos.y_offset * scale))
            self.glyphs[name].draw(tp)
            if d := sp.getCommands():
                parts.append(d)
            pen_x += pos.x_advance + tracking * self.upem
        width = (pen_x - tracking * self.upem) * scale
        return f'<path fill="{colour}" d="{" ".join(parts)}"/>', width

    def width(self, text: str, size: float, tracking: float = 0.0) -> float:
        return self.run(text, size, 0, 0, tracking)[1]


# -- compositions ---------------------------------------------------------------

WORD_TRACKING = -0.02


def wordmark(face: Face, colour: str) -> tuple[str, float, float]:
    """The name alone, 100 units of em. Returns (svg body, width, height)."""
    size = 100
    asc = face.font["hhea"].ascent * size / face.upem
    body, w = face.run(NAME, size, 0, asc * 0.8, WORD_TRACKING, colour)
    # tight box: from the top of the 'l' to the baseline
    top = asc * 0.8 - 0.74 * size
    return f'<g transform="translate(0 {-top:.2f})">{body}</g>', w, 0.74 * size


def lockup(face: Face, text_colour: str, ground: str | None = None,
           mark_ground: str = ACCENT, mark_colour: str = "#fff") -> tuple[str, float, float]:
    """Mark + name, side by side. The name's x-height is 36% of the mark and its
    lowercase body sits on the mark's optical centre."""
    m = 64
    size = m * 0.36 / (face.x_height / face.upem)
    gap = m * 0.25
    centre = m / 2 + 1.5                      # the glyph sits 2.5 low in the square
    baseline = centre + face.x_height / face.upem * size / 2
    text, tw = face.run(NAME, size, m + gap, baseline, WORD_TRACKING, text_colour)
    w = m + gap + tw
    bg = f'<rect width="{w + 32:.1f}" height="{m + 32}" fill="{ground}"/>' if ground else ""
    pad = 16 if ground else 0
    body = (bg + f'<g transform="translate({pad} {pad})">'
            + mark(5, mark_ground, mark_colour) + text + "</g>")
    return body, w + 2 * pad, m + 2 * pad


def social(semi: Face, medium: Face) -> str:
    """1280x640 card for the GitHub repository preview."""
    W, H = 1280, 640
    body = [f'<rect width="{W}" height="{H}" fill="{PAPER}"/>']
    s = 2.0                                     # the mark at 128px
    body.append(f'<g transform="translate(112 126) scale({s})">{mark()}</g>')
    name, _ = semi.run(NAME, 104, 112, 376, WORD_TRACKING, INK)
    body.append(name)
    tag, _ = medium.run(TAGLINE, 40, 116, 452, -0.01, INK)
    body.append(tag)
    foot, _ = medium.run(FOOTNOTE, 26, 116, 516, 0, DIM)
    body.append(foot)
    # the same tick, large and quiet, as a background flourish on the right
    body.append(f'<g transform="translate(820 70) scale(7.4)" opacity="0.05">'
                f'{glyph(5, ACCENT, 0)}</g>')
    return svg(W, H, "".join(body), "mailcleaner - see who fills your inbox")


# -- rasters --------------------------------------------------------------------


def render(svg_path: Path, png_path: Path, w: int, h: int | None = None) -> None:
    subprocess.run(["rsvg-convert", "-w", str(w), "-h", str(h or w), str(svg_path),
                    "-o", str(png_path)], check=True)


def write_ico(pngs: dict[int, bytes], path: Path) -> None:
    """A Windows .ico holding PNG-compressed images (Vista and later)."""
    sizes = sorted(pngs)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries, data = b"", b""
    for s in sizes:
        blob = pngs[s]
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        data += blob
    path.write_bytes(header + entries + data)


def build(fonts: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    semi = Face(fonts / "Geist-SemiBold.ttf")
    medium = Face(fonts / "Geist-Medium.ttf")

    files = {
        "mark.svg": svg(64, 64, mark()),
        # heavier line for 16-32px, where 5 units would be a hairline
        "mark-small.svg": svg(64, 64, mark(6.5)),
        "mark-dark.svg": svg(64, 64, mark(5, NIGHT, ACCENT_LIGHT)),
        "glyph.svg": svg(64, 64, glyph(5, "currentColor")),
    }
    body, w, h = wordmark(semi, INK)
    files["wordmark.svg"] = svg(w, h, body)
    body, w, h = wordmark(semi, NIGHT_TEXT)
    files["wordmark-dark.svg"] = svg(w, h, body)
    body, w, h = lockup(semi, INK)
    files["lockup.svg"] = svg(w, h, body)
    body, w, h = lockup(semi, NIGHT_TEXT)
    files["lockup-dark.svg"] = svg(w, h, body)
    files["social.svg"] = social(semi, medium)
    for name, text in files.items():
        (out / name).write_text(text)

    # app icon: the macOS grid puts the body in 824 of 1024 with clear margin
    padded = svg(80, 80, f'<g transform="translate(8 8)">{mark()}</g>')
    (out / "app-icon.svg").write_text(padded)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pngs = {}
        for s in (16, 24, 32, 48, 64, 128, 256, 512, 1024):
            src = out / ("mark-small.svg" if s <= 32 else "mark.svg")
            p = out / f"icon-{s}.png"
            render(src, p, s)
            pngs[s] = p.read_bytes()
        write_ico({s: pngs[s] for s in (16, 24, 32, 48, 64, 128, 256)}, out / "mailcleaner.ico")
        for s in (16, 24, 48, 64):                  # keep the folder tidy
            (out / f"icon-{s}.png").unlink()

        render(out / "social.svg", out / "social.png", 1280, 640)
        body, w, h = lockup(semi, INK)
        render(out / "lockup.svg", out / "lockup.png", int(w * 8), int(h * 8))

        if shutil.which("iconutil"):
            iconset = tmp / "mailcleaner.iconset"
            iconset.mkdir()
            for s in (16, 32, 128, 256, 512):
                for scale in (1, 2):
                    px = s * scale
                    src = out / ("mark-small.svg" if px <= 32 else "app-icon.svg")
                    suffix = "" if scale == 1 else "@2x"
                    render(src, iconset / f"icon_{s}x{s}{suffix}.png", px)
            subprocess.run(["iconutil", "-c", "icns", str(iconset),
                            "-o", str(out / "mailcleaner.icns")], check=True)

    print("wrote", ", ".join(sorted(p.name for p in out.iterdir())))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fonts", type=Path, required=True)
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parents[2] / "assets" / "brand")
    a = ap.parse_args()
    build(a.fonts, a.out)


if __name__ == "__main__":
    main()
