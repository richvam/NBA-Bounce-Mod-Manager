"""
game_layout.py -- where the game keeps its data, and keeping the app in step
with it across game updates.

WHY THIS EXISTS
---------------
Until the 2026-27 season update (October 2026) NBA Bounce shipped its data as
loose files in NBA Bounce_Data: globalgamemanagers, level0..N,
sharedassets0..N.assets, resources.assets and their .resS companions. Every
feature in this app patches those files in place.

That update moved all of them into one LZ4-compressed Unity bundle,
NBA Bounce_Data/data.unity3d. Only the audio banks (sharedassetsN.resource)
stayed loose. The player still runs happily from loose files when
data.unity3d is absent (live-tested: unpack, rename the bundle, launch -- the
game boots and plays normally), so "preparing" the game is:

  1. extract every file inside data.unity3d next to it, byte for byte,
  2. rename data.unity3d to data.unity3d.original_backup.

Undo puts the bundle back and removes the extracted files.

The same update also renumbered the files (old sharedassets1 is now
sharedassets0, old level2's content now sits in level1) and gave every object a
new path_id. So this module also:

  * finds which file holds what by looking for it (resolve()), instead of the
    hardcoded names the feature modules used to carry,
  * quarantines backups taken from an older game build (check_build_stamp()),
    which would otherwise be "restored" over the new build,
  * re-matches queued mods to their objects by NAME (rematch_mods()), because
    a path_id saved before an update now points at an unrelated object.

Nothing here touches apply_single_mod() or any other patching routine.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import struct

import app_paths

BUNDLE_NAME     = "data.unity3d"
BUNDLE_BACKUP   = BUNDLE_NAME + ".original_backup"
MARKER_FILE     = ".modmanager_unpacked.json"   # in NBA Bounce_Data
STAMP_FILE      = ".modmanager_build.json"      # in NBA Bounce_Data
CACHE_FILE      = app_paths.user("layout_cache.json")
QUARANTINE_DIR  = "_old_build_backups"

# Every backup suffix any part of the app writes. A backup belongs to the game
# build it was taken from; after an update all of them are wrong.
BACKUP_SUFFIXES = (".original_backup", ".teamslot_backup", ".gridscale_backup",
                   ".navpatch_backup", ".sliders_backup")

# The pre-update file names, used as fallbacks when a lookup finds nothing.
LEGACY = {
    "containers_file": "sharedassets1.assets",
    "team_scene":      "level1",
    "grid_scene":      "level2",
    "grid_go_path_id": 74641,
    "slider_scene":    "level2",
    "slider_shared":   "sharedassets2.assets",
    "court_file":      "sharedassets1.assets",
}

CONTAINER_CLASS = "BasketTeamContainer"
SLIDER_SCENE_CLASS = "AIDifficultyManager"
SLIDER_SHARED_CLASS = "AIDifficultyProbabilityBars"
TEAM_MANAGER_CLASS = "BasketTeamManager"
GRID_OBJECT_NAME = "GridLayOut"
COURT_MATERIAL_PREFIX = "mat_bounce_stadium_parquet_"


class LayoutError(Exception):
    """Something the user should read as a sentence, not a traceback."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _close(env):
    """Release file handles UnityPy holds (Windows refuses renames otherwise)."""
    import gc
    if env is None:
        return
    try:
        for f in list(getattr(env, "files", {}).values()):
            for attr in ("reader", "stream"):
                r = getattr(f, attr, None)
                s = getattr(r, "stream", r)
                try:
                    s.close()
                except Exception:
                    pass
        env.files.clear()
    except Exception:
        pass
    gc.collect()


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def asset_files(game_data):
    """.assets files plus extensionless level files -- the same rule as
    app.find_assets_files(), kept here so this module needs nothing from app."""
    out = []
    try:
        names = sorted(os.listdir(game_data))
    except OSError:
        return out
    for f in names:
        if f.endswith(".assets") or (f.startswith("level") and "." not in f):
            out.append(os.path.join(game_data, f))
    return out


def _levels(game_data):
    return [p for p in asset_files(game_data)
            if os.path.basename(p).startswith("level")]


