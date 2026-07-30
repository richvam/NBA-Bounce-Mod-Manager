"""
NBA Bounce Mod Manager -- Court Floor Patterns
==============================================

Self-contained. Imports nothing from app.py and never touches apply_single_mod().

Integration (two lines in app.py):

    from floor_patterns import open_floor_patterns
    ...
    ttk.Button(bar, text="Floor Patterns",
               command=lambda: open_floor_patterns(self.root, self.cfg["game_data_path"],
                                                   theme=self.current_theme())).pack(side="left")

`theme` is optional -- pass a dict with any of:
    bg, panel, text, accent, muted, entry
Falls back to the app's dark "Arena Night" palette.


HOW IT WORKS
------------
Every court has two materials in sharedassets1.assets --
mat_bounce_stadium_parquet_{Team} and mat_bounce_stadium_lines_{Team} -- and both
carry a `_Texture_Parquet` texture slot holding a 12-byte PPtr
(int32 fileID + int64 pathID). Out of the box all 38 NBA courts point at the same
texture, txt_bounce_parquet01_D. The four GOAT courts (Ice/Jungle/Magma/Wind)
already point at their own, which is what proves the mechanism is supported.

Redirecting a court's PPtr to a different texture is size-neutral, so it patches
in place exactly like the Court Colors work.

The catch is that new Texture2D objects cannot be created. Instead we reuse eight
dead textures shipped by accident in resources.assets -- documentation screenshots
from a localization asset-store plugin. Each is 512x512 plain DXT1 with a
174,776-byte slot, which is exactly a complete 512x512 DXT1 mip chain, so a
pattern drops in with zero size delta.

That gives 8 simultaneous custom patterns plus the untouched stock floor. The
pattern LIBRARY is unlimited; the 8 is how many can be live at once, and slots are
allocated automatically as courts are assigned.

The texture is multiplied by the material's _Color_Parquet, so patterns are
authored greyscale and the existing Court Colors picker still drives the wood
tone. Pattern and colour stay independent.
"""

import os, sys, json, struct, base64, shutil, threading

for _pkg, _mod in [("UnityPy", "UnityPy"), ("Pillow", "PIL"), ("etcpak", "etcpak"),
                   ("numpy", "numpy"), ("attrs", "attrs"), ("lz4", "lz4")]:
    try:
        __import__(_mod)
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", _pkg, "--quiet"])

import numpy as np
import UnityPy
from PIL import Image
from UnityPy.export.Texture2DConverter import image_to_texture2d
import retro_eras   # read-only era labels for the court-variant preview picker
from UnityPy.enums import TextureFormat

# The pattern library and the patching layer are usable headless (and testable on
# machines without Tk). Only the window needs tkinter.
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from PIL import ImageTk
    HAVE_TK = True
except ImportError:                                   # pragma: no cover
    tk = ttk = filedialog = messagebox = ImageTk = None
    HAVE_TK = False

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(APP_DIR, "floor_patterns")
CUSTOM_DIR = os.path.join(CACHE_DIR, "custom")
STATE_FILE = os.path.join(APP_DIR, "floor_patterns.json")
JOURNAL_FILE = os.path.join(APP_DIR, "floor_patterns_journal.json")

MAT_FILE = "sharedassets1.assets"
DONOR_FILE = "resources.assets"
SLOT_KEY = b"_Texture_Parquet"
STOCK_PATHID = 1760                 # txt_bounce_parquet01_D, fileID 0
S = 512
EXPECTED_SLOT_BYTES = 174776        # 512x512 DXT1, 10 mips

# Verified unreferenced 512x512 DXT1 leftovers in resources.assets.
DONORS = ["add_new_view", "api_quick_start", "langScreenshotColId", "lang_deleteLang",
          "lang_selectLang", "setup_setting_mw", "st_build_settings", "str_editor"]

DARK = dict(bg="#12151C", panel="#1B2030", text="#E8EAF0", accent="#1D428A",
            muted="#8A93A8", entry="#232838", gold="#FDB927", red="#C8102E")


# ===========================================================================
# Pattern library -- seamless greyscale hardwood, 512x512, all periods divide S
# ===========================================================================
def _fibre(u, w_along, seed, kmin=2):
    rng = np.random.default_rng(seed)
    g = np.ones_like(u, dtype=np.float64)
    for k, amp in [(2, .020), (3, .015), (5, .011), (8, .008), (13, .005), (21, .003)]:
        if k < kmin:
            continue
        g += amp * np.sin(2 * np.pi * k * u / w_along + rng.uniform(0, 2 * np.pi))
    for k, amp in [(29, .008), (47, .005)]:
        g += amp * np.sin(2 * np.pi * k * u / w_along + rng.uniform(0, 2 * np.pi))
    return g


def _planks(shape, along, width, seed, groove=1.6, tone=.055, phase=0.0):
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    u = (xx if along == 0 else yy) + phase
    v = (yy if along == 0 else xx)
    span = w if along == 0 else h
    g = _fibre(u, span, seed)
    across = h if along == 0 else w
    n = max(1, int(round(across / width)))
    pw = across / n
    idx = np.floor(v / pw).astype(int)
    rng = np.random.default_rng(seed + 991)
    g = g + rng.uniform(-tone, tone, n)[np.clip(idx, 0, n - 1)]
    frac = v / pw - idx
    d = np.minimum(frac, 1 - frac) * pw
    return g * np.clip(.52 + .48 * np.minimum(d, groove) / groove, 0, 1)


def _bevel(a, period, depth=.28, px=1.8):
    yy, xx = np.mgrid[0:a.shape[0], 0:a.shape[1]]
    for c in (yy, xx):
        m = c % period
        d = np.minimum(m, period - 1 - m).astype(np.float64)
        a = a * np.clip((1 - depth) + depth * np.minimum(d, px) / px, 0, 1)
    return a


def _to_image(a, mean=.82):
    a = np.clip(a, 1e-6, None)
    a = np.clip(a / a.mean() * mean, 0, 1)
    p = (a * 255).astype(np.uint8)
    return Image.fromarray(np.dstack([p, p, p, np.full_like(p, 255)]), "RGBA")


def _parquet(blocks=4, strips=6, seed=7):
    b = S // blocks
    out = np.zeros((S, S))
    for r in range(blocks):
        for c in range(blocks):
            out[r * b:(r + 1) * b, c * b:(c + 1) * b] = _planks(
                (b, b), (r + c) % 2, b / strips, seed + r * 31 + c * 17, phase=(r + c) * 7.0)
    return _bevel(out, b)


