"""
team_slot_poc.py -- proof of concept for a 33rd team slot.

This is the "minimal proof of concept" the Additional Team Slots feasibility
study asks for, and nothing more: clone the Trail Blazers' team record under a
new unique ID and name, recolor it, append a pointer to it in the team
manager's bundled-team list, and launch the game. One test answers three open
questions at once -- whether an added object loads at all, how the team-select
grid copes with a 33rd tile, and whether a match plays through.

It is a developer script (tools/ is never run by the app) and it is deliberately
NOT wired into the Mods tab: this rewrites whole .assets files, which is a
different and riskier kind of patching than apply_single_mod()'s in-place byte
splices. Nothing here touches app.py's code path.

--------------------------------------------------------------------------------
WHAT IT CHANGES
--------------------------------------------------------------------------------
sharedassets1.assets   gains one new MonoBehaviour: a byte-for-byte copy of the
                       Trail Blazers' BasketTeamContainer with three edits --
                       a new unique ID, new name strings, and new court/team
                       colors. Every other object is carried over untouched.

level1                 the BasketTeamManager's m_aoBundledTeams array grows
                       from 32 to 33 entries, the new one pointing at the
                       cloned container.

Both files are re-serialized by UnityPy rather than patched in place, because
both grow. Both are backed up first, and both are verified after the rebuild:
every pre-existing object must still be there, at its original size, or the
rebuilt file is thrown away and the game file is left exactly as it was.

--------------------------------------------------------------------------------
WHY THE CLONE KEEPS ITS EXACT BYTE LENGTH
--------------------------------------------------------------------------------
The container's field layout isn't known from a type tree -- the game ships
without them -- so this script edits the container the same way app.py already
edits court line colors: by finding values inside the raw serialized bytes and
overwriting them. That only stays sound while nothing moves, so every edit is
length-preserving:

  * a Color is 4 x float32 = 16 fixed bytes, so recoloring can't shift anything;
  * the unique ID is one int32;
  * renaming rewrites a length-prefixed string IN PLACE, padding with spaces or
    truncating so the byte count (and its 4-byte alignment padding) is identical.

A name that would change the length is refused rather than guessed at.

--------------------------------------------------------------------------------
WHAT IT DOES NOT DO
--------------------------------------------------------------------------------
The clone reuses Portland's logo sprite, jerseys, court textures, materials and
three-man roster -- those are pointers and name strings into art this script
does not create. "Change the logo color" here means the team's color table: the
RGBA values the game reapplies to the court and to the team's UI tint on every
load. The logo ARTWORK is still Portland's until new Texture2D and Sprite
objects exist, which is the next piece of work, not this one.

It also does not touch saves. The feasibility study's biggest risk is
UNINSTALLING: the game looks up every saved team ID with no missing-key check,
so a save written while a custom team exists may fail to load once the mod is
gone. Until the manager grows that safeguard, back up your save yourself, and
use --revert (which restores both backups) rather than deleting files by hand.

--------------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------------
    python tools/team_slot_poc.py                     inspect only, writes nothing
    python tools/team_slot_poc.py --apply             do it
    python tools/team_slot_poc.py --apply --color 00c853 --name "Pythons"
    python tools/team_slot_poc.py --verify            is it actually on disk?
    python tools/team_slot_poc.py --revert            put the backups back

Inspect mode prints everything the apply would rely on -- the source container,
the unique ID it found and how it proved it, the strings it would rewrite, the
colors it would change, and the pointer array it would grow. Read that output
before applying; if anything in it looks wrong, the apply would be wrong too.

Close the game and quit Steam first: Windows won't let the rebuilt files be
swapped in while anything holds them open.
"""

import argparse
import copy
import json
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import app_paths                                   # noqa: E402

BACKUP_SUFFIX = ".teamslot_backup"

CONTAINER_ASSETS_FILE  = "sharedassets1.assets"
MANAGER_SCENE_FILE     = "level1"
CONTAINER_SCRIPT_CLASS = "BasketTeamContainer"

# The feasibility study found -1 .. -38 taken by shipped teams, plus four
# unfinished teams in sharedassets2.assets whose IDs were never checked. -101 is
# far outside anything observed; the apply still proves it unused before using it.
DEFAULT_NEW_ID = -101

