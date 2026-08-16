"""
app_paths.py -- where everything lives, in one place.

app.py is the only Python file at the top of the folder; everything else is
filed underneath it:

    modules/   this file and every feature module (nothing here is run directly
               by the end user -- app.py reaches all of it)
    data/      catalogs that ship with the app and are only ever read
    docs/      the guides
    tools/     developer scripts, never run by the app itself
    user/      everything the app WRITES: settings, caches, save backups

Modules ask this module for a path instead of deriving one from their own
__file__, because they no longer sit next to the files they load.

Keeping every written file under user/ means updating the app is a matter of
replacing everything except that one folder, and it keeps the game's paths,
your queued mods and your save backups out of the way of a `git pull`. Installs
from before the folder split keep working: anything left over in the old flat
layout is moved into user/ the first time this module is imported.
"""

import os
import shutil

MODULES_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR     = os.path.dirname(MODULES_DIR)      # the folder app.py sits in
DATA_DIR    = os.path.join(APP_DIR, "data")
DOCS_DIR    = os.path.join(APP_DIR, "docs")
TOOLS_DIR   = os.path.join(APP_DIR, "tools")
USER_DIR    = os.path.join(APP_DIR, "user")

# What the old flat layout wrote next to app.py. Files and folders both.
_LEGACY = ("config.json", "floor_patterns.json", "floor_patterns_journal.json",
           "floor_patterns_journal.json.done", "unlockables_catalog.json",
           "floor_patterns", "save_backups")


def data(*parts):
    """Path to a catalog that ships with the app (read-only)."""
    return os.path.join(DATA_DIR, *parts)


def user(*parts):
    """Path to something the app writes: settings, a cache, a backup."""
    return os.path.join(USER_DIR, *parts)


def docs(*parts):
    """Path to one of the guides."""
    return os.path.join(DOCS_DIR, *parts)


def _migrate_legacy():
    """Move pre-3.0 state files from beside app.py into user/.

    Only ever moves INTO an empty spot, so a file already in user/ wins and a
    second run has nothing left to do. Failures are ignored on purpose: a
    settings file that can't be moved is re-created from defaults, which is
    annoying, while a crash on import would stop the app from starting at all.
    """
    for name in _LEGACY:
        old, new = os.path.join(APP_DIR, name), os.path.join(USER_DIR, name)
        if not os.path.exists(old) or os.path.exists(new):
            continue
        try:
            shutil.move(old, new)
        except Exception:
            pass


os.makedirs(USER_DIR, exist_ok=True)
_migrate_legacy()