def _plank(boards=10, seed=11, tone=.075):
    return _planks((S, S), 0, S / boards, seed, groove=1.9, tone=tone)


def _twotone(boards=12, seed=31):
    """Alternating light/dark stained boards -- modern stained-court look."""
    g = _planks((S, S), 0, S / boards, seed, groove=1.9, tone=.03)
    yy = np.mgrid[0:S, 0:S][0].astype(np.float64)
    idx = np.floor(yy / (S / boards)).astype(int)
    return g * np.where(idx % 2 == 0, 1.0, 0.72)


def _diagonal(boards=12, seed=13):
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float64)
    u = (xx + yy) % S
    v = (xx - yy) % S
    pw = S / boards
    g = _fibre(u, S, seed, kmin=13)
    idx = np.floor(v / pw).astype(int)
    rng = np.random.default_rng(seed + 5)
    g = g + rng.uniform(-.055, .055, boards + 1)[np.clip(idx, 0, boards)]
    frac = v / pw - idx
    d = np.minimum(frac, 1 - frac) * pw
    return g * np.clip(.46 + .54 * np.minimum(d, 1.7) / 1.7, 0, 1)


def _chevron(boards=8, seed=29):
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float64)
    colw = S / boards
    col = np.floor(xx / colw).astype(int)
    flip = (col % 2 == 0)
    u = np.where(flip, xx + yy, xx - yy) % S
    v = np.where(flip, xx - yy, xx + yy) % S
    g = _fibre(u, S, seed, kmin=8)
    idx = np.floor(v / colw).astype(int)
    rng = np.random.default_rng(seed + 3)
    g = g + rng.uniform(-.04, .04, boards + 1)[np.clip(idx, 0, boards)]
    frac = v / colw - idx
    d = np.minimum(frac, 1 - frac) * colw
    g = g * np.clip(.58 + .42 * np.minimum(d, 1.6) / 1.6, 0, 1)
    cd = np.minimum(xx % colw, colw - 1 - (xx % colw))
    return g * np.clip(.70 + .30 * np.minimum(cd, 1.5) / 1.5, 0, 1)


def _herringbone(unit=64, seed=17):
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float64)
    colw = float(unit)
    col = np.floor(xx / colw).astype(int)
    flip = (col % 2 == 0)
    ys = yy + np.where(flip, 0.0, colw)
    u = np.where(flip, xx + ys, xx - ys) % S
    v = np.where(flip, xx - ys, xx + ys) % S
    g = _fibre(u, S, seed, kmin=13)
    n = int(S / colw) + 2
    idx = np.floor(v / colw).astype(int)
    rng = np.random.default_rng(seed + 3)
    g = g + rng.uniform(-.035, .035, n)[np.clip(idx, 0, n - 1)]
    frac = v / colw - idx
    d = np.minimum(frac, 1 - frac) * colw
    g = g * np.clip(.46 + .54 * np.minimum(d, 1.7) / 1.7, 0, 1)
    eu = u % (colw * 2)
    ed = np.minimum(eu, colw * 2 - 1 - eu)
    return g * np.clip(.62 + .38 * np.minimum(ed, 1.4) / 1.4, 0, 1)


def _basketweave(unit=64, strips=3, seed=23):
    out = np.zeros((S, S))
    n = S // unit
    for r in range(n):
        for c in range(n):
            out[r * unit:(r + 1) * unit, c * unit:(c + 1) * unit] = _planks(
                (unit, unit), (r + c) % 2, unit / strips, seed + r * 7 + c * 5, groove=1.4)
    return _bevel(out, unit, depth=.22)


# key, display name, note, generator
BUILTIN = [
    ("boston_parquet",  "Boston Garden Parquet", "The real thing -- 4ft blocks, grain rotated 90 degrees per block.",
     lambda: _parquet(4, 6, 7)),
    ("parquet_fine",    "Parquet (Fine Weave)",  "Tighter 2ft panels. Reads as texture rather than pattern at distance.",
     lambda: _parquet(8, 4, 41)),
    ("league_maple",    "League Maple Plank",    "Straight boards running the length of the floor -- the NBA standard.",
     lambda: _plank(10, 11)),
    ("wide_plank",      "Wide Plank (Retro)",    "Chunky boards, 70s and 80s arena look.",
     lambda: _plank(6, 19, tone=.05)),
    ("two_tone",        "Two-Tone Stained",      "Alternating light and dark boards, as on modern stained courts.",
     lambda: _twotone(12, 31)),
    ("herringbone",     "Herringbone",           "Interlocking staggered zigzag -- classic fieldhouse floor.",
     lambda: _herringbone(64, 17)),
    ("basketweave",     "Basketweave",           "Woven three-strip blocks, a retro parquet variant.",
     lambda: _basketweave(64, 3, 23)),
    ("chevron",         "Chevron",               "Planks meeting point to point in clean V's.",
     lambda: _chevron(8, 29)),
    ("diagonal_maple",  "Diagonal Maple",        "Straight boards laid at 45 degrees.",
     lambda: _diagonal(12, 13)),
]


STOCK_KEY = "stock:parquet"
STOCK_CACHE = os.path.join(CACHE_DIR, "stock_parquet.png")
STOCK_NOTE = ("The floor NBA Bounce ships with -- staggered planks, greyscale. "
              "Decoded straight out of the game, so it's the best starting point "
              "for editing. Assigning it just resets the court and costs no slot.")
STOCK_TEXTURE_NAME = "txt_bounce_parquet01_D"


def extract_stock_pattern(game_path, force=False):
    """Decode the game's own floor texture to STOCK_CACHE. Returns True on success.

    The cache is NOT shipped with the app -- it is game art, so it has to come
    out of the user's own install. This is deliberately standalone: it needs
    nothing but a game path, so the stock floor is available even when the full
    donor probe in FloorPatternManager.discover() can't complete.

    Reads the pristine backup when one exists, so a pattern applied earlier can't
    be mistaken for the original. Matches on path_id first and falls back to the
    texture name, in case a game patch renumbers objects.
    """
    if not force and os.path.exists(STOCK_CACHE):
        return True
    if not game_path or not os.path.isdir(game_path):
        return False

    src = os.path.join(game_path, MAT_FILE)
    bak = src + ".original_backup"
    if os.path.exists(bak):
        src = bak
    if not os.path.exists(src):
        return False

    try:
        env = UnityPy.load(src)
        textures = [o for o in env.objects if getattr(o.type, "name", "") == "Texture2D"]
        by_path_id = [o for o in textures if int(o.path_id) == STOCK_PATHID]
        by_name = [o for o in textures if o.peek_name() == STOCK_TEXTURE_NAME]
        for o in by_path_id + by_name:
            img = o.read().image
            if img is not None:
                _save_stock(img)
                return True
    except Exception:
        pass                # non-fatal: the library just won't offer the entry
    return False


