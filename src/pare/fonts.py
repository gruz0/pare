"""Find a font file and measure text with it, using only the standard library."""

from __future__ import annotations

import shutil
import struct
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pare.errors import PareError

_FALLBACK_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)

# Average advance of a bold sans glyph, as a fraction of the font size. Used
# only when a font file cannot be parsed; deliberately on the wide side.
_FALLBACK_ADVANCE = 0.68


def find_font(spec: str) -> Path:
    """Resolve a font family name or a path to a font file.

    A path is used as given. A family name is looked up with fontconfig
    (``fc-match``), asking for the bold face; if fontconfig is missing, a few
    well-known locations are tried.
    """
    candidate = Path(spec).expanduser()
    if candidate.suffix.lower() in (".ttf", ".otf", ".ttc"):
        if candidate.is_file():
            return candidate
        raise PareError(f"Font file does not exist: {candidate}")
    if shutil.which("fc-match"):
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", f"{spec}:bold"],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        found = Path(result.stdout.strip())
        if result.returncode == 0 and found.is_file():
            return found
    for path in _FALLBACK_FONTS:
        if Path(path).is_file():
            return Path(path)
    raise PareError(
        f"Could not find a font for '{spec}'. Install one with "
        "'sudo apt install fonts-dejavu-core' or pass a .ttf file with --title-font."
    )


@lru_cache(maxsize=8)
def _metrics(path: Path) -> tuple[int, dict[int, int], int] | None:
    """Parse a TrueType/OpenType file into (unitsPerEm, codepoint->advance, default)."""
    try:
        data = path.read_bytes()
        base = struct.unpack_from(">I", data, 12)[0] if data[:4] == b"ttcf" else 0
        count = struct.unpack_from(">H", data, base + 4)[0]
        tables = {}
        for i in range(count):
            tag, _, offset, length = struct.unpack_from(">4sIII", data, base + 12 + 16 * i)
            tables[tag] = (offset, length)
        units = struct.unpack_from(">H", data, tables[b"head"][0] + 18)[0]
        n_metrics = struct.unpack_from(">H", data, tables[b"hhea"][0] + 34)[0]
        hmtx = tables[b"hmtx"][0]
        advances = [struct.unpack_from(">H", data, hmtx + 4 * i)[0] for i in range(n_metrics)]
        glyphs = _cmap(data, tables[b"cmap"][0])
    except (OSError, KeyError, struct.error, IndexError):
        return None
    if not units or not advances or not glyphs:
        return None
    widths = {cp: advances[min(gid, len(advances) - 1)] for cp, gid in glyphs.items()}
    return units, widths, advances[0]


def _cmap(data: bytes, cmap: int) -> dict[int, int]:
    """Read a Unicode character map (format 12 preferred, else format 4)."""
    count = struct.unpack_from(">H", data, cmap + 2)[0]
    subtables: dict[int, int] = {}
    for i in range(count):
        platform, encoding, offset = struct.unpack_from(">HHI", data, cmap + 4 + 8 * i)
        if platform == 0 or (platform == 3 and encoding in (1, 10)):
            fmt = struct.unpack_from(">H", data, cmap + offset)[0]
            subtables.setdefault(fmt, cmap + offset)
    mapping: dict[int, int] = {}
    if 12 in subtables:
        pos = subtables[12]
        groups = struct.unpack_from(">I", data, pos + 12)[0]
        for i in range(groups):
            start, end, gid = struct.unpack_from(">III", data, pos + 16 + 12 * i)
            for cp in range(start, min(end, start + 0x10000) + 1):
                mapping[cp] = gid + cp - start
    elif 4 in subtables:
        pos = subtables[4]
        seg = struct.unpack_from(">H", data, pos + 6)[0] // 2
        ends = struct.unpack_from(f">{seg}H", data, pos + 14)
        starts = struct.unpack_from(f">{seg}H", data, pos + 16 + 2 * seg)
        deltas = struct.unpack_from(f">{seg}h", data, pos + 16 + 4 * seg)
        range_pos = pos + 16 + 6 * seg
        offsets = struct.unpack_from(f">{seg}H", data, range_pos)
        for i in range(seg):
            for cp in range(starts[i], ends[i] + 1):
                if cp == 0xFFFF:
                    continue
                if offsets[i] == 0:
                    gid = (cp + deltas[i]) & 0xFFFF
                else:
                    at = range_pos + 2 * i + offsets[i] + 2 * (cp - starts[i])
                    gid = struct.unpack_from(">H", data, at)[0]
                    if gid:
                        gid = (gid + deltas[i]) & 0xFFFF
                if gid:
                    mapping[cp] = gid
    return mapping


def text_width(font: Path, text: str, size: float) -> float:
    """Width in pixels of one line of ``text`` set in ``font`` at ``size``.

    Uses the font's own advance widths (no kerning), or a conservative
    estimate when the file cannot be parsed.
    """
    metrics = _metrics(font)
    if metrics is None:
        return len(text) * size * _FALLBACK_ADVANCE
    units, widths, default = metrics
    return sum(widths.get(ord(ch), default) for ch in text) * size / units


# fontconfig weights: 180 is semibold, 200 bold.
_BOLD_WEIGHT = 200


@dataclass(frozen=True)
class FamilyCheck:
    """What fontconfig knows about a caption font family."""

    # The family is installed; libass would otherwise quietly use another font.
    found: bool
    # It has a bold face; without one libass would fake bold by smearing the outlines.
    has_bold: bool
    # The family fontconfig picks instead, when ``found`` is false.
    substitute: str = ""


def check_family(family: str) -> FamilyCheck:
    """Ask fontconfig whether ``family`` is installed and has a bold face.

    Without ``fc-match`` nothing can be checked, and the family is assumed to
    be there with a bold face, as libass is then told.
    """
    if not shutil.which("fc-match"):
        return FamilyCheck(found=True, has_bold=True)
    result = subprocess.run(
        ["fc-match", "-f", "%{family}|%{fullname}|%{weight}|%{embolden}", f"{family}:bold"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    names, fullnames, weight, embolden = [*result.stdout.strip().split("|"), "", "", ""][:4]
    known = {n.strip().casefold() for n in f"{names},{fullnames}".split(",") if n.strip()}
    if result.returncode != 0 or family.strip().casefold() not in known:
        return FamilyCheck(found=False, has_bold=True, substitute=names.split(",")[0])
    try:
        heaviest = max(int(w) for w in weight.strip("[]").split())
    except ValueError:
        heaviest = _BOLD_WEIGHT
    # For a family without a bold face fontconfig offers the regular one, marked
    # to be emboldened, and reports the weight it was asked for.
    synthetic = embolden.strip().lower() == "true"
    return FamilyCheck(found=True, has_bold=heaviest >= _BOLD_WEIGHT and not synthetic)
