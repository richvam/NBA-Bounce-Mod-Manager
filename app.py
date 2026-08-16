import sys
import subprocess
import os

# ── Where everything lives ─────────────────────────────────────────────────────
# app.py is the only Python file at the top of the folder; the feature modules
# live in modules/, their catalogs in data/, the guides in docs/, developer
# scripts in tools/, and everything this app writes in user/. Putting modules/
# on the import path first means every module below (and every module they
# import from each other) is still found by its plain name.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "modules"))

# ── Self-installer ─────────────────────────────────────────────────────────────
REQUIRED_PACKAGES = [
    "attrs", "lz4", "brotli", "fsspec", "etcpak",
    "texture2ddecoder", "astc-encoder-py", "Pillow", "UnityPy",
    "dnfile",   # Gameplay Sliders: reads Assembly-CSharp.dll field layout
    "pygame",   # Audio tab: clip playback
    "numpy",    # Meshes tab: optional, only speeds the 3D viewer up
]

def _ensure_dependencies():
    try:
        import UnityPy
        from PIL import Image
        import dnfile          # Gameplay Sliders; probe it or the early return below hides it
        return True
    except ImportError:
        pass
    failed = []
    for pkg in REQUIRED_PACKAGES:
        r = subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--upgrade"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            failed.append(f"{pkg}: {r.stderr.strip()}")
    try:
        import UnityPy
        from PIL import Image
        return True          # dnfile is optional: only Gameplay Sliders needs it,
                             # and slider_manager.py installs it on demand
    except ImportError as e:
        return str(e) + ("\n\nFailed packages:\n" + "\n".join(failed) if failed else "")

status = _ensure_dependencies()
if status is not True:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk(); root.withdraw()
    messagebox.showerror("Install Failed",
        f"Could not install required packages.\n\n"
        f"Open Command Prompt AS ADMINISTRATOR and run:\n\n"
        f"  {sys.executable} -m pip install UnityPy Pillow attrs lz4 brotli\n\n"
        f"Error: {status}")
    root.destroy(); sys.exit(1)

# ── Imports ────────────────────────────────────────────────────────────────────
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json, shutil, threading, re, struct
from datetime import datetime
from PIL import Image, ImageTk, ImageGrab
import UnityPy
import app_paths
from audio_manager import AudioManagerFrame, THEMES
import retro_eras
import sprite_crop

# ── Constants ──────────────────────────────────────────────────────────────────
APP_NAME       = "NBA Bounce Mod Manager"
CONFIG_FILE    = app_paths.user("config.json")
MODS_META_FILE = "mods.json"
BACKUP_SUFFIX  = ".original_backup"

# Court color customization -- validated by hand against a live game session.
# The floor/apron colors aren't Texture2D pixel data like everything else in
# this app; they're named Color properties on each team's
# "mat_bounce_stadium_parquet_{Team}" Material.
#
# Several other same-shader properties were tested and found NOT to work --
# i.e. they can be written and read back off disk just fine, but nothing
# about the live rendered court actually changes:
#   _Color_Outside_Court_R    -- gated behind a disabled, unused texture slot
#   _Color_Restricted_Line_A  -- doesn't drive the restricted-arc line at all;
#     an early test appeared to work only because the test color happened to
#     look similar to the team's actual (code-driven, not material-driven)
#     accent color. Verified wrong by writing a color to BOTH
#     stadium_parquet and stadium_lines and confirming on-disk, live, that
#     neither one matched what actually rendered in-game.
#   _Color_Area_Line_R ("Out-of-Bounds Color") -- originally shipped in this
#     app writing/reading stadium_parquet, where every team has the exact
#     same [1,0,0,0] red, so a per-team difference was never even possible;
#     moved to stadium_lines instead, where the value genuinely does differ
#     by team (LosAngelesLakers gold, GoldenStateWarriors blue, vs. a shared
#     neutral gray for teams without a distinct accent) -- looked like a
#     real fix. Live-tested anyway with an impossible-to-miss neon
#     magenta/cyan on MiamiHeat's stadium_lines material, verified written
#     and read back correctly on disk, and the court in-game showed zero
#     visible change anywhere. Same conclusion as _Color_Restricted_Line_A:
#     this whole cluster of "_Color_*_Line_*" properties (Area_Line_R,
#     Free_Throw_Line_B, Lane_Line_B, ThreePoint_Line_G, Restricted_Line_A)
#     shares identical values within a team on stadium_lines, consistent
#     with all of them being set together by game code at runtime rather
#     than read from the serialized Material -- meaning no amount of
#     asset-patching will make Out-of-Bounds Color render.
#   _Color_Area_G ("Team Color") -- matched real NBA team colors almost
#     exactly across all 40 teams (BostonCeltics green, MiamiHeat red,
#     CharlotteHornets teal, etc.), which made it the most promising lead
#     of the three. Its alpha channel even varies by team unlike everything
#     else here (34 teams ship at 0.0, 5 -- IndianaPacers, NewJerseyNets,
#     OrlandoMagic, SeattleSuperSonics, VancouverGrizzlies -- ship at 1.0),
#     which looked like exactly the kind of detail that would explain a
#     silent failure. Live-tested properly this time: IndianaPacers
#     specifically (one of the 5 alpha=1.0 teams, so not blocked by a
#     transparent alpha like the first MiamiHeat attempt was), forced
#     alpha=1.0 on write, set to an unmissable pink, checked the court, the
#     center-logo decal, AND season mode. Zero visible change anywhere.
#     Matching real team colors this closely and still not rendering is the
#     clearest sign yet that this whole family of per-team "identity"
#     Color properties on this material is authored reference data that
#     something else in the game (not the live shader) reads -- almost
#     certainly the same code-driven system already confirmed for
#     _Color_Restricted_Line_A and _Color_Area_Line_R above. Removed from
#     the UI for the same reason as Out-of-Bounds Color.
# All three of the above can be written and read back off disk correctly --
# this isn't a bug in apply_material_color_mod, it's that the live shader
# genuinely doesn't sample them. Only Floor Color is fully confirmed live,
# on stadium_parquet. Reaching actual per-team court/logo color customization
# beyond it would mean decompiling the game's code (not just its assets) to
# find whatever system actually applies these colors, which is a much
# bigger undertaking than anything else in this app.
#
# _Court_Lines_ON_OFF: confirmed by comparing the very first working test
# (pure red _Color_Parquet, alpha=0, _Court_Lines_ON_OFF left at its default
# 0 -- floor rendered red AND kept its wood-grain texture) against later
# tests that forced _Court_Lines_ON_OFF=1.0 -- with lines ON, _Color_Parquet
# stopped tinting the texture entirely (falls back to the untouched default
# look at alpha<1, or a flat solid fill at alpha=1). So stadium_parquet's
# _Court_Lines_ON_OFF must stay 0 for Floor Color to render as a tint; it's
# normalized back to 0 on every Apply regardless of what's touched, since an
# older version of this feature could leave it stuck at 1 for a team where
# Out-of-Bounds Color (back when it also lived on stadium_parquet) had been
# tried -- which would have been silently breaking that team's Floor Color.
#
# Why "copy colors from Celtics to Heat" first looked like it did nothing:
# team_court_palette.json (dumped straight from the live .assets files) shows
# _Color_Parquet is nearly the same pale wood tint for EVERY team (Celtics
# ~rgb(217,158,114), Heat ~rgb(231,185,145) -- both just "natural wood", not
# team-branded). Copying it between two normal-wood teams is a real write,
# just a visually subtle one -- nothing wrong with the copy itself.
COURT_COLOR_MATERIAL_SUFFIX = "stadium_parquet"
COURT_COLOR_WRITE_SUFFIXES  = ["stadium_parquet"]
COURT_COLOR_PROPERTIES = [
    # (friendly label shown in the UI, shader property name, material suffix
    #  confirmed to actually render this property, forced alpha to write,
    #  whether picking this property requires _Court_Lines_ON_OFF = 1.0)
    ("Floor Color", "_Color_Parquet", "stadium_parquet", 0.0, False),
]
COURT_LINES_TOGGLE_PROPERTY = "_Court_Lines_ON_OFF"

# ── Court LINE colors -- a different mechanism from the Material colors above ──
# The line colors are NOT read from the Material at runtime. Decompiling
# Assembly-CSharp.dll showed bounce.CourtManager.setUpMaterial() reapplies them
# on every court load from per-team data serialized in bounce.BasketTeamContainer
# ScriptableObjects inside sharedassets1.assets -- which is exactly why editing
# mat_bounce_stadium_lines_{Team} verified correctly on disk yet changed nothing
# in game. Full write-up in COURT_LINE_COLORS_SOLVED.md.
#
# Patching these is if anything SAFER than the Material path: each value is a
# bare 4 x float32 RGBA at a known file offset, so there's no reserialization at
# all -- we overwrite 16 bytes in place and the file size cannot change.
CONTAINER_ASSETS_FILE   = "sharedassets1.assets"
CONTAINER_SCRIPT_CLASS  = "BasketTeamContainer"
CONTAINER_DEFAULT_VARIANT = "default"

# IMPORTANT: these labels describe what each property was OBSERVED to colour
# during live rainbow tests (one neon per property, one game launch each). The
# shader property names are actively misleading -- _Color_Restricted_Line_A does
# NOT drive the restricted arc, it drives the main court line work, and
# _Color_Area_G is the painted key fill rather than any kind of "area line".
# Trust the labels, not the names.
#
# Also note _Color_Outside_Court_R works fine here even though the earlier
# material-side investigation concluded it was dead. It was never dead -- it was
# being reapplied from the container every time the court loaded, so edits to the
# material were simply overwritten.
COURT_LINE_PROPERTIES = [
    # (friendly label, shader property name, tooltip/description)
    # -- filled areas --
    ("Sideline / Apron",   "_Color_Outside_Court_R",
     "The wide band around the court where the team name is printed."),
    ("Key / Paint Fill",   "_Color_Area_G",
     "The solid colour of the painted key (the lane)."),
    ("Outer Surround",     "_Color_Floor_Court_B",
     "The outermost floor band, beyond the apron."),
    # -- lines and markings --
    ("Court Lines",        "_Color_Restricted_Line_A",
     "Sidelines, baseline, half-court line and the key outline."),
    ("Key / Paint Marks",  "_Color_Area_Line_R",
     "The hash marks and tick marks in and around the painted key."),
    ("3-Point Line",       "_Color_ThreePoint_Line_G",
     "The three-point arc."),
    ("Free-Throw Circle",  "_Color_Free_Throw_Line_B",
     "The free-throw circle."),
]

# ── Config ─────────────────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f: return json.load(f)
    return {"game_data_path": "", "mods_folder": "", "theme": "dark"}

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=2, ensure_ascii=False)

def load_mods_meta(folder):
    p = os.path.join(folder, MODS_META_FILE)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f: return json.load(f)
    return {}