DEFAULT_SOURCE  = "Portland"        # matched against container names, case-folded
DEFAULT_NAME    = "Pythons"
DEFAULT_COLOR   = "00c853"          # a green nobody could mistake for Blazers red

# Team unique IDs are small negatives. Anything outside this window is some
# other int32 that happens to be negative.
ID_RANGE = (-64, -1)


class PocError(Exception):
    pass


# ---------------------------------------------------------------------------
# Serialized-bytes helpers (same conventions as app.py's container parsing)
# ---------------------------------------------------------------------------

def read_str(raw, off):
    """Read a Unity length-prefixed string; return (text, offset past padding)."""
    (n,) = struct.unpack_from("<i", raw, off)
    if not (0 <= n <= 4096) or off + 4 + n > len(raw):
        raise ValueError(f"not a string at {off}")
    s = raw[off + 4:off + 4 + n].decode("utf-8", "replace")
    return s, (off + 4 + n + 3) & ~3


def script_path_id(raw):
    """m_Script PPtr sits at a fixed offset in every MonoBehaviour:
    m_GameObject PPtr (12) + m_Enabled byte + 3 bytes padding = 16."""
    return struct.unpack_from("<iq", raw, 16)[1]


def find_strings(raw, min_len=2, max_len=64):
    """Every plausible length-prefixed ASCII string in a blob, as
    [(offset, text, offset_past_padding)]. Used to locate the name fields."""
    out = []
    n = len(raw)
    for i in range(0, n - 4, 4):
        (slen,) = struct.unpack_from("<i", raw, i)
        if not (min_len <= slen <= max_len and i + 4 + slen <= n):
            continue
        cand = raw[i + 4:i + 4 + slen]
        if not all(32 <= c < 127 for c in cand):
            continue
        out.append((i, cand.decode("ascii"), (i + 4 + slen + 3) & ~3))
    return out


def find_color_overrides(raw):
    """Every per-team color override in a container, as
    [(offset, property, variant, rgba)].

    Same structure app.py's Court Colors tab already reads and writes:

        m_sMaterialColorParam     : int32 len + bytes, padded to 4
        m_aoRetroExtraColorParams : int32 count
            count x { key: int32 len + bytes padded to 4 ; value: 4 x float32 }

    The key is the logo/unlock variant -- "default" for the normal court, one
    more per retro era. Only these offsets are recolored: a blind hunt for
    "4 floats between 0 and 1" would also hit positions, weights and ratings.
    """
    out = []
    n = len(raw)
    for i in range(0, n - 4, 4):
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
            _p, off = read_str(raw, i)
            (count,) = struct.unpack_from("<i", raw, off)
            off += 4
            if not (0 <= count <= 32):
                continue
            found = []
            for _ in range(count):
                key, off = read_str(raw, off)
                if off + 16 > n:
                    found = []
                    break
                found.append((off, param, key, struct.unpack_from("<4f", raw, off)))
                off += 16
            out.extend(found)
        except Exception:
            continue
    return out


def replace_string_in_place(raw, offset, new_text):
    """Overwrite a length-prefixed string without changing how many bytes it
    occupies. Shorter text is space-padded, longer text is refused -- letting
    the field grow would move every byte after it, and every offset this script
    holds would rot."""
    (slen,) = struct.unpack_from("<i", raw, offset)
    encoded = new_text.encode("utf-8")
    if len(encoded) > slen:
        raise PocError(
            f"'{new_text}' is {len(encoded)} bytes and the field it would "
            f"replace holds {slen}. Pick a name of at most {slen} characters -- "
            f"this script only rewrites strings in place, because a longer one "
            f"would shift every offset after it.")
    encoded = encoded.ljust(slen, b" ")
    return raw[:offset + 4] + encoded + raw[offset + 4 + slen:]


