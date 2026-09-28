"""
team_manager.py -- team slots for the NBA Bounce Mod Manager.

Self-contained, like every other module here: it imports nothing from app.py and
never calls apply_single_mod(). Entry points are plain functions returning plain
data, so team_tab.py owns all the tkinter and this file owns all the bytes.

WHAT IT DOES, AND HOW RISKY EACH PART IS
----------------------------------------
Three very different kinds of edit live here, and the difference matters:

  rename_team(), set_team_colors()      IN-PLACE, length-preserving byte patches.
                                        Work on stock teams and clones alike.
                                        Cannot move any other byte in the file,
                                        so queued texture/audio mods are safe.

  add_teams(), remove_added_teams()     REBUILD sharedassets1.assets and level1.
                                        Object contents are preserved, but every
                                        byte OFFSET in those files moves, so any
                                        mod already patched at a raw offset must
                                        be re-applied afterwards. The caller is
                                        handed that fact via the result's
                                        `rebuilt` list -- app.py re-applies the
                                        mod queue when it sees one.

  set_grid_scale(), set_grid_position() IN-PLACE patches of the grid strip's
                                        RectTransform in level2: size, and where
                                        it sits on the select screen.

  set_team_order()                      IN-PLACE rewrite of m_aoBundledTeams in
                                        level1 (same entries, new order). Only
                                        visible once the grid's nickname sort is
                                        switched off in the game's code.

  reset_team_changes()                  RESTORES every team file from its stock
                                        backup, puts the grid back, and undoes
                                        code patches. repair_save_teams() then
                                        neutralises saved entries for teams that
                                        no longer exist.

  nav_report() / nav_patch() /          EDITS GAME CODE (Assembly-CSharp.dll),
  apply_code_layout() / nav_revert()    not assets. Opt-in only. The last row of
                                        the team-select grid, the column count
                                        and the nickname sort are all constants
                                        in code: past 32 teams, pressing Down in
                                        the middle of the grid jumps to Random,
                                        and neither columns nor order can change
                                        without these same-length byte swaps. A
                                        Steam update or Verify Integrity
                                        silently undoes them.

Every write is verified: in-place patches read their target back off disk before
and after, and rebuilds are checked object-by-object against the original file
and thrown away unless every pre-existing object survived at its original size.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import struct

CONTAINER_ASSETS_FILE = "sharedassets1.assets"
MANAGER_SCENE_FILE    = "level1"
GRID_SCENE_FILE       = "level2"
CONTAINER_SCRIPT_CLASS = "BasketTeamContainer"

TEAM_BACKUP   = ".teamslot_backup"     # sharedassets1.assets + level1
SCALE_BACKUP  = ".gridscale_backup"    # level2
NAV_BACKUP    = ".navpatch_backup"     # Assembly-CSharp.dll
DLL_REL       = os.path.join("Managed", "Assembly-CSharp.dll")

GRID_GO_PATH_ID = 74641        # 'GridLayOut' in level2, parent of the tile grid
FIRST_CLONE_ID       = -101    # unique IDs for added teams run downward
FIRST_CLONE_PATH_ID  = 90000001
FIRST_ART_PATH_ID    = 91000001    # cloned textures and sprites
CUSTOM_ART_PREFIX    = "CUSTOM_"   # every copied texture/sprite is renamed with
                                   # this, so the Textures tab's search box
                                   # finds them in one keystroke instead of the
                                   # user hunting for a second "PSuns_Global"
                                   # among 2,000 identically-named originals.
# Stock teams are -1 .. -38 and 00_Empty is 0, but teams this app adds start at
# -101 and run downward -- so the accepted range has to be wide enough to hold
# them. Too narrow a range and the field stops being recognised as soon as the
# added teams outnumber a fifth of the roster.
ID_RANGE = (-4096, -1)

RETRO_TEAM_COUNT = 4           # appended to the 9-column matrix by the DLC


class TeamError(Exception):
    """Anything the user should see as a sentence rather than a traceback."""


# ---------------------------------------------------------------------------
# Serialized-bytes helpers (same conventions app.py already uses)
# ---------------------------------------------------------------------------

def read_str(raw, off):
    (n,) = struct.unpack_from("<i", raw, off)
    if not (0 <= n <= 4096) or off + 4 + n > len(raw):
        raise ValueError(f"not a string at {off}")
    return raw[off + 4:off + 4 + n].decode("utf-8", "replace"), (off + 4 + n + 3) & ~3


def script_path_id(raw):
    """m_Script PPtr: m_GameObject PPtr (12) + m_Enabled + 3 pad = offset 16."""
    return struct.unpack_from("<iq", raw, 16)[1]


def close_env(env):
    """Release every OS handle an Environment holds.

    Clearing env.files is not enough: each SerializedFile owns a reader with its
    own stream, and on Windows one unclosed handle makes a rename fail in a way
    that looks exactly like Steam holding the file open.
    """
    import gc
    if env is None:
        return

    def shut(obj):
        for attr in ("Stream", "stream", "_buf", "fp"):
            st = getattr(obj, attr, None)
            if st is not None and hasattr(st, "close"):
                try:
                    st.close()
                except Exception:
                    pass

    for holder in ("files", "cabs"):
        table = getattr(env, holder, None)
        if not isinstance(table, dict):
            continue
        for sf in list(table.values()):
            shut(sf)
            shut(getattr(sf, "reader", None))
            shut(getattr(sf, "assetsfile", None))
        try:
            table.clear()
        except Exception:
            pass
    shut(env)
    shut(getattr(env, "reader", None))
    gc.collect()


def string_chain(raw, start=28, limit=24):
    """The run of length-prefixed strings a container opens with, as
    [(offset, text, offset_past_padding)]. Stops where the fixed-width fields
    (the unique ID among them) begin."""
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


def find_color_overrides(raw):
    """Every per-team color override: [(offset_in_object, property, variant, rgba)].

    Same structure the Court Colors tab already reads:
        m_sMaterialColorParam     : int32 len + bytes, padded to 4
        m_aoRetroExtraColorParams : int32 count
            count x { key: int32 len + bytes padded to 4 ; value: 4 x float32 }
    """
    out, n = [], len(raw)
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


def hex_to_rgba(text, alpha=1.0):
    text = str(text).lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        raise TeamError(f"'{text}' is not a six-digit hex color (e.g. 00c853).")
    r, g, b = (int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (r, g, b, alpha)


def rgba_to_hex(rgba):
    return "".join(f"{max(0, min(255, int(round(c * 255)))):02x}" for c in rgba[:3])


def spread_colors(n, base_hex):
    """n distinct RGBAs walking the hue wheel from base_hex."""
    import colorsys
    r, g, b, a = hex_to_rgba(base_hex)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return [colorsys.hsv_to_rgb((h + i / max(n, 1)) % 1.0,
                                max(s, 0.65), max(v, 0.75)) + (a,)
            for i in range(n)]


# ---------------------------------------------------------------------------
# Reading the game
# ---------------------------------------------------------------------------

def _require(game_data, *names):
    for name in names:
        path = os.path.join(game_data, name)
        if not os.path.isfile(path):
            raise TeamError(f"{name} is not in this folder. Point the app at "
                            f"the NBA Bounce_Data folder first.")
    return [os.path.join(game_data, n) for n in names]


def find_container_script(game_data):
    """MonoScript path_id for BasketTeamContainer, looked up rather than
    hardcoded so a game update cannot point us at the wrong script."""
    import UnityPy
    env = UnityPy.load(os.path.join(game_data, "globalgamemanagers.assets"))
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
    raise TeamError("Could not find the BasketTeamContainer script in "
                    "globalgamemanagers.assets.")


def _read_containers(game_data, script_pid):
    """{name: {"path_id", "raw", "byte_start"}} for every BasketTeamContainer."""
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
            start = getattr(obj, "byte_start", None)
            out[name] = {"path_id": obj.path_id, "raw": raw, "byte_start": start}
    finally:
        close_env(env)
    if not out:
        raise TeamError("No team containers found in sharedassets1.assets.")
    return out


def locate_unique_id(containers):
    """Which position in the field order holds the team's unique ID.

    It is not at a fixed byte offset -- the name in front of it is
    variable-length -- so the search is anchored to "the int32 after the Nth
    string". 00_Empty holds 0 rather than a negative, so a position qualifies on
    the clear majority being distinct small negatives, not all of them.
    """
    lo, hi = ID_RANGE
    chains = {name: string_chain(c["raw"]) for name, c in containers.items()}
    depth = min((len(ch) for ch in chains.values()), default=0)
    if depth == 0:
        raise TeamError("No container starts with a readable name; the layout "
                        "is not what this app expects.")
    good = []
    for n in range(1, depth + 1):
        values = []
        for name, chain in chains.items():
            end, raw = chain[n - 1][2], containers[name]["raw"]
            if end + 4 > len(raw):
                values = []
                break
            values.append(struct.unpack_from("<i", raw, end)[0])
        if len(values) != len(chains) or len(set(values)) != len(values):
            continue
        if sum(1 for v in values if lo <= v <= hi) >= max(4, int(len(values) * 0.8)):
            good.append(n)
    if not good:
        # Say what was actually seen. "The game may have been updated" is a
        # guess, and it sent us looking in the wrong place once already.
        sample = []
        for n in range(1, min(depth, 3) + 1):
            vals = []
            for name, chain in chains.items():
                end, raw = chain[n - 1][2], containers[name]["raw"]
                if end + 4 <= len(raw):
                    vals.append(struct.unpack_from("<i", raw, end)[0])
            if vals:
                neg = sum(1 for v in vals if lo <= v <= hi)
                sample.append(f"after string {n}: {len(set(vals))}/{len(vals)} "
                              f"distinct, {neg} in range, "
                              f"min {min(vals)} max {max(vals)}")
        raise TeamError(
            "Could not locate the unique-ID field across "
            f"{len(chains)} teams. What was found — "
            + "; ".join(sample)
            + ". Run tools/container_dump.py and send the output.")
    return good[0]


def id_offset_for(raw, n_strings):
    chain = string_chain(raw)
    if len(chain) < n_strings:
        raise TeamError("This container has fewer leading strings than expected.")
    return chain[n_strings - 1][2]


def scan_pointer_arrays(raw, container_pids):
    """Every PPtr array in an object whose entries all point at known
    containers, as [(offset_of_count, count, file_id)]. A serialized PPtr array
    is an int32 count followed by count x (int32 m_FileID, int64 m_PathID);
    requiring every entry to resolve to a container identifies the team lists
    without a type tree."""
    out = []
    for i in range(0, len(raw) - 4, 4):
        (count,) = struct.unpack_from("<i", raw, i)
        if not (2 <= count <= 256) or i + 4 + count * 12 > len(raw):
            continue
        entries = [struct.unpack_from("<iq", raw, i + 4 + k * 12) for k in range(count)]
        pids = [pid for _fid, pid in entries]
        if len({fid for fid, _p in entries}) != 1 or len(set(pids)) != count:
            continue
        if all(p in container_pids for p in pids):
            out.append((i, count, entries[0][0]))
    return out


def find_bundled_team_array(game_data, container_pids):
    """(manager_path_id, raw, offset_of_count, count, file_id) for the longest
    team list in level1 -- m_aoBundledTeams."""
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
            for off, count, fid in scan_pointer_arrays(raw, container_pids):
                if best is None or count > best[3]:
                    best = (obj.path_id, raw, off, count, fid)
    finally:
        close_env(env)
    if best is None:
        raise TeamError("Could not find the bundled-team list in level1.")
    return best


def list_teams(game_data):
    """Everything the UI needs about every team, newest clones last.

    Returns {"teams": [...], "bundled_count": int, "id_after_string": int}
    where each team is
        {name, path_id, unique_id, colors: {property: hex}, added: bool,
         name_capacity: int, in_list: bool}
    """
    _require(game_data, CONTAINER_ASSETS_FILE, MANAGER_SCENE_FILE)
    script_pid = find_container_script(game_data)
    containers = _read_containers(game_data, script_pid)
    n_strings = locate_unique_id(containers)

    pids = {c["path_id"] for c in containers.values()}
    _mgr, _raw, _off, count, _fid = find_bundled_team_array(game_data, pids)

    teams = []
    for name, c in sorted(containers.items(), key=lambda kv: kv[1]["path_id"]):
        raw = c["raw"]
        try:
            uid = struct.unpack_from("<i", raw, id_offset_for(raw, n_strings))[0]
        except Exception:
            uid = None
        colors = {}
        for _off, param, variant, rgba in find_color_overrides(raw):
            if variant == "default" or param not in colors:
                colors[param] = rgba_to_hex(rgba)
        try:
            ident = {f: {"text": t, "capacity": cap}
                     for f, _o, t, cap in parse_identity(raw)}
            ident_error = None
        except (TeamError, ValueError, struct.error) as exc:
            ident = {}
            ident_error = str(exc)
        teams.append({
            "name": name,
            "path_id": c["path_id"],
            "unique_id": uid,
            "colors": colors,
            "added": c["path_id"] >= FIRST_CLONE_PATH_ID,
            "identity": ident,
            "identity_error": ident_error,
            "name_capacity": ident.get("asset_name", {}).get("capacity", 0),
        })
    return {"teams": teams, "bundled_count": count, "id_after_string": n_strings}


# ---------------------------------------------------------------------------
# In-place edits: rename and recolor. Safe for stock teams and clones alike.
# ---------------------------------------------------------------------------

def _patch_in_place(path, edits, what):
    """edits: [(absolute_offset, expected_bytes, new_bytes)].

    Every offset is checked against what it is expected to hold BEFORE anything
    is written, so a stale offset aborts cleanly instead of corrupting the file.
    All edits are the same length as what they replace, so the file size cannot
    change -- asserted afterwards.
    """
    if not edits:
        return 0
    size_before = os.path.getsize(path)
    with open(path, "rb") as f:
        for off, expect, new in edits:
            if len(expect) != len(new):
                raise TeamError("internal: edit would change the file length.")
            f.seek(off)
            got = f.read(len(expect))
            if got != expect:
                raise TeamError(
                    f"{what} has moved since it was scanned (offset {off}). "
                    f"Reopen the Teams tab to rescan, then try again.")
    with open(path, "r+b") as f:
        for off, _expect, new in edits:
            f.seek(off)
            f.write(new)
    with open(path, "rb") as f:
        for off, _expect, new in edits:
            f.seek(off)
            if f.read(len(new)) != new:
                raise TeamError(f"{what}: the write did not read back correctly.")
    if os.path.getsize(path) != size_before:
        raise TeamError("File size changed -- this should be impossible.")
    return len(edits)


def rename_team(game_data, path_id, new_name):
    """Rename a team in place. The replacement must fit in the bytes the old
    name occupies: a longer string would shift every byte after it."""
    assets = os.path.join(game_data, CONTAINER_ASSETS_FILE)
    script_pid = find_container_script(game_data)
    containers = _read_containers(game_data, script_pid)
    target = next((c for c in containers.values() if c["path_id"] == path_id), None)
    if target is None:
        raise TeamError(f"No team with path_id {path_id}.")
    if target["byte_start"] is None:
        raise TeamError("Could not locate this container in the file.")

    raw = target["raw"]
    (slen,) = struct.unpack_from("<i", raw, 28)
    encoded = new_name.encode("utf-8")
    if len(encoded) > slen:
        raise TeamError(
            f"'{new_name}' is {len(encoded)} bytes and the name field holds "
            f"{slen}. Names are rewritten in place, so a longer one would move "
            f"every byte after it. Use at most {slen} characters.")
    old = raw[32:32 + slen]
    new = encoded.ljust(slen, b" ")
    if old == new:
        return 0
    _patch_in_place(assets, [(target["byte_start"] + 32, old, new)], "The team name")
    return 1


def set_team_colors(game_data, path_id, colors, variants=None):
    """colors: {property_name: "rrggbb"}. Alpha is always preserved -- the game
    uses it, and a hex color has nothing to say about it.

    variants=None edits every variant of each property (the normal court plus
    every retro era); pass a set of variant keys to narrow it."""
    assets = os.path.join(game_data, CONTAINER_ASSETS_FILE)
    script_pid = find_container_script(game_data)
    containers = _read_containers(game_data, script_pid)
    target = next((c for c in containers.values() if c["path_id"] == path_id), None)
    if target is None:
        raise TeamError(f"No team with path_id {path_id}.")
    if target["byte_start"] is None:
        raise TeamError("Could not locate this container in the file.")

    edits = []
    for off, param, variant, rgba in find_color_overrides(target["raw"]):
        if param not in colors:
            continue
        if variants is not None and variant not in variants:
            continue
        r, g, b, _a = hex_to_rgba(colors[param])
        new = struct.pack("<4f", r, g, b, rgba[3])
        old = struct.pack("<4f", *rgba)
        if new != old:
            edits.append((target["byte_start"] + off, old, new))
    return _patch_in_place(assets, edits, "Team color data")



# ---------------------------------------------------------------------------
# Team identity: the names the game actually shows
# ---------------------------------------------------------------------------
#
# A container opens with a fixed run of fields, confirmed against Portland,
# Boston and Chicago in tools/container_dump.py:
#
#     +28   m_Name          "19_Portland Trail Blazers"   asset name, internal
#           int32           unique team ID
#           city            "Portland Trail Blazers"      <- the VS panel's top line
#           nickname        "Trail Blazers"               <- the VS panel's second line
#           int32           (division/conference)
#           abbreviation    "POR"                         <- scoreboard
#           abbreviation    "POR"                         stored TWICE, and the
#                                                         game reads both
#
# Renaming only m_Name changes nothing the player can see, which is why a
# renamed clone still said PHOENIX SUNS on the scoreboard. These are the fields
# that matter, and the abbreviation has to be written to both slots.

IDENTITY_FIELDS = ("asset_name", "city", "nickname", "abbr", "abbr2")


def parse_identity(raw):
    """[(field, offset, text, capacity)] for the five name fields.

    capacity is how many bytes the text may occupy IN PLACE: a string takes
    4 + len padded to 4, so anything that keeps the same padded footprint fits.
    """
    def grab(field, off):
        try:
            return read_str(raw, off)
        except ValueError:
            raise TeamError(
                f"Could not read the '{field}' field at byte {off}: this "
                f"container does not use the expected team layout.")

    out, off = [], 28
    name, off = grab("asset_name", off)
    fields = [("asset_name", 28, name)]
    off += 4                                    # unique ID
    for field in ("city", "nickname"):
        start = off
        text, off = grab(field, start)
        fields.append((field, start, text))
    off += 4                                    # division / conference
    for field in ("abbr", "abbr2"):
        start = off
        text, off = grab(field, start)
        if len(text) > 8:
            raise TeamError(f"Expected a short abbreviation but found "
                            f"'{text[:16]}'. The container layout is not what "
                            f"this app expects.")
        fields.append((field, start, text))
    for field, start, text in fields:
        if start + 4 > len(raw):
            raise TeamError(f"Container is truncated at byte {start}.")
        (slen,) = struct.unpack_from("<i", raw, start)
        total = (4 + slen + 3) & ~3
        out.append((field, start, text, total - 4))
    return out


def read_identity(game_data, path_id):
    """{field: {"text", "offset", "capacity"}} for one team."""
    script_pid = find_container_script(game_data)
    containers = _read_containers(game_data, script_pid)
    target = next((c for c in containers.values() if c["path_id"] == path_id), None)
    if target is None:
        raise TeamError(f"No team with path_id {path_id}.")
    return {f: {"text": t, "offset": o, "capacity": cap}
            for f, o, t, cap in parse_identity(target["raw"])}


def _string_bytes(text):
    enc = text.encode("utf-8")
    pad = ((4 + len(enc) + 3) & ~3) - 4 - len(enc)
    return struct.pack("<i", len(enc)) + enc + b"\x00" * pad


def set_identity(game_data, path_id, **values):
    """Rewrite the visible names in place.

    values: any of asset_name, city, nickname, abbr. Passing abbr writes BOTH
    abbreviation slots, because the game reads both and letting them disagree
    would show one name on the scoreboard and another elsewhere.

    Each field must fit the bytes it already occupies -- these are in-place
    edits, and a longer string would move every byte after it. Anything too long
    is refused by name with its budget, rather than silently truncated.
    """
    assets = os.path.join(game_data, CONTAINER_ASSETS_FILE)
    script_pid = find_container_script(game_data)
    containers = _read_containers(game_data, script_pid)
    target = next((c for c in containers.values() if c["path_id"] == path_id), None)
    if target is None:
        raise TeamError(f"No team with path_id {path_id}.")
    if target["byte_start"] is None:
        raise TeamError("Could not locate this team in the file.")

    if "abbr" in values and "abbr2" not in values:
        values["abbr2"] = values["abbr"]

    current = {f: (o, t, cap) for f, o, t, cap in parse_identity(target["raw"])}
    edits = []
    for field, new_text in values.items():
        if new_text is None or field not in current:
            continue
        off, old_text, cap = current[field]
        if old_text == new_text:
            continue
        enc = new_text.encode("utf-8")
        if len(enc) > cap:
            label = {"asset_name": "Asset name", "city": "City",
                     "nickname": "Nickname", "abbr": "Abbreviation",
                     "abbr2": "Abbreviation"}.get(field, field)
            raise TeamError(
                f"{label} '{new_text}' needs {len(enc)} bytes and this team's "
                f"slot holds {cap}. Existing teams are edited in place, so a "
                f"longer name would move every byte after it. Teams you ADD can "
                f"use any length -- set it in the Add Teams dialog.")
        old_bytes = _string_bytes(old_text)
        new_bytes = _string_bytes(new_text)
        if len(new_bytes) != len(old_bytes):
            # Same 4-byte bucket, so the footprint matches; pad to be certain.
            new_bytes = new_bytes.ljust(len(old_bytes), b"\x00")
        edits.append((target["byte_start"] + off, old_bytes, new_bytes))
    return _patch_in_place(assets, edits, "Team names")


def _resize_strings(raw, changes):
    """Rewrite length-prefixed strings that may change length.

    Only safe on a NEW object: the rebuild recalculates object sizes on save, so
    a clone's strings are not bound by the donor's footprint. Edits are applied
    from the END backwards so each offset is still valid when it is used.
    """
    for off, new_text in sorted(changes, key=lambda c: -c[0]):
        (slen,) = struct.unpack_from("<i", raw, off)
        old_total = 4 + ((slen + 3) & ~3) if False else ((4 + slen + 3) & ~3)
        raw = raw[:off] + _string_bytes(new_text) + raw[off + old_total:]
    return raw


# ---------------------------------------------------------------------------
# Rebuilds: adding and removing teams
# ---------------------------------------------------------------------------

def backup(path):
    bak = path + TEAM_BACKUP
    if os.path.exists(bak):
        return False
    shutil.copy2(path, bak)
    return True


def _replace_file(tmp, dest, data):
    """Put a rebuilt file in place despite Windows file locking.

    First the clean way: rename the verified temp file over the original. If
    that is refused, the lock may be on the TEMP file -- a reader of our own
    that has not let go -- so the second attempt writes the already-verified
    bytes straight into the destination, which needs no handle on the temp at
    all. Only if that fails too is the game file genuinely held by something.
    """
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
        with open(dest, "r+b") as f:
            f.write(data)
            f.truncate()
        try:
            os.remove(tmp)
        except OSError:
            pass
        return
    except OSError as exc:
        last = exc
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise TeamError(
        f"'{os.path.basename(dest)}' is open in another program, so the "
        f"rebuilt file could not be put in place. Close NBA Bounce and quit "
        f"Steam, then try again. Your game file was left untouched.\n\n({last})")


def _rebuild(path, edit, expect_added=0, progress=None):
    """Re-serialize one file after edit(env) has changed it.

    The rebuilt file is written to a temp name, read back, and compared against
    the original object table before it is allowed near the game folder: every
    object that was there must still be there at its original size, and nothing
    unexpected may have appeared. Anything off and the temp file is dropped with
    the game file untouched.
    """
    import UnityPy
    if progress:
        progress(f"Reading {os.path.basename(path)} ...")
    env = UnityPy.load(path)
    before = {o.path_id: o.byte_size for o in env.objects}
    changed = edit(env)
    if progress:
        progress(f"Rebuilding {os.path.basename(path)} ...")
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
        raise TeamError(f"The rebuilt {os.path.basename(path)} could not be "
                        f"read back ({exc}). Your game file was left alone.")
    finally:
        close_env(verify)

    lost = set(before) - set(after)
    gained = set(after) - set(before)
    resized = [p for p in before
               if p in after and after[p] != before[p] and p not in changed]
    if lost or resized or len(gained) != expect_added:
        os.remove(tmp)
        raise TeamError(
            f"Rebuilding {os.path.basename(path)} would have changed "
            f"{len(lost)} lost / {len(gained)} added (expected {expect_added}) "
            f"/ {len(resized)} resized object(s), so it was abandoned and your "
            f"game file was left alone.")

    if progress:
        progress(f"Installing {os.path.basename(path)} ...")
    _replace_file(tmp, path, data)
    return len(before), len(after)


def _add_object(env, template_path_id, new_path_id, raw):
    """Register a new object: a copy of an existing object's reader, re-pointed
    at a new path_id and carrying new bytes. Copying a real reader is what keeps
    the type metadata correct -- the clone is the same MonoBehaviour type as the
    container it came from, which is what the game expects behind the pointer."""
    src = next(o for o in env.objects if o.path_id == template_path_id)
    clone = copy.copy(src)
    clone.path_id = new_path_id
    try:
        clone.set_raw_data(raw)
    except AttributeError as exc:
        raise TeamError(f"This UnityPy version cannot set object bytes ({exc}); "
                        f"upgrade UnityPy.") from exc
    objects = env.file.objects
    if new_path_id in objects:
        raise TeamError(f"path_id {new_path_id} is already taken.")
    objects[new_path_id] = clone
    if hasattr(env.file, "mark_changed"):
        env.file.mark_changed()
    return clone


def _build_clone_bytes(raw, id_offset, new_id, identity, rgba):
    """The cloned container's bytes: new ID, new names, new colors.

    The donor's byte layout does NOT constrain this. The clone is a brand-new
    object and the rebuild recalculates object sizes on save, so its names can
    be any length -- which is what makes "1996 Chicago Bulls" possible on an
    added team while an in-place rename of a shipped team cannot grow at all.

    Order matters. The unique ID is written first, while its offset is still
    valid; then the strings are resized from the end backwards so each offset
    is still correct when it is used; then the colors are located in the
    finished bytes rather than the original.
    """
    raw = raw[:id_offset] + struct.pack("<i", new_id) + raw[id_offset + 4:]

    fields = {f: (o, t) for f, o, t, _cap in parse_identity(raw)}
    changes = []
    for field in IDENTITY_FIELDS:
        want = identity.get("abbr" if field == "abbr2" else field)
        if want:
            changes.append((fields[field][0], want))
    if changes:
        raw = _resize_strings(raw, changes)

    overrides = find_color_overrides(raw)
    if not overrides:
        raise TeamError("No color overrides found in the source team, so the "
                        "copy would be indistinguishable from it.")
    for off, _param, _variant, vals in overrides:
        raw = raw[:off] + struct.pack("<4f", rgba[0], rgba[1], rgba[2], vals[3]) \
            + raw[off + 16:]
    return raw


def add_teams(game_data, source_name, count=1, base_name="New Team",
              base_color="00c853", city=None, nickname=None, abbr=None,
              progress=None):
    """Clone `source_name` `count` times and hang pointers off the team manager.

    Returns {"added": n, "names": [...], "bundled_count": int, "rebuilt": [paths]}.
    `rebuilt` is the caller's cue that byte offsets moved and queued mods need
    re-applying.
    """
    shared, level1 = _require(game_data, CONTAINER_ASSETS_FILE, MANAGER_SCENE_FILE)
    if count < 1:
        raise TeamError("Count must be at least 1.")

    script_pid = find_container_script(game_data)
    containers = _read_containers(game_data, script_pid)
    matches = [n for n in containers if source_name.lower() in n.lower()]
    if len(matches) != 1:
        raise TeamError(f"'{source_name}' matched {len(matches)} teams. "
                        f"Pick exactly one to copy from.")
    source = containers[matches[0]]
    n_strings = locate_unique_id(containers)
    id_offset = id_offset_for(source["raw"], n_strings)

    used_ids = set()
    for c in containers.values():
        try:
            used_ids.add(struct.unpack_from(
                "<i", c["raw"], id_offset_for(c["raw"], n_strings))[0])
        except Exception:
            continue
    used_pids = {c["path_id"] for c in containers.values()}

    palette = spread_colors(count, base_color)
    clones, uid, pid = [], FIRST_CLONE_ID, FIRST_CLONE_PATH_ID
    for i in range(count):
        while uid in used_ids:
            uid -= 1
        while pid in used_pids:
            pid += 1
        suffix = "" if count == 1 else f" {i + 1}"
        name = base_name + suffix
        identity = {
            "asset_name": name,
            "city": (city + suffix) if city else name,
            "nickname": (nickname + suffix) if nickname else name,
            "abbr": abbr or "".join(w[0] for w in name.split()[:3]).upper()[:3],
        }
        clones.append({
            "id": uid, "path_id": pid, "name": name,
            "raw": _build_clone_bytes(source["raw"], id_offset, uid,
                                      identity, palette[i]),
        })
        used_ids.add(uid)
        used_pids.add(pid)

    mgr_pid, mgr_raw, count_off, listed, file_id = \
        find_bundled_team_array(game_data, used_pids | {c["path_id"] for c in clones})

    if progress:
        progress("Backing up game files ...")
    backup(shared)
    backup(level1)

    def insert(env):
        for c in clones:
            _add_object(env, source["path_id"], c["path_id"], c["raw"])
        return set()

    _rebuild(shared, insert, expect_added=count, progress=progress)

    def grow(env):
        obj = next(o for o in env.objects if o.path_id == mgr_pid)
        raw = obj.get_raw_data()
        if raw[count_off:count_off + 4] != struct.pack("<i", listed):
            raise TeamError("level1 changed while this ran; try again.")
        end = count_off + 4 + listed * 12
        added = b"".join(struct.pack("<iq", file_id, c["path_id"]) for c in clones)
        obj.set_raw_data(raw[:count_off] + struct.pack("<i", listed + count)
                         + raw[count_off + 4:end] + added + raw[end:])
        return {mgr_pid}

    _rebuild(level1, grow, expect_added=0, progress=progress)

    return {"added": count, "names": [c["name"] for c in clones],
            "bundled_count": listed + count, "rebuilt": [shared, level1]}


def remove_added_teams(game_data, progress=None):
    """Put back the pre-clone backups of sharedassets1.assets and level1.

    A restore rather than a surgical removal on purpose: the game looks up every
    saved team ID with no missing-key check, and the backup is a known-good
    state rather than one this app reasoned its way to.
    """
    shared, level1 = _require(game_data, CONTAINER_ASSETS_FILE, MANAGER_SCENE_FILE)
    restored = []
    for path in (shared, level1):
        bak = path + TEAM_BACKUP
        if os.path.exists(bak):
            if progress:
                progress(f"Restoring {os.path.basename(path)} ...")
            shutil.copy2(bak, path)
            restored.append(path)
    if not restored:
        raise TeamError("There are no added teams to remove -- no "
                        f"{TEAM_BACKUP} files exist in this folder.")
    return {"rebuilt": restored}


# ---------------------------------------------------------------------------
# Team-select grid scale (level2, in place)
# ---------------------------------------------------------------------------

def _grid_transform(game_data):
    """(transform path_id, raw, byte_start, scale) for the grid container.

    The GridLayoutGroup's own cellSize and column count are NOT usable: the game
    sets both at runtime from SelectTeamBehaviour.Initialize, so edits to them
    are overwritten before the first frame. The container's scale is not touched
    by that code, which is why this works where the obvious edit does not.
    """
    import UnityPy
    scene = os.path.join(game_data, GRID_SCENE_FILE)
    env = UnityPy.load(scene)
    hit = None
    try:
        for obj in env.objects:
            if obj.type.name not in ("Transform", "RectTransform"):
                continue
            try:
                d = obj.read()
            except Exception:
                continue
            go = getattr(getattr(d, "m_GameObject", None), "path_id", None)
            if go != GRID_GO_PATH_ID:
                continue
            scale = getattr(d, "m_LocalScale", None)
            scale = (scale.x, scale.y, scale.z) if scale is not None else None
            hit = (obj.path_id, obj.get_raw_data(),
                   getattr(obj, "byte_start", None), scale)
    finally:
        close_env(env)
    if hit is None:
        raise TeamError("Could not find the team-select grid in level2.")
    return hit


def _scale_offset(raw, scale):
    """Where m_LocalScale sits, proved against what UnityPy read rather than
    assumed. Refuses on an ambiguous match."""
    if scale is None:
        raise TeamError("Could not read the grid's scale.")
    packed = struct.pack("<3f", *scale)
    if len(raw) >= 52 and raw[40:52] == packed:      # the conventional spot
        return 40
    hits, i = [], raw.find(packed)
    while i != -1:
        hits.append(i)
        i = raw.find(packed, i + 1)
    if len(hits) != 1:
        raise TeamError("The grid's scale field could not be located "
                        "unambiguously; refusing to write.")
    return hits[0]


def get_grid_scale(game_data):
    _require(game_data, GRID_SCENE_FILE)
    _pid, _raw, _start, scale = _grid_transform(game_data)
    return scale[0] if scale else 1.0


def set_grid_scale(game_data, scale):
    """Shrink or restore the team-select grid. 1.0 is stock."""
    if not (0.2 <= float(scale) <= 2.0):
        raise TeamError("Grid scale must be between 0.2 and 2.0.")
    scene = os.path.join(game_data, GRID_SCENE_FILE)
    _pid, raw, start, current = _grid_transform(game_data)
    if start is None:
        raise TeamError("Could not locate the grid object in level2.")
    off = _scale_offset(raw, current)
    old = raw[off:off + 12]
    new = struct.pack("<3f", float(scale), float(scale), float(scale))
    if old == new:
        return 0
    bak = scene + SCALE_BACKUP
    if not os.path.exists(bak):
        shutil.copy2(scene, bak)
    _patch_in_place(scene, [(start + off, old, new)], "The team-select grid")
    return 1


# ---------------------------------------------------------------------------
# Team-select grid position (level2, in place)
# ---------------------------------------------------------------------------
#
# GridLayOut is a full-width strip across the bottom of the 1920x1080 select
# screen (anchors stretch 0..1, sizeDelta.y -452.75, anchoredPosition.y
# -226.38, so it covers the bottom 628 px). Its HorizontalLayoutGroup centres
# the tile grid inside that strip. Nothing in the game's code writes this
# RectTransform's position, so moving the strip moves the whole grid.

STOCK_GRID_POSITION = (0.0, -226.37564086914062)
CANVAS_SIZE = (1920.0, 1080.0)
GRID_STRIP_HEIGHT = 1080.0 - 452.7513122558594     # stock sizeDelta.y is -452.75


def _anchored_offset(raw, pos, anchor_min, anchor_max):
    """Byte offset of m_AnchoredPosition inside a RectTransform, worked out
    from the serialized layout and then PROVED against the values UnityPy read:

        m_GameObject 12 | rotation 16 | position 12 | scale 12 |
        m_Children (int32 count + 12 each) | m_Father 12 |
        m_AnchorMin 8 | m_AnchorMax 8 | m_AnchoredPosition 8 | ...
    """
    if len(raw) < 56:
        raise TeamError("The grid's transform is shorter than expected.")
    (n_children,) = struct.unpack_from("<i", raw, 52)
    off = 52 + 4 + n_children * 12 + 12
    want = struct.pack("<6f", anchor_min[0], anchor_min[1],
                       anchor_max[0], anchor_max[1], pos[0], pos[1])
    if raw[off:off + 24] != want:
        raise TeamError("The grid's position field is not where this app "
                        "expects it; refusing to write.")
    return off + 16


def _grid_rect(game_data):
    """(raw, byte_start, offset_of_anchored_position, (x, y), scale)."""
    import UnityPy
    env = UnityPy.load(os.path.join(game_data, GRID_SCENE_FILE))
    hit = None
    try:
        for obj in env.objects:
            if obj.type.name != "RectTransform":
                continue
            try:
                d = obj.read()
            except Exception:
                continue
            if getattr(getattr(d, "m_GameObject", None), "path_id", None) != GRID_GO_PATH_ID:
                continue
            pos = (d.m_AnchoredPosition.x, d.m_AnchoredPosition.y)
            raw = obj.get_raw_data()
            off = _anchored_offset(raw, pos, (d.m_AnchorMin.x, d.m_AnchorMin.y),
                                   (d.m_AnchorMax.x, d.m_AnchorMax.y))
            hit = (raw, getattr(obj, "byte_start", None), off, pos,
                   d.m_LocalScale.x)
    finally:
        close_env(env)
    if hit is None:
        raise TeamError("Could not find the team-select grid in level2.")
    return hit


def get_grid_position(game_data):
    """(x, y) of the grid strip. Stock is STOCK_GRID_POSITION."""
    _require(game_data, GRID_SCENE_FILE)
    return _grid_rect(game_data)[3]


def set_grid_position(game_data, x, y):
    """Move the whole grid. Values are canvas pixels on the 1920x1080 screen."""
    x, y = float(x), float(y)
    if not (-960.0 <= x <= 960.0 and -900.0 <= y <= 500.0):
        raise TeamError("That would put the grid off the screen.")
    scene = os.path.join(game_data, GRID_SCENE_FILE)
    raw, start, off, _cur, _scale = _grid_rect(game_data)
    if start is None:
        raise TeamError("Could not locate the grid object in level2.")
    old = raw[off:off + 8]
    new = struct.pack("<2f", x, y)
    if old == new:
        return 0
    bak = scene + SCALE_BACKUP
    if not os.path.exists(bak):
        shutil.copy2(scene, bak)
    _patch_in_place(scene, [(start + off, old, new)], "The team-select grid")
    return 1


# ---------------------------------------------------------------------------
# Team order (level1, in place)
# ---------------------------------------------------------------------------
#
# The tiles are built from BasketTeamManager.m_aoTeams, which load() fills in
# m_aoBundledTeams order -- but TeamSelectionSettings.Initialize then sorts it
# by nickname before making tiles. So a new order in level1 only shows once
# that sort is switched off (apply_code_layout(custom_order=True)). The
# HardWoods tiles come from their own list and are never sorted.

def _team_lists(game_data, container_pids):
    """Where the team lists live in level1 and what they hold, in order.

    {"manager": path_id, "byte_start": int, "raw": bytes,
     "bundled": {"offset", "count", "file_id", "pids"},
     "hardwoods": same or None}
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
            arrays = scan_pointer_arrays(raw, container_pids)
            if not arrays:
                continue
            top = max(arrays, key=lambda a: a[1])
            if best is None or top[1] > best[3][1]:
                best = (obj.path_id, getattr(obj, "byte_start", None), raw, top, arrays)
    finally:
        close_env(env)
    if best is None:
        raise TeamError("Could not find the bundled-team list in level1.")
    pid, start, raw, top, arrays = best

    def describe(arr):
        off, count, fid = arr
        pids = [struct.unpack_from("<iq", raw, off + 4 + k * 12)[1]
                for k in range(count)]
        return {"offset": off, "count": count, "file_id": fid, "pids": pids}

    # Field order on BasketTeamManager is bundled, hardwood, goat: the list
    # straight after the bundled one is the HardWoods list.
    later = sorted(a for a in arrays if a[0] > top[0])
    return {"manager": pid, "byte_start": start, "raw": raw,
            "bundled": describe(top),
            "hardwoods": describe(later[0]) if later else None}


