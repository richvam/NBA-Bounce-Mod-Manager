"""
NBA Bounce Mod Manager -- Full Courts
=====================================

One image per court: wood, apron, keys, lines, painted logos and all. This
replaces a team's tiled Floor Pattern + Court Colors look with a full-court
picture, optionally a different one for every court variant (retro logo) the
team has.

Imports nothing from app.py and never calls apply_single_mod(). The window is
full_courts_tab.py; this module is the byte work and is usable headless
(`python full_courts.py --selftest [NBA Bounce_Data]`).


HOW A COURT IS DRAWN
--------------------
Each team has two court materials in the court file (sharedassets0.assets on
the 2026-27 build), mat_bounce_stadium_parquet_<Team> and
mat_bounce_stadium_lines_<Team>, both on shd_bounce_courtLit_v2. The shader
samples _Texture_Parquet at

    uv = world_xz * _Tiling_Parquet + _Offset_Parquet

so pointing both materials at one big texture, with a tiling that makes one
tile span the court, puts a whole picture on the floor. The shader also paints
three vertex-colour zones (apron, keys, court) in flat colours; with
_GOAT_Textures_ON_OFF = 1 they become texture x colour instead, so white zone
colours let the picture through (see ZONE_PARAMS). _Court_Lines_ON_OFF = 0 on
the lines material makes the line meshes sample the same picture, so the
game's own lines vanish into it. The playing court (courtLines mesh) is
28.8 x 15.4 m.


ONE COURT PER LOGO VARIANT
--------------------------
All of a team's court variants share those two materials. When a match loads,
CourtManager.setTeam() picks a colour ID -- the selected logo's unlock ID if
the team has retro textures for it, else "default" -- and for every material
part runs each MaterialProfile in the container's m_aoCourtMaterialColors:

    material.SetColor(profile.m_sMaterialColorParam, profile.values[colorID])

The parameter name is data. _Tiling_Parquet and _Offset_Parquet are colour
properties, so adding a profile for each of them to the Parquet and Lines parts
lets every variant set its own tiling and offset. The team's texture is an
atlas -- one 2048x1024 court per row -- and each variant's offset selects its
row. No game code is changed.

Alternate logos without a retro court of their own resolve to "default" in the
game, so they share the default court; that is the game's rule, not ours.


TEXTURE RULES (each one hung the game at launch when broken)
------------------------------------------------------------
* full mip chain down to 1x1
* power-of-two dimensions
* stream offset 16-byte aligned in the .resS

Court textures are DXT1; logo textures DXT5.


WRITES
------
* First apply for a team: one verified rebuild of the court file
  (team_manager._rebuild) adds a FULLCOURT_<Team> Texture2D (a clone of the
  stock floor's object) and, for teams with variants, the two profiles.
* Everything after that is in place and size-neutral: texture headers,
  material properties, profile values. Pixels are appended to the .resS, and
  every Apply Mods run writes them again (reapply_full_courts), because that
  run rewinds the .resS to its backup.
* The centre logo is the team's selected logo sprite's texture, copied into one
  shared scene material at match load. Replacing (or blanking) the logo
  textures of a variant changes its centre logo -- and the same logo in the
  team-select panel and on the scoreboard.
* The baseline text is a 2048x256 strip per variant (txt_bounce_court_*_D in
  resources.assets), loaded by name into the letters and decorations
  materials. It's rebuilt from the text painted in the picture's apron.
"""

import os, sys, json, math, struct, shutil, copy, ntpath

import numpy as np
import UnityPy
from PIL import Image, ImageDraw, ImageFont
from UnityPy.enums import TextureFormat
from UnityPy.export.Texture2DConverter import image_to_texture2d

import app_paths
import retro_eras
import team_manager as tm

STATE_FILE = app_paths.user("full_courts.json")
IMAGE_DIR = app_paths.user("full_courts")

COURT_FILE = "sharedassets1.assets"   # sharedassets0 since the 2026-27 update


def apply_layout(layout):
    """Point at the file holding the court materials on this game build."""
    global COURT_FILE
    COURT_FILE = layout.get("court_file", COURT_FILE)


ORIGINAL_BACKUP = ".original_backup"     # app.py's BACKUP_SUFFIX
STOCK_TEXTURE_NAME = "txt_bounce_parquet01_D"
SLOT_PREFIX = "FULLCOURT_"
FIRST_SLOT_PATH_ID = 93000001
MAT_PREFIX = "mat_bounce_stadium_"
PARQUET = MAT_PREFIX + "parquet_"
LINES = MAT_PREFIX + "lines_"

# Court geometry: the courtLines mesh, not regulation NBA (28.65 x 15.24).
COURT_LEN, COURT_WID = 28.8, 15.4
TEX_W, TEX_H = 2048, 1024
MAX_ROWS = 8
# Template: 38.4 x 19.2 m at 53.3 px/m in both directions -> 4.8 m of apron
# past each baseline, 1.9 m past each sideline.
TEMPLATE_SPAN = (38.4, 19.2)
CENTRE_CIRCLE_M = 1.83
DECAL_CIRCLE_FRACTION = 1 / 3.16    # centre circle / centre-logo decal width, measured in game

# eCourtMaterialParts
PART_PARQUET, PART_LINES = 1, 2
OFFSET_PARAM, TILING_PARAM = "_Offset_Parquet", "_Tiling_Parquet"

LOGO_MODES = ("replace", "hide", "keep")
# Baseline text: the team-name strip the game draws on the apron
# (txt_bounce_court_<Team>_D[_retroLogo_n_team_m] in resources.assets, loaded by
# name into the letters material, one per court variant).
STRIP_MODES = ("picture", "none", "keep")
STRIP_FILE = "resources.assets"
STRIP_PREFIX = "txt_bounce_court_"
STRIP_SIZE = (2048, 256)
STRIP_BIG = (0, 0, 1580, 256)          # the long team name
STRIP_SMALL = (1600, 0, 2040, 118)     # the stacked name above the NBA logo
LOGO_SOURCES = ("court", "file")

DXT1_2048x1024 = 1398120
DXT5_1024 = 1398128


class FullCourtError(Exception):
    """Anything the user should see as a sentence rather than a traceback."""