def hex_to_rgba(text, alpha=1.0):
    text = text.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        raise PocError(f"--color wants six hex digits, e.g. 00c853 (got '{text}').")
    r, g, b = (int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (r, g, b, alpha)


def args_color_hex(rgba):
    return "".join(f"{int(round(c * 255)):02x}" for c in rgba[:3])


# ---------------------------------------------------------------------------
# Finding things in the game files
# ---------------------------------------------------------------------------

def resolve_game_data(explicit=None):
    if explicit:
        path = explicit
    else:
        cfg = app_paths.user("config.json")
        if not os.path.exists(cfg):
            raise PocError("No game folder configured yet. Run the manager once "
                           "and point it at the game, or pass --game-data.")
        with open(cfg, encoding="utf-8") as f:
            path = (json.load(f).get("game_data_path") or "").strip()
    if not path or not os.path.isdir(path):
        raise PocError(f"Game data folder not found: {path!r}")
    for name in (CONTAINER_ASSETS_FILE, MANAGER_SCENE_FILE):
        if not os.path.isfile(os.path.join(path, name)):
            raise PocError(f"{name} is not in {path} -- that doesn't look like "
                           f"the NBA Bounce_Data folder.")
    return path


def find_container_script(game_data):
    """MonoScript path_id for BasketTeamContainer, looked up rather than
    hardcoded so a game update can't point us at the wrong script."""
    import UnityPy
    ggm = os.path.join(game_data, "globalgamemanagers.assets")
    env = UnityPy.load(ggm)
    try:
        for obj in env.objects:
            if obj.type.name != "MonoScript":
                continue
            try:
                if getattr(obj.read(), "m_ClassName", "") == CONTAINER_SCRIPT_CLASS:
                    return obj.path_id
            except Exception:
                continue
    finally:
        close_env(env)
    raise PocError(f"No MonoScript named {CONTAINER_SCRIPT_CLASS} in "
                   f"globalgamemanagers.assets.")


def read_containers(game_data, script_pid):
    """{name: {"path_id", "raw"}} for every BasketTeamContainer."""
    import UnityPy
    env = UnityPy.load(os.path.join(game_data, CONTAINER_ASSETS_FILE))
    out = {}
    try:
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                raw = obj.get_raw_data()
                if script_path_id(raw) != script_pid:
                    continue
                name, _ = read_str(raw, 28)
            except Exception:
                continue
            out[name] = {"path_id": obj.path_id, "raw": raw}
    finally:
        close_env(env)
    if not out:
        raise PocError("Found the BasketTeamContainer script but no containers "
                       "in sharedassets1.assets.")
    return out


def string_chain(raw, start=28, limit=24):
    """Offsets of the run of length-prefixed strings that a container opens
    with, starting at m_Name: [(offset, text, offset_past_padding)].

    Stops at the first thing that doesn't parse as a printable string, which is
    where the fixed-width fields (the unique ID among them) begin.
    """
    out, off = [], start
    while len(out) < limit and off + 4 <= len(raw):
        try:
            text, end = read_str(raw, off)
        except ValueError:
            break
        if text and not all(32 <= ord(ch) < 127 for ch in text):
            break
        out.append((off, text, end))
        off = end
    return out


def locate_unique_id(containers):
    """Work out where a container's unique team ID lives, without a type tree.

    It is NOT at a fixed byte offset: the name strings in front of it are
    variable-length, so every team's ID sits somewhere different. What IS the
    same for every team is its position in the field ORDER -- so the search is
    anchored to "the int32 that follows the Nth string", and N is settled by
    what the values mean: team IDs are one small negative per team, and no two
    teams share one. The N where every container holds a different small
    negative is the ID field.

    Returns (n_strings_before_it, {container_name: id}).
    """
    lo, hi = ID_RANGE
    chains = {}
    for name, c in containers.items():
        chains[name] = string_chain(c["raw"])
    depth = min((len(ch) for ch in chains.values()), default=0)
    if depth == 0:
        raise PocError("No container starts with a readable m_Name string; the "
                       "MonoBehaviour layout is not what this script expects.")

    good = []
    for n in range(1, depth + 1):
        values = []
        for name, chain in chains.items():
            end = chain[n - 1][2]
            raw = containers[name]["raw"]
            if end + 4 > len(raw):
                values = []
                break
            values.append(struct.unpack_from("<i", raw, end)[0])
        if len(values) != len(chains):
            continue
        if all(lo <= v <= hi for v in values) and len(set(values)) == len(values):
            good.append(n)

    if not good:
        raise PocError(
            "Couldn't find the unique-ID field: no position in the field order "
            "holds a different small negative number in every container. The "
            "container layout may have changed in a game update. Pass "
            "--id-after-string to point at it by hand.")
    if len(good) > 1:
        raise PocError(
            "More than one position looks like the unique-ID field (after "
            + ", ".join(str(n) for n in good)
            + " strings). Re-run with --id-after-string to say which; the "
              "Celtics should read -1.")
    n = good[0]
    ids = {name: struct.unpack_from("<i", containers[name]["raw"],
                                    chain[n - 1][2])[0]
           for name, chain in chains.items()}
    return n, ids


def id_offset_for(raw, n_strings):
    """Byte offset of the unique ID in one container, given how many strings
    come before it."""
    chain = string_chain(raw)
    if len(chain) < n_strings:
        raise PocError(f"This container only has {len(chain)} leading strings, "
                       f"so there is no field after string {n_strings}.")
    return chain[n_strings - 1][2]


def scan_pointer_arrays(raw, container_pids):
    """Every PPtr array inside one object's bytes whose entries all point at
    known containers, as [(offset_of_count, count, file_id)].

    A serialized PPtr array is an int32 count followed by count x (int32
    m_FileID, int64 m_PathID). Requiring every entry to resolve to a container
    we already found identifies the team lists without a type tree, and without
    caring what else the manager holds.
    """
    out = []
    for i in range(0, len(raw) - 4, 4):
        (count,) = struct.unpack_from("<i", raw, i)
        if not (2 <= count <= 128) or i + 4 + count * 12 > len(raw):
            continue
        entries = [struct.unpack_from("<iq", raw, i + 4 + k * 12)
                   for k in range(count)]
        file_ids = {fid for fid, _pid in entries}
        pids = [pid for _fid, pid in entries]
        if len(file_ids) != 1 or len(set(pids)) != count:
            continue
        if not all(p in container_pids for p in pids):
            continue
        out.append((i, count, entries[0][0]))
    return out


def find_bundled_team_array(game_data, container_pids):
    """Locate BasketTeamManager.m_aoBundledTeams inside level1.

    A serialized PPtr array is an int32 count followed by count x (int32
    m_FileID, int64 m_PathID). We look for the one whose entries all point at
    containers we already found, which identifies the array without a type tree
    and without caring what else the manager holds.

    Returns (path_id_of_manager, raw_bytes, offset_of_count, count, file_id).
    """
    import UnityPy
    env = UnityPy.load(os.path.join(game_data, MANAGER_SCENE_FILE))
    best = None
    try:
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                raw = obj.get_raw_data()
            except Exception:
                continue
            for off, count, file_id in scan_pointer_arrays(raw, container_pids):
                # the bundled list is the longest such array (32 entries); the
                # HardWood (4) and GOAT (2) lists have the same shape
                if best is None or count > best[3]:
                    best = (obj.path_id, raw, off, count, file_id)
    finally:
        close_env(env)
    if best is None:
        raise PocError(
            "Couldn't find a pointer array in level1 whose entries all point at "
            "BasketTeamContainers. The team manager's layout may have changed.")
    return best


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def close_env(env):
    """Let go of every OS file handle a UnityPy Environment holds -- Windows
    refuses to replace a file while one is still open."""
    import gc
    if env is None:
        return
    for attr in ("files", "cabs"):
        try:
            getattr(env, attr, {}).clear()
        except Exception:
            pass
    try:
        reader = getattr(env, "reader", None)
        if reader is not None and hasattr(reader, "stream"):
            reader.stream.close()
    except Exception:
        pass
    gc.collect()


def backup(path):
    import shutil
    bak = path + BACKUP_SUFFIX
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        return f"backed up -> {os.path.basename(bak)}"
    return f"backup already exists ({os.path.basename(bak)})"


def rebuild_file(path, edit, expect_added=0):
    """Re-serialize one .assets/level file after `edit(env)` has changed it.

    The rebuilt file is written to a temp name, read back, and compared against
    the original object table before it is allowed anywhere near the game
    folder: every object that was there must still be there at its original
    size, and nothing unexpected may have appeared. Anything off and the temp
    file is dropped with the game file untouched.
    """
    import UnityPy
    env = UnityPy.load(path)
    before = {o.path_id: o.byte_size for o in env.objects}
    changed = edit(env)                      # set of path_ids allowed to differ
    data = env.file.save()
    close_env(env)

    tmp = path + ".teamslot_tmp"
    with open(tmp, "wb") as f:
        f.write(data)

    verify = None
    try:
        verify = UnityPy.load(tmp)
        after = {o.path_id: o.byte_size for o in verify.objects}
    except Exception as exc:
        close_env(verify)
        os.remove(tmp)
        raise PocError(f"The rebuilt {os.path.basename(path)} could not be read "
                       f"back ({exc}). Your game file was left alone.")
    finally:
        close_env(verify)

    lost    = set(before) - set(after)
    gained  = set(after) - set(before)
    resized = [p for p in before
               if p in after and after[p] != before[p] and p not in changed]
    if lost or resized or len(gained) != expect_added:
        os.remove(tmp)
        raise PocError(
            f"Rebuilding {os.path.basename(path)} would have changed "
            f"{len(lost)} lost / {len(gained)} added (expected {expect_added}) / "
            f"{len(resized)} resized other object(s), so it was abandoned and "
            f"your game file was left alone.")

    replace_game_file(tmp, path)
    return len(before), len(after)


def replace_game_file(tmp, dest):
    """os.replace() with Windows' file locking accounted for."""
    import gc
    import time
    gc.collect()
    last = None
    for attempt in range(5):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError as exc:
            last = exc
            gc.collect()
            time.sleep(0.3 * (attempt + 1))
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise PocError(
        f"'{os.path.basename(dest)}' is open in another program, so the rebuilt "
        f"file could not be put in place. Close NBA Bounce and quit Steam "
        f"(Steam holds game files open while the game runs or verifies), then "
        f"try again. Your game file was left untouched.\n\n({last})")


def add_object(env, template_path_id, new_path_id, raw):
    """Register a new object in a SerializedFile: a copy of an existing object's
    reader, re-pointed at a new path_id and carrying new bytes. Copying a real
    reader is what keeps the type/class metadata correct -- the clone is the
    same MonoBehaviour type as the container it came from, which is exactly
    what the game expects to find behind the new pointer."""
    src = next(o for o in env.objects if o.path_id == template_path_id)
    clone = copy.copy(src)
    clone.path_id = new_path_id
    try:
        clone.set_raw_data(raw)
    except AttributeError as exc:          # UnityPy API drift
        raise PocError(f"This UnityPy version doesn't expose set_raw_data on an "
                       f"object reader ({exc}); upgrade UnityPy.") from exc
    objects = env.file.objects
    if new_path_id in objects:
        raise PocError(f"path_id {new_path_id} is already taken in "
                       f"{os.path.basename(env.file.name or '?')}.")
    objects[new_path_id] = clone
    if hasattr(env.file, "mark_changed"):
        env.file.mark_changed()
    return clone


# ---------------------------------------------------------------------------
# The proof of concept itself
# ---------------------------------------------------------------------------

def build_clone_bytes(raw, id_offset, new_id, source_name, new_name, rgba,
                      report):
    """Return the cloned container's bytes, with ID, names and colors changed.

    Every edit is length-preserving, so the clone serializes to exactly the
    same number of bytes as the container it was copied from.
    """
    size_before = len(raw)

    # 1. unique ID -- one int32
    old_id = struct.unpack_from("<i", raw, id_offset)[0]
    raw = raw[:id_offset] + struct.pack("<i", new_id) + raw[id_offset + 4:]
    report.append(f"  unique ID       {old_id} -> {new_id}")

    # 2. names -- and ONLY the name fields the container opens with. Strings
    #    further in are resource names: jerseys and court textures are loaded by
    #    Resources.Load on exactly those strings, so rewriting one would leave
    #    the new team with no art at all. The leading chain is renamed; the rest
    #    of the blob keeps pointing at Portland's art, which is the whole point
    #    of a clone-based proof of concept.
    words = [w for w in re.split(r"[^A-Za-z]+", source_name) if len(w) >= 4]
    targets = []
    for off, text, _end in string_chain(raw):
        if any(w.lower() in text.lower() for w in words) and len(text) >= 4:
            targets.append((off, text))
    if not targets:
        raise PocError(
            f"None of the container's leading name fields mention "
            f"'{source_name}', so there's nothing to rename. Check the inspect "
            f"output for what they actually say.")
    for off, text in targets:
        fits = new_name[:len(text.encode("utf-8"))]
        raw = replace_string_in_place(raw, off, fits)
        report.append(f"  name @{off:<7} '{text}' -> '{fits.strip()}'")

    # 3. colors. The team's color tables are what the game reapplies to the
    #    court and to the team's UI tint on every load -- recoloring them is
    #    what makes the 33rd tile visibly NOT Portland at a glance. The logo
    #    artwork itself is a sprite this script doesn't replace.
    overrides = find_color_overrides(raw)
    if not overrides:
        raise PocError("No _Color_* overrides found in the container, so the "
                       "clone would be indistinguishable from the original. "
                       "Nothing was written.")
    params = sorted({p for _o, p, _v, _c in overrides})
    for off, _param, _variant, vals in overrides:
        raw = raw[:off] + struct.pack("<4f", rgba[0], rgba[1], rgba[2], vals[3]) \
            + raw[off + 16:]
    report.append(f"  colors          {len(overrides)} override(s) across "
                  f"{len(params)} propert(ies) set to #{args_color_hex(rgba)} "
                  f"(alpha kept): {', '.join(params)}")

    if len(raw) != size_before:
        raise PocError(f"The clone came out {len(raw)} bytes instead of "
                       f"{size_before} -- an edit moved something it shouldn't "
                       f"have. Nothing was written.")
    return raw


def verify(game_data, shared, level1, args):
    """Check the game files themselves for the change, and say which half (if
    either) is actually there.

    Worth its own mode because "I see no difference in game" has several very
    different causes: the apply never ran, it ran against a different folder,
    Steam restored the files afterwards, the clone landed but nothing points at
    it, or everything is in place and the game ignores it -- which would be a
    real finding about the game rather than a mistake. Each of those leaves a
    different fingerprint on disk.
    """
    import datetime

    print(f"Game folder : {game_data}")
    for path in (shared, level1):
        bak = path + BACKUP_SUFFIX
        st = os.stat(path)
        when = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        if os.path.exists(bak):
            bst = os.stat(bak)
            bwhen = datetime.datetime.fromtimestamp(bst.st_mtime).strftime("%Y-%m-%d %H:%M")
            same = "SAME SIZE as the backup" if bst.st_size == st.st_size \
                   else f"{st.st_size - bst.st_size:+,} bytes vs the backup"
            print(f"{os.path.basename(path):<22} {st.st_size:>14,} b  {when}   "
                  f"backup {bwhen}, {same}")
        else:
            print(f"{os.path.basename(path):<22} {st.st_size:>14,} b  {when}   "
                  f"NO {BACKUP_SUFFIX} -- --apply has never run on this folder")

    script_pid = find_container_script(game_data)
    containers = read_containers(game_data, script_pid)
    try:
        n_strings, ids = locate_unique_id(containers)
    except PocError as exc:
        print(f"\nCouldn't read the unique IDs: {exc}")
        ids = {}
    print(f"\nContainers  : {len(containers)} in {CONTAINER_ASSETS_FILE}")
    extra = {n: v for n, v in ids.items() if v == args.new_id}
    if extra:
        for name, v in extra.items():
            print(f"  clone     : FOUND -- '{name}' holds unique ID {v}")
    else:
        print(f"  clone     : NOT FOUND -- no container holds unique ID "
              f"{args.new_id}")

    # the pointer arrays, straight out of level1
    import UnityPy
    container_pids = {c["path_id"] for c in containers.values()}
    env = UnityPy.load(level1)
    arrays, pointed = [], False
    try:
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                raw = obj.get_raw_data()
            except Exception:
                continue
            for off, count, file_id in scan_pointer_arrays(raw, container_pids):
                arrays.append((obj.path_id, count))
                entries = [struct.unpack_from("<iq", raw, off + 4 + k * 12)[1]
                           for k in range(count)]
                if args.new_path_id in entries:
                    pointed = True
    finally:
        close_env(env)
    print(f"\nTeam lists in {MANAGER_SCENE_FILE}: "
          + (", ".join(f"{c} pointers" for _p, c in sorted(arrays,
                                                           key=lambda a: -a[1]))
             or "none found"))
    print(f"  33rd slot : "
          + ("FOUND -- a list points at the clone"
             if pointed else
             f"NOT FOUND -- no list points at path_id {args.new_path_id}"))

    biggest = max((c for _p, c in arrays), default=0)
    print("\nVerdict:")
    if extra and pointed:
        print("  Both halves are on disk. If the team-select grid still shows "
              "the stock tiles, the game is loading these files from somewhere "
              "else (a Steam re-verify restores them; check the mtimes above) "
              "or reading the team list from a file this script didn't touch.")
    elif extra and not pointed:
        print("  The clone exists but nothing points at it, so the game never "
              "sees it. The level1 half of the apply did not land.")
    elif not extra and biggest > 32:
        print("  A team list is longer than stock but the clone isn't in "
              f"{CONTAINER_ASSETS_FILE}: the pointer is dangling. Revert.")
    else:
        print("  Neither half is on disk. Either --apply was never run, it ran "
              "against a different folder than the one above, or the files "
              "were restored afterwards (Steam > Verify integrity does exactly "
              "that). Re-run with --apply and keep the console output.")
    return 0


def run(args):
    try:
        import UnityPy                                          # noqa: F401
    except ImportError:
        raise PocError("UnityPy is required. Run SETUP_AND_RUN.bat, or "
                       "`python -m pip install UnityPy`.")

    game_data = resolve_game_data(args.game_data)
    shared = os.path.join(game_data, CONTAINER_ASSETS_FILE)
    level1 = os.path.join(game_data, MANAGER_SCENE_FILE)

    if args.revert:
        import shutil
        done = []
        for path in (shared, level1):
            bak = path + BACKUP_SUFFIX
            if os.path.exists(bak):
                shutil.copy2(bak, path)
                done.append(os.path.basename(path))
        print("Restored: " + (", ".join(done) if done else "nothing -- no "
              f"{BACKUP_SUFFIX} files found."))
        return 0

    if args.verify:
        return verify(game_data, shared, level1, args)

    print(f"Game folder : {game_data}")
    script_pid = find_container_script(game_data)
    containers = read_containers(game_data, script_pid)
    print(f"Containers  : {len(containers)} BasketTeamContainer objects "
          f"(script path_id {script_pid})")

    # which one to clone
    matches = [n for n in containers if args.source.lower() in n.lower()]
    if len(matches) != 1:
        raise PocError(f"--source '{args.source}' matched {len(matches)} "
                       f"containers ({', '.join(sorted(matches)) or 'none'}). "
                       f"Available: {', '.join(sorted(containers))}")
    source_name = matches[0]
    source = containers[source_name]

    # the unique ID field
    if args.id_after_string is not None:
        n_strings = args.id_after_string
        ids = {name: struct.unpack_from("<i", c["raw"],
                                        id_offset_for(c["raw"], n_strings))[0]
               for name, c in containers.items()}
    else:
        n_strings, ids = locate_unique_id(containers)
    id_offset = id_offset_for(source["raw"], n_strings)
    print(f"Unique ID   : the int32 after the first {n_strings} string(s); "
          f"{source_name} = {ids[source_name]}, "
          f"range {min(ids.values())} .. {max(ids.values())}")

    if args.new_id in ids.values():
        owner = next(n for n, v in ids.items() if v == args.new_id)
        raise PocError(f"ID {args.new_id} is already {owner}'s. Pick another "
                       f"with --new-id.")

    # the pointer array in level1
    container_pids = {c["path_id"] for c in containers.values()}
    mgr_pid, mgr_raw, count_off, count, file_id = \
        find_bundled_team_array(game_data, container_pids)
    print(f"Team list   : BasketTeamManager path_id {mgr_pid}, "
          f"{count} pointers at +{count_off} (m_FileID {file_id})")

    # the clone
    rgba = hex_to_rgba(args.color)
    report = []
    new_path_id = args.new_path_id
    clone_raw = build_clone_bytes(source["raw"], id_offset, args.new_id,
                                  source_name, args.name, rgba, report)
    print(f"\nClone of '{source_name}' -> path_id {new_path_id}, "
          f"{len(clone_raw):,} bytes:")
    for line in report:
        print(line)

    if not args.apply:
        print("\nInspect only -- nothing was written. "
              "Re-run with --apply to make the change.")
        return 0

    if args.new_id in ids.values():                # belt and braces
        raise PocError("unique ID collision")

    print(f"\n{CONTAINER_ASSETS_FILE}: {backup(shared)}")
    print(f"{MANAGER_SCENE_FILE}: {backup(level1)}")

    print(f"Rebuilding {CONTAINER_ASSETS_FILE} with the clone added ...")
    def insert_clone(env):
        add_object(env, source["path_id"], new_path_id, clone_raw)
        return set()                       # no existing object is resized

    before, after = rebuild_file(shared, insert_clone, expect_added=1)
    print(f"  {before:,} objects -> {after:,}, every original one unchanged")

    print(f"Rebuilding {MANAGER_SCENE_FILE} with a {count + 1}th pointer ...")

    def grow_list(env):
        obj = next(o for o in env.objects if o.path_id == mgr_pid)
        raw = obj.get_raw_data()
        if raw[count_off:count_off + 4] != struct.pack("<i", count):
            raise PocError("level1 changed under us since the scan; re-run.")
        end = count_off + 4 + count * 12
        new = (raw[:count_off] + struct.pack("<i", count + 1)
               + raw[count_off + 4:end]
               + struct.pack("<iq", file_id, new_path_id)
               + raw[end:])
        obj.set_raw_data(new)
        return {mgr_pid}

    before, after = rebuild_file(level1, grow_list, expect_added=0)
    print(f"  {before:,} objects intact, the team manager grew by 12 bytes")

    print(f"\nDone. '{args.name.strip()}' should be the {count + 1}th tile on the "
          f"team-select screen.\n"
          f"Back up your SAVE before playing a match with it: removing the mod "
          f"afterwards can break loading of a save that references team "
          f"{args.new_id}.\n"
          f"To undo: python tools/team_slot_poc.py --revert")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Proof of concept: add a 33rd team by cloning an existing one.")
    p.add_argument("--game-data", help="the NBA Bounce_Data folder "
                                       "(default: whatever the manager is set to)")
    p.add_argument("--apply", action="store_true",
                   help="actually write; without this it only inspects")
    p.add_argument("--verify", action="store_true",
                   help="read the game files back and report whether the clone "
                        "and the 33rd pointer are actually there")
    p.add_argument("--revert", action="store_true",
                   help="restore the .teamslot_backup copies and exit")
    p.add_argument("--source", default=DEFAULT_SOURCE,
                   help=f"container to clone (default: {DEFAULT_SOURCE})")
    p.add_argument("--name", default=DEFAULT_NAME,
                   help=f"name for the new team (default: {DEFAULT_NAME}); it "
                        f"must fit in the bytes the old name occupied")
    p.add_argument("--color", default=DEFAULT_COLOR,
                   help=f"hex color for the new team (default: {DEFAULT_COLOR})")
    p.add_argument("--new-id", type=int, default=DEFAULT_NEW_ID,
                   help=f"unique team ID (default: {DEFAULT_NEW_ID})")
    p.add_argument("--new-path-id", type=int, default=90000001,
                   help="path_id for the cloned object in sharedassets1.assets")
    p.add_argument("--id-after-string", type=int,
                   help="how many leading strings come before the unique ID, "
                        "if the automatic search can't decide")
    args = p.parse_args(argv)

    try:
        return run(args)
    except PocError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