def read_team_order(game_data, container_pids):
    lists = _team_lists(game_data, container_pids)
    return {"bundled": lists["bundled"]["pids"],
            "hardwoods": (lists["hardwoods"] or {}).get("pids", [])}


def set_team_order(game_data, container_pids, new_order):
    """Rewrite m_aoBundledTeams in a new order. Same entries, same count, so
    the edit is length-preserving and nothing else in level1 moves."""
    level1 = os.path.join(game_data, MANAGER_SCENE_FILE)
    lists = _team_lists(game_data, container_pids)
    cur = lists["bundled"]
    new_order = [int(p) for p in new_order]
    if sorted(new_order) != sorted(cur["pids"]):
        raise TeamError("The new order must contain exactly the teams already "
                        "in the list. Refresh the Teams tab and try again.")
    if new_order == cur["pids"]:
        return 0
    if lists["byte_start"] is None:
        raise TeamError("Could not locate the team list in level1.")
    body = cur["offset"] + 4
    old = lists["raw"][body:body + cur["count"] * 12]
    new = b"".join(struct.pack("<iq", cur["file_id"], p) for p in new_order)
    backup(level1)
    _patch_in_place(level1, [(lists["byte_start"] + body, old, new)],
                    "The team list")
    return 1


def sort_key(team):
    """What the game sorts the grid by: the nickname (m_sTeam), ordinal."""
    ident = team.get("identity") or {}
    return (ident.get("nickname") or {}).get("text") or team.get("name", "")