# ===========================================================================
# Images
# ===========================================================================
def mipchain(img, fmt):
    """Full mip chain down to 1x1 (the loader rejects one that stops at 4x4).
    Returns (bytes, mip count). image_to_texture2d does the bottom-up flip."""
    out, cur, mips = [], img.convert("RGBA"), 0
    while True:
        data, _ = image_to_texture2d(cur, fmt)
        out.append(data)
        mips += 1
        w, h = cur.size
        if w == 1 and h == 1:
            return b"".join(out), mips
        cur = cur.resize((max(1, w // 2), max(1, h // 2)), Image.LANCZOS)


def expected_size(w, h, block_bytes):
    """Byte size of a full DXT mip chain (8 = DXT1, 16 = DXT5)."""
    total = 0
    while True:
        total += max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * block_bytes
        if w == 1 and h == 1:
            return total
        w, h = max(1, w // 2), max(1, h // 2)


def _pow2(n):
    return n > 0 and n & (n - 1) == 0


def template_rect(size=(TEX_W, TEX_H)):
    """Court rectangle (x0, y0, x1, y1) in a template-geometry image."""
    w, h = size
    sx, sy = w / TEMPLATE_SPAN[0], h / TEMPLATE_SPAN[1]
    mx, my = (TEMPLATE_SPAN[0] - COURT_LEN) / 2, (TEMPLATE_SPAN[1] - COURT_WID) / 2
    return (round(mx * sx), round(my * sy), round(w - mx * sx), round(h - my * sy))


def make_template(size=(TEX_W, TEX_H)):
    """2048x1024 court template: wood court with NBA markings scaled to the
    game's 28.8 x 15.4 m court, the apron around it, and labels saying where
    the game puts things. Paint over it and import it as-is."""
    w, h = size
    img = Image.new("RGB", size, (38, 52, 84))
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = template_rect(size)
    d.rectangle([x0, y0, x1, y1], fill=(214, 168, 116))
    ppm = (x1 - x0) / COURT_LEN
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    sx, sy = COURT_LEN / 28.65, COURT_WID / 15.24     # NBA markings -> game court

    def P(x, z):
        return cx + x * sx * ppm, cy - z * sy * ppm

    def circle(x, z, r, **kw):
        (a, b), (c, e) = P(x - r, z + r), P(x + r, z - r)
        d.ellipse([a, b, c, e], **kw)

    def arc(x, z, r, start, end, width):
        (a, b), (c, e) = P(x - r, z + r), P(x + r, z - r)
        d.arc([a, b, c, e], start, end, fill="white", width=width)

    lw = 5
    for s in (-1, 1):
        hoop = s * 12.75
        ft = s * (14.325 - 5.79)
        # key
        ka, kb = P(s * 14.325, 2.44), P(ft, -2.44)
        d.rectangle([min(ka[0], kb[0]), ka[1], max(ka[0], kb[0]), kb[1]],
                    fill=(170, 48, 48), outline="white", width=lw)
        # free-throw circle, three-point line, restricted arc
        if s > 0:
            arc(ft, 0, 1.83, 90, 270, lw)
            arc(hoop, 0, 7.24, 112, 248, lw)
            arc(hoop, 0, 1.22, 90, 270, lw)
        else:
            arc(ft, 0, 1.83, 270, 90, lw)
            arc(hoop, 0, 7.24, 292, 68, lw)
            arc(hoop, 0, 1.22, 270, 90, lw)
        corner = s * (12.75 - math.sqrt(7.24 ** 2 - 6.71 ** 2))
        for z in (-6.71, 6.71):
            d.line([P(s * 14.325, z), P(corner, z)], fill="white", width=lw)
        circle(hoop, 0, 0.23, outline=(255, 140, 0), width=4)
    d.line([P(0, 7.62), P(0, -7.62)], fill="white", width=lw)
    circle(0, 0, CENTRE_CIRCLE_M, outline="white", width=lw)
    d.rectangle([x0, y0, x1, y1], outline="white", width=lw + 2)
    try:
        font = ImageFont.truetype("arialbd.ttf", 26)
        small = ImageFont.truetype("arial.ttf", 20)
    except OSError:
        font = small = ImageFont.load_default()
    d.text((20, 18), "NBA BOUNCE FULL COURT TEMPLATE  2048 x 1024", fill="white", font=font)
    d.text((20, h - 40), "Court: 28.8 x 15.4 m (the game's court lines).  "
           "Apron: keep it painted to the image edge -- the floor repeats past it.",
           fill=(200, 210, 230), font=small)
    d.text((x0 + 10, y0 + 10), "FAR SIDELINE", fill=(90, 60, 30), font=small)
    d.text((x0 + 10, y1 - 32), "NEAR SIDELINE (camera side)", fill=(90, 60, 30), font=small)
    d.text((cx + 40, cy - 12), "centre logo", fill=(90, 60, 30), font=small)
    return img


def _longest_run(idx, gap=6):
    """(first, last) of the longest run of indices with no hole wider than
    `gap` -- so a stray wood-coloured column at the image edge is ignored."""
    best, start, prev = (int(idx[0]), int(idx[0])), int(idx[0]), int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev > gap:
            start = i
        prev = i
        if prev - start > best[1] - best[0]:
            best = (start, prev)
    return best


def detect_court_rect(img):
    """Find the playing court in an arbitrary court image: the bounding box of
    the hardwood, +/- 2 px for the boundary line. Falls back to the template's
    geometry (scaled) when there's no clear wood rectangle."""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    # Light maple through darker parquet: warm, fairly bright, not saturated red.
    wood = (r > 150) & (g > 95) & (b < 200) & (r - b > 40) & (r - g < 90) & (g - b > 20)
    h, w = wood.shape
    # Columns first, then rows measured only across the court's columns, so
    # the rows through the keys and centre logo still count as court.
    cols = np.where(wood.mean(axis=0) >= 0.30)[0]
    if len(cols) >= w * 0.3:
        x0, x1 = _longest_run(cols)
        rows = np.where(wood[:, x0:x1 + 1].mean(axis=1) >= 0.30)[0]
    else:
        rows = []
    if len(rows) >= h * 0.3 and len(cols) >= w * 0.3:
        y0, y1 = _longest_run(rows)
        # Grow out across the boundary line: rows/columns that are mostly
        # bright paint (any colour, as long as it isn't the wood).
        line = (a.min(axis=2) > 170) & ~wood
        for _ in range(14):
            if x0 > 0 and line[y0:y1, x0 - 1].mean() > 0.5: x0 -= 1
            if x1 < w - 1 and line[y0:y1, x1 + 1].mean() > 0.5: x1 += 1
            if y0 > 0 and line[y0 - 1, x0:x1].mean() > 0.5: y0 -= 1
            if y1 < h - 1 and line[y1 + 1, x0:x1].mean() > 0.5: y1 += 1
        x0, x1 = max(0, x0 - 2), min(w - 1, x1 + 2)
        y0, y1 = max(0, y0 - 2), min(h - 1, y1 + 2)
        # A court is about 1.87:1; anything wildly off is a misdetection.
        ratio = (x1 - x0) / max(1, y1 - y0)
        if 1.4 < ratio < 2.4:
            return (int(x0), int(y0), int(x1), int(y1)), True
    return template_rect((w, h)), False


def court_mapping(src_size, rect):
    """(tiling, offset) that put `rect` (pixels in an image of src_size) on the
    game's court. Resizing the image changes neither: tiling is per metre."""
    W, H = src_size
    x0, y0, x1, y1 = rect
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise FullCourtError("The court rectangle is too small.")
    tiling = ((x1 - x0) / (W * COURT_LEN), (y1 - y0) / (H * COURT_WID))
    # u runs left to right; v is bottom-up in the texture, so the court
    # centre's row is measured from the bottom.
    offset = ((x0 + x1) / 2 / W, 1.0 - (y0 + y1) / 2 / H)
    return tiling, offset


def atlas_mapping(tiling, offset, row, rows):
    """The same mapping when the court is row `row` (from the top) of an atlas
    `rows` courts tall."""
    return (tiling[0], tiling[1] / rows), (offset[0], (offset[1] + rows - 1 - row) / rows)


def cut_centre_logo(img, rect, scale=1.0):
    """The centre circle of a court image as a round RGBA logo."""
    x0, y0, x1, y1 = rect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    r = CENTRE_CIRCLE_M * scale * (x1 - x0) / COURT_LEN
    box = tuple(int(round(v)) for v in (cx - r, cy - r, cx + r, cy + r))
    crop = img.convert("RGBA").crop(box)
    n = crop.size[0]
    yy, xx = np.mgrid[0:crop.size[1], 0:n]
    dist = np.hypot(xx - (n - 1) / 2, yy - (crop.size[1] - 1) / 2)
    alpha = np.clip((n / 2 - dist) / 2.0, 0, 1) * 255
    a = np.asarray(crop).copy()
    a[..., 3] = (a[..., 3] * (alpha / 255)).astype(np.uint8)
    return Image.fromarray(a, "RGBA")


def extract_baseline_text(img, rect):
    """The text painted in the apron past the left baseline of a court image,
    keyed off the apron colour and turned to read left to right. RGBA, or
    None when there's nothing there."""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    x0, y0, x1, y1 = (int(v) for v in rect)
    right = x0 - 8
    if right < 12:
        return None
    band = a[y0:y1, :right]
    bg = np.median(band.reshape(-1, 3), axis=0)
    dist = np.abs(band - bg).max(axis=2)
    ink = dist > 60
    frac = ink.mean(axis=0)
    # Nearest run of inked columns to the court, ending at a clear gap (so
    # bench boxes and other apron furniture further out are left behind).
    cols, gap, started = [], 0, False
    for x in range(right - 1, -1, -1):
        if frac[x] > 0.05:
            cols.append(x)
            started, gap = True, 0
        elif started:
            gap += 1
            if gap >= 8:
                break
    if len(cols) < 6:
        return None
    cx0, cx1 = min(cols), max(cols)
    # Rows with real ink (3+ px), longest run: drops tick marks at the ends.
    rows = np.where(ink[:, cx0:cx1 + 1].sum(axis=1) >= 3)[0]
    if len(rows) < 20:
        return None
    ry0, ry1 = _longest_run(rows, gap=120)
    crop = a[y0 + ry0:y0 + ry1 + 1, cx0:cx1 + 1].astype(np.uint8)
    d = dist[ry0:ry1 + 1, cx0:cx1 + 1]
    alpha = (np.clip((d - 35) / 50.0, 0, 1) * 255).astype(np.uint8)
    rgba = np.dstack([crop, alpha])
    # Left-apron text reads bottom to top; a clockwise turn lays it flat.
    return Image.fromarray(rgba, "RGBA").rotate(-90, expand=True)


def build_strip(text_img, original=None):
    """A 2048x256 baseline strip: the text across the long name area and,
    smaller, above the NBA logo, which is kept from the original strip."""
    strip = Image.new("RGBA", STRIP_SIZE, (0, 0, 0, 0))
    if original is not None:
        keep = original.convert("RGBA").resize(STRIP_SIZE)
        strip.paste(keep.crop((STRIP_SMALL[0] + 40, STRIP_SMALL[3], 2048, 256)),
                    (STRIP_SMALL[0] + 40, STRIP_SMALL[3]))
    if text_img is not None:
        for box, pad in ((STRIP_BIG, 14), (STRIP_SMALL, 6)):
            w, h = box[2] - box[0] - 2 * pad, box[3] - box[1] - 2 * pad
            k = min(w / text_img.width, h / text_img.height)
            t = text_img.resize((max(1, round(text_img.width * k)),
                                 max(1, round(text_img.height * k))), Image.LANCZOS)
            strip.alpha_composite(t, (box[0] + (box[2] - box[0] - t.width) // 2,
                                      box[1] + (box[3] - box[1] - t.height) // 2))
    return strip


def strip_variant(name):
    """Variant key a txt_bounce_court_* texture belongs to ("default" for the
    plain one)."""
    import re
    m = re.search(r"[Ll]ogo_?(\d+)_?team_?(\d+)", name)
    return f"LOGO_{m.group(1)}_TEAM_{m.group(2)}" if m else "default"


def fit_logo(img, size):
    """A logo centred, aspect kept, on a transparent canvas of `size`."""
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    im = img.convert("RGBA")
    k = min(size[0] / im.width, size[1] / im.height)
    im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    canvas.paste(im, ((size[0] - im.width) // 2, (size[1] - im.height) // 2), im)
    return canvas


# ===========================================================================
# Container profiles (m_aoCourtMaterialColors)
# ===========================================================================
def _i32(raw, off):
    return struct.unpack_from("<i", raw, off)[0]


def _str(raw, off, limit=96):
    n = _i32(raw, off)
    if not (1 <= n <= limit) or off + 4 + n > len(raw):
        return None, off
    b = raw[off + 4:off + 4 + n]
    if not all(32 <= c < 127 for c in b):
        return None, off
    return b.decode("ascii"), (off + 4 + n + 3) & ~3


def _wstr(text):
    b = text.encode("ascii")
    return struct.pack("<i", len(b)) + b + b"\0" * (-len(b) % 4)


def _try_profiles(raw, p):
    n = _i32(raw, p)
    if not 1 <= n <= 12:
        return None
    off, pairs, keys = p + 4, [], set()
    for _ in range(n):
        if off + 8 > len(raw):
            return None
        key, cnt = struct.unpack_from("<ii", raw, off)
        if not (0 <= key <= 15) or key in keys or not (0 <= cnt <= 32):
            return None
        keys.add(key)
        off += 8
        profs = []
        for _ in range(cnt):
            param, off2 = _str(raw, off, 64)
            if not param or not param.startswith("_"):
                return None
            off = off2
            if off + 4 > len(raw):
                return None
            nv = _i32(raw, off)
            if not 0 <= nv <= 64:
                return None
            off += 4
            vals = []
            for _ in range(nv):
                vkey, off2 = _str(raw, off, 64)
                if vkey is None or off2 + 16 > len(raw):
                    return None
                vals.append([vkey, list(struct.unpack_from("<4f", raw, off2)), off2])
                off = off2 + 16
            profs.append({"param": param, "values": vals})
        pairs.append({"key": key, "profiles": profs})
    if not any(pr["param"].startswith("_Color_") for pa in pairs for pr in pa["profiles"]):
        return None
    return {"start": p, "end": off, "pairs": pairs}


def parse_profiles(raw):
    """The container's m_aoCourtMaterialColors, or None. Each value carries its
    byte offset in `raw`, so it can be rewritten in place. Validated by
    re-serializing it to the exact original bytes."""
    for p in range(28, len(raw) - 8, 4):
        d = _try_profiles(raw, p)
        if d and serialize_profiles(d) == raw[d["start"]:d["end"]]:
            return d
    return None


def serialize_profiles(d):
    out = [struct.pack("<i", len(d["pairs"]))]
    for pa in d["pairs"]:
        out.append(struct.pack("<ii", pa["key"], len(pa["profiles"])))
        for pr in pa["profiles"]:
            out.append(_wstr(pr["param"]) + struct.pack("<i", len(pr["values"])))
            for vkey, rgba, _off in pr["values"]:
                out.append(_wstr(vkey) + struct.pack("<4f", *rgba))
    return b"".join(out)


def profile_variants(d):
    """Court variant keys a container's profiles know, "default" first."""
    seen = []
    for pa in (d or {}).get("pairs", []):
        for pr in pa["profiles"]:
            for vkey, _rgba, _off in pr["values"]:
                if vkey not in seen:
                    seen.append(vkey)
    return ["default"] + [v for v in seen if v != "default"]


def _needs_profiles(d, variants):
    for part in (PART_PARQUET, PART_LINES):
        pa = next((x for x in d["pairs"] if x["key"] == part), None)
        for param in (TILING_PARAM, OFFSET_PARAM):
            pr = pa and next((x for x in pa["profiles"] if x["param"] == param), None)
            if not pr or {v[0] for v in pr["values"]} != set(variants):
                return True
    return False


def with_profiles(raw, d, variants, values=None):
    """raw with a _Tiling_Parquet and an _Offset_Parquet profile, carrying
    every variant key, on the Parquet and Lines parts. `values` gives
    {param: rgba} to start them at."""
    d = copy.deepcopy(d)
    values = values or {}
    for part in (PART_PARQUET, PART_LINES):
        pa = next((x for x in d["pairs"] if x["key"] == part), None)
        if pa is None:
            pa = {"key": part, "profiles": []}
            d["pairs"].append(pa)
        for param in (TILING_PARAM, OFFSET_PARAM):
            pr = next((x for x in pa["profiles"] if x["param"] == param), None)
            if pr is None:
                pr = {"param": param, "values": []}
                pa["profiles"].append(pr)
            have = {v[0]: v for v in pr["values"]}
            start = list(values.get(param, (0.0, 0.0, 0.0, 0.0)))
            pr["values"] = [have.get(v, [v, list(start), 0]) for v in variants]
    return raw[:d["start"]] + serialize_profiles(d) + raw[d["end"]:]


def _profile_value_offsets(d, variants):
    """{(part, param, variant): offset of its 16-byte value}."""
    out = {}
    for pa in d["pairs"]:
        if pa["key"] not in (PART_PARQUET, PART_LINES):
            continue
        for pr in pa["profiles"]:
            if pr["param"] in (TILING_PARAM, OFFSET_PARAM):
                for vkey, _rgba, off in pr["values"]:
                    out[(pa["key"], pr["param"], vkey)] = off
    return out


def _logo_entries(raw, catalog):
    """[(unlock_id, sprite_path_id, sprite_name)] for the container's TeamLogos:
    m_sName, m_sUnlockID, then the m_oLogo PPtr."""
    out = []
    for o, pid, t, nm in tm._pointers(raw, catalog):
        if t != "Sprite" or nm.endswith("_Icon"):
            continue
        for back in range(8, 200, 4):
            if o - back < 0:
                break
            s1, e1 = _str(raw, o - back)
            if s1 is None or e1 > o:
                continue
            n2 = _i32(raw, e1)
            if n2 == 0 and e1 + 4 == o:
                out.append(("", pid, nm))
                break
            s2, e2 = _str(raw, e1)
            if s2 is not None and e2 == o:
                out.append((s2, pid, nm))
                break
    return out


# ===========================================================================
# Discovery
# ===========================================================================
def _court_path(game_path):
    return os.path.join(game_path, COURT_FILE)


def _props(seq):
    """A typetree property list ([(name, value)]) as a dict view."""
    return {k: v for k, v in (tuple(x) for x in seq)}


def _set_prop(seq, key, value):
    for i, item in enumerate(seq):
        if tuple(item)[0] == key:
            seq[i] = (key, value)
            return
    raise FullCourtError(f"The court material has no {key} property.")


def discover(game_path):
    """Every court that can take a full-court image.

    {token: {"token", "display", "containers": [{"name", "path_id",
     "byte_start", "has_profiles"}], "variants": [keys], "labels": {key: text},
     "logos": {variant: [{"sprite", "texture_pid", "texture"}]},
     "materials": {"parquet": pid, "lines": pid}, "slot_pid": pid or None,
     "shared_logos": [texture names other courts also use]}}
    """
    if os.path.normcase(tm.CONTAINER_ASSETS_FILE) != os.path.normcase(COURT_FILE):
        raise FullCourtError("On this game build the team containers and the court "
                             "materials are in different files, which Full Courts "
                             "doesn't support.")
    path = _court_path(game_path)
    if not os.path.isfile(path):
        raise FullCourtError(f"{COURT_FILE} is not in the game folder.")
    containers = tm._read_containers(game_path, tm.find_container_script(game_path))
    catalog = tm._object_catalog(game_path)

    materials, slots, sprite_tex, tex_names = {}, {}, {}, {}
    env = UnityPy.load(path)
    try:
        wanted_sprites = set()
        for _n, c in containers.items():
            for _u, spid, _nm in _logo_entries(c["raw"], catalog):
                wanted_sprites.add(spid)
        for o in env.objects:
            tn = o.type.name
            if tn not in ("Material", "Texture2D", "Sprite"):
                continue
            if tn == "Sprite":
                if o.path_id in wanted_sprites:
                    d = o.read_typetree()
                    sprite_tex[o.path_id] = int(d["m_RD"]["texture"]["m_PathID"])
                continue
            try:
                name = o.peek_name()
            except Exception:
                continue
            if tn == "Material" and name.startswith((PARQUET, LINES)):
                materials[name] = int(o.path_id)
            elif tn == "Texture2D":
                tex_names[int(o.path_id)] = name
                if name.startswith(SLOT_PREFIX):
                    slots[name] = int(o.path_id)
    finally:
        tm.close_env(env)

    courts, tex_users = {}, {}
    for cname, c in sorted(containers.items()):
        raw = c["raw"]
        # GOAT-mode teams borrow an NBA team's court materials; their logos
        # and colour data are their own business.
        if "goat" in cname.lower():
            continue
        ptr_names = [nm for _o, _p, t, nm in tm._pointers(raw, catalog) if t == "Material"]
        parquet = next((n for n in ptr_names if n.startswith(PARQUET)), None)
        lines = next((n for n in ptr_names if n.startswith(LINES)), None)
        if not parquet or not lines or parquet not in materials or lines not in materials:
            continue
        token = parquet[len(PARQUET):]
        prof = parse_profiles(raw)
        entry = courts.setdefault(token, {
            "token": token, "display": cname.split("_", 1)[-1], "containers": [],
            "variants": ["default"], "labels": {}, "logos": {},
            "materials": {"parquet": materials[parquet],
                          "lines": materials.get(LINES + token, materials[lines])},
            "slot_pid": slots.get(SLOT_PREFIX + token), "shared_logos": [],
            "white_pid": slots.get(WHITE_SLOT),
            "strips": {}})
        for _o, text, *_rest in tm._strings_with_fit(raw):
            if text.startswith(STRIP_PREFIX):
                lst = entry["strips"].setdefault(strip_variant(text), [])
                if text not in lst:
                    lst.append(text)
        entry["containers"].append({"name": cname, "path_id": c["path_id"],
                                    "byte_start": c["byte_start"],
                                    "has_profiles": prof is not None})
        variants = profile_variants(prof) if prof else ["default"]
        for v in variants:
            if v not in entry["variants"]:
                entry["variants"].append(v)
        for v in entry["variants"]:
            try:
                entry["labels"][v] = retro_eras.display_name(cname, v)
            except Exception:
                entry["labels"][v] = "Default court" if v == "default" else v
        for unlock, spid, sname in _logo_entries(raw, catalog):
            v = unlock if unlock in entry["variants"] else "default"
            tpid = sprite_tex.get(spid)
            if tpid is None:
                continue
            lst = entry["logos"].setdefault(v, [])
            if all(x["texture_pid"] != tpid for x in lst):
                lst.append({"sprite": sname, "texture_pid": tpid,
                            "texture": tex_names.get(tpid, str(tpid))})
            tex_users.setdefault(tpid, set()).add(token)
    for token, e in courts.items():
        for lst in e["logos"].values():
            for x in lst:
                if len(tex_users.get(x["texture_pid"], ())) > 1:
                    e["shared_logos"].append(x["texture"])
    return courts


# ===========================================================================
# State
# ===========================================================================
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
            s.setdefault("teams", {})
            return s
    except (OSError, ValueError):
        return {"teams": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def team_dir(token):
    p = os.path.join(IMAGE_DIR, token)
    os.makedirs(p, exist_ok=True)
    return p


def store_court_image(token, variant, src_path):
    """Copy an imported court picture into user/full_courts/<team>/.
    Returns (stored path, size)."""
    img = Image.open(src_path)
    img.load()
    img = img.convert("RGB")
    dest = os.path.join(team_dir(token), f"court_{variant}.png")
    img.save(dest)
    return dest, img.size


def store_logo_image(token, variant, src_path):
    img = Image.open(src_path)
    img.load()
    dest = os.path.join(team_dir(token), f"logo_{variant}.png")
    img.convert("RGBA").save(dest)
    return dest


def active_tokens():
    """Teams the user has a full court applied for. Floor Patterns and Court
    Colors leave these alone."""
    return {t for t, e in load_state()["teams"].items() if e.get("applied")}


# ===========================================================================
# Building a team's pixels
# ===========================================================================
def _variant_plan(team, cfg):
    """Rows of the atlas and each variant's (row, tiling, offset). Variants
    without their own picture use the default court's, or the first one set."""
    vcfg = cfg.get("variants", {})
    have = [v for v in team["variants"] if vcfg.get(v, {}).get("image")]
    if not have:
        raise FullCourtError(f"{team['display']}: no court picture chosen.")
    fallback = "default" if "default" in have else have[0]
    images, rows, mapping = [], {}, {}
    for v in team["variants"]:
        src = v if v in have else fallback
        key = vcfg[src]["image"]
        if key not in rows:
            rows[key] = len(images)
            images.append(vcfg[src])
        mapping[v] = (rows[key], vcfg[src])
    n = 1
    while n < len(images):
        n *= 2
    if n > MAX_ROWS:
        raise FullCourtError(f"{team['display']}: at most {MAX_ROWS} different "
                             f"court pictures per team.")
    out = {}
    for v, (row, c) in mapping.items():
        tiling, offset = court_mapping(tuple(c["size"]), tuple(c["rect"]))
        out[v] = (row,) + atlas_mapping(tiling, offset, row, n)
    return images, n, out


def build_atlas(images, rows):
    atlas = Image.new("RGB", (TEX_W, TEX_H * rows), (0, 0, 0))
    for i, c in enumerate(images):
        if not os.path.isfile(c["image"]):
            raise FullCourtError(f"Court picture missing: {c['image']}")
        im = Image.open(c["image"]).convert("RGB").resize((TEX_W, TEX_H), Image.LANCZOS)
        atlas.paste(im, (0, i * TEX_H))
    # Rows past the last picture repeat it, so mip bleed has nothing odd to mix in.
    for i in range(len(images), rows):
        atlas.paste(atlas.crop((0, (len(images) - 1) * TEX_H, TEX_W, len(images) * TEX_H)),
                    (0, i * TEX_H))
    return atlas


def variant_picture(cfg, variant):
    """The picture config a variant shows: its own, else the default
    court's, else the first one set -- the same rule _variant_plan uses."""
    vs = cfg.get("variants", {})
    for vc in (vs.get(variant), vs.get("default")):
        if vc and vc.get("image"):
            return vc
    return next((x for x in vs.values() if x.get("image")), None)


def strip_image_for(cfg, variant, original=None):
    """The baseline strip a variant gets, or None for 'keep'."""
    mode = (cfg.get("strips", {}).get(variant) or {}).get("mode", "picture")
    if mode == "keep":
        return None
    text = None
    if mode == "picture":
        vc = variant_picture(cfg, variant)
        if vc and os.path.isfile(vc["image"]):
            text = extract_baseline_text(Image.open(vc["image"]), tuple(vc["rect"]))
    return build_strip(text, original)


def logo_image_for(team, cfg, variant, size):
    """RGBA logo for a variant per its mode, or None for 'keep'."""
    lc = cfg.get("logos", {}).get(variant) or {"mode": "keep"}
    mode = lc.get("mode", "keep")
    if mode == "keep":
        return None
    if mode == "hide":
        return Image.new("RGBA", size, (0, 0, 0, 0))
    if lc.get("source") == "file" and lc.get("image") and os.path.isfile(lc["image"]):
        return fit_logo(Image.open(lc["image"]), size)
    vc = variant_picture(cfg, variant)
    if not vc:
        return None
    src = Image.open(vc["image"])
    # The centre decal draws the whole texture about 3.2x the centre circle's
    # width, so the cut goes in at that fraction: on the floor it then sits
    # exactly over the logo painted in the picture.
    inner = tuple(max(1, round(v * DECAL_CIRCLE_FRACTION)) for v in size)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    cut = fit_logo(cut_centre_logo(src, tuple(vc["rect"])), inner)
    canvas.paste(cut, ((size[0] - inner[0]) // 2, (size[1] - inner[1]) // 2), cut)
    return canvas


# ===========================================================================
# Writing
# ===========================================================================
def _ensure_original_backup(assets_path):
    """Same rule as app.ensure_backup(): copy the file and its companions to
    .original_backup once, never overwriting."""
    for p in [assets_path] + [assets_path + c for c in (".resS", ".resource")]:
        if os.path.exists(p) and not os.path.exists(p + ORIGINAL_BACKUP):
            shutil.copy2(p, p + ORIGINAL_BACKUP)


def _append_pixels(ress, data, ress_reset):
    """Append to the .resS on a 16-byte boundary; returns the offset.
    ress_reset follows apply_single_mod()'s rule: rewind to the backup the
    first time a run touches it. None = standalone apply, never rewind."""
    if ress_reset is not None:
        key = os.path.abspath(ress)
        if key not in ress_reset:
            if os.path.exists(ress + ORIGINAL_BACKUP):
                shutil.copy2(ress + ORIGINAL_BACKUP, ress)
            ress_reset.add(key)
    end = os.path.getsize(ress)
    pad = -end % 16
    with open(ress, "ab") as f:
        f.write(b"\0" * pad + data)
    return end + pad


def _patch_objects(path, edits):
    """edits: {path_id: fn(typetree dict) -> None}. Re-serializes each object
    and splices it in place, refusing any that would change size."""
    from UnityPy.helpers.TypeTreeHelper import write_typetree
    from UnityPy.streams.EndianBinaryWriter import EndianBinaryWriter
    patches = []
    env = UnityPy.load(path)
    try:
        objs = {o.path_id: o for o in env.objects if o.path_id in edits}
        for pid, fn in edits.items():
            o = objs.get(pid)
            if o is None:
                raise FullCourtError(f"Object {pid} is missing from {COURT_FILE}.")
            d = o.read_typetree()
            fn(d)
            w = EndianBinaryWriter(endian=o.reader.endian)
            write_typetree(d, o._get_typetree_node(), w, o.assets_file)
            if len(w.bytes) != o.byte_size:
                raise FullCourtError(f"{d.get('m_Name', pid)} would change size; "
                                     f"nothing was written.")
            patches.append((o.byte_start, w.bytes))
    finally:
        tm.close_env(env)
    with open(path, "r+b") as f:
        for start, blob in patches:
            f.seek(start)
            f.write(blob)


def _read_objects(path, pids):
    env = UnityPy.load(path)
    try:
        return {o.path_id: o.read_typetree() for o in env.objects if o.path_id in pids}
    finally:
        tm.close_env(env)


def _ress_for(path, tex):
    return os.path.join(os.path.dirname(path), ntpath.basename(str(tex["m_StreamData"]["path"])))


def _texture_header(d):
    return {k: d[k] for k in ("m_Width", "m_Height", "m_TextureFormat", "m_MipCount",
                              "m_CompleteImageSize")} | {
        "stream": dict(d["m_StreamData"])}


def _set_texture_header(d, h):
    for k in ("m_Width", "m_Height", "m_TextureFormat", "m_MipCount", "m_CompleteImageSize"):
        d[k] = h[k]
    d["m_StreamData"]["offset"] = h["stream"]["offset"]
    d["m_StreamData"]["size"] = h["stream"]["size"]


# shd_bounce_courtLit_v2 paints three vertex-colour zones over the floor:
# R = apron, G = keys, B = court. Normally each is a flat colour. With
# _GOAT_Textures_ON_OFF the zones become texture x colour -- the court zones
# sample _Texture_Parquet, the apron _OutsideTexture (on the parquet mapping
# when _Outside_Texture_Tiling_ON_OFF is off) -- so white zone colours show the
# picture -- except the apron, whose _OutsideTexture uses a mapping of its own
# with no offset, so it can't line up with the picture. The apron therefore
# gets a small white texture and, per variant, the picture's apron colour.
# (_ONLY_PARQUET, which the PoC set, is not a property of
# this shader and does nothing.) The game re-applies the zone colours per
# variant from the container profiles, so those are set to white as well.
WHITE_SLOT = SLOT_PREFIX + "_WHITE"     # 4x4 white: the apron is texture x colour
ZONE_PARAMS = ("_Color_Outside_Court_R", "_Color_Area_G", "_Color_Floor_Court_B")
WHITE = {"r": 1.0, "g": 1.0, "b": 1.0, "a": 0.0}
MATERIAL_COLORS = ("_Tiling_Parquet", "_Offset_Parquet", "_Color_Parquet") + ZONE_PARAMS
MATERIAL_FLOATS = ("_Rotate_Parquet", "_Court_Lines_ON_OFF", "_GOAT_Textures_ON_OFF",
                   "_Outside_Texture_Tiling_ON_OFF")


def _material_snapshot(d):
    props = d["m_SavedProperties"]
    tex = _props(props["m_TexEnvs"])["_Texture_Parquet"]["m_Texture"]
    colors, floats = _props(props["m_Colors"]), _props(props["m_Floats"])
    out_tex = _props(props["m_TexEnvs"])["_OutsideTexture"]["m_Texture"]
    return {"tex": [tex["m_FileID"], int(tex["m_PathID"])],
            "outside": [out_tex["m_FileID"], int(out_tex["m_PathID"])],
            "colors": {k: dict(colors[k]) for k in MATERIAL_COLORS if k in colors},
            "floats": {k: floats[k] for k in MATERIAL_FLOATS if k in floats}}


def _material_restore(d, snap):
    props = d["m_SavedProperties"]
    tex = _props(props["m_TexEnvs"])["_Texture_Parquet"]["m_Texture"]
    tex["m_FileID"], tex["m_PathID"] = snap["tex"]
    if "outside" in snap:
        out_tex = _props(props["m_TexEnvs"])["_OutsideTexture"]["m_Texture"]
        out_tex["m_FileID"], out_tex["m_PathID"] = snap["outside"]
    for k, v in snap["colors"].items():
        _set_prop(props["m_Colors"], k, dict(v))
    for k, v in snap["floats"].items():
        _set_prop(props["m_Floats"], k, v)


def apron_colour(img, rect):
    """Median colour of a court picture's apron (the band between the court
    rectangle and the image edge), as 0..1 floats."""
    a = np.asarray(img.convert("RGB"))
    h, w, _ = a.shape
    x0, y0, x1, y1 = (int(v) for v in rect)
    mask = np.ones((h, w), bool)
    mask[max(0, y0 - 6):y1 + 6, max(0, x0 - 6):x1 + 6] = False
    px = a[mask]
    if not len(px):
        return (0.5, 0.5, 0.5)
    return tuple(float(v) / 255 for v in np.median(px, axis=0))


def _material_full_court(d, slot_pid, tiling, offset, lines, white_pid=None, apron=None):
    props = d["m_SavedProperties"]
    tex = _props(props["m_TexEnvs"])["_Texture_Parquet"]["m_Texture"]
    tex["m_FileID"], tex["m_PathID"] = 0, slot_pid
    out_tex = _props(props["m_TexEnvs"])["_OutsideTexture"]["m_Texture"]
    out_tex["m_FileID"], out_tex["m_PathID"] = 0, white_pid or slot_pid
    for z in ZONE_PARAMS:
        _set_prop(props["m_Colors"], z, dict(WHITE))
    if apron is not None:
        _set_prop(props["m_Colors"], ZONE_PARAMS[0],
                  {"r": apron[0], "g": apron[1], "b": apron[2], "a": 0.0})
    _set_prop(props["m_Floats"], "_GOAT_Textures_ON_OFF", 1.0)
    _set_prop(props["m_Floats"], "_Outside_Texture_Tiling_ON_OFF", 0.0)
    _set_prop(props["m_Colors"], "_Tiling_Parquet",
              {"r": tiling[0], "g": tiling[1], "b": 0.0, "a": 0.0})
    _set_prop(props["m_Colors"], "_Offset_Parquet",
              {"r": offset[0], "g": offset[1], "b": 0.0, "a": 0.0})
    _set_prop(props["m_Colors"], "_Color_Parquet", {"r": 1.0, "g": 1.0, "b": 1.0, "a": 0.0})
    _set_prop(props["m_Floats"], "_Rotate_Parquet", 0.0)
    if lines:
        _set_prop(props["m_Floats"], "_Court_Lines_ON_OFF", 0.0)


def _structure(game_path, courts, tokens, progress=None):
    """One verified rebuild adding whatever the given teams still lack: a slot
    texture, and (for teams with variants) the tiling/offset profiles.
    Returns True if the file was rebuilt."""
    need_slots = [SLOT_PREFIX + t for t in tokens if courts[t]["slot_pid"] is None]
    if tokens and any(c.get("white_pid") is None for c in courts.values()):
        need_slots.append(WHITE_SLOT)
    raws = tm._read_containers(game_path, tm.find_container_script(game_path))
    new_raw = {}
    for t in tokens:
        team = courts[t]
        if len(team["variants"]) < 2:
            continue
        for c in team["containers"]:
            raw = raws[c["name"]]["raw"]
            d = parse_profiles(raw)
            if d is None:
                raise FullCourtError(f"{c['name']}: its court colour list could not be "
                                     f"read, so per-variant courts can't be set up.")
            if _needs_profiles(d, team["variants"]):
                new_raw[c["path_id"]] = with_profiles(raw, d, team["variants"])
    if not need_slots and not new_raw:
        return False

    from UnityPy.helpers.TypeTreeHelper import write_typetree
    from UnityPy.streams.EndianBinaryWriter import EndianBinaryWriter
    path = _court_path(game_path)
    _ensure_original_backup(path)

    def edit(env):
        objs = {o.path_id: o for o in env.objects}
        for pid, raw in new_raw.items():
            objs[pid].set_raw_data(raw)
        if need_slots:
            stock = next((o for o in objs.values() if o.type.name == "Texture2D"
                          and o.peek_name() == STOCK_TEXTURE_NAME), None)
            if stock is None:
                raise FullCourtError(f"{STOCK_TEXTURE_NAME} not found in {COURT_FILE}.")
            base = stock.read_typetree()
            node = stock._get_typetree_node()
            taken = set(env.file.objects)
            pid = FIRST_SLOT_PATH_ID
            for t in need_slots:
                while pid in taken:
                    pid += 1
                d = copy.deepcopy(base)
                d["m_Name"] = t
                d["m_TextureFormat"] = int(TextureFormat.DXT1)
                w = EndianBinaryWriter(endian=stock.reader.endian)
                write_typetree(d, node, w, env.file)
                tm._add_object(env, stock.path_id, pid, w.bytes)
                taken.add(pid)
        return set(new_raw)

    if progress:
        progress(f"Adding full-court data to {COURT_FILE} ...")
    tm._rebuild(path, edit, expect_added=len(need_slots), progress=progress)
    return True


def _write_team(game_path, team, cfg, tstate, ress_reset=None, progress=None):
    """Pixels, headers, materials, profile values and logos for one team."""
    path = _court_path(game_path)
    images, rows, plan = _variant_plan(team, cfg)
    if progress:
        progress(f"{team['display']}: encoding {len(images)} court(s) ...")
    atlas = build_atlas(images, rows)
    data, mips = mipchain(atlas, TextureFormat.DXT1)
    if len(data) != expected_size(TEX_W, TEX_H * rows, 8):
        raise FullCourtError(f"{team['display']}: encoded court has the wrong size.")

    slot = team["slot_pid"]
    mats = team["materials"]
    logo_jobs = []          # (texture_pid, image or None for restore)
    for v in team["variants"]:
        for x in team["logos"].get(v, []):
            logo_jobs.append((x["texture_pid"], v))
    current = _read_objects(path, {slot, mats["parquet"], mats["lines"]}
                            | {p for p, _v in logo_jobs})

    originals = tstate.setdefault("originals", {"materials": {}, "logos": {}})
    for role in ("parquet", "lines"):
        snap = _material_snapshot(current[mats[role]])
        if snap["tex"][1] != slot and role not in originals["materials"]:
            originals["materials"][role] = snap

    ress = _ress_for(path, current[slot])
    at = _append_pixels(ress, data, ress_reset)
    header = {"m_Width": TEX_W, "m_Height": TEX_H * rows,
              "m_TextureFormat": int(TextureFormat.DXT1), "m_MipCount": mips,
              "m_CompleteImageSize": len(data),
              "stream": {"offset": at, "size": len(data)}}
    edits = {slot: lambda d, h=header: _set_texture_header(d, h)}

    default = plan["default"]
    aprons = {}
    for v in team["variants"]:
        vc = variant_picture(cfg, v)
        aprons[v] = apron_colour(Image.open(vc["image"]), tuple(vc["rect"]))
    wdata, wmips = mipchain(Image.new("RGB", (4, 4), (255, 255, 255)), TextureFormat.DXT1)
    wat = _append_pixels(ress, wdata, ress_reset)
    white_header = {"m_Width": 4, "m_Height": 4, "m_TextureFormat": int(TextureFormat.DXT1),
                    "m_MipCount": wmips, "m_CompleteImageSize": len(wdata),
                    "stream": {"offset": wat, "size": len(wdata)}}
    edits[team["white_pid"]] = lambda d, h=white_header: _set_texture_header(d, h)
    for role in ("parquet", "lines"):
        edits[mats[role]] = (lambda d, r=role: _material_full_court(
            d, slot, default[1], default[2], r == "lines", team["white_pid"], aprons["default"]))

    # Logos
    for tpid, v in logo_jobs:
        d = current[tpid]
        key = str(tpid)
        if key not in originals["logos"]:
            if d["m_StreamData"]["size"] == 0:
                continue               # not streamed: can't be replaced in place
            originals["logos"][key] = _texture_header(d)
        orig = originals["logos"][key]
        img = logo_image_for(team, cfg, v, (orig["m_Width"], orig["m_Height"]))
        if img is None:
            edits[tpid] = lambda d, h=orig: _set_texture_header(d, h)
            continue
        ldata, lmips = mipchain(img, TextureFormat.DXT5)
        lat = _append_pixels(_ress_for(path, d), ldata, ress_reset)
        lh = {"m_Width": orig["m_Width"], "m_Height": orig["m_Height"],
              "m_TextureFormat": int(TextureFormat.DXT5), "m_MipCount": lmips,
              "m_CompleteImageSize": len(ldata),
              "stream": {"offset": lat, "size": len(ldata)}}
        edits[tpid] = lambda d, h=lh: _set_texture_header(d, h)

    _patch_objects(path, edits)
    _write_profile_values(game_path, team, {v: (p[1], p[2]) for v, p in plan.items()},
                          zones={(z, v): ((*aprons[v], 0.0) if z == ZONE_PARAMS[0]
                                          else (1.0, 1.0, 1.0, 0.0))
                                 for z in ZONE_PARAMS for v in team["variants"]},
                          originals=originals.setdefault("zones", {}))
    _write_strips(game_path, team, cfg, originals, ress_reset)
    tstate["slot"] = SLOT_PREFIX + team["token"]
    tstate["rows"] = rows


def _write_strips(game_path, team, cfg, originals, ress_reset=None):
    """Baseline text strips for every variant (see STRIP_MODES). They live in
    resources.assets and are single-level DXT5, as shipped."""
    spath = os.path.join(game_path, STRIP_FILE)
    wanted = {n for lst in team.get("strips", {}).values() for n in lst}
    if not wanted or not os.path.isfile(spath):
        return
    store = originals.setdefault("strips", {})
    found = {}
    env = UnityPy.load(spath)
    try:
        for o in env.objects:
            if o.type.name != "Texture2D":
                continue
            try:
                name = o.peek_name()
            except Exception:
                continue
            if name not in wanted:
                continue
            d = o.read_typetree()
            key = str(o.path_id)
            if key not in store:
                if not d["m_StreamData"]["size"]:
                    continue
                png = os.path.join(team_dir(team["token"]), f"strip_original_{o.path_id}.png")
                o.read().image.save(png)
                store[key] = {"name": name, "header": _texture_header(d), "png": png}
            found[o.path_id] = d
    finally:
        tm.close_env(env)
    _ensure_original_backup(spath)
    edits = {}
    for pid, d in found.items():
        orig = store[str(pid)]
        img = strip_image_for(cfg, strip_variant(orig["name"]), Image.open(orig["png"]))
        if img is None:
            edits[pid] = lambda d, h=orig["header"]: _set_texture_header(d, h)
            continue
        data, _ = image_to_texture2d(img, TextureFormat.DXT5)
        at = _append_pixels(_ress_for(spath, d), data, ress_reset)
        h = {"m_Width": STRIP_SIZE[0], "m_Height": STRIP_SIZE[1],
             "m_TextureFormat": int(TextureFormat.DXT5), "m_MipCount": 1,
             "m_CompleteImageSize": len(data), "stream": {"offset": at, "size": len(data)}}
        edits[pid] = lambda d, h=h: _set_texture_header(d, h)
    if edits:
        _patch_objects(spath, edits)


def _write_profile_values(game_path, team, values, zones=None, originals=None):
    """In place, in every container of the team: values = {variant: (tiling,
    offset)} for the tiling/offset profiles; zones = {(param, variant): rgba}
    (or a single rgba for all) for the Parquet part's zone colours, recording
    the first-seen originals in `originals` = {container: {param: {variant:
    rgba}}}."""
    path = _court_path(game_path)
    raws = tm._read_containers(game_path, tm.find_container_script(game_path))
    with open(path, "r+b") as f:
        for c in team["containers"]:
            info = raws[c["name"]]
            d = parse_profiles(info["raw"])
            if d is None:
                continue
            for pa in d["pairs"]:
                for pr in pa["profiles"]:
                    for vkey, rgba, off in pr["values"]:
                        new = None
                        if (values and pa["key"] in (PART_PARQUET, PART_LINES)
                                and pr["param"] in (TILING_PARAM, OFFSET_PARAM)):
                            tiling, offset = values.get(vkey, values["default"])
                            xy = tiling if pr["param"] == TILING_PARAM else offset
                            new = (xy[0], xy[1], 0.0, 0.0)
                        elif (zones is not None and pa["key"] == PART_PARQUET
                              and pr["param"] in ZONE_PARAMS):
                            if originals is not None:
                                (originals.setdefault(c["name"], {})
                                 .setdefault(pr["param"], {})
                                 .setdefault(vkey, list(rgba)))
                            new = (zones.get((pr["param"], vkey)) if isinstance(zones, dict)
                                   else zones)
                        if new is not None:
                            f.seek(info["byte_start"] + off)
                            f.write(struct.pack("<4f", *new))


def apply(game_path, tokens=None, progress=None):
    """Write every configured team in `tokens` (default: all configured).
    Returns the number of teams written."""
    state = load_state()
    courts = discover(game_path)
    tokens = [t for t in (tokens or list(state["teams"]))
              if t in state["teams"] and t in courts
              and any(v.get("image") for v in state["teams"][t].get("variants", {}).values())]
    if not tokens:
        return 0
    if _structure(game_path, courts, tokens, progress):
        courts = discover(game_path)
    _ensure_original_backup(_court_path(game_path))
    for t in tokens:
        tstate = state["teams"][t]
        _write_team(game_path, courts[t], tstate, tstate, progress=progress)
        tstate["applied"] = True
        save_state(state)
    if progress:
        progress(f"Applied {len(tokens)} full court(s).")
    return len(tokens)


def revert(game_path, token, progress=None):
    """Put a team's materials, profile values and logos back. The slot texture
    stays in the file, unreferenced."""
    state = load_state()
    tstate = state["teams"].get(token)
    courts = discover(game_path)
    team = courts.get(token)
    if not tstate or team is None:
        return False
    originals = tstate.get("originals", {})
    path = _court_path(game_path)
    edits = {}
    mats = originals.get("materials", {})
    for role in ("parquet", "lines"):
        if role in mats:
            edits[team["materials"][role]] = lambda d, s=mats[role]: _material_restore(d, s)
    for key, h in originals.get("logos", {}).items():
        edits[int(key)] = lambda d, h=h: _set_texture_header(d, h)
    if edits:
        _patch_objects(path, edits)
    strips = {int(k): v["header"] for k, v in originals.get("strips", {}).items()}
    if strips:
        _patch_objects(os.path.join(game_path, STRIP_FILE),
                       {pid: (lambda d, h=h: _set_texture_header(d, h))
                        for pid, h in strips.items()})
    p = mats.get("parquet", {}).get("colors", {})
    til = p.get(TILING_PARAM, {"r": 0.4, "g": 0.4})
    off = p.get(OFFSET_PARAM, {"r": 0.0, "g": 0.0})
    stock = ((til["r"], til["g"]), (off["r"], off["g"]))
    _write_profile_values(game_path, team, {v: stock for v in team["variants"]})
    for cname, params in originals.get("zones", {}).items():
        for param, vals in params.items():
            one = {"containers": [c for c in team["containers"] if c["name"] == cname]}
            _write_profile_values(game_path, one, None,
                                  zones={(param, v): tuple(rgba) for v, rgba in vals.items()})
    tstate["applied"] = False
    tstate.pop("originals", None)
    save_state(state)
    return True


def status(game_path, courts=None):
    """{token: "applied" | "undone" | "pending" | "none"} -- "undone" when the
    state says applied but the game file no longer points at the slot (Restore
    Game Files, a team reset)."""
    state = load_state()
    courts = courts if courts is not None else discover(game_path)
    out = {t: "none" for t in courts}
    pids = {c["materials"]["parquet"]: t for t, c in courts.items()
            if t in state["teams"] and state["teams"][t].get("applied")}
    live = _read_objects(_court_path(game_path), set(pids)) if pids else {}
    for t, e in state["teams"].items():
        if t not in courts:
            continue
        if e.get("applied"):
            d = live.get(courts[t]["materials"]["parquet"])
            ok = (d is not None and courts[t]["slot_pid"] is not None
                  and _material_snapshot(d)["tex"][1] == courts[t]["slot_pid"])
            out[t] = "applied" if ok else "undone"
        elif any(v.get("image") for v in e.get("variants", {}).values()):
            out[t] = "pending"
    return out


def reapply_full_courts(game_path, ress_reset):
    """Re-append every applied team's pixels during an Apply Mods run, with the
    run's shared ress_reset (that run rewinds the .resS and the pixels go with
    it). Returns (teams written, [warnings])."""
    state = load_state()
    applied = [t for t, e in state["teams"].items() if e.get("applied")]
    if not applied:
        return 0, []
    try:
        courts = discover(game_path)
        st = status(game_path, courts)
    except Exception as e:
        return 0, [f"Full courts: {e}"]
    written, notes = 0, []
    for t in applied:
        if st.get(t) != "applied":
            notes.append("Full courts were undone by a restore or team reset. "
                         "Open Full Courts and click Apply to put them back.")
            break
        try:
            _write_team(game_path, courts[t], state["teams"][t], state["teams"][t],
                        ress_reset=ress_reset)
            written += 1
        except Exception as e:
            notes.append(f"Full court '{t}': {e}")
    save_state(state)
    return written, notes


# ===========================================================================
# Self-test
# ===========================================================================
def selftest(game_path=None):
    ok = True

    def check(cond, what):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + what)
        ok = ok and bool(cond)

    check(expected_size(2048, 1024, 8) == DXT1_2048x1024, "2048x1024 DXT1 = 1,398,120 bytes")
    check(expected_size(1024, 1024, 16) == DXT5_1024, "1024^2 DXT5 = 1,398,128 bytes")
    data, mips = mipchain(make_template(), TextureFormat.DXT1)
    check(len(data) == DXT1_2048x1024 and mips == 12, f"template encodes to 12 mips ({len(data):,})")
    ldata, lmips = mipchain(Image.new("RGBA", (1024, 1024)), TextureFormat.DXT5)
    check(len(ldata) == DXT5_1024 and lmips == 11, "blank logo encodes to 11 mips")
    rect, found = detect_court_rect(make_template())
    check(found and all(abs(a - b) <= 4 for a, b in zip(rect, template_rect())),
          f"court detected in the template {rect} ~ {template_rect()}")
    t, o = court_mapping((2048, 1024), (0, 0, 2048, 1024))
    check(abs(t[0] - 1 / 28.8) < 1e-9 and abs(o[1] - 0.5) < 1e-9, "edge-to-edge mapping")
    for row in range(4):
        _t, (_ox, oy) = atlas_mapping(t, (0.5, 0.5), row, 4)
        lo, hi = (3 - row) / 4, (4 - row) / 4
        check(lo < oy < hi, f"atlas row {row} centre lands in its band")
    if game_path:
        import game_layout
        game_layout.configure(game_path, sys.modules[__name__], tm)
        raws = tm._read_containers(game_path, tm.find_container_script(game_path))
        n = bad = 0
        for name, c in raws.items():
            d = parse_profiles(c["raw"])
            if d is None:
                continue
            n += 1
            vs = profile_variants(d)
            new = with_profiles(c["raw"], d, vs)
            d2 = parse_profiles(new)
            if d2 is None or _needs_profiles(d2, vs) or len(_profile_value_offsets(d2, vs)) != 4 * len(vs):
                bad += 1
        check(n >= 30 and bad == 0, f"profiles parse and extend in {n} containers")
        courts = discover(game_path)
        check(len(courts) >= 30, f"{len(courts)} courts discovered")
        bulls = courts.get("ChicagoBulls")
        if bulls:
            check(len(bulls["variants"]) >= 2 and bulls["logos"].get("default"),
                  f"Bulls variants {bulls['variants']}, "
                  f"{sum(len(v) for v in bulls['logos'].values())} logo textures")
    print("ALL PASS" if ok else "FAILURES")
    return ok


try:
    import game_layout as _game_layout
    _game_layout.attach(sys.modules[__name__])
except Exception:
    pass


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--selftest"]
        sys.exit(0 if selftest(rest[0] if rest else None) else 1)
