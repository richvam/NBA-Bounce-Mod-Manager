import sys
import subprocess
import os

# ── Self-installer ─────────────────────────────────────────────────────────────
REQUIRED_PACKAGES = [
    "attrs", "lz4", "brotli", "fsspec", "etcpak",
    "texture2ddecoder", "astc-encoder-py", "Pillow", "UnityPy",
]

def _ensure_dependencies():
    try:
        import UnityPy
        from PIL import Image
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
        return True
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
import json, shutil, threading, zipfile, re, string
from io import BytesIO
from PIL import Image, ImageTk
import UnityPy

# ── Constants ──────────────────────────────────────────────────────────────────
APP_NAME       = "NBA Bounce Texture Mod Manager"
APP_DIR        = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE    = os.path.join(APP_DIR, "config.json")
MODS_META_FILE = "mods.json"
BACKUP_SUFFIX  = ".original_backup"

# ── NBA-branded color themes ───────────────────────────────────────────────────
# Dark  = "Arena Night"    — deep court under stadium lights
# Light = "Broadcast Day"  — crisp TV broadcast clarity
THEMES = {
    "dark": {
        # ── Backgrounds ──────────────────────────────────────────────────────
        "bg":          "#08101E",   # near-black navy — main window
        "panel":       "#0E1828",   # dark navy — top bar, panels, status bar
        "ebg":         "#060C16",   # deepest — treeview even rows, canvases
        "row_odd":     "#0C1525",   # treeview odd rows — subtle stripe
        # ── NBA brand ────────────────────────────────────────────────────────
        "accent":      "#1D428A",   # NBA official blue — headings, soft buttons
        "hi":          "#C8102E",   # NBA official red — primary CTA, selection
        "hi_active":   "#A00D24",   # pressed / hover red
        "gold":        "#FDB927",   # NBA gold — modded rows, checkmark highlight
        "soft_active": "#2550A8",   # brighter blue on hover
        # ── Text ─────────────────────────────────────────────────────────────
        "text":        "#ECF0FF",   # near-white with subtle blue cast
        "sub":         "#5B7AA0",   # muted periwinkle — labels, meta text
        # ── Disabled ─────────────────────────────────────────────────────────
        "dis_bg":      "#172030",
        "dis_fg":      "#364E68",
        # ── Borders / structural ─────────────────────────────────────────────
        "border":      "#1D428A",   # orig canvas border — NBA blue
        "mod_border":  "#C8102E",   # mod canvas border — NBA red
        "sash":        "#1D428A",
        "sep":         "#C8102E",   # top-bar accent separator stripe
    },
    "light": {
        # ── Backgrounds ──────────────────────────────────────────────────────
        "bg":          "#EBF0FC",   # cool blue-white — main window
        "panel":       "#FFFFFF",   # pure white — top bar, panels, status bar
        "ebg":         "#D8E4F5",   # pale blue — treeview even rows, canvases
        "row_odd":     "#EFF4FD",   # treeview odd rows — near white
        # ── NBA brand ────────────────────────────────────────────────────────
        "accent":      "#1D428A",   # NBA blue — unchanged
        "hi":          "#C8102E",   # NBA red — unchanged
        "hi_active":   "#A00D24",
        "gold":        "#8A5A00",   # burnished gold — readable on white
        "soft_active": "#2550A8",
        # ── Text ─────────────────────────────────────────────────────────────
        "text":        "#07142A",   # deep navy
        "sub":         "#3D5C88",   # medium navy-blue
        # ── Disabled ─────────────────────────────────────────────────────────
        "dis_bg":      "#BDD0E8",
        "dis_fg":      "#7A98B8",
        # ── Borders / structural ─────────────────────────────────────────────
        "border":      "#1D428A",   # orig canvas border — NBA blue
        "mod_border":  "#C8102E",   # mod canvas border — NBA red
        "sash":        "#1D428A",
        "sep":         "#C8102E",   # top-bar accent separator stripe
    },
}

# ── Config ─────────────────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f: return json.load(f)
    return {"game_data_path": "", "mods_folder": "", "theme": "dark"}

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f: json.dump(cfg, f, indent=2)

def load_mods_meta(folder):
    p = os.path.join(folder, MODS_META_FILE)
    if os.path.exists(p):
        with open(p) as f: return json.load(f)
    return {}

def save_mods_meta(folder, meta):
    with open(os.path.join(folder, MODS_META_FILE), "w") as f:
        json.dump(meta, f, indent=2)

# ── Auto-detect game install ──────────────────────────────────────────────────
def _is_valid_game_data(path):
    return bool(path and os.path.isdir(path) and
                any(f.endswith(".assets") for f in os.listdir(path)))