def read_layout(game_data, teams):
    """Everything the Layout section draws, read from the files as they are.

    teams: list_teams()["teams"]. Returns {"order", "hardwoods", "scale",
    "position", "code" (or None), "code_error"}.
    """
    pids = {t["path_id"] for t in teams}
    lists = read_team_order(game_data, pids)
    out = {"order": lists["bundled"], "hardwoods": lists["hardwoods"]}
    _raw, _start, _off, position, scale = _grid_rect(game_data)
    out["scale"], out["position"] = scale, position
    try:
        out["code"], out["code_error"] = read_code_layout(game_data), None
    except TeamError as exc:
        out["code"], out["code_error"] = None, str(exc)
    return out


def tile_sequence(teams_by_pid, order, hardwoods, custom_order, with_hardwoods):
    """The tiles left to right, top to bottom, as the game will build them:
    the team list (sorted by nickname unless custom order is on), then the
    HardWoods teams if the DLC is active, then Random."""
    main = [teams_by_pid[p] for p in order if p in teams_by_pid]
    if not custom_order:
        main = sorted(main, key=sort_key)
    tiles = [("team", t) for t in main]
    if with_hardwoods:
        tiles += [("classic", teams_by_pid[p]) for p in hardwoods if p in teams_by_pid]
    tiles.append(("random", None))
    return tiles


