"""The client deck as a PDF, drawn from the same template and the same text as the .pptx.

Why this module exists instead of converting the .pptx: a conversion needs LibreOffice or
PowerPoint, neither of which the offline Mac app (or Streamlit Cloud) can count on. A PDF is
simple enough to write directly, standard library only.

What is shared with the PowerPoint deck, so the two cannot drift:
  * the TEXT -- src/prfdeck.deck_values() gives every {{TOKEN}}'s value, and this module draws
    exactly those strings;
  * the ARTWORK -- the background photo and logo are read out of prf_deck_template.pptx;
  * the GEOMETRY -- every position, size, colour and font size below is copied from the
    template's slide XML (EMU / 12,700 = points), which is why page size is 960 x 540 pt,
    the 16:9 slide at 1/12,700.

What it is not: a pixel-for-pixel print of PowerPoint's rendering. Text uses the PDF's built-in
Helvetica (the template's theme font) so nothing is embedded, and line positions inside table cells
are computed rather than laid out by PowerPoint. Treat the .pptx as the master and this as the
version you send.
"""
from __future__ import annotations

import re
import struct
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from . import config

TEMPLATE = config.ROOT / "data" / "assets" / "prf_deck_template.pptx"
W, H = 960.0, 540.0                 # points; 12,192,000 x 6,858,000 EMU at 1/12,700
EMU = 1 / 12700

NAVY, TITLE_NAVY, RED, RED_RULE = "11315D", "003260", "B83831", "C82828"
STRIP, HEAD_FILL, BAND, GREY_TEXT = "F1F4F8", "08103A", "F0F0F0", "5B6B82"

# Advance widths (1/1000 em) of the PDF's built-in Helvetica / Helvetica-Bold, ASCII 32..126.
_HELV = (278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
         1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
         667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
         333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
         556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584)
_HELV_B = (278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
           556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
           975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
           667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
           333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
           611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584)
_OTHER = {"·": 278, "–": 556, "—": 1000}        # cp1252 characters the deck can contain


def _width(s: str, bold: bool, size: float, spc: float = 0.0) -> float:
    table = _HELV_B if bold else _HELV
    total = 0
    for ch in s:
        o = ord(ch)
        total += table[o - 32] if 32 <= o <= 126 else _OTHER.get(ch, 556)
    return total * size / 1000 + spc * len(s)


def _rgb(hex6: str) -> str:
    return " ".join(f"{int(hex6[i:i + 2], 16) / 255:.4f}" for i in (0, 2, 4))