def build_id(game_data):
    """Identifier of the installed game build.

    boot.config carries a build-guid that changes with every build Unity
    produces, which is exactly what we want. Older layouts without one fall
    back to the size and mtime of globalgamemanagers / data.unity3d."""
    cfg = os.path.join(game_data, "boot.config")
    try:
        with open(cfg, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("build-guid="):
                    guid = line.split("=", 1)[1].strip()
                    if guid:
                        return guid
    except OSError:
        pass
    for name in (BUNDLE_NAME, BUNDLE_BACKUP, "globalgamemanagers"):
        p = os.path.join(game_data, name)
        if os.path.isfile(p):
            st = os.stat(p)
            return f"{name}:{st.st_size}:{int(st.st_mtime)}"
    return ""


# ---------------------------------------------------------------------------
# Layout status, unpack and undo
# ---------------------------------------------------------------------------

def status(game_data):
    """What state the game folder is in.

    Returns {"state": ..., "message": ...} where state is one of
      "missing"   -- not an NBA Bounce_Data folder (or not set)
      "packed"    -- data.unity3d present, never unpacked: needs preparing
      "repacked"  -- data.unity3d present AND this app unpacked it before:
                     Steam put the bundle back (an update or Verify Integrity),
                     so the loose files on disk are from an older build
      "ready"     -- loose files present and no bundle: modding works
    """
    if not game_data or not os.path.isdir(game_data):
        return {"state": "missing", "message": "Game data folder not found."}
    bundle = os.path.isfile(os.path.join(game_data, BUNDLE_NAME))
    loose = os.path.isfile(os.path.join(game_data, "globalgamemanagers"))
    marker = os.path.isfile(os.path.join(game_data, MARKER_FILE))
    if bundle and (marker or loose):
        return {"state": "repacked",
                "message": "NBA Bounce was updated or its files were verified by "
                           "Steam, so the game is packed again. The unpacked "
                           "files from before are out of date and need to be "
                           "replaced."}
    if bundle:
        return {"state": "packed",
                "message": "This version of NBA Bounce keeps its data in one "
                           "compressed file (data.unity3d). The mod manager "
                           "needs it unpacked once before it can edit anything."}
    if loose:
        return {"state": "ready", "message": "Game files are ready for modding."}
    return {"state": "missing",
            "message": "No NBA Bounce game files were found in this folder. "
                       "Point Settings at the 'NBA Bounce_Data' folder."}


def _entry_bytes(f):
    """Raw bytes of one file stored inside a loaded bundle."""
    if type(f).__name__ == "SerializedFile":
        f.reader.Position = 0
        return bytes(f.reader.bytes)
    return bytes(f.bytes)


def unpack(game_data, progress=None):
    """Extract data.unity3d into loose files and set the bundle aside.

    Safe to re-run after a game update: older backups are quarantined first,
    then each loose file is written to a temp name, read back and checked, and
    only when every file verified are they moved over the old ones and the
    bundle renamed. Any failure before that point leaves the game exactly as
    it was (still packed). Returns a summary dict.
    """
    import UnityPy

    def say(msg):
        if progress:
            progress(msg)

    bundle = os.path.join(game_data, BUNDLE_NAME)
    if not os.path.isfile(bundle):
        raise LayoutError("data.unity3d is not in this folder; there is nothing "
                          "to unpack.")

    need = os.path.getsize(bundle) * 4
    try:
        free = shutil.disk_usage(game_data).free
    except OSError:
        free = None
    if free is not None and free < need:
        raise LayoutError(
            f"Not enough free disk space to unpack the game "
            f"({need // 2**20:,} MB needed, {free // 2**20:,} MB free).")

    say("Reading data.unity3d … (this can take a minute)")
    env = UnityPy.load(bundle)
    written = []
    try:
        engine = str(getattr(env.file, "version_engine", "") or "")
        files = list(env.file.files.items())
        if not any(n == "globalgamemanagers" for n, _ in files):
            raise LayoutError("data.unity3d does not look like an NBA Bounce "
                              "game bundle (no globalgamemanagers inside). "
                              "Nothing was changed.")
        for i, (name, f) in enumerate(files, 1):
            say(f"Unpacking {name}  ({i}/{len(files)})")
            data = _entry_bytes(f)
            dest = os.path.join(game_data, *name.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".unpack_tmp"
            with open(tmp, "wb") as out:
                out.write(data)
            digest = hashlib.md5(data).hexdigest()
            size = len(data)
            del data
            with open(tmp, "rb") as chk:
                if hashlib.md5(chk.read()).hexdigest() != digest:
                    raise LayoutError(f"{name} did not verify after writing. "
                                      f"The game was left packed.")
            written.append((tmp, dest, name, size, digest))
    except Exception:
        for tmp, *_ in written:
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    finally:
        _close(env)

    # Every file verified. Backups from the build that was here before are
    # wrong for this one, so set them aside, then swap the new files in.
    quarantined = quarantine_backups(game_data, reason="before unpacking a new build")
    for tmp, dest, *_ in written:
        os.replace(tmp, dest)
    os.replace(bundle, os.path.join(game_data, BUNDLE_BACKUP))

    marker = {
        "build_id": build_id(game_data),
        "engine": engine,
        "unpacked_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "files": [{"name": n, "size": s, "md5": d} for _t, _d, n, s, d in written],
    }
    _write_json(os.path.join(game_data, MARKER_FILE), marker)
    write_build_stamp(game_data)
    forget_cache()
    return {"files": len(written), "quarantined": quarantined, "engine": engine}


def can_undo_unpack(game_data):
    return (os.path.isfile(os.path.join(game_data, MARKER_FILE))
            and os.path.isfile(os.path.join(game_data, BUNDLE_BACKUP)))


def undo_unpack(game_data, progress=None):
    """Put data.unity3d back and delete the files unpack() extracted, along
    with every backup taken of them. Mods in the mods folder are untouched."""
    marker_path = os.path.join(game_data, MARKER_FILE)
    marker = _read_json(marker_path, None)
    bak = os.path.join(game_data, BUNDLE_BACKUP)
    if not marker or not os.path.isfile(bak):
        raise LayoutError("This folder was not unpacked by the mod manager, so "
                          "there is nothing to undo. (Steam's 'Verify integrity "
                          "of game files' also restores the original.)")
    if progress:
        progress("Restoring data.unity3d …")
    os.replace(bak, os.path.join(game_data, BUNDLE_NAME))
    removed = 0
    for entry in marker.get("files", []):
        path = os.path.join(game_data, *entry["name"].split("/"))
        for p in [path] + [path + s for s in BACKUP_SUFFIXES]:
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
    try:
        os.remove(marker_path)
    except OSError:
        pass
    forget_cache()
    return {"removed": removed}


# ---------------------------------------------------------------------------
# Backups belong to a game build
# ---------------------------------------------------------------------------

def _backup_files(game_data):
    found = []
    for root in (game_data, os.path.join(game_data, "Managed"),
                 os.path.join(game_data, "Resources")):
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for n in names:
            if n.endswith(BACKUP_SUFFIXES) and n != BUNDLE_BACKUP:
                found.append(os.path.join(root, n))
    return found


def quarantine_backups(game_data, reason=""):
    """Move every backup file into _old_build_backups/<timestamp>/ so nothing
    in the app can restore an old build over a new one. Never deletes."""
    files = _backup_files(game_data)
    if not files:
        return []
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest_root = os.path.join(game_data, QUARANTINE_DIR, stamp)
    moved = []
    for p in files:
        rel = os.path.relpath(p, game_data)
        dest = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(p, dest)
        moved.append(rel)
    with open(os.path.join(dest_root, "README.txt"), "w", encoding="utf-8") as f:
        f.write("These backups were taken from an older build of NBA Bounce.\n"
                "The NBA Bounce Mod Manager moved them here "
                f"({reason or 'after a game update'}) so they can never be\n"
                "restored over the current game. Delete this folder when you "
                "no longer need it.\n")
    return moved


def write_build_stamp(game_data):
    _write_json(os.path.join(game_data, STAMP_FILE),
                {"build_id": build_id(game_data)})


def check_build_stamp(game_data):
    """Quarantine backups if the game build changed since they were taken.

    Returns the list of quarantined files ([] when nothing changed). On the
    first run there is no stamp: we cannot tell when existing backups were
    made, so they are kept, but a backup older than the game's own files is
    still treated as stale.
    """
    current = build_id(game_data)
    if not current:
        return []
    stamp = _read_json(os.path.join(game_data, STAMP_FILE), {})
    moved = []
    if stamp.get("build_id") and stamp["build_id"] != current:
        moved = quarantine_backups(game_data, reason="the game was updated")
    elif not stamp:
        moved = _quarantine_older_than_game(game_data)
    write_build_stamp(game_data)
    return moved


STALE_MARGIN = 6 * 3600   # seconds; an install writes all its files in minutes


def _quarantine_older_than_game(game_data):
    """First run only (no stamp yet), so we cannot know when the backups were
    taken. Two signs a backup belongs to an older build:
      * the file it backs up no longer exists (the update removed it), or
      * it is hours older than boot.config, which Steam rewrites on every
        update. shutil.copy2 keeps the game file's own mtime, so a backup of
        the current build is within minutes of boot.config, never hours.
    """
    cfg = os.path.join(game_data, "boot.config")
    game_time = os.path.getmtime(cfg) if os.path.isfile(cfg) else None
    stale = []
    for p in _backup_files(game_data):
        live = p
        for suf in BACKUP_SUFFIXES:
            if p.endswith(suf):
                live = p[:-len(suf)]
                break
        if not os.path.exists(live):
            stale.append(p)
        elif game_time is not None and os.path.getmtime(p) < game_time - STALE_MARGIN:
            stale.append(p)
    if not stale:
        return []
    # Move only the stale ones, keeping anything taken from the current build.
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest_root = os.path.join(game_data, QUARANTINE_DIR, stamp)
    moved = []
    for p in stale:
        rel = os.path.relpath(p, game_data)
        dest = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(p, dest)
        moved.append(rel)
    return moved


# ---------------------------------------------------------------------------
# Which file holds what
# ---------------------------------------------------------------------------

_memo = {}


def forget_cache():
    _memo.clear()
    try:
        os.remove(CACHE_FILE)
    except OSError:
        pass


def _signature(game_data):
    names = sorted(os.path.basename(p) for p in asset_files(game_data))
    # "v2": the map gained team_scenes; older caches must be rebuilt.
    return "v2|" + build_id(game_data) + "|" + ",".join(names)


def _script_ids(game_data, class_names):
    """{MonoScript path_id: class name} from globalgamemanagers.assets."""
    import UnityPy
    ggm = os.path.join(game_data, "globalgamemanagers.assets")
    if not os.path.isfile(ggm):
        return {}
    env = UnityPy.load(ggm)
    out = {}
    try:
        for o in env.objects:
            if o.type.name != "MonoScript":
                continue
            try:
                n = o.read().m_ClassName
            except Exception:
                continue
            if n in class_names:
                out[o.path_id] = n
    finally:
        _close(env)
    return out


def _scan_file(path, script_ids):
    """Count MonoBehaviours per wanted class, court materials, and find the
    GridLayOut GameObject, reading only object headers."""
    import UnityPy
    env = UnityPy.load(path)
    counts, materials, grid = {}, 0, None
    try:
        sf = next(iter(env.files.values()))
        externals = [os.path.basename(str(getattr(e, "path", e))).lower()
                     for e in (getattr(sf, "externals", []) or [])]
        ggm_ids = {i + 1 for i, n in enumerate(externals)
                   if n == "globalgamemanagers.assets"}
        is_scene = os.path.basename(path).startswith("level")
        for o in env.objects:
            t = o.type.name
            if t == "MonoBehaviour" and script_ids:
                try:
                    r = o.reader
                    r.Position = o.byte_start
                    fid, pid = struct.unpack_from("<iq", r.read_bytes(28), 16)
                except Exception:
                    continue
                if fid in ggm_ids and pid in script_ids:
                    cls = script_ids[pid]
                    counts[cls] = counts.get(cls, 0) + 1
            elif t == "Material":
                try:
                    if o.peek_name().startswith(COURT_MATERIAL_PREFIX):
                        materials += 1
                except Exception:
                    pass
            elif t == "GameObject" and is_scene and grid is None:
                try:
                    if o.peek_name() == GRID_OBJECT_NAME:
                        grid = o.path_id
                except Exception:
                    pass
    finally:
        _close(env)
    return counts, materials, grid


def resolve(game_data, refresh=False):
    """{role: file name or path_id} for the current game build.

    Roles: containers_file, team_scene, grid_scene, grid_go_path_id,
    slider_scene, slider_shared, court_file. Anything not found keeps its
    pre-update value from LEGACY, so an old loose install behaves exactly as
    before. Cached per game build in user/layout_cache.json (a full scan takes
    a few seconds, mostly level1).
    """
    sig = _signature(game_data)
    if not refresh:
        if _memo.get("sig") == sig:
            return dict(_memo["map"])
        cached = _read_json(CACHE_FILE, {})
        if cached.get("sig") == sig and cached.get("map"):
            _memo.update(sig=sig, map=cached["map"])
            return dict(cached["map"])

    result = dict(LEGACY)
    if not os.path.isfile(os.path.join(game_data, "globalgamemanagers.assets")):
        return result
    script_ids = _script_ids(game_data, {CONTAINER_CLASS, SLIDER_SCENE_CLASS,
                                         SLIDER_SHARED_CLASS, TEAM_MANAGER_CLASS})
    team_scenes = []
    best = {"containers": (0, None), "slider_scene": (0, None),
            "slider_shared": (0, None), "court": (0, None)}
    grid_scene = None
    for path in asset_files(game_data):
        name = os.path.basename(path)
        try:
            counts, mats, grid = _scan_file(path, script_ids)
        except Exception:
            continue
        for role, cls in (("containers", CONTAINER_CLASS),
                          ("slider_scene", SLIDER_SCENE_CLASS),
                          ("slider_shared", SLIDER_SHARED_CLASS)):
            n = counts.get(cls, 0)
            if role == "slider_scene" and not name.startswith("level"):
                continue
            if role == "slider_shared" and not name.endswith(".assets"):
                continue
            if n > best[role][0]:
                best[role] = (n, name)
        if mats > best["court"][0]:
            best["court"] = (mats, name)
        if name.startswith("level") and counts.get(TEAM_MANAGER_CLASS):
            team_scenes.append(name)
        if grid is not None and grid_scene is None:
            grid_scene, result["grid_go_path_id"] = name, grid

    if best["containers"][1]:
        result["containers_file"] = best["containers"][1]
    if best["slider_scene"][1]:
        result["slider_scene"] = best["slider_scene"][1]
    if best["slider_shared"][1]:
        result["slider_shared"] = best["slider_shared"][1]
    if best["court"][1]:
        result["court_file"] = best["court"][1]
    if grid_scene:
        result["grid_scene"] = grid_scene
    # Every scene that carries its own BasketTeamManager (and so its own copy
    # of m_aoBundledTeams). Before the 2026-27 update that was level1 alone;
    # since then the boot scene level0 has one too, and it is the copy the
    # game actually keeps (Unity loads level0 first). An added team has to be
    # in all of them. The first is the one the game uses.
    team_scenes.sort(key=lambda n: int(n[5:]) if n[5:].isdigit() else 99)
    if team_scenes:
        result["team_scenes"] = team_scenes
        result["team_scene"] = team_scenes[0]
    elif os.path.isfile(os.path.join(game_data, "level1")):
        result["team_scene"] = "level1"
        result["team_scenes"] = ["level1"]

    _memo.update(sig=sig, map=result)
    try:
        _write_json(CACHE_FILE, {"sig": sig, "map": result})
    except OSError:
        pass
    return dict(result)


def configure(game_data, *modules, refresh=False):
    """Point every feature module at the files this game build actually uses.

    Feature modules keep module-level names (CONTAINER_ASSETS_FILE,
    GRID_SCENE_FILE, ...) that their functions read at call time; this sets
    them. Pass the modules to update (app passes itself plus the feature
    modules it imported). Returns the resolved map.
    """
    global _current
    m = resolve(game_data, refresh=refresh) if game_data else dict(LEGACY)
    _current = dict(m)
    # Feature modules are imported lazily (Sliders and Floor Patterns only when
    # their window opens), so push the map into the ones already loaded and
    # let the rest pick it up through attach() when they are imported.
    import sys
    targets = list(modules) + [sys.modules.get(n) for n in LAYOUT_MODULES]
    for mod in dict.fromkeys(t for t in targets if t is not None):
        hook = getattr(mod, "apply_layout", None)
        if callable(hook):
            hook(dict(m))
    return m


# Modules with an apply_layout() hook that call attach() when imported.
LAYOUT_MODULES = ("team_manager", "slider_manager", "floor_patterns")
_current = None


def attach(module):
    """Called at the end of a feature module's import: apply the layout that
    configure() already resolved, if any. Keeps startup from importing every
    feature module just to tell it file names."""
    if _current is None:
        return
    hook = getattr(module, "apply_layout", None)
    if callable(hook):
        hook(dict(_current))


# ---------------------------------------------------------------------------
# Queued mods follow their object by name
# ---------------------------------------------------------------------------

def _names_in(path, type_name, wanted_ids=None):
    """{path_id: name} for objects of type_name in one file."""
    import UnityPy
    env = UnityPy.load(path)
    out = {}
    try:
        for o in env.objects:
            if o.type.name != type_name:
                continue
            if wanted_ids is not None and o.path_id not in wanted_ids:
                continue
            try:
                name = o.peek_name()
            except Exception:
                continue
            if not name and type_name == "Mesh":
                name = f"mesh_{o.path_id}"   # what mesh_manager lists unnamed meshes as
            out[o.path_id] = name
    finally:
        _close(env)
    return out


def mismatched_mods(entries, type_name):
    """Keys of mod entries whose object is gone or has a different name.

    entries: {key: {"name", "assets_file", "path_id", ...}}. A missing assets
    file counts as a mismatch too."""
    by_file = {}
    for key, e in entries.items():
        by_file.setdefault(e.get("assets_file", ""), []).append(key)
    bad = []
    for af, keys in by_file.items():
        if not af or not os.path.isfile(af):
            bad.extend(keys)
            continue
        try:
            names = _names_in(af, type_name, {entries[k]["path_id"] for k in keys})
        except Exception:
            bad.extend(keys)
            continue
        for k in keys:
            e = entries[k]
            if names.get(e["path_id"]) != e.get("name"):
                bad.append(k)
    return bad


def rematch_mods(game_data, entries, type_name, make_key):
    """Re-point mod entries whose path_id no longer names their object.

    Returns (new_entries, moved, unresolved):
      moved      [(name, old "file:pid", new "file:pid")]
      unresolved [(name, reason)] -- left in new_entries unchanged, so the user
                 can decide; the apply loops refuse to write them.
    A name that now exists more than once of the same type is ambiguous and is
    never guessed.
    """
    bad = set(mismatched_mods(entries, type_name))
    if not bad:
        return dict(entries), [], []
    index = {}
    for path in asset_files(game_data):
        try:
            for pid, name in _names_in(path, type_name).items():
                index.setdefault(name, []).append((path, pid))
        except Exception:
            continue
    new, moved, unresolved = {}, [], []
    for key, e in entries.items():
        if key not in bad:
            new[key] = e
            continue
        hits = index.get(e.get("name"), [])
        old = f"{os.path.basename(e.get('assets_file', ''))}:{e.get('path_id')}"
        if len(hits) == 1:
            path, pid = hits[0]
            e2 = dict(e, assets_file=path, path_id=pid)
            new[make_key(e2)] = e2
            new_loc = f"{os.path.basename(path)}:{pid}"
            if new_loc == old:
                new_loc += " in the current game folder"
            moved.append((e.get("name"), old, new_loc))
        else:
            new[key] = e
            reason = ("no longer in the game" if not hits else
                      f"{len(hits)} objects now share that name")
            unresolved.append((e.get("name"), reason))
    return new, moved, unresolved