def grid_geometry(tile_count, columns, cell, scale=1.0, position=STOCK_GRID_POSITION):
    """Canvas rectangles (x0, y0, x1, y1), top-left origin, for each tile.

    Mirrors the scene: StandardTeans is a fixed-column GridLayoutGroup
    (upper-left start, 6 px spacing) sized by a ContentSizeFitter, centred by
    GridLayOut's HorizontalLayoutGroup in the bottom strip, and the strip is
    scaled about its centre.
    """
    cw, ch = cell
    sx, sy = CELL_SPACING
    rows = max(1, -(-tile_count // columns))
    width = columns * cw + (columns - 1) * sx
    height = rows * ch + (rows - 1) * sy
    W, H = CANVAS_SIZE
    cx = W / 2 + position[0]
    # stock strip centre is 628/2 = 314 px above the bottom edge; anchored y
    # moves it relative to where the stretch anchors put it
    cy = H - GRID_STRIP_HEIGHT / 2 - (position[1] - STOCK_GRID_POSITION[1])
    x0, y0 = cx - width / 2, cy - height / 2
    rects = []
    for i in range(tile_count):
        r, c = divmod(i, columns)
        tx, ty = x0 + c * (cw + sx), y0 + r * (ch + sy)
        rects.append(tuple(cx + (v - cx) * scale if k % 2 == 0 else
                           cy + (v - cy) * scale
                           for k, v in enumerate((tx, ty, tx + cw, ty + ch))))
    return rects


# ---------------------------------------------------------------------------
# Reset: every team-related change back to stock
# ---------------------------------------------------------------------------

ORIGINAL_BACKUP = ".original_backup"     # app.py's BACKUP_SUFFIX: pristine files
RESOURCE_COMPANIONS = (".resS", ".resource")
RESET_FILES = (CONTAINER_ASSETS_FILE, MANAGER_SCENE_FILE,
               "resources.assets", "globalgamemanagers")


def _stock_source(path):
    """The best pristine copy of a file: the app's .original_backup (taken
    before any mod touched it), else the team-slot backup."""
    for suffix in (ORIGINAL_BACKUP, TEAM_BACKUP):
        if os.path.isfile(path + suffix):
            return path + suffix, suffix
    return None, None


def reset_plan(game_data):
    """What reset_team_changes() would do, without doing it. For the confirm
    dialog, so it can say exactly which files change."""
    steps = []
    for name in RESET_FILES:
        path = os.path.join(game_data, name)
        src, suffix = _stock_source(path)
        if src is None:
            continue
        same = (os.path.getsize(src) == os.path.getsize(path)
                and _same_bytes(src, path))
        if not same:
            steps.append(f"{name}  ←  {os.path.basename(src)}")
    try:
        if get_grid_scale(game_data) != 1.0:
            steps.append("level2: grid size back to 100%")
        if get_grid_position(game_data) != STOCK_GRID_POSITION:
            steps.append("level2: grid position back to centre")
    except TeamError:
        pass
    if os.path.exists(os.path.join(game_data, DLL_REL) + NAV_BACKUP):
        dll = os.path.join(game_data, DLL_REL)
        if not _same_bytes(dll, dll + NAV_BACKUP):
            steps.append("Assembly-CSharp.dll  ←  original (undo code patches)")
    return steps


def _same_bytes(a, b, chunk=1 << 22):
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(chunk), fb.read(chunk)
            if x != y:
                return False
            if not x:
                return True


def reset_team_changes(game_data, progress=None):
    """Put the game's teams and team-select screen back exactly as shipped.

      sharedassets1.assets (+ .resS/.resource)   <- .original_backup
      resources.assets (+ .resS)                 <- .original_backup
      level1, globalgamemanagers                 <- .teamslot_backup
      level2 grid size and position              <- stock values, in place
                                                    (slider edits in level2 stay)
      Assembly-CSharp.dll                        <- .navpatch_backup

    Added teams, renames, recolors, borrowed or detached art, localized names,
    custom order, columns and the navigation fix all go. Texture, mesh and audio
    mods are NOT lost: they live in the mods folder, and the caller re-applies
    them because the result lists the rebuilt files.

    Afterwards the .teamslot_backup files are deleted. They describe "before
    the first team edit", and a stale one would make a later Remove Added
    Teams restore an old state instead of this clean one.
    """
    def say(msg):
        if progress:
            progress(msg)

    restored, rebuilt = [], []
    for name in RESET_FILES:
        path = os.path.join(game_data, name)
        if not os.path.isfile(path):
            continue
        src, suffix = _stock_source(path)
        if src is None:
            continue
        say(f"Restoring {name} …")
        if not _same_bytes(src, path):
            shutil.copy2(src, path)
            restored.append(name)
        if suffix == ORIGINAL_BACKUP:
            for comp in RESOURCE_COMPANIONS:
                cpath = path + comp
                cbak = cpath + ORIGINAL_BACKUP
                if os.path.isfile(cpath) and os.path.isfile(cbak) \
                        and not _same_bytes(cbak, cpath):
                    say(f"Restoring {name}{comp} …")
                    shutil.copy2(cbak, cpath)
                    restored.append(name + comp)
        if name.endswith(".assets"):
            rebuilt.append(path)

    if os.path.isfile(os.path.join(game_data, GRID_SCENE_FILE)):
        say("Resetting the team-select grid …")
        if set_grid_scale(game_data, 1.0):
            restored.append("level2 (grid size)")
        if set_grid_position(game_data, *STOCK_GRID_POSITION):
            restored.append("level2 (grid position)")

    dll = os.path.join(game_data, DLL_REL)
    if os.path.isfile(dll + NAV_BACKUP) and not _same_bytes(dll, dll + NAV_BACKUP):
        say("Restoring the game's code …")
        shutil.copy2(dll + NAV_BACKUP, dll)
        restored.append("Assembly-CSharp.dll")

    removed = []
    for name in RESET_FILES:
        bak = os.path.join(game_data, name) + TEAM_BACKUP
        if os.path.isfile(bak):
            try:
                os.remove(bak)
                removed.append(os.path.basename(bak))
            except OSError:
                pass

    return {"restored": restored, "rebuilt": rebuilt, "removed_backups": removed}


# ---------------------------------------------------------------------------
# Saves that remember a team which no longer exists
# ---------------------------------------------------------------------------
#
# BasketTeamManager.Update walks SaveData.m_aoSavedTeams and does
# m_dTeams[id] with no missing-key check. A save that still lists a removed
# custom team throws at that entry, and every saved team AFTER it silently
# loses its saved data for the session.
#
# The fix is a 4-byte edit: point the orphaned entry at the team whose own
# entry comes LAST in the array. The loop then hands that team the orphan's
# data first and its real data a moment later, so the net effect is exactly
# "the orphan was skipped". The next time the game saves, it writes the list
# from its own teams and the orphan is gone for good.

def orphaned_save_teams(save_path, valid_ids):
    """[(array_index, team_id)] for saved teams the game no longer has."""
    import save_manager as sm
    m = sm.load_save(save_path)
    return _orphans(m, set(valid_ids))[0]


def _orphans(m, valid):
    rows = []
    for key, i in {n: k for k, n in enumerate(m["names"])}.items():
        if key.startswith("/m_aoSavedTeams/") and key.endswith("/m_iTeamUniqueId"):
            idx = int(key.split("/")[2])
            rows.append((idx, struct.unpack("<i", m["spans"][i])[0], i))
    rows.sort()
    return [(idx, tid) for idx, tid, _i in rows if tid not in valid], rows


def repair_save_teams(save_path, valid_ids):
    """Neutralise orphaned team entries in a save. Backs the save up first.
    Returns {"fixed": n, "backup": path|None}."""
    import save_manager as sm
    valid = set(valid_ids)
    raw = open(save_path, "rb").read()
    m = sm.load_save(save_path)
    if sm.build_save(m) != raw:
        raise TeamError("This save does not round-trip byte-for-byte, so it "
                        "was left alone.")
    orphans, rows = _orphans(m, valid)
    if not orphans:
        return {"fixed": 0, "backup": None}
    last_valid = next(((idx, tid) for idx, tid, _i in reversed(rows)
                       if tid in valid), None)
    if last_valid is None or last_valid[0] < max(idx for idx, _t in orphans):
        raise TeamError("The removed team is the last entry in this save, so "
                        "there is no safe way to neutralise it in place. "
                        "Restore an older save from the Saves tab instead.")
    for idx, tid, i in rows:
        if tid not in valid:
            m["spans"][i] = struct.pack("<i", last_valid[1])
    data = sm.build_save(m)
    if len(data) != len(raw):
        raise TeamError("internal: the save edit changed its length.")
    bak = sm.backup_save(save_path)
    tmp = save_path + ".teamfix_tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    sm.validate(tmp)
    os.replace(tmp, save_path)
    return {"fixed": len(orphans), "backup": bak}


def stock_team_ids(game_data):
    """Unique IDs of every team the game's files define right now."""
    return {t["unique_id"] for t in list_teams(game_data)["teams"]
            if t["unique_id"] is not None}


# ---------------------------------------------------------------------------
# Navigation patch (Assembly-CSharp.dll) -- opt-in, edits code not assets
# ---------------------------------------------------------------------------

def _sig(text):
    """A byte pattern where '??' matches any byte. The index bytes are
    wildcards on purpose: once patched they no longer read 24 or 27, and a
    literal pattern would never match again -- so changing the team count a
    second time would silently do nothing."""
    return [None if tok == "??" else int(tok, 16) for tok in text.split()]


MATRIX_STANDARD  = "8-column grid (no HardWoods DLC)"
MATRIX_HARDWOODS = "9-column grid (HardWoods DLC)"

# Column counts as the game ships them, and the most each layout can take.
# The ceiling is not a design choice. Two of the numbers that encode a grid's
# width -- the top-row wrap (0..columns-1) on both grids and the matrix width
# itself on the standard grid -- are single-byte IL opcodes, ldc.i4.0 through
# ldc.i4.8. An in-place patch cannot grow an instruction, so the standard grid
# tops out at 8 columns and the HardWoods grid at 9. Fewer columns always fit.
STOCK_COLUMNS = {MATRIX_STANDARD: 8, MATRIX_HARDWOODS: 9}
MAX_COLUMNS   = {MATRIX_STANDARD: 8, MATRIX_HARDWOODS: 9}
MIN_COLUMNS   = 3

# Tile size the game forces at runtime (SelectTeamBehaviour.Initialize), which
# is why the GridLayoutGroup's own 130x100 in level2 is never what you see.
RUNTIME_CELL  = {MATRIX_STANDARD: (200.0, 100.0), MATRIX_HARDWOODS: (180.0, 100.0)}
CELL_SPACING  = (6.0, 6.0)

NAV_MATRICES = {
    MATRIX_STANDARD: (0, [
        ("last-row pair",            "25 1f ?? 1f ?? 06 fe 06", {2: "start", 4: "end"}),
        ("Random tile: Up",          "7e 59 19 00 04 28 59 0e 00 0a 08 1f ??", {12: "start"}),
        ("Random tile: Right",       "7e 5c 19 00 04 28 59 0e 00 0a 08 1f ??", {12: "end"}),
        ("Random tile: hover",       "2b 08 1f ?? 6f 56 0d 00 0a 6f 85 21 00 06", {3: "end"}),
    ]),
    MATRIX_HARDWOODS: (RETRO_TEAM_COUNT, [
        ("last-row pair",            "25 1f ?? 1f ?? 06 fe 06", {2: "start", 4: "end"}),
        ("Random tile: Up",          "7e 59 19 00 04 28 59 0e 00 0a 09 1f ??", {12: "start"}),
        ("Random tile: Right",       "7e 5c 19 00 04 28 59 0e 00 0a 09 1f ??", {12: "end"}),
    ]),
}

# Layout sites, each proved unique in the shipped Assembly-CSharp.dll. The
# column bytes are wildcards so a patched DLL is still recognised -- a literal
# 8 would stop matching the moment it became a 6.
#
#   init        SelectTeamBehaviour.Initialize:
#                   constraintCount = HardWoodUnlocked ? 9 : 8
#               brfalse.s +4 / ldc.i4.s HW / br.s +1 / ldc.i4.N STD
#   *_top       the top-row wrap special case, (0, columns-1), whose lambda
#               sends Up to the Random tile. Found by that lambda's token.
#   *_width     the column count handed to MatrixNavigator.SetUpNormalMatrix.
#               Searched only just after *_top: the same call shape exists for
#               an unrelated menu elsewhere in the DLL.
#   sort        TeamSelectionSettings.Initialize sorts the team list by
#               nickname (String.Compare, Ordinal) before building the tiles.
#               Replacing that one call with two pops keeps the list in the
#               order level1 stores it, which is what makes a custom order
#               possible at all.
LAYOUT_SITES = {
    "init":     "6f c4 1c 00 06 2c 04 1f ?? 2b 01 ?? 6f 43 0d 00 0a",
    "std_top":  "25 16 ?? 06 fe 06 29 2e 00 06",
    "hw_top":   "25 16 ?? 06 fe 06 2c 2e 00 06",
    "sort":     "80 a0 22 00 04 ?? ?? ?? ?? ?? 28 83 00 00 0a 6f d2 0f 00 06",
}
WIDTH_SITES = {           # name: (pattern, found after, search span, value offset)
    "std_width": ("08 ?? 28 ad 21 00 06", "std_top", 160, 1),
    "hw_width":  ("1f ?? 28 ad 21 00 06", "hw_top", 320, 1),
}
SORT_ON  = bytes.fromhex("6f 75 0d 00 0a")      # callvirt List<T>.Sort(Comparison)
SORT_OFF = bytes.fromhex("26 26 00 00 00")      # pop; pop; nop x3
LDC_I4_0 = 0x16                                 # ldc.i4.0 .. ldc.i4.8 = 0x16 .. 0x1e


def _find_unique(blob, pattern, label, span=None):
    n, m = len(blob), len(pattern)
    lo, hi = (0, n - m) if span is None else (span[0], min(span[1], n - m))
    anchor = next(i for i, b in enumerate(pattern) if b is not None)
    needle, hits, i = bytes([pattern[anchor]]), [], lo
    while True:
        i = blob.find(needle, i, hi + m)
        if i == -1 or i - anchor > hi:
            break
        at = i - anchor
        if at >= lo and all(p is None or blob[at + k] == p
                            for k, p in enumerate(pattern)):
            hits.append(at)
        i += 1
    if not hits:
        return None
    if len(hits) > 1:
        raise TeamError(f"The code signature for '{label}' matches "
                        f"{len(hits)} times, so it is ambiguous. Refusing to "
                        f"patch the game's code.")
    return hits[0]


def _ldc_small(n, what):
    if not 0 <= n <= 8:
        raise TeamError(f"{what} would be {n}, but that instruction can only "
                        f"hold 0-8 without growing the game's code.")
    return LDC_I4_0 + n


def _read_ldc_small(byte, what):
    if not LDC_I4_0 <= byte <= LDC_I4_0 + 8:
        raise TeamError(f"The game's code does not hold a small constant for "
                        f"{what} (found 0x{byte:02x}). The game has been updated; "
                        f"refusing to patch.")
    return byte - LDC_I4_0


def _layout_sites(blob):
    """{name: offset} for every layout site, or TeamError if any is missing."""
    at = {}
    for name, pat in LAYOUT_SITES.items():
        hit = _find_unique(blob, _sig(pat), name)
        if hit is None:
            raise TeamError(f"Could not find the '{name}' site in this game's "
                            f"code. The game has been updated; refusing to "
                            f"patch.")
        at[name] = hit
    for name, (pat, after, span, _o) in WIDTH_SITES.items():
        lo = at[after]
        hit = _find_unique(blob, _sig(pat), name, (lo, lo + span))
        if hit is None:
            raise TeamError(f"Could not find the '{name}' site in this game's "
                            f"code. The game has been updated; refusing to "
                            f"patch.")
        at[name] = hit
    sort = blob[at["sort"] + 5:at["sort"] + 10]
    if sort not in (SORT_ON, SORT_OFF):
        raise TeamError("The team-sort call is not what this app expects; "
                        "refusing to patch.")
    return at


def _read_layout_code(blob, at):
    init = at["init"]
    std = _read_ldc_small(blob[init + 11], "the standard grid's column count")
    hw = blob[init + 8]
    return {
        "columns": {MATRIX_STANDARD: std, MATRIX_HARDWOODS: hw},
        "matrix_columns": {
            MATRIX_STANDARD: _read_ldc_small(blob[at["std_width"] + 1],
                                             "the standard matrix width"),
            MATRIX_HARDWOODS: blob[at["hw_width"] + 1],
        },
        "custom_order": blob[at["sort"] + 5:at["sort"] + 10] == SORT_OFF,
    }


def _nav_plan(game_data, teams, columns=None):
    """(dll, blob, rows, layout) -- rows describe every last-row / Random site
    for `teams` teams at `columns` (default: what the DLL holds now)."""
    dll = os.path.join(game_data, DLL_REL)
    if not os.path.isfile(dll):
        raise TeamError(f"Assembly-CSharp.dll is not in {os.path.dirname(dll)}.")
    with open(dll, "rb") as f:
        blob = f.read()

    at = _layout_sites(blob)
    layout = _read_layout_code(blob, at)
    cols_by = dict(layout["matrix_columns"])
    cols_by.update(columns or {})

    anchors = {MATRIX_STANDARD: at["std_width"] + 1,
               MATRIX_HARDWOODS: at["hw_width"]}
    ordered = sorted(anchors.items(), key=lambda kv: kv[1])
    regions = {}
    for i, (mname, pos) in enumerate(ordered):
        # Start after the previous matrix so the two never overlap: they share
        # patterns, and an overlap would read as an ambiguous match.
        lo = max(0, pos - 700) if i == 0 else ordered[i - 1][1] + 210
        regions[mname] = (lo, pos + 200)

    rows = []
    for mname, (extra, sites) in NAV_MATRICES.items():
        cols = cols_by[mname]
        n = (teams + extra) if teams else None
        start = end = None
        if n is not None:
            if n > 128:
                raise TeamError(f"{n} tiles is more than the 128 this patch can "
                                f"express.")
            start, end = ((n - 1) // cols) * cols, n - 1
        for label, pat, slots in sites:
            hit = _find_unique(blob, _sig(pat), f"{mname}: {label}", regions[mname])
            rows.append({"matrix": mname, "label": label, "at": hit,
                         "slots": slots, "start": start, "end": end,
                         "tiles": n, "columns": cols,
                         "current": ({s: blob[hit + o] for o, s in slots.items()}
                                     if hit is not None else {})})
    return dll, blob, rows, (at, layout)


def nav_report(game_data, teams=None):
    """What the DLL holds now, and what it would hold for `teams` teams."""
    dll, _blob, rows, (_at, layout) = _nav_plan(game_data, teams)
    return {"dll": dll, "rows": rows, "has_backup": os.path.exists(dll + NAV_BACKUP),
            "layout": layout}


def read_code_layout(game_data):
    """Columns and team order as the game's code has them right now.

    {"columns": {matrix: n}, "custom_order": bool, "has_backup": bool,
     "max_columns": {...}, "stock_columns": {...}}
    """
    dll = os.path.join(game_data, DLL_REL)
    if not os.path.isfile(dll):
        raise TeamError(f"Assembly-CSharp.dll is not in {os.path.dirname(dll)}.")
    with open(dll, "rb") as f:
        blob = f.read()
    at = _layout_sites(blob)
    out = _read_layout_code(blob, at)
    out.update(has_backup=os.path.exists(dll + NAV_BACKUP),
               max_columns=dict(MAX_COLUMNS), stock_columns=dict(STOCK_COLUMNS))
    return out


def apply_code_layout(game_data, teams, columns=None, custom_order=None):
    """Write columns, team-order mode and navigation for `teams` teams, in one
    pass over the DLL.

    columns: {matrix: n} for either or both grids (omitted = unchanged).
    custom_order: True keeps level1's list order, False restores the game's
    alphabetical sort, None leaves it as it is.

    Every edit is a same-length byte swap at a site proved unique above, the
    original DLL is backed up once, and the result is read back from disk.
    """
    dll, blob, _rows, (at, layout) = _nav_plan(game_data, teams)
    cols = dict(layout["columns"])
    for mname, n in (columns or {}).items():
        if mname not in MAX_COLUMNS:
            raise TeamError(f"Unknown grid '{mname}'.")
        n = int(n)
        if not MIN_COLUMNS <= n <= MAX_COLUMNS[mname]:
            raise TeamError(f"The {mname} can take {MIN_COLUMNS}-"
                            f"{MAX_COLUMNS[mname]} columns, not {n}.")
        cols[mname] = n
    std, hw = cols[MATRIX_STANDARD], cols[MATRIX_HARDWOODS]

    new = bytearray(blob)
    new[at["init"] + 8] = hw
    new[at["init"] + 11] = _ldc_small(std, "Standard grid columns")
    new[at["std_top"] + 2] = _ldc_small(std - 1, "Standard grid top row")
    new[at["hw_top"] + 2] = _ldc_small(hw - 1, "HardWoods grid top row")
    new[at["std_width"] + 1] = _ldc_small(std, "Standard matrix width")
    new[at["hw_width"] + 1] = hw
    if custom_order is not None:
        s = at["sort"] + 5
        new[s:s + 5] = SORT_OFF if custom_order else SORT_ON

    # Navigation rows for the new shape, re-planned against the NEW widths.
    _d, _b, rows, _l = _nav_plan(game_data, teams, cols)
    for r in rows:
        if r["at"] is None or r["start"] is None:
            continue
        for off, which in r["slots"].items():
            new[r["at"] + off] = r["start"] if which == "start" else r["end"]

    changed = sum(1 for a, b in zip(blob, new) if a != b)
    if not changed:
        return {"changed": 0}
    if len(new) != len(blob):
        raise TeamError("internal: the patch changed the file length.")
    bak = dll + NAV_BACKUP
    if not os.path.exists(bak):
        shutil.copy2(dll, bak)
    with open(dll, "r+b") as f:
        f.write(bytes(new))
    with open(dll, "rb") as f:
        if f.read() != bytes(new):
            raise TeamError("The code patch did not read back correctly. "
                            "Restore from the backup before launching.")
    return {"changed": changed}


def nav_patch(game_data, teams):
    """Point both navigation matrices at the real last row for this team count,
    keeping whatever columns and order mode the DLL already has."""
    return apply_code_layout(game_data, teams)


def nav_revert(game_data):
    dll = os.path.join(game_data, DLL_REL)
    bak = dll + NAV_BACKUP
    if not os.path.exists(bak):
        raise TeamError("There is nothing to undo -- the game's code has not "
                        "been patched by this app.")
    shutil.copy2(bak, dll)
    return {"restored": dll}

# ---------------------------------------------------------------------------
# Tier 1: borrowing art from another team
# ---------------------------------------------------------------------------
#
# A container does not carry its art -- it points at it. Three kinds of
# reference, and the difference decides what can be edited in place:
#
#   Sprites (logos, the grid icon)    PPtr: int32 m_FileID + int64 m_PathID.
#   Materials (the eight court mats)  Always 12 bytes, so repointing one is an
#                                     in-place write that moves nothing.
#
#   Jerseys and court textures        Length-prefixed STRINGS, loaded by name
#                                     through Resources.Load. A string occupies
#                                     4 + len padded to 4, so a replacement only
#                                     fits without moving anything when it lands
#                                     in the same alignment bucket -- roughly
#                                     within three characters. Portland's
#                                     35-character jersey name cannot be
#                                     replaced by Boston's 28-character one.
#
# So borrowing logos and courts always works; borrowing jerseys works only
# between teams with similar-length names, and the UI is told which.
#
# Matching across teams is by ROLE, never by position: Portland ships five retro
# logos and Chicago four, so the pointer runs fall out of step almost
# immediately. Sprites are paired up in order within their own run, the grid
# icon is matched by its name, and materials are matched by the suffix in their
# name (chairs, parquet, lines, ...), which is what actually identifies them.

MATERIAL_ROLES = ("chairs", "parquet", "lines", "decorations",
                  "hoop", "stand", "publi", "letters")
MATERIAL_PREFIX = "mat_bounce_stadium_"
JERSEY_PREFIX = "txt_avatar_"
COURT_TEXTURE_PREFIXES = ("txt_bounce_court_", "txt_bounce_publi_")


def _object_catalog(game_data):
    """{path_id: (type_name, object_name)} for sharedassets1.assets.

    Needed to turn a pointer into something meaningful: 'Sprite/BCeltics_Global'
    rather than 'pathID 7942'.
    """
    import UnityPy
    env = UnityPy.load(os.path.join(game_data, CONTAINER_ASSETS_FILE))
    catalog = {}
    try:
        for obj in env.objects:
            name = ""
            if obj.type.name != "MonoBehaviour":
                try:
                    name, _ = read_str(obj.get_raw_data(), 0)   # NamedObject
                except Exception:
                    name = ""
            catalog[obj.path_id] = (obj.type.name, name)
    finally:
        close_env(env)
    return catalog


def _pointers(raw, catalog):
    """Every PPtr into this same file, as [(offset, path_id, type, name)]."""
    out = []
    for i in range(0, len(raw) - 12, 4):
        fid, pid = struct.unpack_from("<iq", raw, i)
        if fid != 0 or pid <= 0 or pid not in catalog:
            continue
        ttype, tname = catalog[pid]
        out.append((i, pid, ttype, tname))
    return out


def _strings_with_fit(raw):
    """[(offset, text, length, fits_min, fits_max)] for every readable string.

    fits_* is the length a replacement may have while occupying the same number
    of bytes: total = 4 + len rounded up to 4, so anything from total-7 to
    total-4 lands in the same footprint.
    """
    out, n = [], len(raw)
    for i in range(0, n - 4, 4):
        (slen,) = struct.unpack_from("<i", raw, i)
        if not (2 <= slen <= 96 and i + 4 + slen <= n):
            continue
        cand = raw[i + 4:i + 4 + slen]
        if not all(32 <= c < 127 for c in cand):
            continue
        total = (4 + slen + 3) & ~3
        out.append((i, cand.decode("ascii"), slen, max(1, total - 7), total - 4))
    return out


def _team_art(raw, catalog):
    """What one container points at, grouped by role."""
    ptrs = _pointers(raw, catalog)
    sprites = [(o, pid, nm) for o, pid, t, nm in ptrs
               if t == "Sprite" and not nm.endswith("_Icon")]
    icons = [(o, pid, nm) for o, pid, t, nm in ptrs
             if t == "Sprite" and nm.endswith("_Icon")]
    materials = {}
    for o, pid, t, nm in ptrs:
        if t != "Material" or not nm.startswith(MATERIAL_PREFIX):
            continue
        role = nm[len(MATERIAL_PREFIX):].split("_")[0]
        materials.setdefault(role, []).append((o, pid, nm))
    strings = _strings_with_fit(raw)
    jerseys = [s for s in strings if s[1].startswith(JERSEY_PREFIX)]
    courts = [s for s in strings
              if any(s[1].startswith(p) for p in COURT_TEXTURE_PREFIXES)]
    return {"sprites": sprites, "icons": icons, "materials": materials,
            "jerseys": jerseys, "court_textures": courts}


def list_art(game_data):
    """Per-team art inventory, for the Borrow dropdowns.

    {team_name: {path_id, logo_count, icon, materials: [roles], jersey: str,
                 jersey_fit: (min, max), shares_with: [team names]}}
    `shares_with` is what makes the donor problem visible: a clone points at its
    donor's art, so editing that texture changes both teams.
    """
    _require(game_data, CONTAINER_ASSETS_FILE)
    script_pid = find_container_script(game_data)
    containers = _read_containers(game_data, script_pid)
    catalog = _object_catalog(game_data)

    art, owners = {}, {}
    for name, c in containers.items():
        a = _team_art(c["raw"], catalog)
        first_jersey = a["jerseys"][0] if a["jerseys"] else None
        art[name] = {
            "path_id": c["path_id"],
            "logo_count": len(a["sprites"]),
            "icon": a["icons"][0][2] if a["icons"] else None,
            "materials": sorted(a["materials"]),
            "jersey": first_jersey[1] if first_jersey else None,
            "jersey_fit": (first_jersey[3], first_jersey[4]) if first_jersey else None,
            "logo_names": [nm for _o, _p, nm in a["sprites"][:4]],
        }
        key = tuple(sorted({pid for _o, pid, _n in a["sprites"]}))
        if key:
            owners.setdefault(key, []).append(name)
    for name, info in art.items():
        group = next((g for g in owners.values() if name in g and len(g) > 1), [])
        info["shares_with"] = [n for n in group if n != name]
    return art


def borrow_art(game_data, target_path_id, donor_name, parts=("logos", "icon",
                                                             "court", "jerseys"),
               dry_run=False):
    """Point one team's art fields at another team's art, in place.

    parts: any of "logos", "icon", "court" (the eight stadium materials),
    "jerseys" (and the court texture names, which are the same kind of string).

    Returns {"changed": n, "skipped": [reasons], "detail": [...]}. Jersey names
    that cannot fit their slot are SKIPPED and reported, never truncated: a
    Resources.Load name that is wrong by one character loads nothing, and a team
    with no jersey texture is worse than a team with the donor's.
    """
    assets = os.path.join(game_data, CONTAINER_ASSETS_FILE)
    script_pid = find_container_script(game_data)
    containers = _read_containers(game_data, script_pid)
    catalog = _object_catalog(game_data)

    target = next((c for c in containers.values()
                   if c["path_id"] == target_path_id), None)
    if target is None:
        raise TeamError(f"No team with path_id {target_path_id}.")
    matches = [n for n in containers if donor_name.lower() in n.lower()]
    if len(matches) != 1:
        raise TeamError(f"'{donor_name}' matched {len(matches)} teams.")
    donor = containers[matches[0]]
    if donor["path_id"] == target_path_id:
        raise TeamError("A team cannot borrow from itself.")
    if target["byte_start"] is None:
        raise TeamError("Could not locate this team in the file.")

    t_art = _team_art(target["raw"], catalog)
    d_art = _team_art(donor["raw"], catalog)
    edits, detail, skipped = [], [], []
    base = target["byte_start"]

    def ptr_bytes(pid):
        return struct.pack("<iq", 0, pid)

    if "logos" in parts:
        # Paired in order within each team's own sprite run. The runs can be
        # different lengths (Portland ships five retro logos, Chicago four), so
        # the shorter one decides; the rest keep what they had.
        pairs = list(zip(t_art["sprites"], d_art["sprites"]))
        for (off, old_pid, old_nm), (_do, new_pid, new_nm) in pairs:
            if old_pid == new_pid:
                continue
            edits.append((base + off, ptr_bytes(old_pid), ptr_bytes(new_pid)))
            detail.append(f"logo: {old_nm} -> {new_nm}")
        if len(d_art["sprites"]) < len(t_art["sprites"]):
            skipped.append(f"{matches[0]} has only {len(d_art['sprites'])} logos; "
                           f"the last {len(t_art['sprites']) - len(d_art['sprites'])} "
                           f"were left as they were")

    if "icon" in parts and t_art["icons"] and d_art["icons"]:
        off, old_pid, old_nm = t_art["icons"][0]
        _do, new_pid, new_nm = d_art["icons"][0]
        if old_pid != new_pid:
            edits.append((base + off, ptr_bytes(old_pid), ptr_bytes(new_pid)))
            detail.append(f"grid icon: {old_nm} -> {new_nm}")

    if "court" in parts:
        for role, slots in t_art["materials"].items():
            donor_slots = d_art["materials"].get(role)
            if not donor_slots:
                skipped.append(f"{matches[0]} has no {role} material")
                continue
            new_pid, new_nm = donor_slots[0][1], donor_slots[0][2]
            for off, old_pid, old_nm in slots:
                if old_pid == new_pid:
                    continue
                edits.append((base + off, ptr_bytes(old_pid), ptr_bytes(new_pid)))
            detail.append(f"court {role}: -> {new_nm}")

    if "jerseys" in parts:
        for group, label in ((("jerseys",), "jersey"),
                             (("court_textures",), "court texture")):
            t_list = t_art[group[0]]
            d_list = d_art[group[0]]
            for i, (off, text, slen, lo, hi) in enumerate(t_list):
                if i >= len(d_list):
                    break
                new_text = d_list[i][1]
                if new_text == text:
                    continue
                if not (lo <= len(new_text) <= hi):
                    skipped.append(
                        f"{label} '{new_text}' is {len(new_text)} characters and "
                        f"the slot holds {lo}-{hi} — left as '{text}'")
                    continue
                old_bytes = struct.pack("<i", slen) + text.encode("ascii")
                pad = ((4 + len(new_text) + 3) & ~3) - 4 - len(new_text)
                new_bytes = (struct.pack("<i", len(new_text))
                             + new_text.encode("ascii") + b"\x00" * pad)
                old_full = old_bytes + b"\x00" * (len(new_bytes) - len(old_bytes)) \
                    if len(new_bytes) > len(old_bytes) else old_bytes
                if len(new_bytes) != len(old_full):
                    skipped.append(f"{label} '{new_text}' would change the byte "
                                   f"count — left as '{text}'")
                    continue
                edits.append((base + off, old_full, new_bytes))
                detail.append(f"{label}: {text} -> {new_text}")

    if dry_run:
        return {"changed": len(edits), "skipped": skipped, "detail": detail}
    n = _patch_in_place(assets, edits, "Team art")
    return {"changed": n, "skipped": skipped, "detail": detail}

# ---------------------------------------------------------------------------
# Tier 2: giving a team art of its own
# ---------------------------------------------------------------------------
#
# A clone points at its DONOR's sprites. That is what makes it playable the
# moment it is created, and also what makes replacing its logo replace the
# donor's logo too -- they are the same object.
#
# detach_art() breaks that link: each Sprite the team points at is copied to a
# new object, the Texture2D behind each sprite is copied as well, the copied
# sprite is re-pointed at the copied texture, and the team is re-pointed at the
# copied sprites. Nothing else in the file is touched, and the donor keeps
# exactly what it had.
#
# After detaching, the Textures tab can replace this team's logo the same way it
# replaces any other texture -- by path_id, through apply_single_mod(), with all
# of its encoding and .resS handling intact. That is the point of doing it this
# way: Tier 2 does not need a second texture pipeline, it needs the first one to
# be aimed at an object this team owns.
#
# It is one rebuild: the new objects and the container's re-pointing happen in
# the same save, because the container's byte offsets move when the file is
# re-serialized and a separate in-place patch afterwards would be aiming at
# stale offsets.


def _texture_pointers_in_sprite(raw, catalog):
    """[(offset, path_id)] for every Texture2D a sprite points at."""
    out = []
    for i in range(0, len(raw) - 12, 4):
        fid, pid = struct.unpack_from("<iq", raw, i)
        if fid != 0 or pid <= 0:
            continue
        entry = catalog.get(pid)
        if entry and entry[0] == "Texture2D":
            out.append((i, pid))
    return out



def _art_label(raw):
    """A short, file-safe tag for a team, taken from its visible name."""
    try:
        ident = {f: t for f, _o, t, _c in parse_identity(raw)}
        base = (ident.get("city") or ident.get("asset_name") or "team").strip()
    except (TeamError, ValueError, struct.error):
        base = "team"
    base = re.sub(r"^\d+_", "", base)
    base = re.sub(r"[^A-Za-z0-9]+", "", base)
    return (base or "team")[:16]


def _rename_named_object(raw, new_name):
    """Rewrite m_Name, which sits at offset 0 of every NamedObject.

    Safe to change length here because this is only ever called on a COPY: the
    rebuild recalculates object sizes on save.
    """
    return _resize_strings(raw, [(0, new_name)])


def art_sharing(game_data, path_id):
    """Which other teams point at this team's sprites -- the donor link."""
    script_pid = find_container_script(game_data)
    containers = _read_containers(game_data, script_pid)
    catalog = _object_catalog(game_data)
    target = next((c for c in containers.values() if c["path_id"] == path_id), None)
    if target is None:
        raise TeamError(f"No team with path_id {path_id}.")
    mine = {pid for _o, pid, _n in _team_art(target["raw"], catalog)["sprites"]}
    shared = []
    for name, c in containers.items():
        if c["path_id"] == path_id:
            continue
        theirs = {pid for _o, pid, _n in _team_art(c["raw"], catalog)["sprites"]}
        if mine & theirs:
            shared.append(name)
    return {"sprites": len(mine), "shared_with": sorted(shared)}


def detach_art(game_data, path_id, progress=None):
    """Give one team its own copies of the sprites and textures it points at.

    Returns {"sprites": n, "textures": n, "new_sprite_ids": [...], "rebuilt": [...]}.
    `rebuilt` tells the caller that byte offsets moved and queued mods must be
    re-applied, exactly as adding a team does.
    """
    shared_path, = _require(game_data, CONTAINER_ASSETS_FILE)
    script_pid = find_container_script(game_data)
    containers = _read_containers(game_data, script_pid)
    catalog = _object_catalog(game_data)

    target = next((c for c in containers.values() if c["path_id"] == path_id), None)
    if target is None:
        raise TeamError(f"No team with path_id {path_id}.")

    art = _team_art(target["raw"], catalog)
    slots = art["sprites"] + art["icons"]
    if not slots:
        raise TeamError("This team has no sprites to detach.")

    used_pids = set(catalog)
    next_pid = FIRST_ART_PATH_ID
    while next_pid in used_pids:
        next_pid += 1

    # Read each sprite once, and the texture behind it.
    import UnityPy
    env = UnityPy.load(shared_path)
    sprite_raw, tex_raw = {}, {}
    try:
        wanted = {pid for _o, pid, _n in slots}
        for obj in env.objects:
            if obj.path_id in wanted and obj.type.name == "Sprite":
                sprite_raw[obj.path_id] = obj.get_raw_data()
        tex_wanted = set()
        for pid, raw in sprite_raw.items():
            tex_wanted |= {t for _o, t in _texture_pointers_in_sprite(raw, catalog)}
        for obj in env.objects:
            if obj.path_id in tex_wanted and obj.type.name == "Texture2D":
                tex_raw[obj.path_id] = obj.get_raw_data()
    finally:
        close_env(env)

    if not sprite_raw:
        raise TeamError("Could not read this team's sprites.")

    # Plan the copies: one per distinct sprite, one per distinct texture.
    label = _art_label(target["raw"])
    tex_map, sprite_map, plan, names = {}, {}, [], {}
    for old_tex in sorted(tex_raw):
        raw = tex_raw[old_tex]
        old_name = catalog.get(old_tex, ("", ""))[1] or f"tex{old_tex}"
        new_name = f"{CUSTOM_ART_PREFIX}{label}_{old_name}"[:110]
        raw = _rename_named_object(raw, new_name)
        names[next_pid] = new_name
        tex_map[old_tex] = next_pid
        plan.append(("Texture2D", old_tex, next_pid, raw))
        next_pid += 1
        while next_pid in used_pids:
            next_pid += 1
    for old_sprite in sorted(sprite_raw):
        raw = sprite_raw[old_sprite]
        for off, old_tex in _texture_pointers_in_sprite(raw, catalog):
            if old_tex in tex_map:
                raw = (raw[:off] + struct.pack("<iq", 0, tex_map[old_tex])
                       + raw[off + 12:])
        old_name = catalog.get(old_sprite, ("", ""))[1] or f"sprite{old_sprite}"
        raw = _rename_named_object(raw, f"{CUSTOM_ART_PREFIX}{label}_{old_name}"[:110])
        sprite_map[old_sprite] = next_pid
        plan.append(("Sprite", old_sprite, next_pid, raw))
        next_pid += 1
        while next_pid in used_pids:
            next_pid += 1

    if progress:
        progress(f"Copying {len(tex_map)} texture(s) and {len(sprite_map)} "
                 f"sprite(s) ...")
    backup(shared_path)

    container_pid = target["path_id"]
    new_container = bytearray(target["raw"])
    for off, old_pid, _name in slots:
        if old_pid in sprite_map:
            struct.pack_into("<iq", new_container, off, 0, sprite_map[old_pid])

    def edit(env):
        for _kind, template, new_pid, raw in plan:
            _add_object(env, template, new_pid, raw)
        obj = next(o for o in env.objects if o.path_id == container_pid)
        obj.set_raw_data(bytes(new_container))
        return {container_pid}          # same size: only pointers changed

    _rebuild(shared_path, edit, expect_added=len(plan), progress=progress)
    return {"sprites": len(sprite_map), "textures": len(tex_map),
            "new_sprite_ids": sorted(sprite_map.values()),
            # The caller MUST register these as mods. A copied texture inherits
            # its source's offset into the .resS, and that offset is only stable
            # while nothing re-appends the file -- which apply_single_mod() does
            # on every run. Registering them is what keeps their pixels findable.
            "new_texture_ids": sorted(tex_map.values()),
            "new_texture_names": [names[p] for p in sorted(tex_map.values())],
            "search_prefix": f"{CUSTOM_ART_PREFIX}{label}",
            "assets_path": shared_path,
            "rebuilt": [shared_path]}

# ---------------------------------------------------------------------------
# Localized team names (the match-end screen)
# ---------------------------------------------------------------------------
#
# That screen does not read the container. From the DLL:
#
#     BasketTeam.get_DisplayName -> getTranslation("nba", "team_" + abbreviation)
#     Localization.getTranslation -> GridlyLocal.GetStringData(grid, record, ...)
#     on a miss -> "*** " + grid + "." + record + " ***"
#
# So a team whose abbreviation has no record shows "*** nba.team_AHS ***".
#
# The table is a Gridly export: one MonoBehaviour named "Project" in
# resources.assets, laid out as
#
#     [int32 column count][int32 record count]
#     record x N:
#         [string id]                      "team_PHX"
#         [int32 column count]             20
#         column x n: [string id][string value]    "enUS" / "Phoenix Suns"
#         [string path]                    "nba"    <- the grid, stored LAST
#
# The path being stored at the END of a record is the thing that makes this
# layout easy to misread: the string in front of a record's id belongs to the
# record before it.
#
# Verified against the shipped file: 2,138 records walk exactly, 145 of them in
# the "nba" grid, and team_PHX reads Phoenix Suns in all 18 languages.

LOC_ASSETS_FILE = "resources.assets"
LOC_PROJECT_NAME = "Project"
LOC_GRID = "nba"
LOC_RECORD_PREFIX = "team_"
LOC_HEADER_COUNT_OFFSET = 92      # int32 record count
LOC_FIRST_RECORD_OFFSET = 96
# Columns that are not languages. VO says whether a spoken line exists; a team
# we invent has no recorded commentary, so ours must say NO or the game looks
# for an AudioClip that was never shipped.
LOC_NON_LANGUAGE = {"VO": "NO", "Notes": ""}


def _loc_read_str(b, off):
    """read_str() with a bigger ceiling: read_str caps strings at 4KB, which is
    right for a container field and wrong here -- some localized lines run
    longer than that, and a cap is not a parse error."""
    (n,) = struct.unpack_from("<i", b, off)
    if not (0 <= n <= 1 << 20) or off + 4 + n > len(b):
        raise TeamError(f"localization: not a string at {off}")
    return b[off + 4:off + 4 + n].decode("utf-8", "replace"), (off + 4 + n + 3) & ~3


def _loc_parse_record(b, off):
    rid, o = _loc_read_str(b, off)
    (n,) = struct.unpack_from("<i", b, o)
    o += 4
    if not (0 <= n <= 64):
        raise TeamError(f"localization: {n} columns at {o} is not plausible.")
    cols = []
    for _ in range(n):
        k, o = _loc_read_str(b, o)
        v, o = _loc_read_str(b, o)
        cols.append((k, v))
    path, o = _loc_read_str(b, o)
    return {"id": rid, "cols": cols, "path": path, "start": off, "end": o}


def _loc_build_record(rec):
    out = _string_bytes(rec["id"]) + struct.pack("<i", len(rec["cols"]))
    for k, v in rec["cols"]:
        out += _string_bytes(k) + _string_bytes(v)
    return out + _string_bytes(rec["path"])


def _loc_find_project(game_data):
    """(path_id, raw) of the Gridly table. Found by NAME and SHAPE, never by a
    hardcoded path_id, so a game update cannot point this at the wrong object."""
    import UnityPy
    path = os.path.join(game_data, LOC_ASSETS_FILE)
    if not os.path.isfile(path):
        raise TeamError(f"{LOC_ASSETS_FILE} is not in this folder.")
    env = UnityPy.load(path)
    try:
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                raw = obj.get_raw_data()
                name, _ = _loc_read_str(raw, 28)
            except Exception:
                continue
            if name == LOC_PROJECT_NAME and b"team_PHX" in raw:
                return obj.path_id, raw
    finally:
        close_env(env)
    raise TeamError("Could not find the localization table in "
                    f"{LOC_ASSETS_FILE}.")


def read_localization(game_data):
    """{"count", "records", "team_ids", "raw", "path_id"} for the table."""
    pid, raw = _loc_find_project(game_data)
    (count,) = struct.unpack_from("<i", raw, LOC_HEADER_COUNT_OFFSET)
    records, off = [], LOC_FIRST_RECORD_OFFSET
    for _ in range(count):
        rec = _loc_parse_record(raw, off)
        records.append(rec)
        off = rec["end"]
    return {"count": count, "records": records, "raw": raw, "path_id": pid,
            "end": off,
            "team_ids": {r["id"] for r in records if r["path"] == LOC_GRID
                         and r["id"].startswith(LOC_RECORD_PREFIX)}}


def localized_abbreviations(game_data):
    """The abbreviations that already have a name on the match-end screen."""
    try:
        table = read_localization(game_data)
    except TeamError:
        return set()
    return {rid[len(LOC_RECORD_PREFIX):] for rid in table["team_ids"]}


def set_localized_team_name(game_data, abbr, display_name, progress=None):
    """Give an abbreviation a name on the match-end screen.

    Adds (or updates) the record "team_<ABBR>" in the "nba" grid, with
    display_name in every language column the table already uses. Every other
    record is copied through byte-for-byte.

    Rebuilds resources.assets, so the caller must re-apply queued mods
    afterwards -- the result carries "rebuilt" for that.
    """
    abbr = (abbr or "").strip()
    if not abbr:
        raise TeamError("No abbreviation given.")
    if not display_name.strip():
        raise TeamError("No name given.")

    assets = os.path.join(game_data, LOC_ASSETS_FILE)
    table = read_localization(game_data)
    raw = table["raw"]
    rid = LOC_RECORD_PREFIX + abbr

    template = next((r for r in table["records"]
                     if r["path"] == LOC_GRID and r["id"].startswith(LOC_RECORD_PREFIX)
                     and not r["id"].endswith(("_alt", "_short"))), None)
    if template is None:
        raise TeamError("No existing team record to model the new one on.")

    cols = []
    for key, _old in template["cols"]:
        if key in LOC_NON_LANGUAGE:
            cols.append((key, LOC_NON_LANGUAGE[key]))
        else:
            cols.append((key, display_name))
    new_rec = {"id": rid, "cols": cols, "path": LOC_GRID}
    new_bytes = _loc_build_record(new_rec)

    existing = next((r for r in table["records"]
                     if r["id"] == rid and r["path"] == LOC_GRID), None)
    if existing is not None:
        head = raw[:existing["start"]] + new_bytes + raw[existing["end"]:]
        count = table["count"]
        action = "updated"
    else:
        at = template["end"]          # insert right after the template record
        head = raw[:at] + new_bytes + raw[at:]
        count = table["count"] + 1
        action = "added"
    head = bytearray(head)
    struct.pack_into("<i", head, LOC_HEADER_COUNT_OFFSET, count)
    new_raw = bytes(head)

    # Parse the result back before it goes anywhere near the game.
    check_count = struct.unpack_from("<i", new_raw, LOC_HEADER_COUNT_OFFSET)[0]
    off, seen = LOC_FIRST_RECORD_OFFSET, []
    for _ in range(check_count):
        rec = _loc_parse_record(new_raw, off)
        seen.append(rec["id"])
        off = rec["end"]
    if off != table["end"] + (len(new_bytes) if existing is None
                              else len(new_bytes) - (existing["end"] - existing["start"])):
        raise TeamError("The rebuilt localization table did not parse back to "
                        "the expected length. Nothing was written.")
    if rid not in seen:
        raise TeamError("The new record is not in the rebuilt table. "
                        "Nothing was written.")

    if progress:
        progress(f"Writing localization for {rid} ...")
    backup(assets)
    pid = table["path_id"]

    def edit(env):
        obj = next(o for o in env.objects if o.path_id == pid)
        obj.set_raw_data(new_raw)
        return {pid}

    _rebuild(assets, edit, expect_added=0, progress=progress)
    return {"action": action, "record": rid, "languages":
            len([k for k, _v in cols if k not in LOC_NON_LANGUAGE]),
            "count": count, "rebuilt": [assets]}

# ---------------------------------------------------------------------------
# Tier 3: jerseys and court textures
# ---------------------------------------------------------------------------
#
# Logos are PPtrs, so detaching them is a pointer swap. Jerseys and courts are
# not: the container holds NAME STRINGS that the game hands to Resources.Load
# at runtime --
#
#     txt_avatar_portlandTrailblazers_001      jerseys
#     txt_bounce_court_PortlandTrailBlazers_D  the floor
#     txt_bounce_publi_PortlandTrailBlazers_D  the boards ("dornas")
#
# Those names resolve through the ResourceManager in globalgamemanagers, whose
# m_Container maps a LOWERCASED name to (fileID 8 = resources.assets, pathID).
# 532 entries, sorted alphabetically, laid out as
#
#     [int32 count][count x ([string key][int32 fileID][int64 pathID])][...]
#
# So giving a team its own jersey means three coordinated edits:
#
#   1. copy the texture inside resources.assets under a new name,
#   2. add a ResourceManager entry so Resources.Load can find that name,
#   3. point the team's container string at it.
#
# Step 3 can change the string's LENGTH, which an in-place patch cannot do --
# but this runs as a rebuild, where the container is re-serialized anyway, so
# the new name may be any length.
#
# Miss step 2 and Resources.Load returns null: the team plays with no jersey at
# all, which is worse than wearing the donor's.

GGM_FILE = "globalgamemanagers"
RESOURCE_ASSETS_FILE = "resources.assets"
RESOURCE_FILE_ID = 8            # resources.assets, as globalgamemanagers sees it
RESOURCE_PREFIXES = ("txt_avatar_", "txt_bounce_court_", "txt_bounce_publi_")
FIRST_RESOURCE_PATH_ID = 92000001



def _same_length_name(original, tag):
    """A unique resource name with EXACTLY the same byte length as `original`.

    Why bother: the alternative is rewriting the team's name string to something
    longer, which means re-serializing sharedassets1.assets. That produced a
    file UnityPy read back happily and Unity rejected outright --

        The file 'sharedassets1.assets' is corrupted! [Position out of bounds!]

    -- so Tier 3 no longer resizes anything in that file. A same-length name is
    patched IN PLACE instead, the technique already proven for renaming teams.

    The game does not care what a resource is called, only that the name
    resolves, so the name is free to be synthetic. A short hash of the team and
    the original name keeps two teams cloning the same donor texture apart.
    """
    import hashlib
    target = len(original.encode("utf-8"))
    prefix = next((p for p in RESOURCE_PREFIXES if original.startswith(p)),
                  "txt_")
    digest = hashlib.sha1(f"{tag}|{original}".encode()).hexdigest()[:8]
    stem = f"{prefix}c{digest}{re.sub(r'[^A-Za-z0-9]', '', tag)}"
    if len(stem) > target:
        stem = stem[:target]
    return (stem + "0" * (target - len(stem)))[:target]


def _parse_resource_container(raw):
    """[(key, file_id, path_id)] plus where the list ends."""
    (count,) = struct.unpack_from("<i", raw, 0)
    if not (0 <= count <= 1 << 20):
        raise TeamError("The resource table's count is not plausible.")
    entries, off = [], 4
    for _ in range(count):
        key, off = _loc_read_str(raw, off)
        fid, pid = struct.unpack_from("<iq", raw, off)
        off += 12
        entries.append((key, fid, pid))
    return entries, off


def _build_resource_container(entries, tail):
    out = struct.pack("<i", len(entries))
    for key, fid, pid in entries:
        out += _string_bytes(key) + struct.pack("<iq", fid, pid)
    return out + tail


def read_resource_names(game_data):
    """{lowercased name: (file_id, path_id)} that Resources.Load can resolve."""
    import UnityPy
    path = os.path.join(game_data, GGM_FILE)
    if not os.path.isfile(path):
        raise TeamError(f"{GGM_FILE} is not in this folder.")
    env = UnityPy.load(path)
    try:
        rm = next((o for o in env.objects if o.type.name == "ResourceManager"),
                  None)
        if rm is None:
            raise TeamError("No ResourceManager in globalgamemanagers.")
        entries, _end = _parse_resource_container(rm.get_raw_data())
    finally:
        close_env(env)
    return {k: (f, p) for k, f, p in entries}


def resource_strings(raw):
    """[(offset, name)] for every jersey/court/board name in a container."""
    out = []
    for off, text, _slen, _lo, _hi in _strings_with_fit(raw):
        if any(text.startswith(p) for p in RESOURCE_PREFIXES):
            out.append((off, text))
    return out


def detach_resource_art(game_data, path_id, progress=None):
    """Give one team its own jersey, court and board textures.

    Three files are rebuilt, in an order chosen so the game is never left
    pointing at something that does not exist yet:

        resources.assets     the texture copies are created first
        globalgamemanagers   then the names that resolve to them
        sharedassets1.assets and only then does the team point at those names

    Returns the copies' path_ids for the caller to register as mods -- the same
    requirement as detached logos, for the same reason: a copied texture's
    pixels live at an offset in the .resS that only stays valid while something
    keeps re-appending them.
    """
    import UnityPy
    shared, = _require(game_data, CONTAINER_ASSETS_FILE)
    res_path = os.path.join(game_data, RESOURCE_ASSETS_FILE)
    ggm_path = os.path.join(game_data, GGM_FILE)
    for p in (res_path, ggm_path):
        if not os.path.isfile(p):
            raise TeamError(f"{os.path.basename(p)} is not in this folder.")

    script_pid = find_container_script(game_data)
    containers = _read_containers(game_data, script_pid)
    target = next((c for c in containers.values() if c["path_id"] == path_id), None)
    if target is None:
        raise TeamError(f"No team with path_id {path_id}.")
    label = _art_label(target["raw"])

    wanted = resource_strings(target["raw"])
    if not wanted:
        raise TeamError("This team has no jersey or court texture names.")
    distinct = sorted({name for _o, name in wanted})

    known = read_resource_names(game_data)
    if progress:
        progress(f"Looking up {len(distinct)} texture name(s) ...")

    # locate each name's texture in resources.assets
    by_pid = {}
    for name in distinct:
        entry = known.get(name.lower())
        if entry is None:
            raise TeamError(f"'{name}' is not in the game's resource table, so "
                            f"it cannot be copied. Nothing was written.")
        by_pid[name] = entry[1]

    env = UnityPy.load(res_path)
    tex_raw, used_pids = {}, set()
    try:
        for obj in env.objects:
            used_pids.add(obj.path_id)
            if obj.path_id in set(by_pid.values()) and obj.type.name == "Texture2D":
                tex_raw[obj.path_id] = obj.get_raw_data()
    finally:
        close_env(env)
    missing = [n for n, pid in by_pid.items() if pid not in tex_raw]
    if missing:
        raise TeamError("These textures could not be read: "
                        + ", ".join(missing[:4]))

    next_pid = FIRST_RESOURCE_PATH_ID
    while next_pid in used_pids:
        next_pid += 1
    plan, rename = [], {}
    for name in distinct:
        old_pid = by_pid[name]
        new_name = _same_length_name(name, label)
        if new_name.lower() in known:
            raise TeamError(f"The generated name '{new_name}' is already in "
                            f"use. Nothing was written.")
        # Two different names for two different jobs: the RESOURCE key has to
        # match the container string byte for byte, while the OBJECT name is
        # what the Textures tab lists, so that one stays human-readable.
        object_name = f"{CUSTOM_ART_PREFIX}{label}_{name}"[:110]
        raw = _rename_named_object(tex_raw[old_pid], object_name)
        plan.append((old_pid, next_pid, raw))
        rename[name] = (new_name, next_pid)
        next_pid += 1
        while next_pid in used_pids:
            next_pid += 1

    # 1. the copies
    if progress:
        progress(f"Copying {len(plan)} texture(s) into "
                 f"{RESOURCE_ASSETS_FILE} ...")
    backup(res_path)

    def add_textures(env):
        for template, new_pid, raw in plan:
            _add_object(env, template, new_pid, raw)
        return set()

    _rebuild(res_path, add_textures, expect_added=len(plan), progress=progress)

    # 2. the names that resolve to them
    if progress:
        progress("Registering the new names ...")
    backup(ggm_path)

    def add_names(env):
        rm = next(o for o in env.objects if o.type.name == "ResourceManager")
        raw = rm.get_raw_data()
        entries, end = _parse_resource_container(raw)
        tail = raw[end:]
        have = {k for k, _f, _p in entries}
        for _name, (new_name, new_pid) in rename.items():
            key = new_name.lower()
            if key in have:
                entries = [(k, f, p) if k != key else (k, RESOURCE_FILE_ID, new_pid)
                           for k, f, p in entries]
            else:
                entries.append((key, RESOURCE_FILE_ID, new_pid))
        # Unity keeps this list sorted; keep it that way.
        entries.sort(key=lambda e: e[0])
        rm.set_raw_data(_build_resource_container(entries, tail))
        return {rm.path_id}

    _rebuild(ggm_path, add_names, expect_added=0, progress=progress)

    # 3. and only now, point the team at them -- IN PLACE.
    #
    # Every new name was built to the same byte length as the one it replaces,
    # so this is a fixed-width overwrite: sharedassets1.assets is never
    # re-serialized, its object table never moves, and the queued texture mods
    # that live in it keep their offsets. Rebuilding this file to fit a longer
    # name is what corrupted it before.
    if progress:
        progress("Pointing the team at its own textures ...")
    edits = []
    base = target["byte_start"]
    if base is None:
        raise TeamError("Could not locate this team in the file. The textures "
                        "were copied and registered, but the team still points "
                        "at the donor's.")
    for off, name in resource_strings(target["raw"]):
        if name not in rename:
            continue
        new_name = rename[name][0]
        old_bytes = _string_bytes(name)
        new_bytes = _string_bytes(new_name)
        if len(old_bytes) != len(new_bytes):
            raise TeamError(f"'{new_name}' is not the same length as '{name}'. "
                            f"Refusing to write.")
        edits.append((base + off, old_bytes, new_bytes))
    _patch_in_place(shared, edits, "The team's texture names")

    return {"textures": len(plan),
            "names": [n for n, _v in sorted(rename.items())],
            "new_texture_ids": [pid for _n, (_nm, pid) in sorted(rename.items())],
            "assets_path": res_path,
            "search_prefix": f"{CUSTOM_ART_PREFIX}{label}",
            "new_names": [rename[n][0] for n in sorted(rename)],
            # sharedassets1 is patched in place, so it is NOT in this list --
            # nothing in it moved and its mods do not need re-applying.
            "rebuilt": [res_path, ggm_path]}