def detect_game_install():
    drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    steam_roots = []
    for drive in drives:
        for folder in ["Steam", "SteamLibrary", "Program Files/Steam",
                       "Program Files (x86)/Steam"]:
            p = os.path.join(drive, folder)
            if os.path.isdir(p):
                steam_roots.append(p)
    # Parse libraryfolders.vdf for non-standard Steam libraries
    for root in list(steam_roots):
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        if os.path.exists(vdf):
            try:
                with open(vdf, encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
                for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
                    p = m.group(1).replace("\\\\", "\\")
                    if os.path.isdir(p) and p not in steam_roots:
                        steam_roots.append(p)
            except Exception:
                pass
    for root in steam_roots:
        candidate = os.path.join(root, "steamapps", "common",
                                 "NBA BOUNCE", "NBA Bounce_Data")
        if _is_valid_game_data(candidate):
            return candidate
    return None

# ── Version reading ───────────────────────────────────────────────────────────
def get_game_info(game_data_path):
    """
    Read Unity engine version and game build version from globalgamemanagers.

    The Unity version is a null-terminated ASCII string stored in the
    serialized-file header, typically at byte offset ~28.  Searching raw bytes
    is more reliable than decoding to text first.

    The previous regex anchored on '20' so it silently missed Unity 6
    (version string '6000.0.37f1').  The fix uses \\d{4} instead.
    """
    info = {"unity": "unknown", "app": "unknown"}
    ggm  = os.path.join(game_data_path, "globalgamemanagers")
    if not os.path.exists(ggm):
        return info
    try:
        with open(ggm, "rb") as f:
            raw = f.read(16384)          # read more for app-version search

        # ── Unity engine version ─────────────────────────────────────────────
        # Covers legacy Unity (2019-2022: "20xx.y.zfN")
        # AND Unity 6+ ("6000.y.zfN", "6001.y.zfN", …)
        m = re.search(rb"(\d{4}\.\d+\.\d+[a-z]\d+)", raw)
        if m:
            info["unity"] = m.group(1).decode("ascii")

        # ── Game / app build version ─────────────────────────────────────────
        # bundleVersion is stored as a length-prefixed or null-terminated
        # string in the binary metadata; the text decode below works for the
        # ASCII range used by typical version strings.
        txt = raw.decode("utf-8", errors="replace")
        for key in ["bundleVersion", "m_BundleVersion",
                    "applicationVersion", "m_ApplicationVersion"]:
            idx = txt.find(key)
            if idx != -1:
                # The actual value follows the key; skip non-digit prefix chars
                chunk = txt[idx + len(key) : idx + len(key) + 64]
                vm = re.search(r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)", chunk)
                if vm:
                    info["app"] = vm.group(1)
                    break

    except Exception:
        pass
    return info

# ── Unity helpers ──────────────────────────────────────────────────────────────
def find_assets_files(game_data_path):
    result = []
    for f in sorted(os.listdir(game_data_path)):
        full = os.path.join(game_data_path, f)
        if f.endswith(".assets"):
            result.append(full)
        elif f.startswith("level") and "." not in f:
            result.append(full)
    return result

def extract_textures_from_file(assets_path):
    textures = []
    env = UnityPy.load(assets_path)
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        try:
            name = obj.peek_name() or f"texture_{obj.path_id}"
            data = obj.read()
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
        except Exception:
            try:
                textures.append({
                    "name":        obj.peek_name() or f"texture_{obj.path_id}",
                    "path_id":     obj.path_id,
                    "assets_file": assets_path,
                    "width":       0, "height": 0, "format": "unknown",
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
            return data.image
    return None

def _find_ress_file(assets_path, stream_path):
    import ntpath
    assets_dir = os.path.dirname(assets_path)
    basename = ntpath.basename(stream_path)
    candidate = os.path.join(assets_dir, basename)
    if os.path.exists(candidate):
        return candidate
    for part in stream_path.replace("\\", "/").split("/"):
        candidate = os.path.join(assets_dir, part)
        if os.path.exists(candidate):
            return candidate
    return None

def _get_encode_format(original_format_id):
    from UnityPy.enums import TextureFormat as TF
    DXT5_FAMILY = {TF.DXT5.value, TF.DXT5Crunched.value}
    DXT1_FAMILY = {TF.DXT1.value, TF.DXT1Crunched.value}
    if original_format_id in DXT5_FAMILY:
        return TF.BC7, TF.BC7.value
    elif original_format_id in DXT1_FAMILY:
        return TF.DXT1, TF.DXT1.value
    else:
        try:
            fmt = TF(original_format_id)
            return fmt, fmt.value
        except ValueError:
            return TF.RGBA32, TF.RGBA32.value

def apply_single_mod(assets_path, path_id, replacement_png_path):
    """
    IN-PLACE BINARY PATCH — the only reliable method that prevents data loss.
    DO NOT MODIFY THIS FUNCTION.
    """
    from UnityPy.export.Texture2DConverter import image_to_texture2d
    from UnityPy.streams.EndianBinaryWriter import EndianBinaryWriter
    from UnityPy.helpers.TypeTreeHelper import write_typetree

    env = UnityPy.load(assets_path)
    target_obj = None
    for obj in env.objects:
        if obj.path_id == path_id:
            target_obj = obj; break
    if target_obj is None:
        raise ValueError(f"Texture with path_id {path_id} not found")

    data        = target_obj.read()
    new_image   = Image.open(replacement_png_path).convert("RGBA")
    orig_format = data.m_TextureFormat
    stream_path = getattr(data.m_StreamData, 'path', '') if data.m_StreamData else ''
    ress_path   = _find_ress_file(assets_path, stream_path) if stream_path else None
    has_ress    = bool(ress_path and os.path.exists(ress_path))

    encode_fmt, write_format_id = _get_encode_format(orig_format)
    enc_bytes, _ = image_to_texture2d(new_image, encode_fmt)

    if has_ress:
        ress_bak = ress_path + BACKUP_SUFFIX
        if os.path.exists(ress_bak):
            shutil.copy2(ress_bak, ress_path)
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

    data.m_Width             = new_image.width
    data.m_Height            = new_image.height
    data.m_TextureFormat     = write_format_id
    data.m_CompleteImageSize = len(enc_bytes)
    if data.m_MipMap   is not None: data.m_MipMap   = False
    if data.m_MipCount is not None: data.m_MipCount = 1

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
            f"orig_format={orig_format} encode={encode_fmt.name}. Cannot patch in-place.")

    with open(assets_path, "rb") as f:
        file_bytes = bytearray(f.read())
    file_bytes[byte_start : byte_start + original_byte_size] = new_obj_bytes
    with open(assets_path, "wb") as f:
        f.write(file_bytes)

def get_companion_files(assets_path):
    companions = []
    for suffix in [".resS", ".resource"]:
        c = assets_path + suffix
        if os.path.exists(c):
            companions.append(c)
    return companions

def ensure_backup(assets_path):
    bak = assets_path + BACKUP_SUFFIX
    if not os.path.exists(bak):
        shutil.copy2(assets_path, bak)
        for companion in get_companion_files(assets_path):
            comp_bak = companion + BACKUP_SUFFIX
            if not os.path.exists(comp_bak):
                shutil.copy2(companion, comp_bak)
        return True
    return False

def restore_backup(assets_path):
    bak = assets_path + BACKUP_SUFFIX
    if os.path.exists(bak):
        shutil.copy2(bak, assets_path)
        for companion in get_companion_files(assets_path):
            comp_bak = companion + BACKUP_SUFFIX
            if os.path.exists(comp_bak):
                shutil.copy2(comp_bak, companion)
        return True
    return False

def revert_single_texture(assets_path, path_id):
    """Patch one texture's bytes back from backup without touching other mods."""
    from UnityPy.streams.EndianBinaryWriter import EndianBinaryWriter
    from UnityPy.helpers.TypeTreeHelper import write_typetree

    backup_path = assets_path + BACKUP_SUFFIX
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"No backup found: {backup_path}")

    bak_env = UnityPy.load(backup_path)
    bak_obj = None
    for obj in bak_env.objects:
        if obj.path_id == path_id:
            bak_obj = obj; break
    if bak_obj is None:
        raise ValueError(f"path_id {path_id} not in backup — possibly added after backup was created")

    bak_data = bak_obj.read()
    writer   = EndianBinaryWriter(endian=bak_obj.reader.endian)
    node     = bak_obj._get_typetree_node()
    write_typetree(bak_data, node, writer, bak_env.file)
    orig_bytes  = writer.bytes
    orig_bsize  = bak_obj.byte_size
    orig_bstart = bak_obj.byte_start

    if len(orig_bytes) != orig_bsize:
        raise ValueError(f"Serialization mismatch: backup says {orig_bsize}b, re-serialized to {len(orig_bytes)}b")

    live_env = UnityPy.load(assets_path)
    live_obj = None
    for obj in live_env.objects:
        if obj.path_id == path_id:
            live_obj = obj; break
    if live_obj is None:
        raise ValueError(f"path_id {path_id} not found in live file")
    if live_obj.byte_size != orig_bsize:
        raise ValueError(f"Live object size {live_obj.byte_size}b != backup {orig_bsize}b — game may have updated")
    if live_obj.byte_start != orig_bstart:
        raise ValueError(f"Live byte offset changed — game may have updated")

    with open(assets_path, "rb") as f:
        file_bytes = bytearray(f.read())
    file_bytes[orig_bstart : orig_bstart + orig_bsize] = orig_bytes
    with open(assets_path, "wb") as f:
        f.write(file_bytes)

# ── Mod pack export / import ──────────────────────────────────────────────────
def export_mod_pack(mods_folder, output_zip_path):
    mods = load_mods_meta(mods_folder)
    if not mods:
        raise ValueError("No mods to export")
    portable = {}
    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for key, mod in mods.items():
            png = mod.get("png_path", "")
            if not os.path.exists(png):
                continue
            arc_name = os.path.basename(png)
            zf.write(png, arc_name)
            portable[key] = {
                "name":        mod.get("name", ""),
                "assets_file": os.path.basename(mod.get("assets_file", "")),
                "path_id":     mod.get("path_id"),
                "png_path":    arc_name,
            }
        zf.writestr(MODS_META_FILE, json.dumps(portable, indent=2))
    return len(portable)

def import_mod_pack(mods_folder, zip_path, current_game_data_path):
    os.makedirs(mods_folder, exist_ok=True)
    existing = load_mods_meta(mods_folder)
    skipped  = []
    imported = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if MODS_META_FILE not in names:
            raise ValueError("Not a valid mod pack (missing mods.json)")
        portable = json.loads(zf.read(MODS_META_FILE).decode())
        for key, mod in portable.items():
            assets_basename = mod.get("assets_file", "")
            arc_png         = mod.get("png_path", "")
            if not arc_png or arc_png not in names:
                skipped.append(assets_basename); continue
            if arc_png == MODS_META_FILE or "/" in arc_png or "\\" in arc_png:
                continue
            abs_assets = os.path.join(current_game_data_path, assets_basename)
            if not os.path.exists(abs_assets):
                skipped.append(assets_basename); continue
            dest_png = os.path.join(mods_folder, arc_png)
            with zf.open(arc_png) as src, open(dest_png, "wb") as dst:
                dst.write(src.read())
            existing[key] = {
                "name":        mod.get("name", ""),
                "assets_file": abs_assets,
                "path_id":     mod.get("path_id"),
                "png_path":    dest_png,
            }
            imported += 1
    save_mods_meta(mods_folder, existing)
    return imported, skipped

# ── App ────────────────────────────────────────────────────────────────────────
class ModManagerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x720")
        self.minsize(960, 600)

        self.cfg               = load_config()
        self.C                 = THEMES.get(self.cfg.get("theme", "dark"), THEMES["dark"])
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

        # Theming registry — populated in _build_* methods
        self._themed           = []   # list of (widget, {cfg_key: color_role})

        self._apply_styles()
        self._build_ui()

        # Auto-detect game install on first launch
        auto_detected = False
        if not self.cfg.get("game_data_path") or not os.path.isdir(self.cfg.get("game_data_path", "")):
            detected = detect_game_install()
            if detected:
                self.cfg["game_data_path"] = detected
                if not self.cfg.get("mods_folder"):
                    self.cfg["mods_folder"] = os.path.join(detected, "mods")
                save_config(self.cfg)
                auto_detected = True

        self._refresh_version_display()

        if self.cfg.get("game_data_path") and self.cfg.get("mods_folder"):
            if auto_detected:
                self.status_var.set(f"🔍 Auto-detected game at: {self.cfg['game_data_path']}")
            self.after(200, self._load_textures_async)

    # ── Theme helpers ─────────────────────────────────────────────────────────
    def _tw(self, widget, **roles):
        """Register a widget for live theme updates. Returns widget."""
        self._themed.append((widget, roles))
        return widget

    def _apply_styles(self):
        """Apply all ttk styles from current palette self.C."""
        C = self.C
        s = ttk.Style(self); s.theme_use("clam")
        # Base
        s.configure(".",
            background=C["bg"], foreground=C["text"], font=("Segoe UI", 10))
        s.configure("TFrame",        background=C["bg"])
        s.configure("Panel.TFrame",  background=C["panel"])
        # Labels
        s.configure("TLabel",
            background=C["bg"], foreground=C["text"])
        s.configure("Sub.TLabel",
            background=C["panel"], foreground=C["sub"], font=("Segoe UI", 9))
        s.configure("Header.TLabel",
            background=C["panel"], foreground=C["text"],
            font=("Segoe UI", 13, "bold"))
        s.configure("Brand.TLabel",
            background=C["panel"], foreground=C["hi"],
            font=("Segoe UI", 14, "bold"))
        s.configure("BrandSub.TLabel",
            background=C["panel"], foreground=C["sub"],
            font=("Segoe UI", 10))
        # Entry / Combobox
        s.configure("TEntry",
            fieldbackground=C["ebg"], foreground=C["text"],
            insertcolor=C["text"], bordercolor=C["accent"],
            lightcolor=C["ebg"], darkcolor=C["ebg"])
        s.configure("TScrollbar",
            background=C["accent"], troughcolor=C["ebg"], arrowcolor=C["text"])
        s.configure("TCombobox",
            fieldbackground=C["ebg"], background=C["ebg"],
            foreground=C["text"], selectbackground=C["accent"],
            selectforeground="#FFFFFF", arrowcolor=C["text"])
        s.map("TCombobox",
            fieldbackground=[("readonly", C["ebg"])],
            foreground=[("readonly", C["text"])])
        # ── Buttons ───────────────────────────────────────────────────────────
        # Primary: NBA Red — apply, import, CTA
        s.configure("Accent.TButton",
            background=C["hi"], foreground="#FFFFFF",
            font=("Segoe UI", 10, "bold"), relief="flat", padding=(8, 6))
        s.map("Accent.TButton",
            background=[("active", C["hi_active"]), ("disabled", C["dis_bg"])],
            foreground=[("disabled", C["dis_fg"])])
        # Secondary: NBA Blue — settings, export, soft actions
        s.configure("Soft.TButton",
            background=C["accent"], foreground="#FFFFFF",
            font=("Segoe UI", 10), relief="flat", padding=(8, 6))
        s.map("Soft.TButton",
            background=[("active", C["soft_active"]), ("disabled", C["dis_bg"])],
            foreground=[("disabled", C["dis_fg"])])
        # Gold button — used for active theme selection
        s.configure("Gold.TButton",
            background=C["gold"], foreground="#07142A",
            font=("Segoe UI", 10, "bold"), relief="flat", padding=(10, 5))
        s.map("Gold.TButton",
            background=[("active", C["gold"])],
            foreground=[("active", "#07142A")])
        # ── Treeview ──────────────────────────────────────────────────────────
        s.configure("Treeview",
            background=C["ebg"], foreground=C["text"],
            fieldbackground=C["ebg"], rowheight=28, font=("Segoe UI", 10))
        s.configure("Treeview.Heading",
            background=C["accent"], foreground="#FFFFFF",
            font=("Segoe UI", 10, "bold"), relief="flat",
            padding=(4, 6))
        s.map("Treeview.Heading",
            background=[("active", C["soft_active"])],
            foreground=[("active", "#FFFFFF")])
        s.map("Treeview",
            background=[("selected", C["hi"])],
            foreground=[("selected", "#FFFFFF")])
        # ── Progress bars ─────────────────────────────────────────────────────
        s.configure("Mod.Horizontal.TProgressbar",
            background=C["hi"], troughcolor=C["ebg"],
            borderwidth=0, lightcolor=C["hi"], darkcolor=C["hi"])
        s.configure("Gold.Horizontal.TProgressbar",
            background=C["gold"], troughcolor=C["ebg"],
            borderwidth=0, lightcolor=C["gold"], darkcolor=C["gold"])

    def _apply_theme_colors(self):
        """Push current palette to all registered tk widgets and key named widgets."""
        C = self.C
        self.configure(bg=C["bg"])
        for widget, roles in self._themed:
            try:
                cfg = {k: C[v] for k, v in roles.items()}
                widget.configure(**cfg)
            except Exception:
                pass
        try: self.paned.configure(bg=C["sash"])
        except Exception: pass
        try:
            # Orig = NBA blue border (unmodified), Mod = NBA red border (your replacement)
            self.orig_canvas.configure(bg=C["ebg"], highlightbackground=C["border"])
            self.mod_canvas.configure(bg=C["ebg"],  highlightbackground=C["mod_border"])
        except Exception: pass
        try:
            self.bottom_bar.configure(bg=C["panel"])
            self.status_label.configure(bg=C["panel"], fg=C["text"])
            self.version_label.configure(bg=C["panel"], fg=C["sub"])
        except Exception: pass
        try: self._sep_stripe.configure(bg=C["sep"])
        except Exception: pass
        try:
            self.tree.tag_configure("modded",      foreground=C["gold"])
            self.tree.tag_configure("odd_row",     background=C["row_odd"])
            self.tree.tag_configure("even_row",    background=C["ebg"])
            self.tree.tag_configure("modded_odd",  background=C["row_odd"], foreground=C["gold"])
            self.tree.tag_configure("modded_even", background=C["ebg"],     foreground=C["gold"])
        except Exception: pass

    def _switch_theme(self, mode):
        """Switch between 'dark' and 'light' and persist choice."""
        if mode not in THEMES: return
        self.cfg["theme"] = mode
        save_config(self.cfg)
        self.C = THEMES[mode]
        self._apply_styles()
        self._apply_theme_colors()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        C = self.C
        # Top bar
        top = ttk.Frame(self, style="Panel.TFrame")
        top.pack(fill="x")
        self._tw(top)

        # Brand area — ball icon + "NBA BOUNCE" in red + subtitle muted
        brand_frame = ttk.Frame(top, style="Panel.TFrame")
        brand_frame.pack(side="left", padx=16, pady=9)
        ball_lbl = tk.Label(brand_frame, text="🏀", bg=C["panel"],
                            font=("Segoe UI", 18))
        ball_lbl.pack(side="left", padx=(0, 6))
        self._tw(ball_lbl, bg="panel")
        title_col = ttk.Frame(brand_frame, style="Panel.TFrame")
        title_col.pack(side="left")
        ttk.Label(title_col, text="NBA BOUNCE",
                  style="Brand.TLabel").pack(anchor="w")
        ttk.Label(title_col, text="Texture Mod Manager",
                  style="BrandSub.TLabel").pack(anchor="w")

        # Top bar buttons (right to left)
        ttk.Button(top, text="⚙ Settings",         style="Soft.TButton",
                   command=self._open_settings).pack(side="right", padx=(4, 10), pady=8)
        ttk.Button(top, text="📥 Import Pack",      style="Soft.TButton",
                   command=self._import_pack).pack(side="right", padx=4, pady=8)
        ttk.Button(top, text="📦 Export Pack",      style="Soft.TButton",
                   command=self._export_pack).pack(side="right", padx=4, pady=8)
        ttk.Button(top, text="♻ Restore Originals", style="Soft.TButton",
                   command=self._restore_all).pack(side="right", padx=4, pady=8)
        ttk.Button(top, text="▶ Apply All Mods",    style="Accent.TButton",
                   command=self._apply_all_mods).pack(side="right", padx=(4, 0), pady=8)

        # Thin separator stripe — NBA red line under the top bar
        self._sep_stripe = tk.Frame(self, bg=C["sep"], height=3)
        self._sep_stripe.pack(fill="x")

        # Bottom status bar
        self.bottom_bar = tk.Frame(self, bg=C["panel"])
        self.bottom_bar.pack(fill="x", side="bottom")

        self.status_var   = tk.StringVar(value="Configure your game path in ⚙ Settings to begin.")
        self.status_label = tk.Label(self.bottom_bar, textvariable=self.status_var,
                                     bg=C["panel"], fg=C["text"],
                                     font=("Segoe UI", 9), anchor="w", padx=10, pady=3)
        self.status_label.pack(side="left", fill="x", expand=True)

        self.version_var   = tk.StringVar(value="")
        self.version_label = tk.Label(self.bottom_bar, textvariable=self.version_var,
                                      bg=C["panel"], fg=C["sub"],
                                      font=("Segoe UI", 9), anchor="e", padx=10, pady=3)
        self.version_label.pack(side="right")

        # Main split pane
        self.paned = tk.PanedWindow(self, orient=tk.HORIZONTAL,
                                    sashwidth=6, sashrelief="flat",
                                    bg=C["sash"], borderwidth=0)
        self.paned.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_left_panel(self.paned)
        self._build_right_panel(self.paned)

    def _build_left_panel(self, parent):
        C = self.C
        frame = ttk.Frame(parent, style="Panel.TFrame")
        parent.add(frame, minsize=280, width=420, stretch="always")
        frame.rowconfigure(1, weight=1); frame.columnconfigure(0, weight=1)

        # Search row
        sf = ttk.Frame(frame, style="Panel.TFrame")
        sf.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        sf.columnconfigure(1, weight=1)

        icon_lbl = tk.Label(sf, text="🔍", bg=C["panel"], fg=C["sub"],
                            font=("Segoe UI", 11))
        icon_lbl.grid(row=0, column=0, padx=(0, 4))
        self._tw(icon_lbl, bg="panel", fg="sub")

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._on_search)
        ttk.Entry(sf, textvariable=self.search_var).grid(row=0, column=1, sticky="ew")

        # Treeview
        tf = ttk.Frame(frame, style="Panel.TFrame")
        tf.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        tf.rowconfigure(0, weight=1); tf.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(tf, columns=("name", "size", "mod", "file"),
                                 show="headings", selectmode="extended")
        self.tree.heading("name", text="Texture Name", command=lambda: self._sort_by("name"))
        self.tree.heading("size", text="Size",         command=lambda: self._sort_by("size"))
        self.tree.heading("mod",  text="Mod",          command=lambda: self._sort_by("mod"))
        self.tree.heading("file", text="File",         command=lambda: self._sort_by("file"))
        self.tree.column("name", width=155)
        self.tree.column("size", width=80,  anchor="center")
        self.tree.column("mod",  width=40,  anchor="center")
        self.tree.column("file", width=115, anchor="w")
        self.tree.tag_configure("modded", foreground=C["gold"])
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_texture_select)

        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vsb.set)

        # Filter row
        ff = ttk.Frame(frame, style="Panel.TFrame")
        ff.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))

        filter_lbl = tk.Label(ff, text="Filter by file:", bg=C["panel"], fg=C["sub"],
                              font=("Segoe UI", 9))
        filter_lbl.pack(side="left")
        self._tw(filter_lbl, bg="panel", fg="sub")

        self.file_filter_var = tk.StringVar(value="All")
        self.file_filter_cb  = ttk.Combobox(ff, textvariable=self.file_filter_var,
                                             state="readonly", width=22)
        self.file_filter_cb.pack(side="left", padx=6)
        self.file_filter_cb.bind("<<ComboboxSelected>>", self._on_search)

        multi_lbl = tk.Label(ff, text="(Ctrl+click for multi)", bg=C["panel"], fg=C["sub"],
                             font=("Segoe UI", 8))
        multi_lbl.pack(side="right")
        self._tw(multi_lbl, bg="panel", fg="sub")

    def _build_right_panel(self, parent):
        C = self.C
        frame = ttk.Frame(parent, style="Panel.TFrame")
        parent.add(frame, minsize=320, stretch="always")
        frame.columnconfigure(0, weight=1); frame.columnconfigure(1, weight=1)
        frame.rowconfigure(1, weight=1)

        # Column headers
        # "Original" in NBA blue — matches blue canvas border
        orig_lbl = tk.Label(frame, text="Original", bg=C["panel"], fg=C["accent"],
                            font=("Segoe UI", 11, "bold"))
        orig_lbl.grid(row=0, column=0, pady=(10, 4))
        self._tw(orig_lbl, bg="panel", fg="accent")

        # "Your Mod" in NBA red — matches red canvas border
        mod_lbl = tk.Label(frame, text="Your Mod", bg=C["panel"], fg=C["hi"],
                           font=("Segoe UI", 11, "bold"))
        mod_lbl.grid(row=0, column=1, pady=(10, 4))
        self._tw(mod_lbl, bg="panel", fg="hi")

        # Preview canvases
        self.orig_canvas = tk.Canvas(frame, bg=C["ebg"], width=320, height=320,
                                     highlightthickness=2,
                                     highlightbackground=C["border"])
        self.orig_canvas.grid(row=1, column=0, padx=12, pady=4, sticky="nsew")

        self.mod_canvas = tk.Canvas(frame, bg=C["ebg"], width=320, height=320,
                                    highlightthickness=2,
                                    highlightbackground=C["mod_border"])
        self.mod_canvas.grid(row=1, column=1, padx=12, pady=4, sticky="nsew")

        self.orig_canvas.bind("<Configure>", lambda e: self._redraw_previews())
        self.mod_canvas.bind("<Configure>",  lambda e: self._redraw_previews())

        # Info bar
        self.info_var = tk.StringVar(value="Select a texture from the list.")
        info_lbl = tk.Label(frame, textvariable=self.info_var,
                            bg=C["panel"], fg=C["sub"],
                            font=("Segoe UI", 9))
        info_lbl.grid(row=2, column=0, columnspan=2, pady=4)
        self._tw(info_lbl, bg="panel", fg="sub")

        # Action buttons
        bf = ttk.Frame(frame, style="Panel.TFrame")
        bf.grid(row=3, column=0, columnspan=2, pady=(4, 12))
        ttk.Button(bf, text="⬇ Export Original",    style="Soft.TButton",
                   command=self._export_original).pack(side="left", padx=3)
        ttk.Button(bf, text="📦 Batch Export",       style="Soft.TButton",
                   command=self._batch_export).pack(side="left", padx=3)
        ttk.Button(bf, text="⬆ Import Replacement", style="Accent.TButton",
                   command=self._import_replacement).pack(side="left", padx=3)
        ttk.Button(bf, text="↶ Revert in Game",     style="Soft.TButton",
                   command=self._revert_in_game).pack(side="left", padx=3)
        ttk.Button(bf, text="✕ Remove Mod",          style="Soft.TButton",
                   command=self._remove_mod).pack(side="left", padx=3)

    # ── Version display ───────────────────────────────────────────────────────
    def _refresh_version_display(self):
        gp = self.cfg.get("game_data_path", "")
        if not gp or not os.path.isdir(gp):
            self.version_var.set(""); return
        def worker():
            info  = get_game_info(gp)
            unity = info.get("unity", "unknown")
            app   = info.get("app",   "unknown")
            parts = []
            if app   != "unknown": parts.append(f"Game v{app}")
            if unity != "unknown": parts.append(f"Unity {unity}")
            txt = "   ·   ".join(parts) if parts else "Version unknown"
            self.after(0, lambda: self.version_var.set(txt))
        threading.Thread(target=worker, daemon=True).start()

    # ── Settings ──────────────────────────────────────────────────────────────
    def _open_settings(self):
        C   = self.C
        dlg = tk.Toplevel(self)
        dlg.title("Settings")
        dlg.geometry("660x400")
        dlg.configure(bg=C["bg"])
        dlg.grab_set()

        def lbl(text, r, c=0, span=1, style="TLabel"):
            w = ttk.Label(dlg, text=text, style=style)
            w.grid(row=r, column=c, columnspan=span, sticky="w", padx=12, pady=6)
            return w

        def row(label, var, fn, r):
            lbl(label, r)
            ttk.Entry(dlg, textvariable=var, width=50).grid(
                row=r, column=1, padx=4, pady=6)
            ttk.Button(dlg, text="Browse", style="Soft.TButton",
                       command=fn).grid(row=r, column=2, padx=8)

        gv = tk.StringVar(value=self.cfg.get("game_data_path", ""))
        mv = tk.StringVar(value=self.cfg.get("mods_folder",    ""))
        row("Game Data Folder:", gv,
            lambda: gv.set(filedialog.askdirectory(
                title="Select NBA Bounce_Data folder") or gv.get()), 0)
        row("Mods Folder:", mv,
            lambda: mv.set(filedialog.askdirectory(
                title="Select your Mods folder") or mv.get()), 1)

        # Auto-detect button
        def auto_detect():
            self.status_var.set("Searching for NBA Bounce install...")
            dlg.update_idletasks()
            detected = detect_game_install()
            if detected:
                gv.set(detected)
                if not mv.get():
                    mv.set(os.path.join(detected, "mods"))
                messagebox.showinfo("Found",
                    f"Found NBA Bounce at:\n\n{detected}", parent=dlg)
            else:
                messagebox.showinfo("Not found",
                    "Could not auto-detect NBA Bounce.\n\n"
                    "Make sure the game is installed via Steam, "
                    "or browse for the folder manually.", parent=dlg)

        ttk.Button(dlg, text="🔍 Auto-Detect Game", style="Soft.TButton",
                   command=auto_detect).grid(row=2, column=1, sticky="w", padx=4, pady=6)

        # Tip label
        lbl("Tip: Game Data folder should end in 'NBA Bounce_Data'", 3,
            span=3, style="Sub.TLabel")

        # ── Theme toggle ──────────────────────────────────────────────────────
        sep_line = tk.Frame(dlg, bg=C["sep"], height=2)
        sep_line.grid(row=4, column=0, columnspan=3, sticky="ew", padx=12, pady=(12, 6))

        theme_header = ttk.Label(dlg, text="Appearance", style="Header.TLabel")
        theme_header.grid(row=5, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 4))

        theme_frame = ttk.Frame(dlg)
        theme_frame.grid(row=6, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 4))

        cur = self.cfg.get("theme", "dark")

        # Preview swatch — small colored rectangle showing the selected palette
        swatch = tk.Canvas(theme_frame, width=18, height=18, bd=0,
                           highlightthickness=0)
        swatch.grid(row=0, column=2, padx=(16, 0))

        def _draw_swatch(mode):
            t = THEMES[mode]
            swatch.configure(bg=t["panel"])
            swatch.delete("all")
            swatch.create_rectangle(0, 0, 9, 18, fill=t["accent"], outline="")
            swatch.create_rectangle(9, 0, 18, 18, fill=t["hi"], outline="")

        def make_btn(mode, label, col):
            style = "Gold.TButton" if mode == cur else "Soft.TButton"
            btn = ttk.Button(theme_frame, text=label, style=style,
                             command=lambda m=mode: apply_theme(m))
            btn.grid(row=0, column=col, padx=(0, 6))
            return btn

        dark_btn  = make_btn("dark",  "🌙  Dark Mode",  0)
        light_btn = make_btn("light", "☀️   Light Mode", 1)
        _draw_swatch(cur)

        def apply_theme(mode):
            nonlocal cur
            cur = mode
            dark_btn.configure(
                style="Gold.TButton" if mode == "dark"  else "Soft.TButton")
            light_btn.configure(
                style="Gold.TButton" if mode == "light" else "Soft.TButton")
            _draw_swatch(mode)
            self._switch_theme(mode)
            dlg.configure(bg=self.C["bg"])
            sep_line.configure(bg=self.C["sep"])
            theme_header.configure(background=self.C["bg"])

        # Save button
        def save():
            self.cfg["game_data_path"] = gv.get()
            self.cfg["mods_folder"]    = mv.get()
            self.cfg["theme"]          = cur
            save_config(self.cfg)
            os.makedirs(self.cfg["mods_folder"], exist_ok=True)
            self.mods_meta = load_mods_meta(self.cfg["mods_folder"])
            self._refresh_version_display()
            dlg.destroy()
            self._load_textures_async()

        ttk.Button(dlg, text="Save & Reload", style="Accent.TButton",
                   command=save).grid(row=7, column=0, columnspan=3, pady=16)

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
                self.after(0, lambda: self.status_var.set(
                    "❌ Game data folder not found. Check Settings."))
                return
            os.makedirs(mods_folder, exist_ok=True)
            self.mods_meta = load_mods_meta(mods_folder)
            asset_files    = find_assets_files(game_path)
            if not asset_files:
                self.after(0, lambda: self.status_var.set(
                    "❌ No asset files found in the game data folder."))
                return
            all_textures = []
            for i, af in enumerate(asset_files):
                fname = os.path.basename(af)
                self.after(0, lambda f=fname, n=i+1, t=len(asset_files):
                           self.status_var.set(f"Scanning {f}  ({n}/{t})…"))
                try:
                    all_textures.extend(extract_textures_from_file(af))
                except Exception as e:
                    print(f"Warning: could not read {af}: {e}")
            self.all_textures = all_textures
            self.after(0, self._on_textures_loaded)
        except Exception as e:
            self.after(0, lambda: self.status_var.set(f"❌ Error: {e}"))
        finally:
            self._loading = False

    def _on_textures_loaded(self):
        file_names = sorted(set(os.path.basename(t["assets_file"])
                                for t in self.all_textures))
        self.file_filter_cb["values"] = ["All"] + file_names
        self.file_filter_var.set("All")
        self._populate_tree(self.all_textures)
        n       = len(find_assets_files(self.cfg["game_data_path"]))
        n_mods  = len(self.mods_meta)
        mod_msg = f"  ·  {n_mods} mod(s) configured" if n_mods else ""
        self.status_var.set(
            f"✅ Loaded {len(self.all_textures)} textures from {n} asset files.{mod_msg}")

    # ── Sort ──────────────────────────────────────────────────────────────────
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
        for i, t in enumerate(self.filtered_textures):
            is_mod   = self._mod_key(t) in self.mods_meta
            mod      = "✔" if is_mod else ""
            sz       = f"{t['width']}x{t['height']}" if t["width"] else "?"
            fname    = os.path.basename(t["assets_file"])
            row_tag  = "even_row" if i % 2 == 0 else "odd_row"
            if is_mod:
                tags = ("modded_even" if i % 2 == 0 else "modded_odd", row_tag)
            else:
                tags = (row_tag,)
            self.tree.insert("", "end", values=(t["name"], sz, mod, fname), tags=tags)

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
    def _selected_textures_list(self):
        sel = self.tree.selection()
        out = []
        for item in sel:
            try:
                idx = self.tree.index(item)
                if idx < len(self.filtered_textures):
                    out.append(self.filtered_textures[idx])
            except Exception:
                continue
        return out

    def _on_texture_select(self, _=None):
        C   = self.C
        sel = self.tree.selection()
        if not sel: return
        focused = self.tree.focus() or sel[-1]
        try:
            idx = self.tree.index(focused)
        except Exception:
            return
        if idx >= len(self.filtered_textures): return
        self.selected_texture = t = self.filtered_textures[idx]
        sz    = f"{t['width']}×{t['height']}" if t["width"] else "unknown size"
        extra = f"   ({len(sel)} selected)" if len(sel) > 1 else ""
        self.info_var.set(
            f"{t['name']}  |  {sz}  |  {t['format']}  |  "
            f"{os.path.basename(t['assets_file'])}{extra}")

        self._orig_img = None
        self.orig_canvas.delete("all")
        self.orig_canvas.create_text(
            160, 160, text="Loading…", fill=C["sub"], font=("Segoe UI", 10))
        threading.Thread(
            target=self._load_orig_preview, args=(t,), daemon=True).start()

        key = self._mod_key(t)
        if key in self.mods_meta:
            png = self.mods_meta[key]["png_path"]
            try:
                self._mod_img = (Image.open(png).convert("RGBA")
                                 if os.path.exists(png) else None)
            except Exception:
                self._mod_img = None
        else:
            self._mod_img = None
            self.mod_canvas.delete("all")
            self.mod_canvas.create_text(
                160, 160, text="No mod set", fill=C["sub"], font=("Segoe UI", 10))
        self._redraw_previews()

    def _load_orig_preview(self, t):
        try:
            img = get_texture_image(t["assets_file"], t["path_id"])
            if img:
                self._orig_img = img.convert("RGBA")
                self.after(0, self._redraw_previews)
        except Exception as e:
            self.after(0, lambda: (
                self.orig_canvas.delete("all"),
                self.orig_canvas.create_text(
                    160, 160, text=f"Preview error:\n{e}",
                    fill=self.C["hi"], font=("Segoe UI", 9))
            ))

    def _fit_image(self, img, canvas):
        cw = canvas.winfo_width()  or 320
        ch = canvas.winfo_height() or 320
        iw, ih = img.size
        scale  = min(cw / iw, ch / ih, 1.0)
        return img.resize(
            (max(1, int(iw * scale)), max(1, int(ih * scale))), Image.LANCZOS)

    def _redraw_previews(self):
        if self._orig_img:
            f = self._fit_image(self._orig_img, self.orig_canvas)
            self._orig_photo = ImageTk.PhotoImage(f)
            cw, ch = (self.orig_canvas.winfo_width()  or 320,
                      self.orig_canvas.winfo_height() or 320)
            self.orig_canvas.delete("all")
            self.orig_canvas.create_image(cw // 2, ch // 2,
                                          image=self._orig_photo, anchor="center")
        if self._mod_img:
            f = self._fit_image(self._mod_img, self.mod_canvas)
            self._mod_photo = ImageTk.PhotoImage(f)
            cw, ch = (self.mod_canvas.winfo_width()  or 320,
                      self.mod_canvas.winfo_height() or 320)
            self.mod_canvas.delete("all")
            self.mod_canvas.create_image(cw // 2, ch // 2,
                                         image=self._mod_photo, anchor="center")

    # ── Actions ───────────────────────────────────────────────────────────────
    def _export_original(self):
        if not self.selected_texture:
            messagebox.showinfo("No selection", "Select a texture first."); return
        t    = self.selected_texture
        path = filedialog.asksaveasfilename(
            defaultextension=".png", filetypes=[("PNG", "*.png")],
            initialfile=t["name"] + ".png")
        if not path: return
        try:
            img = get_texture_image(t["assets_file"], t["path_id"])
            img.save(path)
            self.status_var.set(f"✅ Exported {t['name']} to {path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    def _batch_export(self):
        textures = self._selected_textures_list()
        if not textures:
            messagebox.showinfo("No selection",
                "Select one or more textures first.\n\n"
                "Use Ctrl+click or Shift+click to select multiple.")
            return
        if len(textures) == 1:
            self._export_original(); return

        path = filedialog.asksaveasfilename(
            defaultextension=".zip", filetypes=[("ZIP archive", "*.zip")],
            initialfile=f"NBA_Bounce_textures_{len(textures)}.zip",
            title="Save batch export as ZIP")
        if not path: return

        dlg = self._show_progress_dialog("Exporting Textures", len(textures))

        def worker():
            errors, written = [], 0
            try:
                with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                    used_names = set()
                    for i, t in enumerate(textures):
                        name = t["name"]
                        self.after(0, lambda n=name, idx=i:
                                   self._update_progress(
                                       dlg, idx, len(textures), f"Exporting: {n}"))
                        try:
                            img = get_texture_image(t["assets_file"], t["path_id"])
                            if img is None:
                                errors.append(f"{name}: could not read image"); continue
                            base = (f"{os.path.basename(t['assets_file'])}"
                                    f"__{t['path_id']}__{name}.png")
                            safe, n_dup = base, 1
                            while safe in used_names:
                                safe = f"{os.path.splitext(base)[0]}_{n_dup}.png"
                                n_dup += 1
                            used_names.add(safe)
                            buf = BytesIO()
                            img.save(buf, format="PNG")
                            zf.writestr(safe, buf.getvalue())
                            written += 1
                        except Exception as e:
                            errors.append(f"{name}: {e}")
            except Exception as e:
                errors.append(f"ZIP write error: {e}")
            self.after(0, lambda: self._finish_progress_dialog(dlg))
            def done():
                self.status_var.set(
                    f"✅ Exported {written}/{len(textures)} textures "
                    f"to {os.path.basename(path)}")
                if errors:
                    messagebox.showwarning("Export complete (with errors)",
                        f"✅ Exported {written}/{len(textures)} textures.\n\n"
                        f"⚠ {len(errors)} error(s):\n" + "\n".join(errors[:10]))
            self.after(100, done)

        threading.Thread(target=worker, daemon=True).start()

    def _import_replacement(self):
        if not self.selected_texture:
            messagebox.showinfo("No selection", "Select a texture first."); return
        if not self.cfg.get("mods_folder"):
            messagebox.showwarning("No mods folder",
                "Set your mods folder in Settings first."); return
        t    = self.selected_texture
        path = filedialog.askopenfilename(
            filetypes=[("PNG", "*.png")], title="Select replacement PNG")
        if not path: return
        folder    = self.cfg["mods_folder"]
        dest_name = (f"{os.path.basename(t['assets_file'])}"
                     f"__{t['path_id']}__{t['name']}.png")
        dest = os.path.join(folder, dest_name)
        shutil.copy2(path, dest)
        key = self._mod_key(t)
        self.mods_meta[key] = {
            "name": t["name"], "assets_file": t["assets_file"],
            "path_id": t["path_id"], "png_path": dest}
        save_mods_meta(folder, self.mods_meta)
        try:   self._mod_img = Image.open(dest).convert("RGBA")
        except: self._mod_img = None
        self._redraw_previews(); self._refresh_tree_rows()
        self.status_var.set(
            f"✅ Mod saved for '{t['name']}'. Click '▶ Apply All Mods' to write to game.")

    def _remove_mod(self):
        if not self.selected_texture: return
        t   = self.selected_texture
        key = self._mod_key(t)
        if key not in self.mods_meta:
            messagebox.showinfo("No mod", "No mod is set for this texture."); return
        if not messagebox.askyesno("Remove Mod",
            f"Remove the mod for '{t['name']}'?\n\n"
            "This deletes your replacement PNG and the mod's settings.\n\n"
            "If the mod is currently applied in-game, click '↶ Revert in Game' first "
            "to restore the original texture."):
            return
        mod = self.mods_meta.pop(key)
        if os.path.exists(mod["png_path"]): os.remove(mod["png_path"])
        save_mods_meta(self.cfg["mods_folder"], self.mods_meta)
        self._mod_img = None
        self.mod_canvas.delete("all")
        self.mod_canvas.create_text(
            160, 160, text="No mod set", fill=self.C["sub"], font=("Segoe UI", 10))
        self._refresh_tree_rows()
        self.status_var.set(f"✅ Mod removed for '{t['name']}'.")

    def _revert_in_game(self):
        if not self.selected_texture:
            messagebox.showinfo("No selection", "Select a texture first."); return
        t      = self.selected_texture
        backup = t["assets_file"] + BACKUP_SUFFIX
        if not os.path.exists(backup):
            messagebox.showinfo("No backup",
                "No backup file exists for this texture's source.\n\n"
                "Apply a mod at least once to create a backup."); return
        if not messagebox.askyesno("Revert in Game",
            f"Restore the original '{t['name']}' in the game?\n\n"
            "• Only this texture goes back to original — other mods are NOT affected.\n"
            "• Your mod PNG and settings stay saved.\n"
            "• Re-apply anytime with '▶ Apply All Mods'.\n\nContinue?"):
            return
        try:
            revert_single_texture(t["assets_file"], t["path_id"])
            self.status_var.set(
                f"✅ Reverted '{t['name']}' in game. "
                f"Mod settings preserved — click '▶ Apply All Mods' to put it back.")
        except Exception as e:
            messagebox.showerror("Revert failed", str(e))

    def _refresh_tree_rows(self):
        sel = self.tree.selection()
        if not sel: return
        for item in sel:
            try:
                idx = self.tree.index(item)
                if idx >= len(self.filtered_textures): continue
                t        = self.filtered_textures[idx]
                sz       = f"{t['width']}×{t['height']}" if t["width"] else "?"
                fname    = os.path.basename(t["assets_file"])
                is_mod   = self._mod_key(t) in self.mods_meta
                mod      = "✔" if is_mod else ""
                row_tag  = "even_row" if idx % 2 == 0 else "odd_row"
                if is_mod:
                    tags = ("modded_even" if idx % 2 == 0 else "modded_odd", row_tag)
                else:
                    tags = (row_tag,)
                self.tree.item(item, values=(t["name"], sz, mod, fname), tags=tags)
            except Exception:
                continue

    # ── Apply / Restore (with progress dialog) ────────────────────────────────
    def _apply_all_mods(self):
        if not self.mods_meta:
            messagebox.showinfo("No mods", "You haven't added any texture mods yet."); return
        count = len(self.mods_meta)
        if not messagebox.askyesno("Apply Mods",
            f"This will write {count} texture replacement(s) to your game files.\n\n"
            "Original files will be backed up automatically (once).\n\nContinue?"):
            return

        items   = list(self.mods_meta.items())
        dlg     = self._show_progress_dialog("Applying Mods", count)
        results = {"applied": 0, "errors": [], "backed_up": []}

        def worker():
            for i, (key, mod) in enumerate(items):
                name = mod.get("name", f"mod_{i}")
                self.after(0, lambda n=name, idx=i:
                           self._update_progress(dlg, idx, count, f"Applying: {n}"))
                af, png, pid = mod["assets_file"], mod["png_path"], mod["path_id"]
                if not os.path.exists(af):
                    results["errors"].append(f"File not found: {af}"); continue
                if not os.path.exists(png):
                    results["errors"].append(f"Mod PNG missing: {png}"); continue
                if ensure_backup(af):
                    results["backed_up"].append(os.path.basename(af))
                try:
                    apply_single_mod(af, pid, png)
                    results["applied"] += 1
                except Exception as e:
                    results["errors"].append(f"{name}: {e}")
            self.after(0, lambda: self._finish_progress_dialog(dlg))
            def done():
                msg = f"✅ Applied {results['applied']}/{count} mods."
                if results["backed_up"]:
                    msg += f"  Backed up: {', '.join(results['backed_up'])}."
                if results["errors"]:
                    msg += f"  ⚠ {len(results['errors'])} error(s)."
                    messagebox.showwarning("Some mods failed",
                        "\n".join(results["errors"]))
                self.status_var.set(msg)
            self.after(100, done)

        threading.Thread(target=worker, daemon=True).start()

    def _restore_all(self):
        gp = self.cfg.get("game_data_path", "")
        if not gp:
            messagebox.showwarning("No game path",
                "Set your game data folder in Settings first."); return
        restorable = [f for f in find_assets_files(gp)
                      if os.path.exists(f + BACKUP_SUFFIX)]
        if not restorable:
            messagebox.showinfo("No backups",
                "No backup files found. Have you applied any mods?"); return
        if not messagebox.askyesno("Restore Originals",
            f"This will restore {len(restorable)} original file(s).\n"
            "Your mod PNGs are NOT deleted.\n\nContinue?"):
            return
        restored = sum(1 for f in restorable if restore_backup(f))
        self.status_var.set(
            f"✅ Restored {restored} file(s). Your mods are still saved — re-apply anytime.")

    # ── Mod packs ─────────────────────────────────────────────────────────────
    def _export_pack(self):
        if not self.mods_meta:
            messagebox.showinfo("No mods", "You haven't added any texture mods yet."); return
        if not self.cfg.get("mods_folder"):
            messagebox.showwarning("No mods folder",
                "Set your mods folder in Settings first."); return
        path = filedialog.asksaveasfilename(
            defaultextension=".zip", filetypes=[("Mod pack ZIP", "*.zip")],
            initialfile="NBA_Bounce_modpack.zip", title="Save mod pack as ZIP")
        if not path: return
        try:
            n = export_mod_pack(self.cfg["mods_folder"], path)
            self.status_var.set(
                f"✅ Exported mod pack with {n} texture(s) to {os.path.basename(path)}")
            messagebox.showinfo("Mod pack exported",
                f"Mod pack saved with {n} texture(s).\n\n"
                "Share the .zip with anyone — they import it via '📥 Import Pack'.")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    def _import_pack(self):
        if not self.cfg.get("mods_folder"):
            messagebox.showwarning("No mods folder",
                "Set your mods folder in Settings first."); return
        if not self.cfg.get("game_data_path"):
            messagebox.showwarning("No game path",
                "Set your game data folder in Settings first."); return
        path = filedialog.askopenfilename(
            filetypes=[("Mod pack ZIP", "*.zip")],
            title="Select a mod pack to import")
        if not path: return
        if not messagebox.askyesno("Import Mod Pack",
            "Importing will:\n"
            "• Add the pack's mods to your collection\n"
            "• Overwrite any existing mod for the same texture\n"
            "• NOT apply mods to the game (use '▶ Apply All Mods' for that)\n\n"
            "Continue?"):
            return
        try:
            n_imported, skipped = import_mod_pack(
                self.cfg["mods_folder"], path, self.cfg["game_data_path"])
            self.mods_meta = load_mods_meta(self.cfg["mods_folder"])
            self._populate_tree(self.filtered_textures or self.all_textures)
            msg = f"✅ Imported {n_imported} mod(s)."
            if skipped:
                msg += f"  ⚠ Skipped {len(skipped)} for missing files."
            self.status_var.set(msg)
            info_msg = (f"Imported {n_imported} texture mod(s).\n\n"
                        "Click '▶ Apply All Mods' to write them into the game.")
            if skipped:
                info_msg += (
                    f"\n\n⚠ Skipped {len(skipped)} entries — their .assets file "
                    f"isn't in this install:\n" +
                    "\n".join(f"  • {s}" for s in skipped[:8]))
                if len(skipped) > 8:
                    info_msg += f"\n  ...and {len(skipped)-8} more"
            messagebox.showinfo("Mod pack imported", info_msg)
        except Exception as e:
            messagebox.showerror("Import failed", str(e))

    # ── Progress dialog helpers ───────────────────────────────────────────────
    def _show_progress_dialog(self, title, total):
        C   = self.C
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.geometry("480x180")
        dlg.configure(bg=C["bg"])
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width()  - 480) // 2
        y = self.winfo_y() + (self.winfo_height() - 180) // 2
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        try: dlg.grab_set()
        except Exception: pass

        # Top accent stripe — NBA red
        dlg._bar = tk.Frame(dlg, bg=C["hi"], height=4)
        dlg._bar.pack(fill="x")

        # Title label inside dialog
        tk.Label(dlg, text=title, bg=C["bg"], fg=C["accent"],
                 font=("Segoe UI", 11, "bold"), anchor="w", padx=20).pack(
                 fill="x", pady=(10, 2))

        status = tk.Label(dlg, text="Preparing...", bg=C["bg"], fg=C["text"],
                          font=("Segoe UI", 9), anchor="w", padx=20)
        status.pack(fill="x", pady=(0, 6))

        pb = ttk.Progressbar(dlg, length=440, mode="determinate",
                             maximum=max(total, 1),
                             style="Mod.Horizontal.TProgressbar")
        pb.pack(padx=20, pady=2)

        count_lbl = tk.Label(dlg, text=f"0 / {total}", bg=C["bg"], fg=C["sub"],
                             font=("Segoe UI", 9))
        count_lbl.pack(pady=8)

        dlg._status = status
        dlg._pb     = pb
        dlg._count  = count_lbl
        dlg._total  = total
        return dlg

    def _update_progress(self, dlg, current, total, text):
        try:
            dlg._status.config(text=text)
            dlg._pb.config(value=current)
            dlg._count.config(text=f"{current} / {total}")
        except Exception:
            pass

    def _finish_progress_dialog(self, dlg):
        try:
            C = self.C
            dlg._bar.configure(bg=C["gold"])           # stripe turns gold on complete
            dlg._pb.configure(style="Gold.Horizontal.TProgressbar")
            dlg._pb.config(value=dlg._total)
            dlg._status.config(text="Done!", fg=C["gold"])
            dlg._count.config(text=f"{dlg._total} / {dlg._total}")
            dlg.after(700, dlg.destroy)
        except Exception:
            try: dlg.destroy()
            except Exception: pass


if __name__ == "__main__":
    app = ModManagerApp()
    app.mainloop()