def _save_stock(img):
    img = img.convert("RGBA")
    if img.size != (S, S):
        img = img.resize((S, S), Image.LANCZOS)
    os.makedirs(CACHE_DIR, exist_ok=True)
    img.save(STOCK_CACHE)


def stock_available(game_path=None):
    """True if the stock floor can be shown -- cached already, or extractable."""
    return os.path.exists(STOCK_CACHE) or extract_stock_pattern(game_path)


def library(game_path=None):
    """[(key, label, note, is_custom)].

    The stock game floor comes first when it is available, then the built-in
    designs, then anything the user has imported. Pass game_path and it will be
    extracted from the game on demand rather than silently omitted."""
    out = []
    if stock_available(game_path):
        out.append((STOCK_KEY, "Stock Parquet (game original)", STOCK_NOTE, False))
    out += [(k, lab, note, False) for k, lab, note, _fn in BUILTIN]
    if os.path.isdir(CUSTOM_DIR):
        for fn in sorted(os.listdir(CUSTOM_DIR)):
            if fn.lower().endswith(".png"):
                key = "custom:" + os.path.splitext(fn)[0]
                out.append((key, os.path.splitext(fn)[0], "Imported custom pattern.", True))
    return out


def pattern_image(key, game_path=None):
    """512x512 RGBA. Built-ins are generated then disk-cached; customs and the
    extracted stock floor are loaded from disk."""
    if key == STOCK_KEY:
        if not stock_available(game_path):
            raise FileNotFoundError(
                "The stock floor is extracted from your copy of the game and "
                "isn't cached yet. Set the Game Data Folder in Settings, then "
                "reopen Floor Patterns.")
        return Image.open(STOCK_CACHE).convert("RGBA")
    if key.startswith("custom:"):
        p = os.path.join(CUSTOM_DIR, key.split(":", 1)[1] + ".png")
        im = Image.open(p).convert("RGBA")
        return im if im.size == (S, S) else im.resize((S, S), Image.LANCZOS)
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, key + ".png")
    if os.path.exists(cache):
        return Image.open(cache).convert("RGBA")
    for k, _lab, _n, fn in BUILTIN:
        if k == key:
            im = _to_image(fn())
            im.save(cache)
            return im
    raise KeyError(key)


def export_pattern(key, dest_path, tiled=False, game_path=None):
    """Save a pattern as an editable 512x512 PNG.

    tiled=True writes a 2x2 (1024x1024) sheet instead, which makes seams at the
    tile edge obvious while editing. Crop back to any single 512 quadrant before
    re-importing."""
    im = pattern_image(key, game_path).convert("RGBA")
    if tiled:
        sheet = Image.new("RGBA", (S * 2, S * 2))
        for r in range(2):
            for c in range(2):
                sheet.paste(im, (c * S, r * S))
        im = sheet
    if not dest_path.lower().endswith(".png"):
        dest_path += ".png"
    im.save(dest_path)
    return dest_path


def tiled_preview(key, tint=(0.851, 0.621, 0.447), reps=3, size=384, game_path=None):
    """What the pattern looks like repeated, with the court's _Color_Parquet applied."""
    im = pattern_image(key, game_path)
    t = Image.new("RGBA", (S * reps, S * reps))
    for r in range(reps):
        for c in range(reps):
            t.paste(im, (c * S, r * S))
    a = np.asarray(t.convert("RGB"), float) / 255.0 * np.array(tint)
    return Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8)).resize(
        (size, size), Image.LANCZOS)


def import_custom(png_path, name=None):
    os.makedirs(CUSTOM_DIR, exist_ok=True)
    im = Image.open(png_path).convert("RGBA")
    if im.size != (S, S):
        im = im.resize((S, S), Image.LANCZOS)
    name = name or os.path.splitext(os.path.basename(png_path))[0]
    name = "".join(ch for ch in name if ch.isalnum() or ch in " _-").strip() or "custom"
    im.save(os.path.join(CUSTOM_DIR, name + ".png"))
    return "custom:" + name


def encode_dxt1_mipchain(img, mips=10):
    """Unity mip chain. image_to_texture2d handles the bottom-up flip."""
    out = b""
    cur = img.convert("RGBA")
    w, h = cur.size
    for _ in range(mips):
        data, _fmt = image_to_texture2d(cur, TextureFormat.DXT1)
        out += data
        w, h = max(1, w // 2), max(1, h // 2)
        cur = cur.resize((w, h), Image.LANCZOS)
    return out


# ===========================================================================
# Game file layer
# ===========================================================================
class Journal:
    """Records pristine bytes the first time a region is written, so revert
    always returns to the original game state no matter how many applies."""

    def __init__(self, path=JOURNAL_FILE):
        self.path = path
        self.entries = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.entries = json.load(f).get("entries", {})

    def write(self, full_path, offset, data):
        key = f"{os.path.basename(full_path)}:{offset}"
        with open(full_path, "r+b") as f:
            f.seek(offset)
            original = f.read(len(data))
            if len(original) != len(data):
                raise RuntimeError(f"short read in {full_path} at {offset}")
            if key not in self.entries:
                self.entries[key] = {"file": os.path.basename(full_path),
                                     "offset": offset, "length": len(data),
                                     "original": base64.b64encode(original).decode()}
            f.seek(offset)
            f.write(data)

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"entries": self.entries}, f, indent=1)

    def revert(self, game_path):
        n = 0
        for e in self.entries.values():
            p = os.path.join(game_path, e["file"])
            if not os.path.exists(p):
                continue
            with open(p, "r+b") as f:
                f.seek(e["offset"])
                f.write(base64.b64decode(e["original"]))
            n += 1
        self.entries = {}
        if os.path.exists(self.path):
            shutil.move(self.path, self.path + ".done")
        return n

    @property
    def active(self):
        return bool(self.entries)