def _pdf_text(s: str) -> bytes:
    """A PDF string literal in WinAnsi (cp1252); anything it cannot hold becomes '?'."""
    s = s.replace("−", "-")
    raw = s.encode("cp1252", errors="replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


# --------------------------------------------------------------------------- the artwork

def _jpeg_size(data: bytes) -> tuple[int, int, int]:
    """(width, height, components) from a baseline/progressive JPEG's SOF marker."""
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h, data[i + 9]
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise ValueError("not a JPEG")


@lru_cache(maxsize=4)
def _decode_png_rgba(data: bytes) -> tuple[int, int, bytes, bytes]:
    """(w, h, rgb bytes, alpha bytes) from an 8-bit RGBA, non-interlaced PNG -- the logo's format."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, ihdr = 8, [], None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        pos += 12 + n
    w, h, depth, ctype, _, _, interlace = ihdr
    if (depth, ctype, interlace) != (8, 6, 0):
        raise ValueError(f"logo PNG must be 8-bit RGBA, not interlaced (got {depth}/{ctype}/{interlace})")
    raw, stride, bpp = zlib.decompress(b"".join(idat)), w * 4, 4
    out = bytearray(h * stride)
    prev = bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    rgba = bytes(out)
    return w, h, b"".join(rgba[i:i + 3] for i in range(0, len(rgba), 4)), rgba[3::4]


# --------------------------------------------------------------------------- a tiny PDF writer

class _Pdf:
    def __init__(self) -> None:
        self.objs: list[bytes | None] = []

    def reserve(self) -> int:
        self.objs.append(None)
        return len(self.objs)

    def put(self, num: int, body: bytes) -> None:
        self.objs[num - 1] = body

    def add(self, body: bytes) -> int:
        n = self.reserve()
        self.put(n, body)
        return n

    def stream(self, header: str, data: bytes, compress: bool = True) -> int:
        if compress:
            data, header = zlib.compress(data, 9), header + " /Filter /FlateDecode"
        return self.add(f"<< {header} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream")

    def finish(self, root: int) -> bytes:
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(self.objs, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(self.objs) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        out += (f"trailer\n<< /Size {len(self.objs) + 1} /Root {root} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)


class _Page:
    """Drawing commands in top-left, point coordinates (PDF's origin is bottom-left)."""

    def __init__(self) -> None:
        self.ops: list[str] = []

    def rect(self, x, y, w, h, fill: str | None = None, stroke: str | None = None, lw=0.5) -> None:
        self.ops.append("q")
        if fill:
            self.ops.append(f"{_rgb(fill)} rg")
        if stroke:
            self.ops.append(f"{_rgb(stroke)} RG {lw} w")
        op = "B" if fill and stroke else "f" if fill else "S"
        self.ops.append(f"{x:.2f} {H - y - h:.2f} {w:.2f} {h:.2f} re {op}")
        self.ops.append("Q")

    def pill(self, x, y, w, h, fill: str) -> None:
        """A fully rounded bar (the red rules)."""
        r, k = h / 2, 0.5523 * h / 2
        X0, X1, Y0, Y1 = x, x + w, H - y - h, H - y
        self.ops.append(
            f"q {_rgb(fill)} rg {X0 + r:.2f} {Y0:.2f} m {X1 - r:.2f} {Y0:.2f} l "
            f"{X1 - r + k:.2f} {Y0:.2f} {X1:.2f} {Y0 + r - k:.2f} {X1:.2f} {Y0 + r:.2f} c "
            f"{X1:.2f} {Y0 + r + k:.2f} {X1 - r + k:.2f} {Y1:.2f} {X1 - r:.2f} {Y1:.2f} c "
            f"{X0 + r:.2f} {Y1:.2f} l "
            f"{X0 + r - k:.2f} {Y1:.2f} {X0:.2f} {Y0 + r + k:.2f} {X0:.2f} {Y0 + r:.2f} c "
            f"{X0:.2f} {Y0 + r - k:.2f} {X0 + r - k:.2f} {Y0:.2f} {X0 + r:.2f} {Y0:.2f} c f Q")

    def image(self, name: str, x, y, w, h) -> None:
        self.ops.append(f"q {w:.2f} 0 0 {h:.2f} {x:.2f} {H - y - h:.2f} cm /{name} Do Q")

    def text(self, s: str, x, base, size, color, bold=False, spc=0.0, align="l") -> None:
        """One line at baseline `base`; `x` is its left edge, or its centre when align="c"."""
        if not s:
            return
        x0 = x - _width(s, bold, size, spc) / 2 if align == "c" else x
        self.ops.append(f"q {_rgb(color)} rg BT /{'F2' if bold else 'F1'} {size} Tf {spc:.2f} Tc "
                        f"1 0 0 1 {x0:.2f} {H - base:.2f} Tm {_pdf_text(s).decode('latin-1')} Tj ET Q")

    def text_up(self, s: str, base_x, mid_y, size, color, spc=0.0) -> None:
        """A line reading bottom-to-top (the side tabs), centred on `mid_y`, baseline at `base_x`."""
        if not s:
            return
        half = _width(s, False, size, spc) / 2
        self.ops.append(f"q {_rgb(color)} rg BT /F1 {size} Tf {spc:.2f} Tc "
                        f"0 1 -1 0 {base_x:.2f} {H - mid_y - half:.2f} Tm {_pdf_text(s).decode('latin-1')} Tj ET Q")

    def data(self) -> bytes:
        return "\n".join(self.ops).encode("latin-1")


# --------------------------------------------------------------------------- the three slides

def _pt(emu: int | float) -> float:
    return emu * EMU


def _slide1(p: _Page, v: dict[str, str]) -> None:
    p.image("Bg", 0, 0, W, H)
    p.image("Logo", _pt(880610), _pt(1617419), _pt(2812182), _pt(971083))
    x = _pt(1217359)
    size, lead = int(v["FRONT_SZ"]) / 100, int(v["FRONT_LS"]) / 100
    for i, line in enumerate((f"PRF Analysis {v['DATE']}", v["AREA"])):
        p.text(line, x, _pt(3081037) + 0.8 * lead + lead * i, size, "FFFFFF", bold=True)
    p.pill(_pt(1146747), _pt(5340766), _pt(802729), _pt(57956), RED_RULE)


def _slide3(p: _Page, v: dict[str, str]) -> None:
    p.image("Bg", 0, 0, W, H)
    p.image("Logo", _pt(8185514), _pt(2754129), _pt(3546863), _pt(1224777))
    p.pill(_pt(10585250), _pt(4028293), _pt(802729), _pt(57956), RED_RULE)


COLS = [1256569, 1310115, 3114499, 2440362, 2419855]       # the template's gridCol widths, EMU
HEAD_H, FIRST_H, ROW_H = 740489, 379430, 372068
TABLE_X, TABLE_Y = 1088306, 2113547


def _cell_lines(p: _Page, lines: Sequence[tuple[str, bool]], cx, top, h, size, color, spc, lh) -> None:
    """Lines centred in a cell, vertically centred as a block."""
    block = lh * len(lines)
    first = top + (h - block) / 2
    for i, (s, bold) in enumerate(lines):
        p.text(s, cx, first + lh * i + lh / 2 + 0.35 * size, size, color, bold=bold, spc=spc, align="c")


def _slide2(p: _Page, v: dict[str, str]) -> None:
    p.rect(_pt(-91974), _pt(-132573), _pt(12375948), _pt(742558), fill=STRIP)
    p.text(f"TOP COVERAGE OPTIONS CROP YEAR {v['CROP_YEAR']}", W / 2, 30, 10, NAVY, spc=2.72, align="c")
    p.text(v["GRID_TITLE"], W / 2, 93, int(v["TITLE_SZ"]) / 100, TITLE_NAVY, bold=True, align="c")
    p.text("TOP 5 CONSISTENT AND TOP 5 RETURN", W / 2, 120.5, 10, RED, spc=2.72, align="c")
    p.text(v["BASIS"], W / 2, 139, 9, GREY_TEXT, align="c")
    p.pill(_pt(5694636), _pt(1842095), _pt(802729), _pt(57956), RED_RULE)

    xs = [_pt(TABLE_X)]
    for c in COLS:
        xs.append(xs[-1] + _pt(c))
    y = _pt(TABLE_Y)
    heads = [[("RANK", True)], [("SUCCESS", True), ("RATE", True)],
             [(f"{v['YEARS']} YEAR NET RETURN", True), ("PER ACRE (ANNUALIZED)", True)],
             [("INTERVALS UTILIZED", True)], [("INTERVAL", True), ("ALLOCATIONS BY %", True)]]
    hh = _pt(HEAD_H)
    for i, lines in enumerate(heads):
        p.rect(xs[i] + 0.75, y + 0.75, xs[i + 1] - xs[i] - 1.5, hh - 1.5, fill=HEAD_FILL)
        _cell_lines(p, lines, (xs[i] + xs[i + 1]) / 2, y, hh, 11, "FFFFFF", 3.0, 13.5)
    y += hh
    for n in range(10):
        rh = _pt(FIRST_H if n == 0 else ROW_H)
        prefix, rank = ("C", n + 1) if n < 5 else ("R", n - 4)
        fill = BAND if n % 2 == 1 else None
        cells = [[(f"Ranked #{rank}", False)],
                 [(v[f"{prefix}{rank}_WIN"], False)], [(v[f"{prefix}{rank}_NET"], False)],
                 [(ln, False) for ln in v[f"{prefix}{rank}_IV"].split("\n")],
                 [(v[f"{prefix}{rank}_AL"], False)]]
        for i, lines in enumerate(cells):
            if fill:
                p.rect(xs[i] + 0.75, y + 0.75, xs[i + 1] - xs[i] - 1.5, rh - 1.5, fill=fill)
            small = i >= 3
            _cell_lines(p, lines, (xs[i] + xs[i + 1]) / 2, y, rh, 9 if small else 10, "000000",
                        0.63 if small else 0.70, 10.8 if small else 12.0)
        y += rh

    # The two side tabs: 1,840,726 x 448,875 EMU outlined boxes rotated 270 degrees, so they stand
    # 35.3 pt wide and 144.9 pt tall left of the table, their text reading upward.
    for cy_emu, lines in ((3550316 + 448875 / 2, ("SELECTED FOR", "CONSISTENCY")),
                          (5415334 + 448875 / 2, ("SELECTED FOR", "RETURN"))):
        cx, cy = _pt(-206659 + 1840726 / 2), _pt(cy_emu)
        bw, bh = _pt(448875), _pt(1840726)
        p.rect(cx - bw / 2, cy - bh / 2, bw, bh, stroke="000000", lw=0.5)
        for i, line in enumerate(lines):          # two lines, 12 pt apart, centred on the tab
            p.text_up(line, cx + (i - 0.5) * 12 + 3.5, cy, 10, NAVY, spc=1.12)


# --------------------------------------------------------------------------- assembly

def render(values: "dict[str, str] | Sequence[dict[str, str]]", template: Path | None = None) -> bytes:
    """The deck as PDF bytes, from prfdeck.deck_values()'s token -> text mapping -- or a list of
    them for a deck of several grids (a table page each; the title page takes the first)."""
    decks = [values] if isinstance(values, dict) else list(values)
    import zipfile
    with zipfile.ZipFile(Path(template or TEMPLATE)) as z:
        bg, logo = z.read("ppt/media/image1.jpeg"), z.read("ppt/media/image1.png")
    bw, bh, ncomp = _jpeg_size(bg)
    if ncomp != 3:
        raise ValueError("template background must be an RGB JPEG")
    lw, lh, rgb, alpha = _decode_png_rgba(logo)

    pdf = _Pdf()
    catalog, pages = pdf.reserve(), pdf.reserve()
    f1 = pdf.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    f2 = pdf.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    bg_obj = pdf.stream(f"/Type /XObject /Subtype /Image /Width {bw} /Height {bh} /ColorSpace /DeviceRGB "
                        "/BitsPerComponent 8 /Filter /DCTDecode", bg, compress=False)
    mask = pdf.stream(f"/Type /XObject /Subtype /Image /Width {lw} /Height {lh} /ColorSpace /DeviceGray "
                      "/BitsPerComponent 8", alpha)
    logo_obj = pdf.stream(f"/Type /XObject /Subtype /Image /Width {lw} /Height {lh} /ColorSpace /DeviceRGB "
                          f"/BitsPerComponent 8 /SMask {mask} 0 R", rgb)
    resources = (f"<< /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> "
                 f"/XObject << /Bg {bg_obj} 0 R /Logo {logo_obj} 0 R >> >>")

    kids = []
    for draw, v in [(_slide1, decks[0])] + [(_slide2, d) for d in decks] + [(_slide3, decks[0])]:
        page = _Page()
        draw(page, v)
        content = pdf.stream("", page.data())
        kids.append(pdf.add(
            f"<< /Type /Page /Parent {pages} 0 R /MediaBox [0 0 {W:g} {H:g}] /Resources {resources} "
            f"/Contents {content} 0 R >>".encode()))
    pdf.put(pages, f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(f'{k} 0 R' for k in kids)}] >>".encode())
    pdf.put(catalog, f"<< /Type /Catalog /Pages {pages} 0 R >>".encode())
    return pdf.finish(catalog)