def save_mods_meta(folder, meta):
    with open(os.path.join(folder, MODS_META_FILE), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

# ── Unity helpers ──────────────────────────────────────────────────────────────
def find_assets_files(game_data_path):
    """Return all .assets files, plus level files which also contain textures."""
    result = []
    for f in sorted(os.listdir(game_data_path)):
        full = os.path.join(game_data_path, f)
        if f.endswith(".assets"):
            result.append(full)
        # level files (level0, level1, level2 ...) also contain Texture2D
        elif f.startswith("level") and not "." in f:
            result.append(full)
    return result

def extract_textures_from_file(assets_path):
    """
    UnityPy 1.25 API:
      obj.read()   → Texture2D instance
      data.m_Name  → texture name (use peek_name for speed)
      data.m_Width / data.m_Height
      data.m_TextureFormat  → int, convert via TextureFormat enum
      data.image   → PIL Image
    Use obj.peek_name() for fast name reading without full parse.
    """
    textures = []
    env = UnityPy.load(assets_path)
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        try:
            # peek_name is fast — reads only the name field
            name = obj.peek_name() or f"texture_{obj.path_id}"
            # Read full object only for the remaining metadata
            data = obj.read()
            # m_TextureFormat is an int; try to get the enum name
            try:
                from UnityPy.enums import TextureFormat
                fmt = TextureFormat(data.m_TextureFormat).name
            except Exception:
                fmt = str(data.m_TextureFormat)
            textures.append({
                "name":        name,
                "path_id":     obj.path_id,
                "assets_file": assets_path,
                "width":       data.m_Width,
                "height":      data.m_Height,
                "format":      fmt,
            })
        except Exception as e:
            # Still add it with fallback info so it shows in the list
            try:
                textures.append({
                    "name":        obj.peek_name() or f"texture_{obj.path_id}",
                    "path_id":     obj.path_id,
                    "assets_file": assets_path,
                    "width":       0,
                    "height":      0,
                    "format":      "unknown",
                })
            except Exception:
                pass
    return textures

def get_texture_image(assets_path, path_id):
    source = assets_path + BACKUP_SUFFIX if os.path.exists(assets_path + BACKUP_SUFFIX) else assets_path
    env = UnityPy.load(source)
    for obj in env.objects:
        if obj.path_id == path_id:
            data = obj.read()
            return data.image   # PIL Image
    return None

COURT_DECAL_ASSETS_FILE = "resources.assets"
COURT_DECAL_PREFIX      = "txt_bounce_court_"

# {assets_path: {texture_name: path_id}} -- resources.assets is large, so the
# name index is built once per session and reused for every preview.
_court_decal_index = {}

def court_decal_index(game_data_path):
    """Map every txt_bounce_court_* decal name to its path_id.

    Uses peek_name(), which reads only the name field, so indexing doesn't pay
    the cost of parsing every Texture2D in resources.assets.
    """
    assets_path = os.path.join(game_data_path, COURT_DECAL_ASSETS_FILE)
    if assets_path in _court_decal_index:
        return _court_decal_index[assets_path]
    index = {}
    if os.path.isfile(assets_path):
        source = (assets_path + BACKUP_SUFFIX
                  if os.path.exists(assets_path + BACKUP_SUFFIX) else assets_path)
        try:
            env = UnityPy.load(source)
            for obj in env.objects:
                if obj.type.name != "Texture2D":
                    continue
                try:
                    name = obj.peek_name()
                except Exception:
                    continue
                if name and name.startswith(COURT_DECAL_PREFIX):
                    index[name] = obj.path_id
        except Exception:
            pass                      # non-fatal: previews just stay unavailable
    _court_decal_index[assets_path] = index
    return index

def get_court_decal_image(game_data_path, texture_name):
    """Decode one center-court logo decal to a PIL image, or None.

    These decals are 2048x256 and mostly transparent -- they carry only the
    centre-court wordmark/logo, not the floor -- so callers should composite
    the result over a background to make it legible.
    """
    index = court_decal_index(game_data_path)
    path_id = index.get(texture_name)
    if path_id is None:
        return None
    assets_path = os.path.join(game_data_path, COURT_DECAL_ASSETS_FILE)
    try:
        img = get_texture_image(assets_path, path_id)
    except Exception:
        return None
    return img.convert("RGBA") if img is not None else None

def _find_ress_file(assets_path, stream_path):
    """Find the .resS companion file from an m_StreamData.path string."""
    import ntpath
    assets_dir = os.path.dirname(assets_path)
    # stream_path is e.g. 'resources.assets.resS' or 'sharedassets1.assets.resS'
    basename = ntpath.basename(stream_path)
    candidate = os.path.join(assets_dir, basename)
    if os.path.exists(candidate):
        return candidate
    # Try without archive:/ prefix
    for part in stream_path.replace("\\", "/").split("/"):
        candidate = os.path.join(assets_dir, part)
        if os.path.exists(candidate):
            return candidate
    return None


def _get_encode_format(original_format_id):
    """
    Returns (encode_as_fmt, write_format_id) for the given original format.

    Rules:
    - DXT5/DXT5Crunched -> encode BC7, write BC7 format ID (same 1bpp size, better quality)
    - DXT1/DXT1Crunched -> encode DXT1, write DXT1 format ID (same 0.5bpp size)
    - Anything else     -> encode same format, write same format ID (safe fallback)

    CRITICAL: encode format and write format ID MUST match.
    Unity uses the format ID to decide how to decode the bytes.
    Writing BC7 bytes but leaving format=DXT5Crunched produces garbage.
    """
    from UnityPy.enums import TextureFormat as TF
    DXT5_FAMILY = {TF.DXT5.value, TF.DXT5Crunched.value}
    DXT1_FAMILY = {TF.DXT1.value, TF.DXT1Crunched.value}
    if original_format_id in DXT5_FAMILY:
        return TF.BC7, TF.BC7.value    # upgrade to BC7 (same 1bpp size, higher quality)
    elif original_format_id in DXT1_FAMILY:
        return TF.DXT1, TF.DXT1.value  # decode-compatible (same 0.5bpp size)
    else:
        try:
            fmt = TF(original_format_id)
            return fmt, fmt.value
        except ValueError:
            return TF.RGBA32, TF.RGBA32.value


def apply_single_mod(assets_path, path_id, replacement_png_path, ress_reset=None):
    """
    IN-PLACE BINARY PATCH — the only reliable method that prevents data loss.

    Encoding rules (encode format and format ID always match):
    - DXT5/DXT5Crunched -> BC7  format + BC7  bytes (same 1bpp, better quality)
    - DXT1/DXT1Crunched -> DXT1 format + DXT1 bytes (same 0.5bpp)
    - All others        -> same format + same bytes  (safe)

    Since all format conversions keep the same bytes-per-pixel:
    - 1bpp formats stay 1bpp  (DXT5/BC7 family)
    - 0.5bpp formats stay 0.5bpp (DXT1 family)
    The serialized object byte count stays identical -> in-place patch works for ALL sizes.

    ress_reset: set of .resS paths already rewound to their backup during this
    apply run. Every texture in this game is streamed, so pixel bytes are
    appended to the .resS and the object points at that offset. The .resS may
    only be rewound ONCE per run -- rewinding it per mod (as this used to) drops
    the bytes of every mod applied before it in the same file, leaving those
    textures pointing at a stale offset, often past the new end of file, which
    the game renders as a garbled or half-missing image. Pass a shared set when
    applying several mods; None means "this is a one-off" and rewinds.
    """
    from UnityPy.export.Texture2DConverter import image_to_texture2d
    from UnityPy.streams.EndianBinaryWriter import EndianBinaryWriter
    from UnityPy.helpers.TypeTreeHelper import write_typetree

    env = UnityPy.load(assets_path)

    target_obj = None
    for obj in env.objects:
        if obj.path_id == path_id:
            target_obj = obj
            break

    if target_obj is None:
        raise ValueError(f"Texture with path_id {path_id} not found")

    data        = target_obj.read()
    new_image   = Image.open(replacement_png_path).convert("RGBA")
    orig_format = data.m_TextureFormat
    stream_path = getattr(data.m_StreamData, 'path', '') if data.m_StreamData else ''
    ress_path   = _find_ress_file(assets_path, stream_path) if stream_path else None
    has_ress    = bool(ress_path and os.path.exists(ress_path))

    # Get matching encode format + format ID (both must match)
    encode_fmt, write_format_id = _get_encode_format(orig_format)
    enc_bytes, _ = image_to_texture2d(new_image, encode_fmt)

    if has_ress:
        # Rewind .resS to its backup so we append to a clean base -- but only the
        # first time this run touches this .resS, or we'd throw away the bytes of
        # every mod already applied to the same file (see docstring).
        if ress_reset is None:
            ress_reset = set()
        ress_key = os.path.abspath(ress_path)
        if ress_key not in ress_reset:
            ress_bak = ress_path + BACKUP_SUFFIX
            if os.path.exists(ress_bak):
                shutil.copy2(ress_bak, ress_path)
            ress_reset.add(ress_key)

        ress_size_before = os.path.getsize(ress_path)
        with open(ress_path, 'ab') as rf:
            rf.write(enc_bytes)

        data.m_StreamData.offset = ress_size_before
        data.m_StreamData.size   = len(enc_bytes)
        data.image_data          = b""
    else:
        data.image_data = enc_bytes
        if data.m_StreamData is not None:
            data.m_StreamData.path   = ""
            data.m_StreamData.offset = 0
            data.m_StreamData.size   = 0

    # Update metadata — format ID and bytes always match
    data.m_Width             = new_image.width
    data.m_Height            = new_image.height
    data.m_TextureFormat     = write_format_id  # MUST match encode_fmt
    data.m_CompleteImageSize = len(enc_bytes)
    if data.m_MipMap   is not None: data.m_MipMap   = False
    if data.m_MipCount is not None: data.m_MipCount = 1

    # Serialize only this object's bytes
    writer = EndianBinaryWriter(endian=target_obj.reader.endian)
    node   = target_obj._get_typetree_node()
    write_typetree(data, node, writer, env.file)
    new_obj_bytes      = writer.bytes
    original_byte_size = target_obj.byte_size
    byte_start         = target_obj.byte_start

    if len(new_obj_bytes) != original_byte_size:
        raise ValueError(
            f"Size mismatch for '{target_obj.peek_name()}': "
            f"original={original_byte_size}b new={len(new_obj_bytes)}b. "
            f"orig_format={orig_format} encode={encode_fmt.name}. Cannot patch in-place."
        )

    # Splice new bytes into the original file — everything else bit-for-bit identical
    with open(assets_path, "rb") as f:
        file_bytes = bytearray(f.read())

    file_bytes[byte_start : byte_start + original_byte_size] = new_obj_bytes

    with open(assets_path, "wb") as f:
        f.write(file_bytes)


def revert_object_from_backup(assets_path, path_id):
    """Restore ONE object's serialized bytes from the .assets backup, in place.

    Used when a mod is removed: the texture (and the sprite crop repaired
    alongside it) goes back to stock without disturbing any other mod in the same
    file.

    The object is located in BOTH files rather than assuming they share a
    layout. Every texture patch is size-neutral, so the two usually do agree --
    but the Meshes tab's rebuild fallback re-serializes a whole .assets file and
    shifts every object after the one it grew, and reading the backup at the
    live file's offset would then splice in the wrong object's bytes.
    """
    bak = assets_path + BACKUP_SUFFIX
    if not os.path.exists(bak):
        return False
    live = next((o for o in UnityPy.load(assets_path).objects
                 if o.path_id == path_id), None)
    orig = next((o for o in UnityPy.load(bak).objects
                 if o.path_id == path_id), None)
    if live is None or orig is None or live.byte_size != orig.byte_size:
        return False
    with open(bak, "rb") as f:
        f.seek(orig.byte_start)
        original = f.read(orig.byte_size)
    if len(original) != live.byte_size:
        return False
    with open(assets_path, "r+b") as f:
        f.seek(live.byte_start)
        f.write(original)
    return True


def get_companion_files(assets_path):
    """Return list of companion .resS/.resource files for a given .assets file."""
    companions = []
    for suffix in [".resS", ".resource"]:
        c = assets_path + suffix
        if os.path.exists(c):
            companions.append(c)
    return companions

def ensure_backup(assets_path):
    """Back up the .assets file AND its companion .resS/.resource files."""
    bak = assets_path + BACKUP_SUFFIX
    if not os.path.exists(bak):
        shutil.copy2(assets_path, bak)
        # Also back up companion files
        for companion in get_companion_files(assets_path):
            comp_bak = companion + BACKUP_SUFFIX
            if not os.path.exists(comp_bak):
                shutil.copy2(companion, comp_bak)
        return True
    return False

def restore_backup(assets_path):
    """Restore the .assets file AND its companion .resS/.resource files."""
    bak = assets_path + BACKUP_SUFFIX
    if os.path.exists(bak):
        shutil.copy2(bak, assets_path)
        # Also restore companion files
        for companion in get_companion_files(assets_path):
            comp_bak = companion + BACKUP_SUFFIX
            if os.path.exists(comp_bak):
                shutil.copy2(comp_bak, companion)
        return True
    return False

# ── Court color helpers ──────────────────────────────────────────────────────
# Same in-place binary patch technique as apply_single_mod(), applied to a
# Material's Color/float properties instead of a Texture2D's pixel data.
# Safe for the same reason: we only overwrite values inside already-existing,
# fixed-size fields, so the reserialized object comes out the same byte size
# as the original -- the guarantee apply_single_mod() also relies on.

def _iter_named_props(container):
    """Yield (name, value) from a dict-like or list-of-tuple-like UnityPy
    typetree container -- m_Colors / m_Floats vary in shape by UnityPy version."""
    if container is None:
        return
    if hasattr(container, "items"):
        for k, v in container.items():
            yield k, v
    else:
        for entry in container:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                yield entry[0], entry[1]
            else:
                first = getattr(entry, "first", None)
                second = getattr(entry, "second", None)
                if first is not None:
                    yield first, second

def _color_to_tuple(c):
    try:
        return (float(c.r), float(c.g), float(c.b), float(c.a))
    except AttributeError:
        return None

def _collect_material_props(data):
    saved = getattr(data, "m_SavedProperties", None)
    if saved is None:
        return {}, {}
    colors = {name: _color_to_tuple(c) for name, c in _iter_named_props(getattr(saved, "m_Colors", None))}
    floats = {}
    for name, f in _iter_named_props(getattr(saved, "m_Floats", None)):
        try:
            floats[name] = float(f)
        except (TypeError, ValueError):
            pass
    return colors, floats

def _set_color_prop(saved_properties, prop_name, rgba):
    container = getattr(saved_properties, "m_Colors", None)
    if container is None:
        return False
    if hasattr(container, "keys"):
        if prop_name not in container:
            return False
        c = container[prop_name]
        c.r, c.g, c.b, c.a = rgba
        return True
    for entry in container:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            name, c = entry
        else:
            name = getattr(entry, "first", None)
            c = getattr(entry, "second", None)
        if name == prop_name and c is not None:
            c.r, c.g, c.b, c.a = rgba
            return True
    return False

def _set_float_prop(saved_properties, prop_name, value):
    container = getattr(saved_properties, "m_Floats", None)
    if container is None:
        return False
    if hasattr(container, "keys"):
        if prop_name not in container:
            return False
        container[prop_name] = value
        return True
    for i, entry in enumerate(container):
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            name, _v = entry
            if name == prop_name:
                container[i] = (name, value)
                return True
        else:
            name = getattr(entry, "first", None)
            if name == prop_name and hasattr(entry, "second"):
                entry.second = value
                return True
    return False

def find_court_teams(game_data_path):
    """Scan every .assets file for mat_bounce_stadium_parquet_{Team} Materials
    and return the sorted list of team names found (e.g. 'LosAngelesLakers').
    Live scan, not hardcoded, so it stays correct after a game update."""
    pattern = re.compile(rf"^mat_bounce_{re.escape(COURT_COLOR_MATERIAL_SUFFIX)}_(.+)$")
    teams = set()
    for af in find_assets_files(game_data_path):
        try:
            env = UnityPy.load(af)
        except Exception:
            continue
        for obj in env.objects:
            if obj.type.name != "Material":
                continue
            try:
                name = obj.peek_name()
            except Exception:
                continue
            if not name:
                continue
            m = pattern.match(name)
            if m:
                teams.add(m.group(1))
    return sorted(teams)

def find_court_material(game_data_path, team, material_suffix=COURT_COLOR_MATERIAL_SUFFIX):
    """Locate the mat_bounce_{material_suffix}_{team} Material.
    Returns (assets_path, path_id) or (None, None)."""
    mat_name = f"mat_bounce_{material_suffix}_{team}"
    for af in find_assets_files(game_data_path):
        try:
            env = UnityPy.load(af)
        except Exception:
            continue
        for obj in env.objects:
            if obj.type.name != "Material":
                continue
            try:
                name = obj.peek_name()
            except Exception:
                continue
            if name == mat_name:
                return af, obj.path_id
    return None, None

def read_court_colors(assets_path, path_id, from_backup=False):
    """Read current (color_name -> rgba, float_name -> value) for the Material
    at path_id. With from_backup=True, reads from the .original_backup file
    instead (if one exists) -- used by Reset to recover true original values
    even after other edits have already been made to the live file."""
    source = assets_path
    if from_backup:
        bak = assets_path + BACKUP_SUFFIX
        if os.path.exists(bak):
            source = bak
    env = UnityPy.load(source)
    for obj in env.objects:
        if obj.path_id == path_id:
            data = obj.read()
            return _collect_material_props(data)
    return {}, {}

def apply_material_color_mod(assets_path, path_id, color_sets=None, float_sets=None):
    """
    IN-PLACE BINARY PATCH for a Material's Color/float properties -- the
    court-color equivalent of apply_single_mod(). Only overwrites values
    inside already-existing, fixed-size Color/float fields, so the
    reserialized object comes out the same byte size as the original.
    """
    from UnityPy.streams.EndianBinaryWriter import EndianBinaryWriter
    from UnityPy.helpers.TypeTreeHelper import write_typetree

    env = UnityPy.load(assets_path)
    target_obj = None
    for obj in env.objects:
        if obj.path_id == path_id:
            target_obj = obj
            break
    if target_obj is None:
        raise ValueError(f"Object with path_id {path_id} not found in {assets_path}")

    data  = target_obj.read()
    saved = getattr(data, "m_SavedProperties", None)
    if saved is None:
        raise ValueError(f"'{target_obj.peek_name()}' has no m_SavedProperties -- is this a Material?")

    missing = []
    for prop_name, rgba in (color_sets or {}).items():
        if not _set_color_prop(saved, prop_name, rgba):
            missing.append(prop_name)
    for prop_name, value in (float_sets or {}).items():
        if not _set_float_prop(saved, prop_name, value):
            missing.append(prop_name)
    if missing:
        raise ValueError(f"Propert(y/ies) not found on this material: {', '.join(missing)}")

    # Serialize only this object's bytes -- same technique as apply_single_mod()
    writer = EndianBinaryWriter(endian=target_obj.reader.endian)
    node   = target_obj._get_typetree_node()
    write_typetree(data, node, writer, env.file)
    new_obj_bytes      = writer.bytes
    original_byte_size = target_obj.byte_size
    byte_start         = target_obj.byte_start

    if len(new_obj_bytes) != original_byte_size:
        raise ValueError(
            f"Size mismatch for '{target_obj.peek_name()}': "
            f"original={original_byte_size}b new={len(new_obj_bytes)}b. Cannot patch in-place."
        )

    with open(assets_path, "rb") as f:
        file_bytes = bytearray(f.read())
    file_bytes[byte_start : byte_start + original_byte_size] = new_obj_bytes
    with open(assets_path, "wb") as f:
        f.write(file_bytes)

# ── Court LINE color helpers (BasketTeamContainer runtime overrides) ─────────
# These read/write the per-team color table that CourtManager applies over the
# Material at load time. Everything here is raw-byte work: UnityPy cannot
# read_typetree() these objects at all (this build ships no usable type tree for
# custom MonoBehaviours -- 2,356 of 2,361 fail), so the MonoBehaviour header and
# the MaterialProfile list are parsed directly from the object's bytes using the
# layout recovered from the DLL metadata.

def _mb_read_str(raw, off):
    """Read an int32-length-prefixed string at off, then align to 4."""
    (n,) = struct.unpack_from("<i", raw, off)
    off += 4
    s = raw[off:off + n].decode("utf-8", "replace")
    off += n
    return s, (off + 3) & ~3

def _mb_script_path_id(raw):
    """m_Script PPtr lives at a fixed offset in every MonoBehaviour:
    m_GameObject PPtr (12) + m_Enabled byte + 3 pad (4) = 16."""
    return struct.unpack_from("<iq", raw, 16)[1]

def _find_container_script_path_id(game_data_path):
    """Locate the MonoScript for BasketTeamContainer rather than hardcoding its
    path_id, so a game update can't silently point us at the wrong script."""
    ggm = os.path.join(game_data_path, "globalgamemanagers.assets")
    if not os.path.isfile(ggm):
        return None
    try:
        env = UnityPy.load(ggm)
    except Exception:
        return None
    for obj in env.objects:
        if obj.type.name != "MonoScript":
            continue
        try:
            if getattr(obj.read(), "m_ClassName", "") == CONTAINER_SCRIPT_CLASS:
                return obj.path_id
        except Exception:
            continue
    return None

def _parse_material_profiles(raw, byte_start):
    """Pull every MaterialProfile out of one BasketTeamContainer's bytes.

    Layout (from DLL metadata):
        m_sMaterialColorParam     : int32 len + bytes, padded to 4
        m_aoRetroExtraColorParams : int32 count
            count x { key: int32 len + bytes padded to 4 ; value: 4 x float32 }

    The dict key is the logo/unlock variant id -- "default" for the team's normal
    court, plus one entry per retro/throwback logo era.

    Returns {property_name: {variant: {"offset": abs_file_offset, "rgba": (...)}}}
    """
    out = {}
    n = len(raw)
    for i in range(0, n - 4):
        (slen,) = struct.unpack_from("<i", raw, i)
        if not (5 <= slen <= 48 and i + 4 + slen <= n):
            continue
        cand = raw[i + 4:i + 4 + slen]
        if cand[:1] != b"_" or not all(32 <= c < 127 for c in cand):
            continue
        param = cand.decode("ascii")
        if not param.startswith("_Color_"):
            continue
        try:
            _p, off = _mb_read_str(raw, i)
            (count,) = struct.unpack_from("<i", raw, off)
            off += 4
            if not (0 <= count <= 32):
                continue
            variants = {}
            ok = True
            for _ in range(count):
                if off + 4 > n:
                    ok = False; break
                key, off = _mb_read_str(raw, off)
                if off + 16 > n:
                    ok = False; break
                variants[key] = {"offset": byte_start + off,
                                 "rgba": struct.unpack_from("<4f", raw, off)}
                off += 16
            if ok and variants:
                out[param] = variants
        except Exception:
            continue
    return out

def find_team_containers(game_data_path):
    """Scan sharedassets1.assets for every BasketTeamContainer and return
    {container_name: {"assets_path", "path_id", "profiles"}}.
    Live scan, not hardcoded, so offsets stay correct after a game update."""
    assets_path = os.path.join(game_data_path, CONTAINER_ASSETS_FILE)
    if not os.path.isfile(assets_path):
        return {}
    script_pid = _find_container_script_path_id(game_data_path)
    if script_pid is None:
        return {}
    try:
        env = UnityPy.load(assets_path)
    except Exception:
        return {}

    teams = {}
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            raw = obj.get_raw_data()
            if _mb_script_path_id(raw) != script_pid:
                continue
            name, _ = _mb_read_str(raw, 28)
        except Exception:
            continue
        profiles = _parse_material_profiles(raw, obj.byte_start)
        if profiles:
            teams[name] = {"assets_path": assets_path, "path_id": obj.path_id,
                           "profiles": profiles}
    return teams

def read_container_colors_from_backup(assets_path, offsets):
    """Read the given absolute offsets out of the .original_backup instead of the
    live file -- used by Reset to recover true shipped values even after edits.
    Returns {offset: rgba} (empty if no backup exists)."""
    bak = assets_path + BACKUP_SUFFIX
    if not os.path.exists(bak):
        return {}
    out = {}
    with open(bak, "rb") as f:
        for off in offsets:
            f.seek(off)
            chunk = f.read(16)
            if len(chunk) == 16:
                out[off] = struct.unpack("<4f", chunk)
    return out

def apply_container_color_mod(assets_path, patches):
    """
    IN-PLACE BINARY PATCH of BasketTeamContainer color overrides.

    patches: list of {"offset": int, "expect": rgba, "new": rgba}

    Every offset is verified against its expected current value BEFORE anything
    is written, so a stale offset (game updated, file already modified) aborts
    cleanly rather than corrupting the file. A Color is exactly 16 fixed bytes,
    so the file size cannot change -- asserted at the end.
    """
    if not patches:
        return
    size_before = os.path.getsize(assets_path)

    with open(assets_path, "rb") as f:
        for p in patches:
            f.seek(p["offset"])
            chunk = f.read(16)
            if len(chunk) != 16:
                raise ValueError(f"Offset {p['offset']} is past the end of {os.path.basename(assets_path)}.")
            cur = struct.unpack("<4f", chunk)
            if any(abs(a - b) > 1e-5 for a, b in zip(cur, p["expect"])):
                raise ValueError(
                    "Court line data has moved since it was last scanned "
                    f"(offset {p['offset']}). Reopen Court Colors to rescan, then try again."
                )

    with open(assets_path, "r+b") as f:
        for p in patches:
            f.seek(p["offset"])
            f.write(struct.pack("<4f", *p["new"]))

    size_after = os.path.getsize(assets_path)
    if size_after != size_before:
        raise ValueError(f"File size changed ({size_before} -> {size_after}) -- this should be impossible.")

def verify_container_colors(assets_path, patches):
    """Read values straight back off disk and return a list of mismatch strings."""
    problems = []
    with open(assets_path, "rb") as f:
        for p in patches:
            f.seek(p["offset"])
            got = struct.unpack("<4f", f.read(16))
            if any(abs(a - b) > 0.004 for a, b in zip(got, p["new"])):
                problems.append(f"offset {p['offset']}: wrote {tuple(round(x,3) for x in p['new'])} "
                                f"but read back {tuple(round(x,3) for x in got)}")
    return problems

def _normalize_team_key(name):
    """'23_Los Angeles Lakers' and 'LosAngelesLakers' both -> 'losangeleslakers',
    so the Material-derived team list can be matched to container names."""
    return re.sub(r"[^a-z]", "", re.sub(r"^\d+_", "", name).lower())

def match_container_for_team(containers, team):
    """Find the container whose name matches a Material-derived team name."""
    key = _normalize_team_key(team)
    for cname, cdata in containers.items():
        if _normalize_team_key(cname) == key:
            return cname, cdata
    return None, None

_LEGACY_DARK_THEME = {
    "bg": "#1a1a2e", "panel": "#16213e", "accent": "#0f3460", "hi": "#e94560",
    "hi_active": "#c73652", "soft_active": "#1a4a80", "text": "#eaeaea", "ebg": "#0d1b2a",
}

def _pick_color_with_eyedropper(root, on_done, theme=None):
    """
    Photoshop-style eyedropper: a near-invisible fullscreen overlay that
    tracks the cursor anywhere on screen (not just inside this app), grabs
    the pixel color under it live via PIL.ImageGrab, and shows a small
    floating swatch+hex readout that follows the mouse. Left-click samples
    and closes; Escape or right-click cancels.

    on_done is always called exactly once with either an (r,g,b) 0-255 tuple
    or None (cancelled).
    """
    T = theme or _LEGACY_DARK_THEME
    overlay = tk.Toplevel(root)
    overlay.attributes("-fullscreen", True)
    try:
        overlay.attributes("-alpha", 0.01)   # invisible, but still receives clicks
    except Exception:
        pass
    overlay.attributes("-topmost", True)
    overlay.configure(cursor="crosshair", bg="black")

    preview = tk.Toplevel(overlay)
    preview.overrideredirect(True)
    preview.attributes("-topmost", True)
    frame = tk.Frame(preview, bg=T["panel"], bd=1, relief="solid")
    frame.pack()
    swatch  = tk.Label(frame, width=4, height=2, bg="#000000")
    swatch.pack(side="left", padx=(2,4), pady=2)
    hex_lbl = tk.Label(frame, text="#000000", bg=T["panel"], fg=T["text"],
                        font=("Consolas", 9), padx=4)
    hex_lbl.pack(side="left", padx=(0,4), pady=2)

    state = {"rgb": None}

    def sample(x, y):
        try:
            img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
            return img.getpixel((0, 0))[:3]
        except Exception:
            return None

    def on_motion(event):
        x, y = event.x_root, event.y_root
        preview.geometry(f"+{x+18}+{y+18}")
        rgb = sample(x, y)
        if rgb:
            state["rgb"] = rgb
            hx = "#%02x%02x%02x" % rgb
            swatch.configure(bg=hx)
            hex_lbl.configure(text=hx)

    def cleanup():
        try: overlay.grab_release()
        except Exception: pass
        try: preview.destroy()
        except Exception: pass
        try: overlay.destroy()
        except Exception: pass

    def finish(event):
        rgb = state["rgb"] or sample(event.x_root, event.y_root)
        cleanup()
        on_done(rgb)

    def cancel(event=None):
        cleanup()
        on_done(None)

    overlay.bind("<Motion>",   on_motion)
    overlay.bind("<Button-1>", finish)
    overlay.bind("<Escape>",   cancel)
    overlay.bind("<Button-3>", cancel)
    overlay.focus_force()
    overlay.grab_set()

def open_color_picker(parent, initial_rgb, title="Pick a Color", theme=None):
    """
    Self-contained RGB color picker (sliders + hex entry + live preview),
    themed to match the app instead of relying on the OS-native color
    dialog. Returns (r, g, b) as 0-1 floats, or None if cancelled.
    """
    T = theme or _LEGACY_DARK_THEME
    result = {"rgb": None}
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=T["bg"])
    win.resizable(False, False)
    win.transient(parent)

    r0, g0, b0 = (max(0, min(255, int(round(c * 255)))) for c in initial_rgb)

    tk.Label(win, text=title, bg=T["bg"], fg=T["hi"], font=("Segoe UI",11,"bold")
             ).grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(14,6))

    preview = tk.Canvas(win, width=220, height=60, highlightthickness=1, highlightbackground=T["accent"])
    preview.grid(row=1, column=0, columnspan=2, padx=16, pady=(0,10))
    preview_rect = preview.create_rectangle(0, 0, 220, 60, fill="#%02x%02x%02x" % (r0, g0, b0), outline="")

    hex_var = tk.StringVar(value="#%02x%02x%02x" % (r0, g0, b0))
    sliders = {}

    def update_from_sliders(_=None):
        r, g, b = sliders["R"].get(), sliders["G"].get(), sliders["B"].get()
        hx = "#%02x%02x%02x" % (r, g, b)
        hex_var.set(hx)
        preview.itemconfig(preview_rect, fill=hx)

    def make_slider(label, row, init):
        tk.Label(win, text=label, bg=T["bg"], fg=T["text"], font=("Segoe UI",9), width=2
                 ).grid(row=row, column=0, sticky="w", padx=(16,0))
        var = tk.IntVar(value=init)
        sc = tk.Scale(win, from_=0, to=255, orient="horizontal", variable=var, length=220,
                       bg=T["bg"], fg=T["text"], troughcolor=T["accent"], highlightthickness=0,
                       activebackground=T["hi"], command=update_from_sliders)
        sc.grid(row=row, column=1, padx=(4,16), pady=2)
        sliders[label] = var

    make_slider("R", 2, r0)
    make_slider("G", 3, g0)
    make_slider("B", 4, b0)

    def update_from_hex(_=None):
        hx = hex_var.get().strip()
        if len(hx) == 7 and hx.startswith("#"):
            try:
                r, g, b = int(hx[1:3],16), int(hx[3:5],16), int(hx[5:7],16)
                sliders["R"].set(r); sliders["G"].set(g); sliders["B"].set(b)
                preview.itemconfig(preview_rect, fill=hx)
            except ValueError:
                pass

    hf = tk.Frame(win, bg=T["bg"])
    hf.grid(row=5, column=0, columnspan=2, sticky="w", padx=16, pady=(6,4))
    tk.Label(hf, text="Hex:", bg=T["bg"], fg=T["text"], font=("Segoe UI",9)).pack(side="left")
    hex_entry = tk.Entry(hf, textvariable=hex_var, width=10, bg=T["ebg"], fg=T["text"],
                          insertbackground=T["text"], relief="flat")
    hex_entry.pack(side="left", padx=8)
    hex_entry.bind("<Return>", update_from_hex)
    hex_entry.bind("<FocusOut>", update_from_hex)

    def on_ok():
        result["rgb"] = (sliders["R"].get()/255.0, sliders["G"].get()/255.0, sliders["B"].get()/255.0)
        win.destroy()
    def on_cancel():
        win.destroy()

    def start_eyedropper():
        win.withdraw()   # get the picker out of the way while sampling the screen
        def done(rgb):
            win.deiconify(); win.lift(); win.focus_force()
            if rgb:
                r, g, b = rgb
                sliders["R"].set(r); sliders["G"].set(g); sliders["B"].set(b)
                update_from_sliders()
        _pick_color_with_eyedropper(win, done, theme=T)

    ef = tk.Frame(win, bg=T["bg"])
    ef.grid(row=6, column=0, columnspan=2, sticky="w", padx=16, pady=(0,4))
    tk.Button(ef, text="🎨 Eyedropper — sample from anywhere on screen", bg=T["accent"], fg="#FFFFFF",
              relief="flat", padx=10, pady=4, activebackground=T["soft_active"],
              command=start_eyedropper).pack(side="left")

    bf = tk.Frame(win, bg=T["bg"])
    bf.grid(row=7, column=0, columnspan=2, pady=16)
    tk.Button(bf, text="OK", bg=T["hi"], fg="white", relief="flat", padx=18, pady=4,
              activebackground=T["hi_active"], command=on_ok).pack(side="left", padx=6)
    tk.Button(bf, text="Cancel", bg=T["accent"], fg="#FFFFFF", relief="flat", padx=14, pady=4,
              activebackground=T["soft_active"], command=on_cancel).pack(side="left", padx=6)

    win.update_idletasks()
    x = parent.winfo_rootx() + 60
    y = parent.winfo_rooty() + 60
    win.geometry(f"+{x}+{y}")
    win.lift(); win.attributes("-topmost", True); win.after(10, lambda: win.attributes("-topmost", False))
    win.focus_force()
    win.grab_set()
    parent.wait_window(win)
    return result["rgb"]

