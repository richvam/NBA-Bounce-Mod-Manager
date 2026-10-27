"""
ratings_manager.py -- logic for the Ratings tab (no tkinter, no app.py imports).

Reads and writes <game>/BepInEx/config/BounceRatings.csv, the file the
BounceRatings BepInEx plugin reads at every match, and installs / removes the
BepInEx loader + plugin. Never touches any other game file and never calls
apply_single_mod().
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import zipfile

import app_paths

COLS = ["speed", "shooting", "dunk", "pass", "steal", "block", "power"]
HEADER = ["avatar_id", "team", "number", "type"] + COLS
NCOL = len(HEADER)
DEFAULT_RATING = 5

LOADER_DIR = os.path.join(app_paths.APP_DIR, "loader")
ZIP_NAME = "BepInEx_win_x64_5.4.23.3.zip"
PLUGIN_NAME = "BounceRatings.dll"
CSV_NAME = "BounceRatings.csv"
BACKUP_DIR = app_paths.user("ratings_backup")

# Everything the loader puts in the game root. Nothing else is ever removed.
ROOT_FILES = ["winhttp.dll", "doorstop_config.ini", ".doorstop_version", "changelog.txt"]
ROOT_DIR = "BepInEx"


def game_root(game_data_path: str) -> str:
    """Folder holding the game exe: the parent of '<name>_Data'."""
    p = (game_data_path or "").rstrip("/\\")
    return os.path.dirname(p) if p else ""


def clamp(v) -> int:
    try:
        return max(1, min(10, int(float(str(v).strip()))))
    except (ValueError, TypeError):
        return DEFAULT_RATING


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


# --------------------------------------------------------------------- loader
class Loader:
    def __init__(self, root: str):
        self.root = root
        self.bep = os.path.join(root, ROOT_DIR)
        self.plugin = os.path.join(self.bep, "plugins", PLUGIN_NAME)
        self.csv = os.path.join(self.bep, "config", CSV_NAME)
        self.ini = os.path.join(root, "doorstop_config.ini")
        self.zip = os.path.join(LOADER_DIR, ZIP_NAME)
        self.dll = os.path.join(LOADER_DIR, PLUGIN_NAME)

    def valid(self) -> bool:
        return bool(self.root) and os.path.isdir(self.root)

    def bepinex_installed(self) -> bool:
        return (os.path.isfile(os.path.join(self.root, "winhttp.dll"))
                and os.path.isfile(os.path.join(self.bep, "core", "BepInEx.dll")))

    def plugin_state(self) -> str:
        """'installed', 'outdated' or 'missing'."""
        if not os.path.isfile(self.plugin):
            return "missing"
        try:
            if os.path.isfile(self.dll) and _sha(self.plugin) != _sha(self.dll):
                return "outdated"
        except OSError:
            pass
        return "installed"

    def enabled(self) -> bool:
        """True unless doorstop_config.ini says enabled = false."""
        for line in self._ini_lines():
            k, _, v = line.partition("=")
            if k.strip().lower() == "enabled":
                return v.strip().lower() != "false"
        return True

    def _ini_lines(self):
        try:
            with open(self.ini, encoding="utf-8") as f:
                return f.read().splitlines()
        except OSError:
            return []

    def set_enabled(self, on: bool):
        if not os.path.isfile(self.ini):
            raise FileNotFoundError("doorstop_config.ini not found; install the loader first.")
        with open(self.ini, encoding="utf-8", newline="") as f:
            text = f.read()
        out, done = [], False
        for line in text.splitlines(keepends=True):
            k, _, _v = line.partition("=")
            if not done and k.strip().lower() == "enabled" and not line.lstrip().startswith("#"):
                eol = line[len(line.rstrip("\r\n")):]
                line = f"enabled = {'true' if on else 'false'}{eol}"
                done = True
            out.append(line)
        if not done:
            raise ValueError("No 'enabled' setting found in doorstop_config.ini.")
        with open(self.ini, "w", encoding="utf-8", newline="") as f:
            f.write("".join(out))

    def install(self):
        if not os.path.isfile(self.zip):
            raise FileNotFoundError(f"Missing {self.zip}")
        if not os.path.isfile(self.dll):
            raise FileNotFoundError(f"Missing {self.dll}")
        with zipfile.ZipFile(self.zip) as z:
            for info in z.infolist():
                name = info.filename.replace("\\", "/")
                top = name.split("/", 1)[0]
                if info.is_dir() or (top not in ROOT_FILES and top != ROOT_DIR):
                    continue
                dest = os.path.normpath(os.path.join(self.root, *name.split("/")))
                if not dest.startswith(os.path.normpath(self.root) + os.sep):
                    continue            # path traversal guard
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        os.makedirs(os.path.dirname(self.plugin), exist_ok=True)
        shutil.copy2(self.dll, self.plugin)

    def update_plugin(self):
        os.makedirs(os.path.dirname(self.plugin), exist_ok=True)
        shutil.copy2(self.dll, self.plugin)

    def uninstall(self) -> str:
        """Remove the loader. Returns the path of the CSV backup ('' if none)."""
        backup = ""
        if os.path.isfile(self.csv):
            os.makedirs(BACKUP_DIR, exist_ok=True)
            backup = os.path.join(BACKUP_DIR, "BounceRatings-" + time.strftime("%Y%m%d-%H%M%S") + ".csv")
            shutil.copy2(self.csv, backup)
        for n in ROOT_FILES:
            p = os.path.join(self.root, n)
            if os.path.isfile(p):
                os.remove(p)
        if os.path.isdir(self.bep):
            shutil.rmtree(self.bep)
        return backup


# ------------------------------------------------------------------------ CSV
class Ratings:
    """In-memory copy of the CSV. Rows are kept in file order; lines that don't
    parse as a player row are carried through untouched."""

    def __init__(self):
        self.header = ",".join(HEADER)
        self.items = []     # str (raw line) or list[str] of >= NCOL fields

    # -- parse / render
    @staticmethod
    def _parse(text: str):
        lines = text.lstrip("﻿").splitlines()
        if not lines:
            raise ValueError("The file is empty.")
        head = [h.strip().lower() for h in lines[0].split(",")]
        if head[:NCOL] != HEADER:
            raise ValueError("Unexpected header; expected: " + ",".join(HEADER))
        items = []
        for ln in lines[1:]:
            if not ln.strip():
                continue
            f = ln.split(",")
            if len(f) >= NCOL and f[0].strip().lstrip("-").isdigit():
                items.append(f)
            else:
                items.append(ln)
        return lines[0], items

    def load_text(self, text: str):
        self.header, self.items = self._parse(text)

    def load(self, path: str):
        with open(path, encoding="utf-8-sig") as f:
            self.load_text(f.read())

    def render(self) -> str:
        out = [self.header]
        for it in self.items:
            out.append(it if isinstance(it, str) else ",".join(it))
        return "\n".join(out) + "\n"

    def save(self, path: str):
        """Write utf-8 without BOM, keeping the previous file as .bak."""
        if os.path.isfile(path):
            shutil.copy2(path, path + ".bak")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(self.render())
        os.replace(tmp, path)

    # -- players
    def players(self):
        """[(index, team, number, avatar_id, [7 ratings])] for parsed rows."""
        res = []
        for i, it in enumerate(self.items):
            if isinstance(it, str):
                continue
            res.append((i, it[1].strip(), it[2].strip(), it[0].strip(),
                        [clamp(x) for x in it[4:NCOL]]))
        return res

    def teams(self):
        return sorted({p[1] for p in self.players() if p[1]})

    def set_ratings(self, index: int, vals):
        row = self.items[index]
        for k, v in enumerate(vals):
            row[4 + k] = str(clamp(v))

    def reset(self, index: int):
        self.set_ratings(index, [DEFAULT_RATING] * len(COLS))

    def import_text(self, text: str):
        """Replace contents from another roster file; ratings are clamped."""
        header, items = self._parse(text)
        for it in items:
            if not isinstance(it, str):
                for k in range(4, NCOL):
                    it[k] = str(clamp(it[k]))
        self.header, self.items = header, items