class FloorPatternManager:
    """Discovers courts and donor slots, then applies pattern assignments."""

    def __init__(self, game_path):
        self.game_path = game_path
        self.courts = {}     # team -> {"materials":[(name,pptr_offset)], "tint":(r,g,b)}
        self.donors = {}     # donor name -> {path_id,offset,size,res}
        self.file_id = None  # fileID that reaches resources.assets from sharedassets1
        self.loaded = False

    # -- discovery ---------------------------------------------------------
    def discover(self, progress=None):
        mat_path = os.path.join(self.game_path, MAT_FILE)
        don_path = os.path.join(self.game_path, DONOR_FILE)
        for p in (mat_path, don_path):
            if not os.path.exists(p):
                raise FileNotFoundError(p)

        if progress:
            progress("Reading sharedassets1.assets ...")
        raw = open(mat_path, "rb").read()
        env = UnityPy.load(mat_path)

        externals = None
        for o in env.objects:
            if getattr(o.type, "name", "") != "Material":
                continue
            d = o.read_typetree()
            name = d.get("m_Name", "")
            if not name.startswith(("mat_bounce_stadium_parquet_", "mat_bounce_stadium_lines_")):
                continue
            team = name.split("_")[-1]
            bs, sz = int(o.byte_start), int(o.byte_size)
            blob = raw[bs:bs + sz]
            i = blob.find(SLOT_KEY)
            if i < 0:
                continue
            if struct.unpack_from("<i", blob, i - 4)[0] != len(SLOT_KEY):
                continue
            end = i + len(SLOT_KEY)
            p = end + (-end % 4)
            entry = self.courts.setdefault(team, {"materials": [], "tint": (1.0, 1.0, 1.0)})
            entry["materials"].append((name, bs + p))
            if name.startswith("mat_bounce_stadium_parquet_"):
                cols = d.get("m_SavedProperties", {}).get("m_Colors")
                items = cols.items() if isinstance(cols, dict) else [
                    (x.get("first"), x.get("second")) if isinstance(x, dict) else tuple(x)
                    for x in (cols or [])]
                for k, v in items:
                    if k == "_Color_Parquet":
                        entry["tint"] = (float(v["r"]), float(v["g"]), float(v["b"]))
            if externals is None:
                externals = [os.path.basename(str(getattr(e, "path", e)))
                             for e in (getattr(o.assets_file, "externals", []) or [])]

        if externals and DONOR_FILE in externals:
            self.file_id = externals.index(DONOR_FILE) + 1
        else:
            raise RuntimeError(f"{DONOR_FILE} not reachable from {MAT_FILE}")

        if progress:
            progress("Reading resources.assets ...")
        env_d = UnityPy.load(don_path)
        for o in env_d.objects:
            if getattr(o.type, "name", "") != "Texture2D":
                continue
            d = o.read_typetree()
            nm = d.get("m_Name")
            if nm not in DONORS:
                continue
            sd = d.get("m_StreamData") or {}
            size = int(sd.get("size") or 0)
            if (d.get("m_Width"), d.get("m_Height")) != (S, S) or size != EXPECTED_SLOT_BYTES:
                continue
            if int(d.get("m_TextureFormat")) != int(TextureFormat.DXT1):
                continue
            self.donors[nm] = {"path_id": int(o.path_id), "offset": int(sd.get("offset") or 0),
                               "size": size, "res": os.path.basename(str(sd.get("path", "")))}

        self._extract_stock(env, progress)
        self.loaded = True
        if progress:
            progress(f"{len(self.courts)} courts, {len(self.donors)} slots")
        return self

    def _extract_stock(self, env=None, progress=None):
        """Cache the shipped floor texture as a PNG, so it can be previewed and
        exported as an editing template. See extract_stock_pattern()."""
        if os.path.exists(STOCK_CACHE):
            return
        if progress:
            progress("Extracting the stock floor texture ...")
        extract_stock_pattern(self.game_path)

    @property
    def max_slots(self):
        return len(self.donors)

    # -- apply -------------------------------------------------------------
    def apply(self, assignments, progress=None):
        """assignments: {team: pattern_key or None}. Returns (n_courts, slot_map).

        The stock-floor key is equivalent to None -- it points the court back at
        the shipped texture, so it consumes no donor slot."""
        assignments = {t: (None if k == STOCK_KEY else k) for t, k in assignments.items()}
        used = []
        for t, key in assignments.items():
            if key and key not in used:
                used.append(key)
        if len(used) > self.max_slots:
            raise RuntimeError(f"{len(used)} patterns assigned but only "
                               f"{self.max_slots} slots exist. Free one first.")

        slot_map = dict(zip(used, list(self.donors)[:len(used)]))
        j = Journal()
        mat_path = os.path.join(self.game_path, MAT_FILE)

        for key, donor in slot_map.items():
            if progress:
                progress(f"Encoding {key} -> {donor} ...")
            info = self.donors[donor]
            data = encode_dxt1_mipchain(pattern_image(key, self.game_path))
            if len(data) != info["size"]:
                raise RuntimeError(f"{key}: encoded {len(data)} bytes, slot is {info['size']}")
            j.write(os.path.join(self.game_path, info["res"]), info["offset"], data)

        n = 0
        for team, key in assignments.items():
            if team not in self.courts:
                continue
            if key:
                pptr = struct.pack("<iq", self.file_id, self.donors[slot_map[key]]["path_id"])
            else:
                pptr = struct.pack("<iq", 0, STOCK_PATHID)
            for _name, off in self.courts[team]["materials"]:
                j.write(mat_path, off, pptr)
            n += 1
        j.save()
        if progress:
            progress(f"Applied to {n} courts using {len(slot_map)} slots.")
        return n, slot_map

    def revert(self):
        return Journal().revert(self.game_path)

    @staticmethod
    def journal_active():
        return Journal().active


# ===========================================================================
# State
# ===========================================================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"assignments": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