# ── App ────────────────────────────────────────────────────────────────────────
class ModManagerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg               = load_config()
        self.C                 = THEMES.get(self.cfg.get("theme", "dark"), THEMES["dark"])

        self.title(APP_NAME)
        self.geometry("1100x760")
        self.minsize(900, 640)
        self.configure(bg=self.C["bg"])

        self.mods_meta         = {}
        self.all_textures      = []
        self.filtered_textures = []
        self.selected_texture  = None
        self._orig_img         = None
        self._mod_img          = None
        self._orig_photo       = None
        self._mod_photo        = None
        self._loading          = False
        self._sort_col         = None
        self._sort_asc         = True
        self.audio_frame       = None
        self.mesh_frame        = None

        self._apply_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.cfg["game_data_path"] and self.cfg["mods_folder"]:
            self.after(200, self._load_textures_async)

    def _on_close(self):
        try:
            if self.audio_frame is not None:
                self.audio_frame.shutdown()
        except Exception:
            pass
        self.destroy()

    # ── Styles ────────────────────────────────────────────────────────────────
    def _apply_styles(self):
        C = self.C
        s = ttk.Style(self)
        try: s.theme_use("clam")
        except Exception: pass
        s.configure(".",                background=C["bg"],    foreground=C["text"],  font=("Segoe UI",10))
        s.configure("TFrame",           background=C["bg"])
        s.configure("Panel.TFrame",     background=C["panel"])
        s.configure("TLabel",           background=C["bg"],    foreground=C["text"])
        s.configure("Sub.TLabel",       background=C["panel"], foreground=C["sub"],   font=("Segoe UI",9))
        s.configure("Header.TLabel",    background=C["bg"],    foreground=C["hi"],    font=("Segoe UI",13,"bold"))
        s.configure("TEntry",           fieldbackground=C["ebg"], foreground=C["text"], insertcolor=C["text"])
        # NOTE: C["accent"] is the same fixed navy blue in both themes (it's a brand
        # color, not a light/dark surface), so anything drawn ON TOP of it always
        # uses a fixed white foreground rather than C["text"] -- C["text"] flips to
        # near-black in Light mode, which would be unreadable on navy.
        s.configure("TScrollbar",       background=C["accent"], troughcolor=C["panel"], arrowcolor="#FFFFFF")
        s.configure("Accent.TButton",   background=C["hi"],  foreground="#FFFFFF", font=("Segoe UI",10,"bold"), relief="flat", padding=6)
        s.map("Accent.TButton",         background=[("active",C["hi_active"]),("disabled",C["dis_bg"])],
                                        foreground=[("disabled",C["dis_fg"])])
        s.configure("Soft.TButton",     background=C["accent"], foreground="#FFFFFF", font=("Segoe UI",10), relief="flat", padding=6)
        s.map("Soft.TButton",           background=[("active",C["soft_active"]),("disabled",C["dis_bg"])],
                                        foreground=[("disabled",C["dis_fg"])])
        s.configure("Treeview",         background=C["ebg"], foreground=C["text"], fieldbackground=C["ebg"], rowheight=26, font=("Segoe UI",10))
        s.configure("Treeview.Heading", background=C["accent"], foreground="#FFFFFF", font=("Segoe UI",10,"bold"), relief="flat")
        s.map("Treeview",               background=[("selected",C["hi"])], foreground=[("selected","#FFFFFF")])
        s.configure("TNotebook",        background=C["bg"], borderwidth=0)
        s.configure("TNotebook.Tab",    background=C["panel"], foreground=C["text"],
                                        padding=(16,9), font=("Segoe UI",10,"bold"))
        s.map("TNotebook.Tab",          background=[("selected", C["accent"])],
                                        foreground=[("selected","white")])

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        C = self.C
        top = ttk.Frame(self, style="Panel.TFrame"); top.pack(fill="x")
        ttk.Label(top, text="🏀  "+APP_NAME, style="Header.TLabel",
                  background=C["panel"]).pack(side="left", padx=16, pady=10)
        ttk.Button(top, text="⚙ Settings",           style="Soft.TButton",   command=self._open_settings).pack(side="right", padx=8, pady=8)
        ttk.Button(top, text="♻ Restore Game Files",  style="Soft.TButton",   command=self._restore_all).pack(side="right",  padx=4, pady=8)
        ttk.Button(top, text="▶ Apply Mods",          style="Accent.TButton", command=self._apply_all_mods).pack(side="right", padx=4, pady=8)

        self.status_var = tk.StringVar(value="Configure your game path in ⚙ Settings to begin.")
        tk.Label(self, textvariable=self.status_var, bg=C["accent"], fg="#FFFFFF",
                 font=("Segoe UI",9), anchor="w", padx=10).pack(fill="x", side="bottom")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self._build_home_tab(self.notebook)
        self._build_textures_tab(self.notebook)
        self._build_audio_tab(self.notebook)
        self._build_meshes_tab(self.notebook)
        self.floor_patterns_tab = self._build_tool_tab(
            self.notebook, "🪵 Floor Patterns", "🪵", "Floor Patterns",
            "Pick a wood-grain pattern for each team's court, throwback eras included.",
            self._open_floor_patterns_dialog)
        self.sliders_tab = self._build_tool_tab(
            self.notebook, "🎚 Gameplay Sliders", "🎚", "Gameplay Sliders",
            "Tune CPU skill, shot windows, and movement to match how you like to play.",
            self._open_gameplay_sliders_dialog)
        self.court_colors_tab = self._build_tool_tab(
            self.notebook, "🎨 Court Colors", "🎨", "Court Colors",
            "Recolor each team's floor and court lines.",
            self._open_court_colors_dialog)
        self._build_saves_tab(self.notebook)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    # ── Meshes tab ────────────────────────────────────────────────────────────
    def _build_meshes_tab(self, notebook):
        """Browse, view in 3D, export and replace the game's Mesh assets.

        Lives in mesh_tab.py + mesh_view.py + mesh_manager.py, which import
        nothing from this file. The one thing that IS shared is the apply run:
        meshes stream their vertex data into the same .resS files textures
        stream their pixels into, so both are written from _apply_all_mods()
        with a single ress_reset set (see mesh_tab.apply_all). Imported lazily
        so a missing or broken module degrades to a message instead of blocking
        startup.
        """
        tab = ttk.Frame(notebook, style="TFrame")
        notebook.add(tab, text="🧊 Meshes")
        self.meshes_tab = tab
        try:
            from mesh_tab import MeshTab
            self.mesh_frame = MeshTab(tab, self.cfg, host=self, theme=self.C)
            self.mesh_frame.pack(fill="both", expand=True)
        except Exception as e:
            self.mesh_frame = None
            tk.Label(tab, bg=self.C["bg"], fg=self.C["hi"], font=("Segoe UI", 10),
                     justify="left",
                     text=f"Meshes tab unavailable:\n{e}\n\nmesh_tab.py, "
                          f"mesh_view.py and mesh_manager.py should sit in the "
                          f"modules folder next to app.py.").pack(anchor="w", padx=20, pady=20)

    # ── Saves tab ─────────────────────────────────────────────────────────────
    def _build_saves_tab(self, notebook):
        """Unlock cosmetics by editing the player's .sav.

        Lives in save_tab.py + save_manager.py, which are self-contained: they
        import nothing from this file and never call apply_single_mod(). They
        touch no .assets file at all -- only the game's level1 scene (read-only,
        to build the unlockable catalog) and the save in AppData\\LocalLow.
        Imported lazily so a missing or broken module degrades to a message
        instead of blocking startup.
        """
        tab = ttk.Frame(notebook, style="TFrame")
        notebook.add(tab, text="💾 Saves")
        self.saves_tab = tab
        try:
            from save_tab import SaveTab
            self.save_frame = SaveTab(
                tab, self.cfg.get("game_data_path", ""), host=self, theme={
                    "bg":     self.C["bg"],
                    "panel":  self.C["panel"],
                    "accent": self.C["accent"],
                    "text":   self.C["text"],
                    "muted":  self.C["sub"],
                    "entry":  self.C["ebg"],
                    "gold":   self.C["gold"],
                    "red":    self.C["hi"],
                })
            self.save_frame.pack(fill="both", expand=True)
        except Exception as e:
            self.save_frame = None
            tk.Label(tab, bg=self.C["bg"], fg=self.C["hi"], font=("Segoe UI", 10),
                     justify="left",
                     text=f"Saves tab unavailable:\n{e}\n\nsave_tab.py and "
                          f"save_manager.py should sit in the modules folder "
                          f"next to app.py.").pack(anchor="w", padx=20, pady=20)

    # ── Home ──────────────────────────────────────────────────────────────────
    def _build_home_tab(self, notebook):
        C = self.C
        tab = ttk.Frame(notebook, style="TFrame")
        notebook.add(tab, text="🏠 Home")
        self.home_tab = tab
        tab.columnconfigure(0, weight=1)

        tk.Label(tab, text="🏀  "+APP_NAME, bg=C["bg"], fg=C["hi"],
                 font=("Segoe UI", 20, "bold")).grid(row=0, column=0, pady=(36, 6))
        tk.Label(tab, text="Choose where you want to start. Every section below is always\n"
                            "one click away from the tabs at the top, too.",
                 bg=C["bg"], fg=C["sub"], font=("Segoe UI", 10), justify="center"
                 ).grid(row=1, column=0, pady=(0, 26))

        cards = tk.Frame(tab, bg=C["bg"])
        cards.grid(row=2, column=0)
        card_defs = [
            ("🖼", "Textures", "Browse, preview, export, and replace in-game textures.",
             lambda: self._goto_tab(self.textures_tab)),
            ("🔊", "Audio", "Browse, play, tag languages, and replace audio clips.",
             lambda: self._goto_tab(self.audio_tab)),
            ("🧊", "Meshes", "View, export, and replace the game's 3D models.",
             lambda: self._goto_tab(self.meshes_tab)),
            ("🪵", "Floor Patterns", "Pick a wood-grain pattern for each team's court.",
             lambda: self._goto_tab(self.floor_patterns_tab)),
            ("🎚", "Gameplay Sliders", "Tune CPU skill, shot windows, and movement.",
             lambda: self._goto_tab(self.sliders_tab)),
            ("🎨", "Court Colors", "Recolor each team's floor and court lines.",
             lambda: self._goto_tab(self.court_colors_tab)),
            ("💾", "Saves", "Unlock mascots, jerseys, logos, balls, and trails.",
             lambda: self._goto_tab(self.saves_tab)),
        ]
        for i, (icon, title, desc, command) in enumerate(card_defs):
            self._build_home_card(cards, icon, title, desc, command).grid(
                row=i // 3, column=i % 3, padx=16, pady=10)

    def _build_tool_tab(self, notebook, tab_label, icon, title, desc, open_command):
        """A tab for a tool that lives in its own popup dialog (Floor Patterns,
        Gameplay Sliders, Court Colors). Selecting the tab opens the dialog
        immediately (see _on_tab_changed); this page is what's behind it, and
        also works as a manual fallback (e.g. if the dialog's module failed to
        import) via its own Open button."""
        C = self.C
        tab = ttk.Frame(notebook, style="TFrame")
        notebook.add(tab, text=tab_label)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        tab.rowconfigure(5, weight=1)

        tk.Label(tab, text=icon, bg=C["bg"], fg=C["hi"], font=("Segoe UI", 40)).grid(row=1, column=0)
        tk.Label(tab, text=title, bg=C["bg"], fg=C["text"], font=("Segoe UI", 18, "bold")
                 ).grid(row=2, column=0, pady=(6, 0))
        tk.Label(tab, text=desc, bg=C["bg"], fg=C["sub"], font=("Segoe UI", 10),
                 wraplength=460, justify="center").grid(row=3, column=0, pady=(8, 20))
        ttk.Button(tab, text=f"Open {title}  →", style="Accent.TButton",
                   command=open_command).grid(row=4, column=0)
        return tab

    def _build_home_card(self, parent, icon, title, desc, command):
        C = self.C
        card = tk.Frame(parent, bg=C["panel"], highlightbackground=C["accent"],
                         highlightthickness=1, bd=0, cursor="hand2", width=240, height=210)
        card.pack_propagate(False)
        clickable = [card]
        for text, font in ((icon, ("Segoe UI", 30)), (title, ("Segoe UI", 13, "bold"))):
            lbl = tk.Label(card, text=text, bg=C["panel"], fg=(C["hi"] if text == icon else C["text"]), font=font)
            lbl.pack(pady=(22, 4) if text == icon else (0, 0))
            clickable.append(lbl)
        desc_lbl = tk.Label(card, text=desc, bg=C["panel"], fg=C["sub"], font=("Segoe UI", 9),
                             wraplength=200, justify="center")
        desc_lbl.pack(pady=(4, 12), padx=14)
        clickable.append(desc_lbl)
        ttk.Button(card, text="Open →", style="Accent.TButton", command=command).pack()
        for widget in clickable:
            widget.bind("<Button-1>", lambda e: command())
        return card

    def _goto_tab(self, tab_widget):
        try:
            self.notebook.select(tab_widget)
        except Exception:
            pass

    def _on_tab_changed(self, event=None):
        try:
            current = self.notebook.nametowidget(self.notebook.select())
        except Exception:
            return
        if self.audio_frame is not None and current is self.audio_tab:
            self.audio_frame.load()
        elif self.mesh_frame is not None and current is getattr(self, "meshes_tab", None):
            self.mesh_frame.load()
        elif current is getattr(self, "floor_patterns_tab", None):
            self.after(50, self._open_floor_patterns_dialog)
        elif current is getattr(self, "sliders_tab", None):
            self.after(50, self._open_gameplay_sliders_dialog)
        elif current is getattr(self, "court_colors_tab", None):
            self.after(50, self._open_court_colors_dialog)

    # ── Textures tab ──────────────────────────────────────────────────────────
    def _build_textures_tab(self, notebook):
        tab = ttk.Frame(notebook, style="TFrame")
        notebook.add(tab, text="🖼 Textures")
        self.textures_tab = tab

        self.paned = tk.PanedWindow(tab, orient=tk.HORIZONTAL,
                                    sashwidth=6, sashrelief="flat",
                                    bg=self.C["accent"], borderwidth=0)
        self.paned.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_left_panel(self.paned)
        self._build_right_panel(self.paned)

    # ── Audio tab ─────────────────────────────────────────────────────────────
    def _build_audio_tab(self, notebook):
        tab = ttk.Frame(notebook, style="TFrame")
        notebook.add(tab, text="🔊 Audio")
        self.audio_tab = tab
        try:
            self.audio_frame = AudioManagerFrame(tab, host=self)
            self.audio_frame.pack(fill="both", expand=True)
        except Exception as e:
            self.audio_frame = None
            tk.Label(tab, bg=self.C["bg"], fg=self.C["hi"], font=("Segoe UI", 10), justify="left",
                     text=f"Audio tab unavailable:\n{e}\n\naudio_manager.py should sit in the same "
                          f"folder as app.py.").pack(anchor="w", padx=20, pady=20)

    def _build_left_panel(self, parent):
        C = self.C
        frame = ttk.Frame(parent, style="Panel.TFrame")
        parent.add(frame, minsize=280, width=420, stretch="always")
        frame.rowconfigure(1, weight=1); frame.columnconfigure(0, weight=1)

        sf = ttk.Frame(frame, style="Panel.TFrame")
        sf.grid(row=0, column=0, sticky="ew", padx=8, pady=8); sf.columnconfigure(1, weight=1)
        ttk.Label(sf, text="🔍", background=C["panel"]).grid(row=0, column=0, padx=(0,4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._on_search)
        ttk.Entry(sf, textvariable=self.search_var).grid(row=0, column=1, sticky="ew")

        tf = ttk.Frame(frame, style="Panel.TFrame")
        tf.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0,8))
        tf.rowconfigure(0, weight=1); tf.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tf, columns=("name","size","mod","file"), show="headings", selectmode="browse")
        self.tree.heading("name", text="Texture Name", command=lambda: self._sort_by("name"))
        self.tree.heading("size", text="Size",         command=lambda: self._sort_by("size"))
        self.tree.heading("mod",  text="Mod",          command=lambda: self._sort_by("mod"))
        self.tree.heading("file", text="File",         command=lambda: self._sort_by("file"))
        self.tree.column("name", width=155)
        self.tree.column("size", width=80,  anchor="center")
        self.tree.column("mod",  width=40,  anchor="center")
        self.tree.column("file", width=115, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_texture_select)
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vsb.set)

        ff = ttk.Frame(frame, style="Panel.TFrame")
        ff.grid(row=2, column=0, sticky="ew", padx=8, pady=(0,8))
        ttk.Label(ff, text="Filter by file:", background=C["panel"], font=("Segoe UI",9)).pack(side="left")
        self.file_filter_var = tk.StringVar(value="All")
        self.file_filter_cb  = ttk.Combobox(ff, textvariable=self.file_filter_var, state="readonly", width=22)
        self.file_filter_cb.pack(side="left", padx=6)
        self.file_filter_cb.bind("<<ComboboxSelected>>", self._on_search)

    def _build_right_panel(self, parent):
        C = self.C
        frame = ttk.Frame(parent, style="Panel.TFrame")
        parent.add(frame, minsize=300, stretch="always")
        frame.columnconfigure(0, weight=1); frame.columnconfigure(1, weight=1); frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text="Original", background=C["panel"], font=("Segoe UI",11,"bold")).grid(row=0, column=0, pady=(10,4))
        ttk.Label(frame, text="Your Mod", background=C["panel"], font=("Segoe UI",11,"bold")).grid(row=0, column=1, pady=(10,4))

        self.orig_canvas = tk.Canvas(frame, bg=C["ebg"], width=320, height=320,
                                     highlightthickness=1, highlightbackground=C["accent"])
        self.orig_canvas.grid(row=1, column=0, padx=12, pady=4, sticky="nsew")
        self.mod_canvas  = tk.Canvas(frame, bg=C["ebg"], width=320, height=320,
                                     highlightthickness=1, highlightbackground=C["accent"])
        self.mod_canvas.grid(row=1, column=1, padx=12, pady=4, sticky="nsew")
        self.orig_canvas.bind("<Configure>", lambda e: self._redraw_previews())
        self.mod_canvas.bind("<Configure>",  lambda e: self._redraw_previews())

        self.info_var = tk.StringVar(value="Select a texture from the list.")
        ttk.Label(frame, textvariable=self.info_var, style="Sub.TLabel",
                  background=C["panel"]).grid(row=2, column=0, columnspan=2, pady=4)

        bf = ttk.Frame(frame, style="Panel.TFrame")
        bf.grid(row=3, column=0, columnspan=2, pady=(4,12))
        ttk.Button(bf, text="⬇ Export Original",    style="Soft.TButton",   command=self._export_original).pack(side="left", padx=6)
        ttk.Button(bf, text="⬆ Import Replacement", style="Accent.TButton", command=self._import_replacement).pack(side="left", padx=6)
        ttk.Button(bf, text="✕ Remove Mod",          style="Soft.TButton",   command=self._remove_mod).pack(side="left", padx=6)

    # ── Settings ──────────────────────────────────────────────────────────────
    def _open_settings(self):
        C = self.C
        dlg = tk.Toplevel(self); dlg.title("Settings")
        dlg.geometry("560x300"); dlg.configure(bg=C["bg"]); dlg.grab_set()

        def row(label, var, fn, r):
            ttk.Label(dlg, text=label).grid(row=r, column=0, sticky="w", padx=12, pady=8)
            ttk.Entry(dlg, textvariable=var, width=44).grid(row=r, column=1, padx=4)
            ttk.Button(dlg, text="Browse", style="Soft.TButton", command=fn).grid(row=r, column=2, padx=8)

        gv = tk.StringVar(value=self.cfg.get("game_data_path",""))
        mv = tk.StringVar(value=self.cfg.get("mods_folder",""))
        row("Game Data Folder:", gv, lambda: gv.set(filedialog.askdirectory(title="Select NBA Bounce_Data folder") or gv.get()), 0)
        row("Mods Folder:",      mv, lambda: mv.set(filedialog.askdirectory(title="Select your Mods folder")       or mv.get()), 1)
        ttk.Label(dlg, text="Tip: Game Data folder should end in 'NBA Bounce_Data'",
                  style="Sub.TLabel", background=C["bg"]).grid(row=2, column=0, columnspan=3, padx=12, pady=4)

        ttk.Label(dlg, text="Appearance:").grid(row=3, column=0, sticky="w", padx=12, pady=(18,8))
        theme_var = tk.StringVar(value="Light" if self.cfg.get("theme", "dark") == "light" else "Dark")
        theme_cb  = ttk.Combobox(dlg, textvariable=theme_var, state="readonly",
                                  values=["Dark", "Light"], width=41)
        theme_cb.grid(row=3, column=1, sticky="w", padx=4, pady=(18,8))
        ttk.Label(dlg, text="Changing this takes effect the next time you launch the app.",
                  style="Sub.TLabel", background=C["bg"]).grid(row=4, column=0, columnspan=3, sticky="w", padx=12)

        def save():
            self.cfg["game_data_path"] = gv.get()
            self.cfg["mods_folder"]    = mv.get()
            self.cfg["theme"]          = "light" if theme_var.get() == "Light" else "dark"
            save_config(self.cfg)
            os.makedirs(self.cfg["mods_folder"], exist_ok=True)
            self.mods_meta = load_mods_meta(self.cfg["mods_folder"])
            # Saves tab reads the unlockable catalog out of the game folder, so
            # it needs to hear about a game-path change too.
            if getattr(self, "save_frame", None) is not None:
                try:
                    self.save_frame.set_game_data_path(self.cfg["game_data_path"])
                except Exception:
                    pass
            # Meshes tab keeps its own list and mesh_mods.json, both of which are
            # tied to the two folders that just changed.
            if getattr(self, "mesh_frame", None) is not None:
                try:
                    self.mesh_frame.reload_config(self.cfg)
                except Exception:
                    pass
            dlg.destroy(); self._load_textures_async()

        ttk.Button(dlg, text="Save & Reload", style="Accent.TButton", command=save).grid(row=5, column=0, columnspan=3, pady=16)

    # ── Loading ───────────────────────────────────────────────────────────────
    def _load_textures_async(self):
        if self._loading: return
        self._loading = True
        self.status_var.set("Loading textures… this may take a moment for large files.")
        self.tree.delete(*self.tree.get_children())
        threading.Thread(target=self._load_textures_worker, daemon=True).start()

    def _load_textures_worker(self):
        try:
            game_path   = self.cfg["game_data_path"]
            mods_folder = self.cfg["mods_folder"]
            if not game_path or not os.path.isdir(game_path):
                self.after(0, lambda: self.status_var.set("❌ Game data folder not found. Check Settings."))
                return
            os.makedirs(mods_folder, exist_ok=True)
            self.mods_meta  = load_mods_meta(mods_folder)
            asset_files     = find_assets_files(game_path)
            if not asset_files:
                self.after(0, lambda: self.status_var.set("❌ No asset files found in the game data folder."))
                return

            all_textures = []
            for i, af in enumerate(asset_files):
                fname = os.path.basename(af)
                self.after(0, lambda f=fname, n=i+1, t=len(asset_files):
                           self.status_var.set(f"Scanning {f}  ({n}/{t})…"))
                try:
                    found = extract_textures_from_file(af)
                    all_textures.extend(found)
                except Exception as e:
                    print(f"Warning: could not read {af}: {e}")

            self.all_textures = all_textures
            self.after(0, self._on_textures_loaded)
        except Exception as e:
            # Bind e as a default arg: Python deletes the name at the end of the
            # except block, so a bare closure would raise NameError when the
            # scheduled callback finally runs.
            self.after(0, lambda e=e: self.status_var.set(f"❌ Error: {e}"))
        finally:
            self._loading = False

    def _on_textures_loaded(self):
        file_names = sorted(set(os.path.basename(t["assets_file"]) for t in self.all_textures))
        self.file_filter_cb["values"] = ["All"] + file_names
        self.file_filter_var.set("All")
        self._populate_tree(self.all_textures)
        n = len(find_assets_files(self.cfg["game_data_path"]))
        self.status_var.set(f"✅ Loaded {len(self.all_textures)} textures from {n} asset files.")

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self._update_sort_indicators()
        self._populate_tree(self.filtered_textures)

    def _update_sort_indicators(self):
        labels = {"name": "Texture Name", "size": "Size", "mod": "Mod", "file": "File"}
        for col, base in labels.items():
            arrow = (" ↑" if self._sort_asc else " ↓") if col == self._sort_col else ""
            self.tree.heading(col, text=base + arrow)

    def _sorted_textures(self, textures):
        if not self._sort_col:
            return list(textures)
        def key_fn(t):
            if self._sort_col == "name": return t["name"].lower()
            if self._sort_col == "size": return t["width"] * t["height"] if t["width"] else 0
            if self._sort_col == "mod":  return 0 if self._mod_key(t) in self.mods_meta else 1
            if self._sort_col == "file": return os.path.basename(t["assets_file"]).lower()
            return ""
        return sorted(textures, key=key_fn, reverse=not self._sort_asc)

    def _populate_tree(self, textures):
        self.tree.delete(*self.tree.get_children())
        self.filtered_textures = self._sorted_textures(textures)
        for t in self.filtered_textures:
            mod   = "✔" if self._mod_key(t) in self.mods_meta else ""
            sz    = f"{t['width']}x{t['height']}" if t['width'] else "?"
            fname = os.path.basename(t["assets_file"])
            self.tree.insert("", "end", values=(t["name"], sz, mod, fname))

    def _mod_key(self, t):
        return f"{os.path.basename(t['assets_file'])}::{t['path_id']}"

    # ── Search ────────────────────────────────────────────────────────────────
    def _on_search(self, *_):
        q  = self.search_var.get().lower()
        ff = self.file_filter_var.get()
        self._populate_tree([
            t for t in self.all_textures
            if (not q or q in t["name"].lower())
            and (ff == "All" or os.path.basename(t["assets_file"]) == ff)
        ])

    # ── Selection ─────────────────────────────────────────────────────────────
    def _on_texture_select(self, _=None):
        sel = self.tree.selection()
        if not sel: return
        idx = self.tree.index(sel[0])
        if idx >= len(self.filtered_textures): return
        self.selected_texture = t = self.filtered_textures[idx]
        sz = f"{t['width']}×{t['height']}" if t['width'] else "unknown size"
        info = f"{t['name']}  |  {sz}  |  {t['format']}  |  {os.path.basename(t['assets_file'])}"

        # How the sprite in front of this texture crops it -- the reason a
        # replacement logo can come out cut off even when it lined up in
        # Photoshop. Indexing a file reads all its sprites, so do it off-thread.
        self._crop_report = None
        threading.Thread(target=self._load_crop_report, args=(t,), daemon=True).start()
        self.info_var.set(info)

        self._orig_img = None
        self.orig_canvas.delete("all")
        self.orig_canvas.create_text(160, 160, text="Loading…", fill="#666", font=("Segoe UI",10))
        threading.Thread(target=self._load_orig_preview, args=(t,), daemon=True).start()

        key = self._mod_key(t)
        if key in self.mods_meta:
            png = self.mods_meta[key]["png_path"]
            try:   self._mod_img = Image.open(png).convert("RGBA") if os.path.exists(png) else None
            except: self._mod_img = None
        else:
            self._mod_img = None
            self.mod_canvas.delete("all")
            self.mod_canvas.create_text(160, 160, text="No mod set", fill="#555", font=("Segoe UI",10))
        self._redraw_previews()

    def _load_crop_report(self, t):
        """Off-thread: fetch the sprite crop for t and fold it into the info line."""
        try:
            report = sprite_crop.crop_report(t["assets_file"], t["path_id"])
        except Exception:
            return

        def done():
            if self.selected_texture is not t:
                return          # selection moved on while we were indexing
            self._crop_report = report
            note = sprite_crop.describe(report)
            if note:
                self.info_var.set(self.info_var.get() + f"  |  {note}")
            self._redraw_previews()
        self.after(0, done)

    def _draw_crop_outline(self):
        """Outline the sprite's crop box on the Original preview.

        Anything the replacement puts outside this box is what the game throws
        away today; on Apply the box is widened to the full canvas, so this is
        also a picture of how much the old logo was actually using.
        """
        report = getattr(self, "_crop_report", None)
        if not report or report["status"] == "none" or not self._orig_img:
            return
        img_w, img_h = self._orig_img.size
        if not img_w or not img_h:
            return
        cw = self.orig_canvas.winfo_width()  or 320
        ch = self.orig_canvas.winfo_height() or 320
        scale = min(cw / img_w, ch / img_h, 1.0)
        draw_w, draw_h = img_w * scale, img_h * scale
        left, top = (cw - draw_w) / 2, (ch - draw_h) / 2
        for s in report["sprites"]:
            if s["full"]:
                continue
            x, y, w, h = s["rect"]
            # Sprite rects are bottom-left origin; canvas y grows downward.
            self.orig_canvas.create_rectangle(
                left + x * scale, top + (img_h - y - h) * scale,
                left + (x + w) * scale, top + (img_h - y) * scale,
                outline=self.C["hi"], dash=(4, 3), width=2)

    def _load_orig_preview(self, t):
        try:
            img = get_texture_image(t["assets_file"], t["path_id"])
            if img:
                self._orig_img = img.convert("RGBA")
                self.after(0, self._redraw_previews)
        except Exception as e:
            # See note above: e must be bound as a default arg to survive into
            # the scheduled callback.
            self.after(0, lambda e=e: (
                self.orig_canvas.delete("all"),
                self.orig_canvas.create_text(160, 160, text=f"Preview error:\n{e}",
                                             fill=self.C["hi"], font=("Segoe UI",9))
            ))

    def _fit_image(self, img, canvas):
        cw = canvas.winfo_width()  or 320
        ch = canvas.winfo_height() or 320
        iw, ih = img.size
        scale  = min(cw/iw, ch/ih, 1.0)
        return img.resize((max(1,int(iw*scale)), max(1,int(ih*scale))), Image.LANCZOS)

    def _redraw_previews(self):
        if self._orig_img:
            f = self._fit_image(self._orig_img, self.orig_canvas)
            self._orig_photo = ImageTk.PhotoImage(f)
            cw,ch = self.orig_canvas.winfo_width() or 320, self.orig_canvas.winfo_height() or 320
            self.orig_canvas.delete("all")
            self.orig_canvas.create_image(cw//2, ch//2, image=self._orig_photo, anchor="center")
            self._draw_crop_outline()
        if self._mod_img:
            f = self._fit_image(self._mod_img, self.mod_canvas)
            self._mod_photo = ImageTk.PhotoImage(f)
            cw,ch = self.mod_canvas.winfo_width() or 320, self.mod_canvas.winfo_height() or 320
            self.mod_canvas.delete("all")
            self.mod_canvas.create_image(cw//2, ch//2, image=self._mod_photo, anchor="center")

    # ── Actions ───────────────────────────────────────────────────────────────
    def _export_original(self):
        if not self.selected_texture:
            messagebox.showinfo("No selection", "Select a texture first."); return
        t    = self.selected_texture
        path = filedialog.asksaveasfilename(defaultextension=".png",
               filetypes=[("PNG","*.png")], initialfile=t["name"]+".png")
        if not path: return
        try:
            img = get_texture_image(t["assets_file"], t["path_id"])
            img.save(path)
            self.status_var.set(f"✅ Exported {t['name']} to {path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    def _import_replacement(self):
        if not self.selected_texture:
            messagebox.showinfo("No selection", "Select a texture first."); return
        if not self.cfg.get("mods_folder"):
            messagebox.showwarning("No mods folder", "Set your mods folder in Settings first."); return
        t    = self.selected_texture
        path = filedialog.askopenfilename(filetypes=[("PNG","*.png")], title="Select replacement PNG")
        if not path: return
        folder    = self.cfg["mods_folder"]
        dest_name = f"{os.path.basename(t['assets_file'])}__{t['path_id']}__{t['name']}.png"
        dest      = os.path.join(folder, dest_name)
        shutil.copy2(path, dest)
        key = self._mod_key(t)
        self.mods_meta[key] = {"name": t["name"], "assets_file": t["assets_file"],
                                "path_id": t["path_id"], "png_path": dest}
        save_mods_meta(folder, self.mods_meta)
        try:   self._mod_img = Image.open(dest).convert("RGBA")
        except: self._mod_img = None
        self._redraw_previews(); self._refresh_tree_row()

        # An atlas texture is the one case the crop can't be widened -- each
        # sprite's rect is a real sub-region -- so say so while the artwork can
        # still be changed.
        try:
            report = sprite_crop.crop_report(t["assets_file"], t["path_id"])
        except Exception:
            report = {"status": "none", "sprites": []}
        if report["status"] == "atlas":
            rects = "\n".join(
                f"  • {s['name']}: {s['rect'][2]:.0f}×{s['rect'][3]:.0f} at "
                f"({s['rect'][0]:.0f}, {s['rect'][1]:.0f})"
                for s in report["sprites"])
            messagebox.showwarning(
                "Texture is a sprite atlas",
                f"'{t['name']}' is shared by {len(report['sprites'])} sprites, each "
                f"drawing its own region of the image:\n\n{rects}\n\n"
                "Those regions can't be widened without moving the other sprites' "
                "artwork, so keep your replacement's content inside them "
                "(measured from the BOTTOM-left corner in Photoshop's terms).")
        self.status_var.set(f"✅ Mod saved for '{t['name']}'. Click 'Apply All Mods' to write to game.")

    def _remove_mod(self):
        if not self.selected_texture: return
        t   = self.selected_texture
        key = self._mod_key(t)
        if key not in self.mods_meta:
            messagebox.showinfo("No mod", "No mod is set for this texture."); return
        mod = self.mods_meta.pop(key)
        if os.path.exists(mod["png_path"]): os.remove(mod["png_path"])
        save_mods_meta(self.cfg["mods_folder"], self.mods_meta)

        # Put this texture and its sprite back to stock right away. Without it the
        # texture would keep pointing at appended .resS bytes that the next Apply
        # rewinds away, and the sprite would keep the widened crop.
        reverted = 0
        try:
            if revert_object_from_backup(mod["assets_file"], mod["path_id"]):
                reverted += 1
            for s in sprite_crop.sprite_index(mod["assets_file"]).get(mod["path_id"], []):
                if revert_object_from_backup(mod["assets_file"], s["sprite_path_id"]):
                    reverted += 1
        except Exception:
            reverted = 0        # non-fatal: Restore Textures still puts it right

        self._mod_img = None
        self.mod_canvas.delete("all")
        self.mod_canvas.create_text(160, 160, text="No mod set", fill="#555", font=("Segoe UI",10))
        self._refresh_tree_row()
        self.status_var.set(f"✅ Mod removed for '{t['name']}'."
                            + (f" Reverted {reverted} object(s) in the game file." if reverted else ""))

    def _refresh_tree_row(self):
        sel = self.tree.selection()
        if not sel: return
        idx = self.tree.index(sel[0])
        if idx >= len(self.filtered_textures): return
        t     = self.filtered_textures[idx]
        sz    = f"{t['width']}×{t['height']}" if t['width'] else "?"
        fname = os.path.basename(t["assets_file"])
        self.tree.item(sel[0], values=(t["name"], sz, "✔" if self._mod_key(t) in self.mods_meta else "", fname))

    def _mesh_mod_count(self):
        frame = getattr(self, "mesh_frame", None)
        return len(getattr(frame, "mesh_mods", {}) or {}) if frame is not None else 0

    def _apply_all_mods(self):
        """Write every queued texture AND mesh replacement in one pass.

        They have to go together. Textures append their pixel bytes to the
        .resS companion file and meshes append their vertex buffers to the very
        same file, and both rewind it to its backup once at the start of a run
        so it can't grow without bound. Applying one kind on its own would
        rewind the .resS out from under the other kind's offsets, leaving those
        objects pointing at bytes that are no longer there.
        """
        count      = len(self.mods_meta)
        mesh_count = self._mesh_mod_count()
        if not count and not mesh_count:
            messagebox.showinfo("No mods", "You haven't added any texture or mesh mods yet."); return
        what = []
        if count:      what.append(f"{count} texture replacement(s)")
        if mesh_count: what.append(f"{mesh_count} mesh replacement(s)")
        if not messagebox.askyesno("Apply Mods",
            f"This will write {' and '.join(what)} to your game files.\n\n"
            "Original files will be backed up automatically (once).\n\nContinue?"): return
        self.status_var.set("Applying mods… please wait."); self.update_idletasks()
        errors, backed_up, applied = [], [], 0
        warnings, uncropped = [], 0
        # Shared across the whole run: each .resS may only be rewound once, or
        # earlier mods in the same file lose their appended pixel bytes.
        ress_reset = set()
        for key, mod in self.mods_meta.items():
            af, png, pid = mod["assets_file"], mod["png_path"], mod["path_id"]
            if not os.path.exists(af):  errors.append(f"File not found: {af}"); continue
            if not os.path.exists(png): errors.append(f"Mod PNG missing: {png}"); continue
            if ensure_backup(af): backed_up.append(os.path.basename(af))
            try:
                apply_single_mod(af, pid, png, ress_reset=ress_reset); applied += 1
            except Exception as e:
                errors.append(f"{mod['name']}: {e}")
                continue
            # Widen the Sprite that fronts this texture to the whole canvas,
            # otherwise it keeps showing only the old logo's tight crop and the
            # replacement comes out cut off. See sprite_crop.py.
            try:
                n, notes = sprite_crop.uncrop_texture_sprites(af, pid)
                uncropped += n
                warnings += [f"{mod['name']}: {note}" for note in notes]
            except Exception as e:
                warnings.append(f"{mod['name']}: sprite crop not repaired ({e})")

        # Meshes second, sharing ress_reset so both kinds of mod end up in the
        # same .resS pass (see the docstring).
        mesh_applied, mesh_notes = 0, []
        if mesh_count:
            self.status_var.set("Applying mesh mods…"); self.update_idletasks()
            m_applied, m_errors, m_warnings = self.mesh_frame.apply_all(
                ress_reset=ress_reset, ensure_backup=ensure_backup)
            mesh_applied = m_applied
            errors      += m_errors
            mesh_notes   = m_warnings

        msg = f"✅ Applied {applied}/{count} texture mods."
        if mesh_count: msg = msg[:-1] + f" and {mesh_applied}/{mesh_count} mesh mods."
        if uncropped: msg += f" Un-cropped {uncropped} sprite(s)."
        if backed_up: msg += f" Backed up: {', '.join(backed_up)}."
        if errors:
            msg += f" ⚠ {len(errors)} error(s)."
            messagebox.showwarning("Some mods failed", "\n".join(errors))
        if warnings:
            msg += f" ⚠ {len(warnings)} warning(s)."
            messagebox.showwarning("Sprite crop notes", "\n\n".join(warnings))
        if mesh_notes:
            messagebox.showinfo("Mesh notes", "\n\n".join(mesh_notes))
        self.status_var.set(msg)

    def _restore_all(self):
        gp = self.cfg.get("game_data_path","")
        if not gp:
            messagebox.showwarning("No game path", "Set your game data folder in Settings first."); return
        restorable = [f for f in find_assets_files(gp) if os.path.exists(f+BACKUP_SUFFIX)]
        if not restorable:
            messagebox.showinfo("No backups", "No backup files found. Have you applied any mods?"); return
        if not messagebox.askyesno("Restore Originals",
            f"This will restore {len(restorable)} original file(s), undoing every\n"
            "texture and mesh replacement written into the game.\n"
            "Your mod PNGs and model files are NOT deleted.\n\nContinue?"): return
        restored = sum(1 for f in restorable if restore_backup(f))
        if self.mesh_frame is not None:
            try:
                self.mesh_frame.refresh_after_restore()
            except Exception:
                pass
        self.status_var.set(f"✅ Restored {restored} file(s). Your mods are still saved — re-apply anytime.")

    # ── Floor Patterns ────────────────────────────────────────────────────────
    def _open_floor_patterns_dialog(self):
        """Per-court wood-grain pattern via the materials' _Texture_Parquet slot.

        Lives in floor_patterns.py, which is self-contained: it imports nothing
        from this file and never calls apply_single_mod(). Imported lazily so a
        missing or broken module degrades to a dialog instead of blocking startup.
        """
        gp = self.cfg.get("game_data_path", "")
        if not gp or not os.path.isdir(gp):
            messagebox.showwarning("No game path",
                                   "Set your game data folder in Settings first."); return
        try:
            from floor_patterns import open_floor_patterns
        except Exception as e:
            messagebox.showerror(
                "Floor Patterns unavailable",
                f"Could not load floor_patterns.py:\n\n{e}\n\n"
                f"It should sit in the modules folder next to app.py."); return
        open_floor_patterns(self, gp, theme={
            "bg":     self.C["bg"],      # matches _apply_styles / current theme
            "panel":  self.C["panel"],
            "accent": self.C["accent"],
            "text":   self.C["text"],
            "muted":  self.C["sub"],
            "entry":  self.C["ebg"],
            "gold":   self.C["gold"],
            "red":    self.C["hi"],
        })

    def _open_gameplay_sliders_dialog(self):
        """2K-style gameplay sliders: CPU skill, user shot windows, movement.

        Lives in slider_tab.py + slider_manager.py, which are self-contained:
        they import nothing from this file and never call apply_single_mod().
        All slider writes are fixed-width in-place float writes, so file size
        never changes. Imported lazily so a missing or broken module degrades
        to a dialog instead of blocking startup.
        """
        gp = self.cfg.get("game_data_path", "")
        if not gp or not os.path.isdir(gp):
            messagebox.showwarning("No game path",
                                   "Set your game data folder in Settings first."); return
        try:
            from slider_tab import open_gameplay_sliders
        except Exception as e:
            messagebox.showerror(
                "Gameplay Sliders unavailable",
                f"Could not load slider_tab.py:\n\n{e}\n\n"
                f"slider_tab.py and slider_manager.py should sit in the "
                f"modules folder next to app.py, with sliders_catalog.json and "
                f"presets.json in the data folder."); return
        open_gameplay_sliders(self, gp, theme={
            "bg":     self.C["bg"],      # matches _apply_styles / current theme
            "panel":  self.C["panel"],
            "accent": self.C["accent"],
            "text":   self.C["text"],
            "muted":  self.C["sub"],
            "entry":  self.C["ebg"],
            "gold":   self.C["gold"],
            "red":    self.C["hi"],
        })

    # ── Court Colors ──────────────────────────────────────────────────────────
    def _open_court_colors_dialog(self):
        gp = self.cfg.get("game_data_path", "")
        if not gp or not os.path.isdir(gp):
            messagebox.showwarning("No game path", "Set your game data folder in Settings first."); return

        C = self.C
        dlg = tk.Toplevel(self); dlg.title("Court Colors")
        # Taller than the old layout to fit the court-variant picker and its note.
        dlg.geometry("560x780"); dlg.configure(bg=C["bg"]); dlg.grab_set()
        dlg.columnconfigure(1, weight=1)

        ttk.Label(dlg, text="Team:").grid(row=0, column=0, sticky="w", padx=12, pady=(14,4))
        team_var = tk.StringVar()
        team_cb  = ttk.Combobox(dlg, textvariable=team_var, state="readonly", width=30)
        team_cb.grid(row=0, column=1, sticky="w", padx=4, pady=(14,4))

        status_lbl = tk.Label(dlg, text="Scanning for teams…", bg=C["bg"], fg=C["sub"],
                               font=("Segoe UI",9), anchor="w", wraplength=440, justify="left")
        status_lbl.grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0,8))

        # Copy colors from another team's court onto the currently selected
        # team. Pure convenience on top of the existing per-property color
        # pickers below -- it only fills in the swatches shown here, it does
        # NOT touch the game until "Apply to Game" is clicked, and it never
        # reads or writes txt_bounce_court_*_D (those are the separate,
        # mostly-transparent center-court logo decals, not the floor color).
        copy_frame = ttk.Frame(dlg, style="Panel.TFrame")
        copy_frame.grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(0,10))
        ttk.Label(copy_frame, text="Copy colors from:", background=C["panel"]).pack(side="left")
        copy_var = tk.StringVar()
        copy_cb  = ttk.Combobox(copy_frame, textvariable=copy_var, state="readonly", width=22)
        copy_cb.pack(side="left", padx=6)
        ttk.Button(copy_frame, text="Copy", style="Soft.TButton",
                   command=lambda: copy_from_team(copy_var.get())).pack(side="left", padx=4)

        swatches = {}   # prop_name -> {"btn": Button, "rgba": (r,g,b,a), "touched": bool}
        # materials: suffix ("stadium_parquet" / "stadium_lines") -> (assets_path, path_id)
        # containers: container_name -> {"assets_path","path_id","profiles"} (scanned once)
        # container: the container matched to the currently selected team
        state = {"materials": {}, "containers": None, "container": None}

        r = 3
        for label, prop, _read_suffix, _alpha, _lines in COURT_COLOR_PROPERTIES:
            ttk.Label(dlg, text=label + ":").grid(row=r, column=0, sticky="w", padx=12, pady=8)
            btn = tk.Button(dlg, text="            ", relief="flat", cursor="hand2",
                             command=lambda p=prop: pick_color(p))
            btn.grid(row=r, column=1, sticky="w", padx=4)
            swatches[prop] = {"btn": btn, "rgba": (1.0, 1.0, 1.0, 1.0), "touched": False}
            r += 1

        # ── Court Lines section ──────────────────────────────────────────────
        # Separate mechanism from the Material colors above: these are runtime
        # overrides stored per-team in BasketTeamContainer. Same dialog and same
        # Apply button, because from the user's point of view it's all "what
        # colour is my court".
        ttk.Separator(dlg, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=(14, 6))
        r += 1
        tk.Label(dlg, text="Court Lines & Areas", bg=C["bg"], fg=C["hi"],
                 font=("Segoe UI", 10, "bold"), anchor="w").grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12)
        r += 1
        lines_note = tk.Label(dlg, text="", bg=C["bg"], fg=C["sub"],
                              font=("Segoe UI", 8), anchor="w", wraplength=480, justify="left")
        lines_note.grid(row=r, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 4))
        r += 1

        line_swatches = {}   # prop_name -> {"btn","rgba","touched"}
        for label, prop, desc in COURT_LINE_PROPERTIES:
            lbl = ttk.Label(dlg, text=label + ":")
            lbl.grid(row=r, column=0, sticky="w", padx=12, pady=6)
            btn = tk.Button(dlg, text="            ", relief="flat", cursor="hand2",
                            state="disabled",
                            command=lambda p=prop: pick_line_color(p))
            btn.grid(row=r, column=1, sticky="w", padx=4)
            tk.Label(dlg, text=desc, bg=C["bg"], fg=C["sub"],
                     font=("Segoe UI", 8), anchor="w").grid(
                row=r, column=1, sticky="w", padx=(130, 4))
            line_swatches[prop] = {"btn": btn, "rgba": (1.0, 1.0, 1.0, 0.0), "touched": False}
            r += 1

        # Retro/throwback courts carry their own separate color entries in the
        # container. Rather than a blunt "apply to everything" toggle, let the
        # user pick exactly which court they're editing -- named by the real NBA
        # era it represents (see retro_court_eras.json). "All courts" reproduces
        # the old broadcast-to-every-variant behaviour.
        ttk.Separator(dlg, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=(10, 6))
        r += 1
        ttk.Label(dlg, text="Court variant:").grid(row=r, column=0, sticky="w", padx=12, pady=(2, 2))
        variant_var = tk.StringVar()
        variant_cb = ttk.Combobox(dlg, textvariable=variant_var, state="readonly", width=34)
        variant_cb.grid(row=r, column=1, sticky="w", padx=4, pady=(2, 2))
        r += 1
        # Centre-court logo decal for the selected variant, composited over that
        # era's floor colour. The decal itself is mostly transparent, so shown
        # on its own it would read as a blank strip.
        logo_canvas = tk.Canvas(dlg, height=64, bg=C["panel"], highlightthickness=0)
        logo_canvas.grid(row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=(2, 2))
        r += 1

        variant_note = tk.Label(dlg, text="", bg=C["bg"], fg=C["sub"],
                                font=("Segoe UI", 8), anchor="w",
                                wraplength=480, justify="left")
        variant_note.grid(row=r, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 4))
        r += 1

        # How many courts this team has, and a reminder that unlike floor
        # patterns, colour edits are in-place and have no slot budget.
        budget_lbl = tk.Label(dlg, text="", bg=C["bg"], fg=C["sub"],
                              font=("Segoe UI", 8), anchor="w",
                              wraplength=480, justify="left")
        budget_lbl.grid(row=r, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 6))
        r += 1

        # variant_choices: list of (variant_key, display_string) for the loaded team.
        state["variant_choices"] = []
        state["logo_photo"] = None      # keep the PhotoImage alive
        state["logo_token"] = 0         # guards against out-of-order async loads

        def show_logo_message(text):
            logo_canvas.delete("all")
            w = logo_canvas.winfo_width() or 500
            logo_canvas.create_text(w // 2, 32, text=text, fill=C["sub"],
                                    font=("Segoe UI", 8))

        def render_logo(img, floor_rgb):
            """Composite the decal over the era's floor colour and draw it."""
            cw = logo_canvas.winfo_width() or 500
            bg = tuple(max(0, min(255, int(c * 255))) for c in floor_rgb)
            plate = Image.new("RGBA", img.size, bg + (255,))
            plate.alpha_composite(img)
            scale = min(cw / plate.width, 60 / plate.height)
            plate = plate.resize((max(1, int(plate.width * scale)),
                                  max(1, int(plate.height * scale))), Image.LANCZOS)
            state["logo_photo"] = ImageTk.PhotoImage(plate)
            logo_canvas.delete("all")
            logo_canvas.create_image(cw // 2, 32, image=state["logo_photo"], anchor="center")

        def load_logo_async():
            """Decode the selected variant's decal off the UI thread.

            resources.assets is large; the name index is cached after the first
            call, so only the first preview in a session is slow.
            """
            cname = state.get("container_name")
            vkey = current_variant()
            tex = retro_eras.court_texture_for(cname, vkey)
            floor_rgb = retro_eras.floor_color_for(cname, vkey)
            state["logo_token"] += 1
            token = state["logo_token"]
            if not tex:
                show_logo_message("No centre-court logo mapped for this court.")
                return
            show_logo_message("Loading logo…")

            def work():
                img, err = None, None
                try:
                    img = get_court_decal_image(gp, tex)
                except Exception as e:
                    err = e

                def done():
                    if token != state["logo_token"]:
                        return          # a newer selection already superseded this
                    if img is not None:
                        try:
                            render_logo(img, floor_rgb)
                        except Exception as e:
                            show_logo_message(f"Could not draw logo: {e}")
                    elif err is not None:
                        show_logo_message(f"Could not read {tex}: {err}")
                    else:
                        show_logo_message(f"{tex} not found in resources.assets.")
                self.after(0, done)

            threading.Thread(target=work, daemon=True).start()

        def current_variant():
            """The container variant key the swatches are currently bound to."""
            label = variant_var.get()
            for key, disp in state.get("variant_choices", []):
                if disp == label:
                    return key
            return CONTAINER_DEFAULT_VARIANT

        def rgba_to_hex(rgba):
            rr, gg, bb = rgba[0], rgba[1], rgba[2]
            return "#%02x%02x%02x" % (
                max(0, min(255, int(rr * 255))),
                max(0, min(255, int(gg * 255))),
                max(0, min(255, int(bb * 255))),
            )

        def pick_color(prop):
            label, alpha = next((l, a) for l, p, _s, a, _ln in COURT_COLOR_PROPERTIES if p == prop)
            cur_rgba = swatches[prop]["rgba"]
            rgb = open_color_picker(dlg, cur_rgba[:3], title=f"Pick a Color — {label}", theme=C)
            if rgb is None:
                return
            rr, gg, bb = rgb
            swatches[prop]["rgba"] = (rr, gg, bb, alpha)
            swatches[prop]["touched"] = True
            hexval = rgba_to_hex((rr, gg, bb, alpha))
            swatches[prop]["btn"].configure(bg=hexval, activebackground=hexval)

        def pick_line_color(prop):
            label = next(l for l, p, _d in COURT_LINE_PROPERTIES if p == prop)
            cur_rgba = line_swatches[prop]["rgba"]
            rgb = open_color_picker(dlg, cur_rgba[:3], title=f"Pick a Color — {label}", theme=C)
            if rgb is None:
                return
            # Preserve the shipped alpha. On this shader alpha is not opacity --
            # most of these ship at 0.0 and still render fully -- so it behaves as
            # a blend/mode switch and is not safe to normalize to 1.0.
            rr, gg, bb = rgb
            line_swatches[prop]["rgba"] = (rr, gg, bb, cur_rgba[3])
            line_swatches[prop]["touched"] = True
            hexval = rgba_to_hex((rr, gg, bb, cur_rgba[3]))
            line_swatches[prop]["btn"].configure(bg=hexval, activebackground=hexval)

        def load_line_colors(team):
            """Match the selected team to its BasketTeamContainer and fill the
            line swatches. Containers are scanned once and cached, since it means
            walking all of sharedassets1.assets."""
            for prop in line_swatches:
                line_swatches[prop]["touched"] = False

            if state["containers"] is None:
                state["containers"] = find_team_containers(gp)

            cname, cdata = match_container_for_team(state["containers"], team)
            state["container"] = cdata
            state["container_name"] = cname

            if not cdata:
                for prop in line_swatches:
                    line_swatches[prop]["btn"].configure(state="disabled", bg=C["dis_bg"],
                                                         activebackground=C["dis_bg"])
                lines_note.configure(
                    text="No line-color data found for this team — line colors unavailable here.")
                state["variant_choices"] = []
                variant_cb.configure(values=[], state="disabled")
                variant_var.set("")
                variant_note.configure(text="")
                budget_lbl.configure(text="")
                state["logo_token"] += 1        # cancel any in-flight logo load
                show_logo_message("No court variants for this team.")
                return

            # Build the era-named dropdown from whatever variants this team's
            # container actually has, then load the first one (the modern court).
            choices = retro_eras.choices_for(cname, cdata)
            state["variant_choices"] = choices
            variant_cb.configure(values=[disp for _k, disp in choices])
            if choices:
                variant_var.set(choices[0][1])
            variant_cb.configure(state="readonly" if len(choices) > 1 else "disabled")

            n_retro = sum(1 for k, _d in choices
                          if k not in (CONTAINER_DEFAULT_VARIANT, retro_eras.ALL_VARIANTS))
            lines_note.configure(
                text=(f"From {cname}. These are runtime overrides — they win over the court "
                      f"material, which is why editing the material alone did nothing."
                      + (f" This team has {n_retro} retro court(s); pick one below to edit it "
                         f"on its own." if n_retro else "")))
            budget_lbl.configure(
                text=(f"Courts for this team: {n_retro + 1} "
                      f"(1 modern + {n_retro} retro). "
                      "Colour edits patch in place, so there is no slot budget here — "
                      "every court of every team can be recoloured. The 8-slot limit "
                      "applies only to Floor Patterns."))
            load_variant_colors()

        def load_variant_colors(*_a):
            """Fill the line swatches from whichever court variant is selected.

            'All courts' previews the modern court's values, since that's what
            would be broadcast to the rest.
            """
            cdata = state.get("container")
            cname = state.get("container_name")
            if not cdata:
                return
            vkey = current_variant()
            read_key = CONTAINER_DEFAULT_VARIANT if vkey == retro_eras.ALL_VARIANTS else vkey

            for prop in line_swatches:
                line_swatches[prop]["touched"] = False

            for _label, prop, _desc in COURT_LINE_PROPERTIES:
                variants = cdata["profiles"].get(prop, {})
                # Not every property exists on every variant; fall back to the
                # modern court so the swatch still shows something meaningful.
                entry = variants.get(read_key) or variants.get(CONTAINER_DEFAULT_VARIANT)
                if entry is None:
                    line_swatches[prop]["btn"].configure(state="disabled", bg=C["dis_bg"],
                                                         activebackground=C["dis_bg"])
                    continue
                rgba = tuple(entry["rgba"])
                line_swatches[prop]["rgba"] = rgba
                hexval = rgba_to_hex(rgba)
                line_swatches[prop]["btn"].configure(state="normal", bg=hexval,
                                                     activebackground=hexval)

            rec = retro_eras.describe(cname, vkey)
            bits = []
            if vkey == retro_eras.ALL_VARIANTS:
                bits.append("Edits will be written to every court this team has, "
                            "modern and retro.")
            elif vkey != CONTAINER_DEFAULT_VARIANT:
                if rec.get("evidence"):
                    bits.append(rec["evidence"])
                caveat = retro_eras.confidence_note(cname, vkey)
                if caveat:
                    bits.append(caveat)
            variant_note.configure(text=" ".join(bits))
            load_logo_async()

        variant_cb.bind("<<ComboboxSelected>>", load_variant_colors)

        def load_team():
            team = team_var.get()
            if not team:
                return
            status_lbl.configure(text=f"Loading {team}…")
            self.update_idletasks()

            state["materials"] = {}
            missing = []
            for suffix in COURT_COLOR_WRITE_SUFFIXES:
                af, pid = find_court_material(gp, team, suffix)
                if af is None:
                    missing.append(suffix)
                else:
                    state["materials"][suffix] = (af, pid)

            if not state["materials"]:
                status_lbl.configure(text=f"⚠ Could not find any court material for {team}.")
                return

            # Each property previews from whichever material is confirmed to
            # actually render it (parquet for floor/apron, lines for the arc).
            for label, prop, read_suffix, _alpha, _lines in COURT_COLOR_PROPERTIES:
                mat = state["materials"].get(read_suffix)
                rgba = (1.0, 1.0, 1.0, 1.0)
                if mat:
                    af, pid = mat
                    colors, _floats = read_court_colors(af, pid)
                    rgba = colors.get(prop, rgba)
                swatches[prop]["rgba"] = rgba
                swatches[prop]["touched"] = False  # loading a team resets edits, not user picks
                swatches[prop]["btn"].configure(bg=rgba_to_hex(rgba), activebackground=rgba_to_hex(rgba))

            # Court line colors come from a different place entirely (see
            # load_line_colors) -- scanned once, then matched per team.
            try:
                load_line_colors(team)
            except Exception as e:
                lines_note.configure(text=f"⚠ Could not read line colors: {e}")

            msg = "Loaded. Colors are only visible in-game after you click Apply and restart the game."
            if missing:
                msg += f" (Note: no {', '.join(missing)} material found for this team — some colors may not apply.)"
            status_lbl.configure(text=msg)

        def copy_from_team(source_team):
            """Read the source team's current court colors, show the user
            exactly what's changing (destination's current value -> source's
            value) for confirmation, then fill the swatches above for
            whichever team is loaded as the destination. Only updates the
            in-dialog preview -- nothing is written to any file until
            'Apply to Game' is clicked, so this is safe to try and change
            your mind about."""
            if not source_team:
                messagebox.showinfo("No source", "Choose a team to copy colors from.", parent=dlg); return
            if not state["materials"]:
                messagebox.showwarning("No team selected", "Pick a destination team first.", parent=dlg); return
            dest_team = team_var.get()
            if source_team == dest_team:
                messagebox.showinfo("Same team", "Source and destination are the same team.", parent=dlg); return

            # Look up whichever materials the configured properties actually
            # need, per their own read_suffix, rather than assuming they're
            # all on one -- both current properties (Floor Color, Team
            # Color) are on stadium_parquet, but this stays generic in case
            # a future confirmed property lives elsewhere.
            needed_suffixes = sorted({read_suffix for _l, _p, read_suffix, _a, _ln in COURT_COLOR_PROPERTIES})
            src_by_suffix = {}   # suffix -> (colors dict, floats dict)
            try:
                for suffix in needed_suffixes:
                    src_af, src_pid = find_court_material(gp, source_team, suffix)
                    if src_af is not None:
                        src_by_suffix[suffix] = read_court_colors(src_af, src_pid)
            except Exception as e:
                messagebox.showerror("Read failed", f"Couldn't read colors from {source_team}:\n{e}", parent=dlg); return

            if not src_by_suffix:
                messagebox.showwarning("Not found", f"No court material found for {source_team}.", parent=dlg); return

            def src_color(prop, read_suffix):
                colors, _floats = src_by_suffix.get(read_suffix, ({}, {}))
                return colors.get(prop)

            if not any(src_color(prop, rs) is not None for _l, prop, rs, _a, _ln in COURT_COLOR_PROPERTIES):
                messagebox.showwarning("Nothing to copy",
                    f"Couldn't read any color properties off {source_team}'s court material(s).", parent=dlg)
                return

            # Show exactly what's changing -- destination's current color vs.
            # the incoming source color -- for every property, before
            # touching anything.
            lines_preview = [f"Copy court colors:  {source_team}  →  {dest_team}", ""]
            any_diff = False
            for _label, prop, read_suffix, _a, needs_lines in COURT_COLOR_PROPERTIES:
                new_rgba = src_color(prop, read_suffix)
                if new_rgba is None:
                    continue
                cur_hex = rgba_to_hex(swatches[prop]["rgba"])
                new_hex = rgba_to_hex(new_rgba)
                lines_preview.append(f"{_label}:   {dest_team} (current) {cur_hex}   →   {source_team} {new_hex}")
                any_diff = any_diff or (cur_hex != new_hex)
            if not any_diff:
                lines_preview.append("")
                lines_preview.append("(These already match — applying will make no visible change.)")

            if not messagebox.askyesno("Copy Court Colors", "\n".join(lines_preview), parent=dlg):
                return

            # No current property needs _Court_Lines_ON_OFF forced on (that
            # was only true of the now-removed Out-of-Bounds Color), but
            # this stays generic/safe in case a future confirmed property
            # does: for those, carry over whether the source team had its
            # own material's lines mode on, defaulting to off if unknown.
            needs_lines_suffixes = {rs for _l, _p, rs, _a, needs_lines in COURT_COLOR_PROPERTIES if needs_lines}
            lines_on_by_suffix = {}
            for rs in needs_lines_suffixes:
                _c, f = src_by_suffix.get(rs, ({}, {}))
                lines_on_by_suffix[rs] = f.get(COURT_LINES_TOGGLE_PROPERTY, 0.0) >= 0.5

            for _label, prop, read_suffix, _a, needs_lines in COURT_COLOR_PROPERTIES:
                new_rgba = src_color(prop, read_suffix)
                if new_rgba is None:
                    continue
                swatches[prop]["rgba"] = new_rgba
                swatches[prop]["touched"] = lines_on_by_suffix.get(read_suffix, False) if needs_lines else True
                hexval = rgba_to_hex(new_rgba)
                swatches[prop]["btn"].configure(bg=hexval, activebackground=hexval)
            # Court line colors live in the source team's container, not its
            # material, so copy those across too -- otherwise "copy colors from"
            # would silently leave half the court behind.
            copied_lines = 0
            try:
                if state["containers"] is None:
                    state["containers"] = find_team_containers(gp)
                _sname, src_container = match_container_for_team(state["containers"], source_team)
                if src_container and state["container"]:
                    for _label, prop, _desc in COURT_LINE_PROPERTIES:
                        src_entry = src_container["profiles"].get(prop, {}).get(CONTAINER_DEFAULT_VARIANT)
                        dst_entry = state["container"]["profiles"].get(prop, {}).get(CONTAINER_DEFAULT_VARIANT)
                        if src_entry is None or dst_entry is None:
                            continue
                        src_rgba = tuple(src_entry["rgba"])
                        # Keep the destination's own alpha: it's a shader mode
                        # switch here, not opacity, so it isn't safe to carry over.
                        new_rgba = (src_rgba[0], src_rgba[1], src_rgba[2],
                                    line_swatches[prop]["rgba"][3])
                        line_swatches[prop]["rgba"] = new_rgba
                        line_swatches[prop]["touched"] = True
                        hexval = rgba_to_hex(new_rgba)
                        line_swatches[prop]["btn"].configure(bg=hexval, activebackground=hexval)
                        copied_lines += 1
            except Exception:
                pass   # line colors are a bonus here; never block the material copy

            status_lbl.configure(text=f"Copied colors from {source_team} — not saved yet. "
                                       f"Click 'Apply to Game' to write this to {dest_team}, "
                                       "or pick a different team above to discard it."
                                       + (f" ({copied_lines} line color(s) included.)" if copied_lines else ""))

        team_cb.bind("<<ComboboxSelected>>", lambda e: load_team())

        def populate_teams():
            teams = find_court_teams(gp)
            def done():
                team_cb.configure(values=teams)
                copy_cb.configure(values=teams)
                if teams:
                    team_var.set(teams[0])
                    load_team()
                else:
                    status_lbl.configure(text="⚠ No court materials found in this game data folder.")
            self.after(0, done)
        threading.Thread(target=populate_teams, daemon=True).start()

        # Materials whose _Court_Lines_ON_OFF default is confirmed to matter
        # and confirmed safe to normalize back to 0 when nothing on them
        # needs it forced on. stadium_parquet is the one material where this
        # was hand-validated (lines OFF is required for Floor Color to tint
        # the texture at all). Other materials -- currently just
        # stadium_lines, which ships at 1.0 for every team in
        # team_court_palette.json and has never been tested at 0 -- are left
        # untouched unless one of their own properties explicitly needs
        # lines forced on, rather than risk writing an unconfirmed value.
        CONFIRMED_LINES_OFF_SUFFIXES = {"stadium_parquet"}

        def float_sets_for(suffix):
            needs_on = any(swatches[prop]["touched"] for _l, prop, rs, _a, needs_lines
                           in COURT_COLOR_PROPERTIES if needs_lines and rs == suffix)
            if needs_on:
                return {COURT_LINES_TOGGLE_PROPERTY: 1.0}
            if suffix in CONFIRMED_LINES_OFF_SUFFIXES:
                # Normalizes this back to 0 even if an older version of this
                # feature (when Out-of-Bounds Color still lived on this same
                # material) left it stuck at 1, which would otherwise keep
                # silently breaking Floor Color's tint on this team.
                return {COURT_LINES_TOGGLE_PROPERTY: 0.0}
            return {}

        def apply_colors():
            if not state["materials"]:
                messagebox.showwarning("No team selected", "Pick a team first.", parent=dlg); return
            status_lbl.configure(text="Applying…"); self.update_idletasks()
            # Write every color property to every material we found for this
            # team -- we can't always be sure in advance which material
            # renders which property, so this covers it regardless (harmless
            # where a property isn't actually used by a given material).
            color_sets = {prop: swatches[prop]["rgba"] for _label, prop, _s, _a, _ln in COURT_COLOR_PROPERTIES}
            errors, verify_errors = [], []
            any_lines_forced_on = False
            for suffix, (af, pid) in state["materials"].items():
                ensure_backup(af)
                float_sets = float_sets_for(suffix)
                if float_sets.get(COURT_LINES_TOGGLE_PROPERTY) == 1.0:
                    any_lines_forced_on = True
                try:
                    apply_material_color_mod(af, pid, color_sets, float_sets)
                except Exception as e:
                    errors.append(f"{suffix}: {e}")
                    continue
                # Read the value straight back off disk to confirm the write
                # actually stuck, instead of just trusting that the call
                # didn't raise -- catches silent property-name/shape
                # mismatches (UnityPy's typetree layout can vary) rather than
                # reporting success when nothing really changed.
                try:
                    check_colors, check_floats = read_court_colors(af, pid)
                    for prop, rgba in color_sets.items():
                        got = check_colors.get(prop)
                        if got is None or any(abs(a - b) > 0.004 for a, b in zip(got, rgba)):
                            verify_errors.append(f"{suffix}.{prop}: wrote {rgba} but read back {got}")
                    for prop_name, want in float_sets.items():
                        got = check_floats.get(prop_name)
                        if got is None or abs(got - want) > 0.004:
                            verify_errors.append(f"{suffix}.{prop_name}: wrote {want} but read back {got}")
                except Exception as e:
                    verify_errors.append(f"{suffix}: couldn't verify write ({e})")

            # ── Court line colors (BasketTeamContainer runtime overrides) ────
            # Only writes properties the user actually touched, so opening the
            # dialog and clicking Apply for a floor change can't quietly rewrite
            # line colors that were never edited.
            n_lines_written = 0
            cdata = state.get("container")
            if cdata:
                patches = []
                target_variant = current_variant()
                for _label, prop, _desc in COURT_LINE_PROPERTIES:
                    if not line_swatches[prop]["touched"]:
                        continue
                    new_rgb = line_swatches[prop]["rgba"][:3]
                    variants = cdata["profiles"].get(prop, {})
                    for vname, entry in variants.items():
                        # Write only the court the user picked, unless they chose
                        # "All courts" -- so recoloring a throwback can no longer
                        # silently repaint the modern court, or vice versa.
                        if (target_variant != retro_eras.ALL_VARIANTS
                                and vname != target_variant):
                            continue
                        cur = tuple(entry["rgba"])
                        patches.append({
                            "offset": entry["offset"],
                            "expect": cur,
                            # Preserve each variant's own alpha (shader mode switch,
                            # not opacity) and only change RGB.
                            "new": (new_rgb[0], new_rgb[1], new_rgb[2], cur[3]),
                        })
                if patches:
                    af = cdata["assets_path"]
                    ensure_backup(af)
                    try:
                        apply_container_color_mod(af, patches)
                        for p in verify_container_colors(af, patches):
                            verify_errors.append(f"court lines: {p}")
                        n_lines_written = len(patches)
                        # Offsets are unchanged by an in-place patch, but the cached
                        # rgba values are now stale -- refresh so a second Apply in
                        # the same session doesn't trip the expected-value check.
                        for p in patches:
                            for variants in cdata["profiles"].values():
                                for entry in variants.values():
                                    if entry["offset"] == p["offset"]:
                                        entry["rgba"] = p["new"]
                    except Exception as e:
                        errors.append(f"court lines: {e}")

            if errors:
                messagebox.showerror("Some materials failed", "\n".join(errors), parent=dlg)
                status_lbl.configure(text="⚠ Applied with errors — see dialog.")
            elif verify_errors:
                messagebox.showwarning("Applied, but verification found a mismatch",
                    "The write call didn't raise an error, but reading the values back off disk "
                    "doesn't match what was requested:\n\n" + "\n".join(verify_errors), parent=dlg)
                status_lbl.configure(text="⚠ Applied, but verification found a mismatch — see dialog.")
            else:
                msg = "✅ Applied and verified on disk. Fully quit and relaunch the game to see it."
                if n_lines_written:
                    where = retro_eras.describe(state.get("container_name"),
                                                current_variant()).get("label", "")
                    msg += f" ({n_lines_written} court line value(s) written to “{where}”.)"
                if any_lines_forced_on:
                    msg += (" Note: this forced a material's lines mode on for one of the "
                            "properties you set -- see the comment on COURT_COLOR_PROPERTIES "
                            "if that combination looks off.")
                status_lbl.configure(text=msg)
                self.status_var.set(f"✅ Court colors applied for {team_var.get()}.")

        def reset_team():
            if not state["materials"]:
                messagebox.showwarning("No team selected", "Pick a team first.", parent=dlg); return
            resettable = [(suf, af, pid) for suf, (af, pid) in state["materials"].items()
                          if os.path.exists(af + BACKUP_SUFFIX)]
            if not resettable:
                messagebox.showinfo("Nothing to reset",
                    "No backup exists yet — this team hasn't been modified.", parent=dlg)
                return
            status_lbl.configure(text="Resetting…"); self.update_idletasks()
            errors = []
            for suffix, af, pid in resettable:
                # Each material has its own original defaults (parquet and
                # lines are NOT the same color by default) -- reset each from
                # its own backup, not from a shared value.
                orig_colors, orig_floats = read_court_colors(af, pid, from_backup=True)
                color_sets = {prop: orig_colors[prop] for _l, prop, _s, _a, _ln in COURT_COLOR_PROPERTIES if prop in orig_colors}
                float_sets = {COURT_LINES_TOGGLE_PROPERTY: orig_floats.get(COURT_LINES_TOGGLE_PROPERTY, 0.0)}
                try:
                    apply_material_color_mod(af, pid, color_sets, float_sets)
                except Exception as e:
                    errors.append(f"{suffix}: {e}")

            # Court line colors reset from the same .original_backup, read at the
            # same offsets -- the shipped values, not whatever is live now.
            cdata = state.get("container")
            if cdata:
                try:
                    wanted = []
                    for _label, prop, _desc in COURT_LINE_PROPERTIES:
                        for entry in cdata["profiles"].get(prop, {}).values():
                            wanted.append(entry)
                    originals = read_container_colors_from_backup(
                        cdata["assets_path"], [e["offset"] for e in wanted])
                    patches = [{"offset": e["offset"], "expect": tuple(e["rgba"]),
                                "new": originals[e["offset"]]}
                               for e in wanted
                               if e["offset"] in originals
                               and any(abs(a - b) > 1e-5
                                       for a, b in zip(e["rgba"], originals[e["offset"]]))]
                    if patches:
                        apply_container_color_mod(cdata["assets_path"], patches)
                        for p in patches:
                            for variants in cdata["profiles"].values():
                                for entry in variants.values():
                                    if entry["offset"] == p["offset"]:
                                        entry["rgba"] = p["new"]
                except Exception as e:
                    errors.append(f"court lines: {e}")

            load_team()
            if errors:
                messagebox.showerror("Some materials failed", "\n".join(errors), parent=dlg)
                status_lbl.configure(text="⚠ Reset with errors — see dialog.")
            else:
                status_lbl.configure(text="✅ Reset to original. Fully quit and relaunch the game to see it.")
                self.status_var.set(f"✅ Court colors reset for {team_var.get()}.")

        bf = ttk.Frame(dlg, style="Panel.TFrame")
        bf.grid(row=r+1, column=0, columnspan=2, pady=16)
        ttk.Button(bf, text="Apply to Game",     style="Accent.TButton", command=apply_colors).pack(side="left", padx=6)
        ttk.Button(bf, text="Reset This Team",   style="Soft.TButton",   command=reset_team).pack(side="left", padx=6)

if __name__ == "__main__":
    app = ModManagerApp()
    app.mainloop()
