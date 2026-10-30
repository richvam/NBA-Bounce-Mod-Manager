"""
push_plugin.py -- the Bounce Push BepInEx plugin (loader/BouncePush.dll).

NBA Bounce only lets players shove in GOAT mode; the plugin lifts that for
normal Bounce matches. It ships off: it is installed with the other plugins by
the Roster tab, and this module flips EnableInBounceMode in its BepInEx config.
Imports nothing from app.py.
"""

from __future__ import annotations

import os
import shutil

import app_paths

DLL_NAME = "BouncePush.dll"
CFG_NAME = "richvam.bounce.push.cfg"
KEY = "EnableInBounceMode"
SRC_DLL = os.path.join(app_paths.APP_DIR, "loader", DLL_NAME)


class PushPlugin:
    def __init__(self, game_data_path: str):
        # game_data_path is ".../NBA Bounce_Data"; BepInEx sits beside it
        self.root = os.path.dirname(os.path.normpath(game_data_path or ""))
        self.bep = os.path.join(self.root, "BepInEx")
        self.dll = os.path.join(self.bep, "plugins", DLL_NAME)
        self.cfg = os.path.join(self.bep, "config", CFG_NAME)

    def bepinex_installed(self) -> bool:
        return os.path.isfile(os.path.join(self.bep, "core", "BepInEx.dll"))

    def enabled(self) -> bool:
        if not os.path.isfile(self.dll):
            return False
        try:
            with open(self.cfg, encoding="utf-8") as f:
                for line in f:
                    k, _, v = line.partition("=")
                    if k.strip() == KEY:
                        return v.strip().lower() == "true"
        except OSError:
            pass
        return False

    def set_enabled(self, on: bool):
        """Install or refresh the dll, then write the switch. Needs BepInEx."""
        if not self.bepinex_installed():
            raise RuntimeError("BepInEx is not installed. Install the loader from "
                               "the Roster tab first.")
        if os.path.isfile(SRC_DLL):
            os.makedirs(os.path.dirname(self.dll), exist_ok=True)
            shutil.copy2(SRC_DLL, self.dll)
        lines = []
        try:
            with open(self.cfg, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            pass
        val = "%s = %s" % (KEY, "true" if on else "false")
        for i, line in enumerate(lines):
            if line.partition("=")[0].strip() == KEY:
                lines[i] = val
                break
        else:
            if "[General]" not in lines:
                lines += ["[General]", ""]
            lines.insert(lines.index("[General]") + 1, val)
        os.makedirs(os.path.dirname(self.cfg), exist_ok=True)
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