# ===========================================================================
# UI
# ===========================================================================
if HAVE_TK:
  class FloorPatternWindow(tk.Toplevel):
      def __init__(self, parent, game_path, theme=None):
          super().__init__(parent)
          self.T = dict(DARK)
          if theme:
              self.T.update({k: v for k, v in theme.items() if v})
          self.game_path = game_path
          self.mgr = FloorPatternManager(game_path)
          self.state = load_state()
          self.assignments = dict(self.state.get("assignments", {}))
          self.sel_pattern = None
          self.sel_team = None
          self._thumbs = {}          # keep PhotoImage refs alive
          self._preview_img = None

          self.title("Court Floor Patterns")
          self.geometry("1360x830")
          self.minsize(1180, 780)
          self.configure(bg=self.T["bg"])
          self._styles()
          self._build()
          self.on_closed = None          # set by standalone callers that own the root
          self.protocol("WM_DELETE_WINDOW", self._on_close)
          self.after(60, self._discover_async)

      def _unapplied(self):
          """Assignments made in this window that were never written to the game."""
          return self.assignments != dict(self.state.get("assignments", {}))

      def _on_close(self):
          """Closing with assignments that were never applied is the whole failure
          mode this guards: the court list shows the new pattern, the game shows the
          old one, and nothing says why."""
          if self._unapplied():
              answer = messagebox.askyesnocancel(
                  "Apply floor patterns?",
                  "Your court assignments haven't been written to the game yet.\n\n"
                  "Apply them now?", parent=self)
              if answer is None:
                  return
              if answer:
                  self._apply(then_close=True)
                  return
          if self.on_closed:
              self.on_closed()
          self.destroy()

      # -- chrome ------------------------------------------------------------
      def _styles(self):
          s = ttk.Style(self)
          try:
              s.theme_use("clam")
          except tk.TclError:
              pass
          T = self.T
          s.configure(".", background=T["bg"], foreground=T["text"], fieldbackground=T["entry"])
          s.configure("FP.TFrame", background=T["bg"])
          s.configure("FPPanel.TFrame", background=T["panel"])
          s.configure("FP.TLabel", background=T["bg"], foreground=T["text"])
          s.configure("FPPanel.TLabel", background=T["panel"], foreground=T["text"])
          s.configure("FPMuted.TLabel", background=T["panel"], foreground=T["muted"])
          s.configure("FPHead.TLabel", background=T["bg"], foreground=T["gold"],
                      font=("Segoe UI", 11, "bold"))
          s.configure("FP.TButton", background=T["accent"], foreground="#FFFFFF",
                      borderwidth=0, padding=(12, 6))
          s.map("FP.TButton", background=[("active", T["gold"]), ("disabled", T["entry"])],
                foreground=[("active", "#101010")])
          s.configure("FPDanger.TButton", background=T["red"], foreground="#FFFFFF",
                      borderwidth=0, padding=(12, 6))
          s.configure("FP.Treeview", background=T["entry"], fieldbackground=T["entry"],
                      foreground=T["text"], borderwidth=0, rowheight=22)
          s.configure("FP.Treeview.Heading", background=T["panel"], foreground=T["text"])
          s.map("FP.Treeview", background=[("selected", T["accent"])],
                foreground=[("selected", "#FFFFFF")])

      def _build(self):
          T = self.T
          root = ttk.Frame(self, style="FP.TFrame")
          root.pack(fill="both", expand=True, padx=12, pady=10)
          root.columnconfigure(0, weight=0)
          root.columnconfigure(1, weight=1)
          root.columnconfigure(2, weight=0)
          root.rowconfigure(1, weight=1)

          self.status = ttk.Label(root, text="Loading game files ...", style="FP.TLabel")
          self.status.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

          # ---- courts
          left = ttk.Frame(root, style="FP.TFrame")
          left.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(0, 12))
          ttk.Label(left, text="Courts", style="FPHead.TLabel").pack(anchor="w", pady=(0, 6))
          self.tree = ttk.Treeview(left, columns=("pat",), show="tree headings",
                                   style="FP.Treeview", height=26, selectmode="browse")
          self.tree.heading("#0", text="Team")
          self.tree.heading("pat", text="Floor Pattern")
          self.tree.column("#0", width=190, stretch=False)
          self.tree.column("pat", width=190, stretch=False)
          self.tree.pack(side="left", fill="y")
          sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
          sb.pack(side="left", fill="y")
          self.tree.configure(yscrollcommand=sb.set)
          self.tree.bind("<<TreeviewSelect>>", self._on_team)

          # ---- library
          mid = ttk.Frame(root, style="FP.TFrame")
          mid.grid(row=0, column=1, rowspan=2, sticky="nsew")
          head = ttk.Frame(mid, style="FP.TFrame")
          head.pack(fill="x")
          ttk.Label(head, text="Pattern Library", style="FPHead.TLabel").pack(side="left")
          self.slots_lbl = ttk.Label(head, text="", style="FP.TLabel",
                                     font=("Segoe UI", 10, "bold"))
          self.slots_lbl.pack(side="right")
          # Spelled out because the limit is easy to misread: it caps distinct
          # patterns, not courts.
          self.slots_help = ttk.Label(mid, text="", style="FP.TLabel",
                                      foreground=T["muted"], font=("Segoe UI", 8),
                                      wraplength=560, justify="left")
          self.slots_help.pack(anchor="w", pady=(2, 0))

          canvas_wrap = ttk.Frame(mid, style="FP.TFrame")
          canvas_wrap.pack(fill="both", expand=True, pady=(6, 0))
          self.gallery = tk.Canvas(canvas_wrap, bg=T["bg"], highlightthickness=0)
          gsb = ttk.Scrollbar(canvas_wrap, orient="vertical", command=self.gallery.yview)
          self.gallery.configure(yscrollcommand=gsb.set)
          self.gallery.pack(side="left", fill="both", expand=True)
          gsb.pack(side="left", fill="y")
          self.gal_inner = ttk.Frame(self.gallery, style="FP.TFrame")
          self.gallery.create_window((0, 0), window=self.gal_inner, anchor="nw")
          self.gal_inner.bind("<Configure>", lambda e: self.gallery.configure(
              scrollregion=self.gallery.bbox("all")))
          self.gallery.bind_all("<MouseWheel>", self._wheel)

          # ---- preview
          right = ttk.Frame(root, style="FP.TFrame")
          right.grid(row=0, column=2, rowspan=2, sticky="ns", padx=(12, 0))
          ttk.Label(right, text="Preview", style="FPHead.TLabel").pack(anchor="w", pady=(0, 6))
          pv_box = tk.Frame(right, bg=T["panel"], width=352, height=352)
          pv_box.pack()
          pv_box.pack_propagate(False)   # reserve the box before an image exists
          self.preview = tk.Label(pv_box, bg=T["panel"])
          self.preview.pack(fill="both", expand=True)
          # ---- court variant (retro era) selector
          # A team's floor texture lives on a single mat_bounce_stadium_parquet_*
          # material shared by every one of its courts, so a pattern can NOT be
          # set per era, and the wood tint isn't per-era either. This picker is
          # therefore reference only: it shows the court colours each era sits
          # under, so a wood tone can be judged against them.
          var_row = ttk.Frame(right, style="FP.TFrame")
          var_row.pack(fill="x", pady=(8, 0))
          ttk.Label(var_row, text="Court palette:", style="FP.TLabel").pack(side="left")
          self.variant_var = tk.StringVar()
          self.variant_cb = ttk.Combobox(var_row, textvariable=self.variant_var,
                                         state="disabled", width=24)
          self.variant_cb.pack(side="left", padx=(6, 0))
          self.variant_cb.bind("<<ComboboxSelected>>", lambda e: self._draw_palette())
          self._variant_choices = []
          self._variant_container = None
          self.palette_canvas = tk.Canvas(right, height=46, bg=T["bg"],
                                          highlightthickness=0)
          self.palette_canvas.pack(fill="x", pady=(4, 0))
          # The buttons are packed side="bottom" and BEFORE the notes below them, so
          # they get their space first and stay anchored to the bottom of the panel.
          # Packed top-down instead, a long note -- the retro-court text runs four
          # lines for a team like the Warriors -- pushed "Apply to game" past the
          # bottom edge of the window, where tk simply doesn't draw it. Assigning a
          # pattern then looked like it had taken effect (the court list and the
          # slot counter both update) while nothing was ever written to the game.
          actions = ttk.Frame(right, style="FP.TFrame")
          actions.pack(side="bottom", fill="x", pady=(10, 0))

          self.btn_assign = ttk.Button(actions, text="Assign to court", style="FP.TButton",
                                       command=self._assign, state="disabled")
          self.btn_assign.pack(fill="x", pady=2)
          ttk.Button(actions, text="Reset court to stock floor", style="FP.TButton",
                     command=self._unassign).pack(fill="x", pady=2)
          self.btn_export = ttk.Button(actions, text="Export pattern for editing ...",
                                       style="FP.TButton", command=self._export,
                                       state="disabled")
          self.btn_export.pack(fill="x", pady=(12, 2))
          self.tiled_export = tk.BooleanVar(value=False)
          tk.Checkbutton(actions, text="Export as 2x2 sheet (shows tile seams)",
                         variable=self.tiled_export, bg=T["bg"], fg=T["muted"],
                         selectcolor=T["entry"], activebackground=T["bg"],
                         activeforeground=T["text"], highlightthickness=0,
                         bd=0, anchor="w", font=("Segoe UI", 8)).pack(fill="x")
          ttk.Button(actions, text="Import custom PNG ...", style="FP.TButton",
                     command=self._import).pack(fill="x", pady=(6, 2))
          self.btn_apply = ttk.Button(actions, text="Apply to game", style="FP.TButton",
                                      command=self._apply)
          self.btn_apply.pack(fill="x", pady=(12, 2))
          ttk.Button(actions, text="Revert all floors", style="FPDanger.TButton",
                     command=self._revert).pack(fill="x", pady=2)

          # These wrap to a variable number of lines, so they take what's left and
          # are the things that clip on a short window -- never the buttons.
          self.variant_note = ttk.Label(right, text="", style="FP.TLabel",
                                        foreground=T["muted"], wraplength=352,
                                        justify="left", font=("Segoe UI", 8))
          self.variant_note.pack(anchor="w", pady=(3, 0))

          self.pv_caption = ttk.Label(right, text="Select a pattern", style="FP.TLabel",
                                      wraplength=352, justify="left")
          self.pv_caption.pack(anchor="w", pady=(8, 2))
          self.pv_note = ttk.Label(right, text="", style="FP.TLabel", foreground=T["muted"],
                                   wraplength=352, justify="left")
          self.pv_note.pack(anchor="w", pady=(0, 10))

      def _wheel(self, e):
          try:
              self.gallery.yview_scroll(int(-e.delta / 120), "units")
          except Exception:
              pass

      # -- data --------------------------------------------------------------
      def _discover_async(self):
          def work():
              # Cache the stock floor first and on its own. It isn't shipped with
              # the app (it's game art), and the gallery should still be able to
              # offer it as a template even if the donor probe below fails.
              try:
                  if not os.path.exists(STOCK_CACHE):
                      self.after(0, self._status, "Extracting the stock floor texture ...")
                      extract_stock_pattern(self.game_path)
              except Exception:
                  pass
              try:
                  self.mgr.discover(progress=lambda m: self.after(0, self._status, m))
                  self.after(0, self._populate)
              except Exception as ex:
                  self.after(0, lambda: self._status(f"ERROR: {ex}"))
                  self.after(0, self._build_gallery)   # patterns are still usable
          threading.Thread(target=work, daemon=True).start()

      def _status(self, msg):
          self.status.configure(text=msg)

      def _populate(self):
          for t in sorted(self.mgr.courts):
              self.tree.insert("", "end", iid=t, text=t,
                               values=(self._label_for(self.assignments.get(t)),))
          self._build_gallery()
          self._refresh_slots()
          j = " -- patterns currently applied" if FloorPatternManager.journal_active() else ""
          self._status(f"{len(self.mgr.courts)} courts found. "
                       f"{self.mgr.max_slots} pattern slots available "
                       f"({self.mgr.max_slots} distinct patterns can be live at once){j}")

      def _label_for(self, key):
          if not key:
              return "Stock parquet"
          for k, lab, _n, _c in library(self.game_path):
              if k == key:
                  return lab
          return key

      def _build_gallery(self):
          for w in self.gal_inner.winfo_children():
              w.destroy()
          cols = 3
          for i, (key, label, note, is_custom) in enumerate(library(self.game_path)):
              r, c = divmod(i, cols)
              try:
                  im = pattern_image(key, self.game_path).convert("RGB").resize(
                      (140, 140), Image.LANCZOS)
              except Exception:
                  continue      # one unreadable pattern shouldn't empty the gallery
              card = ttk.Frame(self.gal_inner, style="FPPanel.TFrame", padding=6)
              card.grid(row=r, column=c, padx=5, pady=5, sticky="n")
              ph = ImageTk.PhotoImage(im)
              self._thumbs[key] = ph
              lbl = tk.Label(card, image=ph, bd=2, relief="flat",
                             bg=self.T["panel"], cursor="hand2")
              lbl.pack()
              name = ttk.Label(card, text=label + ("  *" if is_custom else ""),
                               style="FPPanel.TLabel", wraplength=140)
              name.pack(anchor="w", pady=(4, 0))
              for w in (lbl, name, card):
                  w.bind("<Button-1>", lambda e, k=key: self._select_pattern(k))
          self.gal_inner.update_idletasks()
          self.gallery.configure(scrollregion=self.gallery.bbox("all"))

      def _current_variant(self):
          """Variant key currently chosen in the 'Preview as' picker."""
          label = self.variant_var.get()
          for key, disp in self._variant_choices:
              if disp == label:
                  return key
          return retro_eras.DEFAULT_VARIANT

      def _select_pattern(self, key, showing_court_floor=False):
          """Draw `key` in the preview.

          showing_court_floor says the preview is the selected court's actual
          floor rather than a pattern being auditioned. The caption has to make
          that difference obvious: previewing a library pattern tinted for
          whichever court happens to be selected reads as though that court
          already has the pattern, which is how a single assignment could look
          like it had changed every court.
          """
          self.sel_pattern = key
          # The wood is multiplied by the material's _Color_Parquet and nothing
          # else. Do NOT substitute a court colour here: those are surface and
          # marking colours (often near-black, e.g. #1A1A1A), and multiplying a
          # greyscale pattern by one renders the preview black.
          tint = (0.851, 0.621, 0.447)
          if self.sel_team and self.sel_team in self.mgr.courts:
              tint = self.mgr.courts[self.sel_team]["tint"]
          im = tiled_preview(key, tint=tint, reps=3, size=352, game_path=self.game_path)
          self._preview_img = ImageTk.PhotoImage(im)
          self.preview.configure(image=self._preview_img)
          assigned = self.assignments.get(self.sel_team) if self.sel_team else None
          for k, lab, note, _c in library(self.game_path):
              if k == key:
                  if showing_court_floor:
                      caption = f"{self.sel_team} floor: {lab}"
                  elif self.sel_team and assigned != key:
                      caption = f"{lab} -- previewing on {self.sel_team}, not assigned to it yet"
                  elif self.sel_team:
                      caption = f"{self.sel_team} floor: {lab}"
                  else:
                      caption = lab
                  self.pv_caption.configure(text=caption)
                  self.pv_note.configure(text=note)
          self.btn_assign.configure(state="normal" if self.sel_team else "disabled")
          self.btn_export.configure(state="normal")

      def _draw_palette(self):
          """Swatch strip of the selected era's court colours.

          Shown alongside the pattern rather than applied to it, so you can judge
          a wood tone against the palette it will sit under without the preview
          being destroyed by a near-black court colour.
          """
          cv = self.palette_canvas
          cv.delete("all")
          if not self._variant_container:
              return
          variant = self._current_variant()
          swatches = retro_eras.palette_for(self._variant_container, variant)
          if not swatches:
              return
          w = cv.winfo_width() or 352
          box = max(1, w // max(1, len(swatches)))
          for i, (label, hexval) in enumerate(swatches):
              x0 = i * box
              cv.create_rectangle(x0, 0, x0 + box - 2, 26, fill=hexval, outline="")
              cv.create_text(x0 + (box - 2) / 2, 34, text=label, anchor="n",
                             fill=self.T["muted"], font=("Segoe UI", 7))

      def _refresh_variants(self):
          """Repopulate the era picker for the selected team."""
          if not self.sel_team:
              self._variant_choices, self._variant_container = [], None
              self.variant_cb.configure(values=[], state="disabled")
              self.variant_var.set("")
              self.variant_note.configure(text="")
              self.palette_canvas.delete("all")
              return
          cname, choices = retro_eras.choices_from_json(self.sel_team)
          self._variant_container = cname
          self._variant_choices = choices
          self.variant_cb.configure(values=[d for _k, d in choices])
          self.variant_var.set(choices[0][1])
          if len(choices) > 1:
              self.variant_cb.configure(state="readonly")
              self.variant_note.configure(
                  text=("This team has %d retro court(s). They all share one floor pattern — the "
                        "game stores a single parquet material per team — so this picker is "
                        "reference only: it shows the court colours each era sits under. The "
                        "preview above always uses the team's real wood tint."
                        % (len(choices) - 1)))
          else:
              self.variant_cb.configure(state="disabled")
              self.variant_note.configure(
                  text="This team has no retro courts.")
          self._draw_palette()

      def _on_team(self, _e=None):
          sel = self.tree.selection()
          self.sel_team = sel[0] if sel else None
          self._refresh_variants()
          # Show the court its OWN floor, not whatever was last clicked in the
          # library. Carrying the library pick across every court made one
          # assignment look like it had been applied to all 36 of them.
          floor = self.assignments.get(self.sel_team) if self.sel_team else None
          if floor is None and self.sel_team and stock_available(self.game_path):
              floor = STOCK_KEY
          if floor:
              self._select_pattern(floor, showing_court_floor=True)
          elif self.sel_pattern:
              self._select_pattern(self.sel_pattern)
          self.btn_assign.configure(state="normal" if (self.sel_team and self.sel_pattern)
                                    else "disabled")

      def _refresh_slots(self):
          """Update the slot budget readout.

          A 'slot' is one of the dead donor textures a custom pattern is written
          into -- NOT a court. Distinct patterns consume slots; courts sharing a
          pattern share its slot, and courts left on the stock floor cost
          nothing. So all 40 courts can be reskinned with 8 or fewer patterns.
          """
          used = {k for k in self.assignments.values() if k}
          n, cap = len(used), self.mgr.max_slots
          self.slots_lbl.configure(text=f"Pattern slots used: {n} / {cap}")
          try:
              self.slots_lbl.configure(
                  foreground=self.T["red"] if n >= cap else self.T["text"])
          except tk.TclError:
              pass
          courts = len(self.mgr.courts)
          assigned = sum(1 for t, k in self.assignments.items() if k and t in self.mgr.courts)
          self.slots_help.configure(
              text=(f"{assigned} of {courts} courts have a custom pattern. "
                    f"A slot holds one distinct pattern — courts sharing a pattern share its "
                    f"slot, and courts on the stock floor use none, so all {courts} courts can "
                    f"be reskinned with {cap} or fewer patterns. "
                    f"{cap} is the hard cap: it's how many spare textures the game ships that "
                    f"are the right size to overwrite."))

      def _assign(self):
          if not (self.sel_team and self.sel_pattern):
              return
          if self.sel_pattern == STOCK_KEY:      # stock == reset, costs no slot
              self._unassign()
              return
          prospective = dict(self.assignments)
          prospective[self.sel_team] = self.sel_pattern
          used = {k for k in prospective.values() if k}
          if len(used) > self.mgr.max_slots:
              messagebox.showwarning(
                  "Slot limit",
                  f"Only {self.mgr.max_slots} patterns can be live at once, and you already "
                  f"have {len(used) - 1} in use.\n\nThe game has no spare texture objects "
                  f"beyond these. Reset a court that uses a pattern you no longer need, "
                  f"then try again.", parent=self)
              return
          self.assignments = prospective
          self.tree.set(self.sel_team, "pat", self._label_for(self.sel_pattern))
          self._refresh_slots()
          self._pending(f"{self._label_for(self.sel_pattern)} assigned to {self.sel_team}")

      def _unassign(self):
          if not self.sel_team:
              return
          self.assignments.pop(self.sel_team, None)
          self.tree.set(self.sel_team, "pat", "Stock parquet")
          self._refresh_slots()
          self._pending(f"{self.sel_team} reset to the stock floor")

      def _pending(self, what):
          """Assigning only changes this window -- say so, so it can't be mistaken
          for a change that reached the game."""
          self._status(f"{what}. Not written yet -- click 'Apply to game'.")

      def _export(self):
          """Save the selected pattern as an editable PNG."""
          if not self.sel_pattern:
              return
          label = self._label_for(self.sel_pattern)
          safe = "".join(ch for ch in label if ch.isalnum() or ch in " _-").strip()
          safe = safe.replace(" ", "_") or "floor_pattern"
          tiled = bool(self.tiled_export.get())
          if tiled:
              safe += "_2x2"
          p = filedialog.asksaveasfilename(
              parent=self, title="Export floor pattern",
              defaultextension=".png", initialfile=safe + ".png",
              filetypes=[("PNG images", "*.png")])
          if not p:
              return
          try:
              out = export_pattern(self.sel_pattern, p, tiled=tiled,
                                   game_path=self.game_path)
          except Exception as ex:
              messagebox.showerror("Export failed", str(ex), parent=self)
              return
          dims = "1024 x 1024 (2x2 sheet)" if tiled else "512 x 512"
          messagebox.showinfo(
              "Exported",
              f"{label}\n{os.path.basename(out)}  --  {dims}\n\n"
              f"Editing notes:\n"
              f"  - Keep it 512 x 512. A 2x2 sheet must be cropped back to one "
              f"512 quadrant before re-importing.\n"
              f"  - Keep it greyscale. The court's colour is multiplied over the "
              f"pattern, so colour in the file comes out tinted twice.\n"
              f"  - Keep it seamless. Opposite edges must line up or you'll see a "
              f"grid across the floor.\n\n"
              f"Bring it back in with Import custom PNG.", parent=self)
          self._status(f"Exported to {out}")

      def _import(self):
          p = filedialog.askopenfilename(parent=self, title="Choose a pattern PNG",
                                         filetypes=[("PNG images", "*.png"), ("All files", "*.*")])
          if not p:
              return
          try:
              key = import_custom(p)
          except Exception as ex:
              messagebox.showerror("Import failed", str(ex), parent=self)
              return
          self._build_gallery()
          self._select_pattern(key)
          self._status(f"Imported {os.path.basename(p)} -- greyscale images work best, "
                       f"since the court colour is multiplied over the pattern.")

      def _apply(self, then_close=False):
          if not self.mgr.loaded:
              self._status("Still reading the game files -- try again in a moment.")
              return
          self.btn_assign.configure(state="disabled")

          def work():
              try:
                  n, _slots = self.mgr.apply(self.assignments,
                                             progress=lambda m: self.after(0, self._status, m))
                  self.state["assignments"] = dict(self.assignments)
                  save_state(self.state)
                  self.after(0, lambda: messagebox.showinfo(
                      "Applied", f"{n} courts updated.\n\nRestart the game to see the change.",
                      parent=self))
                  if then_close:
                      self.after(0, self._on_close)
              except Exception as ex:
                  self.after(0, lambda: messagebox.showerror("Apply failed", str(ex), parent=self))
          threading.Thread(target=work, daemon=True).start()

      def _revert(self):
          if not messagebox.askyesno("Revert floors",
                                     "Restore every court to the stock parquet?\n\n"
                                     "This only undoes floor pattern changes -- your other "
                                     "mods are untouched.", parent=self):
              return
          n = self.mgr.revert()
          self.assignments = {}
          for t in self.mgr.courts:
              if self.tree.exists(t):
                  self.tree.set(t, "pat", "Stock parquet")
          self.state["assignments"] = {}
          save_state(self.state)
          self._refresh_slots()
          self._status(f"Reverted {n} regions. Floors are back to stock.")


def open_floor_patterns(parent, game_path, theme=None):
    """Entry point for app.py."""
    if not HAVE_TK:
        raise RuntimeError("tkinter is not available in this Python install")
    return FloorPatternWindow(parent, game_path, theme=theme)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    gp = a.path
    if not gp:
        cfg = os.path.join(APP_DIR, "config.json")
        if os.path.exists(cfg):
            gp = json.load(open(cfg, encoding="utf-8")).get("game_data_path")
    if a.selftest:
        m = FloorPatternManager(gp).discover(progress=print)
        print(f"courts={len(m.courts)} slots={m.max_slots} fileID={m.file_id}")
        for key, lab, _n, _c in library(gp):
            d = encode_dxt1_mipchain(pattern_image(key, gp))
            print(f"  {key:<18} {len(d):>8,} bytes  "
                  f"{'OK' if len(d) == EXPECTED_SLOT_BYTES else 'SIZE MISMATCH'}")
        sys.exit(0)
    root = tk.Tk()
    root.withdraw()
    w = open_floor_patterns(root, gp)
    w.on_closed = root.destroy      # window's own handler prompts, then calls this
    root.mainloop()
