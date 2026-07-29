"""
save_manager.py -- Save-file backend for the NBA Bounce Mod Manager.

Self-contained, exactly like slider_manager.py: imports nothing from app.py,
audio_manager.py or slider_tab.py, and never calls apply_single_mod(). It does
not touch .assets files at all -- it only reads the game's level1 scene (to
build the unlockable catalog) and reads/writes the player's .sav file in
%USERPROFILE%\\AppData\\LocalLow\\UnfinishedPixel\\NBA Bounce\\.

Save format
-----------
NBABounce18.sav is a plain .NET BinaryFormatter serialisation of a single class,
bounce.SaveData. No encryption, no checksum, no signature, no account binding.
The object graph is flattened into path-style member names:

    /m_fVolumeMusic
    /m_aoUnlockedUnlockables_COUNT_
    /m_aoUnlockedUnlockables/0
    /m_aoSavedAvatars/12/values/33/m_name

Arrays are a "<name>_COUNT_" int followed by contiguous "<name>/0", "<name>/1"
members, so growing an array means inserting member names, type bytes AND
values -- not just bumping the count.

Three traps this module exists to handle
----------------------------------------
1. Object IDs. Every string/enum record carries a 4-byte object id, and .NET
   registers objects in ONE id space keyed by ABSOLUTE value. Enum records use
   NEGATIVE ids. Allocating new ids from max(signed)+1 collides with them and
   the game dies with "An object cannot be registered twice". Always allocate
   from max(abs)+1. validate() enforces this.

2. m_bEnabled. UnlockablesManager.loadUnlocks() resolves each saved id against
   getUnlockablesEnabled(), which is m_aoUnlockables.Where(u => u.m_bEnabled).
   34 of the 333 shipped unlockables have m_bEnabled = 0. Writing one of those
   into the save makes .First() throw "Sequence contains no matching element"
   and the game hangs on the loading screen. extract_catalog() filters on it.

3. Steam Cloud. The save folder is synced by Steam Auto-Cloud (path-based, not
   the ISteamRemoteStorage API -- note the steam_autocloud.vdf that Steam drops
   in the folder). Steam re-syncs when it launches the game, so a save edited
   while Steam is running, or with cloud sync still enabled, gets silently
   replaced by the cloud copy. See STEAM_CLOUD_WARNING.

Public API
----------
    find_save_dir()                      -> str | None
    find_save_files()                    -> [str]
    backup_save(path, backup_dir=None)   -> str
    extract_catalog(game_data_path, ...) -> {"main": [...], "goat": [...]}
    read_unlocks(save_path)              -> {"main": [...], "goat": [...]}
    build_unlocked(src, dst, catalog, ...) -> dict summary

Every write path round-trips the input byte-for-byte before editing and
validates the output before returning. If any gate fails it raises SaveError
and leaves the destination untouched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_CACHE = os.path.join(APP_DIR, "unlockables_catalog.json")

SAVE_COMPANY = "UnfinishedPixel"
SAVE_PRODUCT = "NBA Bounce"
STEAM_APPID = "3115130"

MAIN_ARRAY = "/m_aoUnlockedUnlockables"
SEEN_ARRAY = "/m_aoUnlocksToBeSeen"
GOAT_ARRAY = "/m_oMountainUnlocks/m_aoGoatUnlockedUnlockables"
GOAT_SEEN_ARRAY = "/m_oMountainUnlocks/m_aoGOATUnlocksToBeSeen"

ID_PATTERN = re.compile(r"^(?:MASCOT|JERSEY|LOGO|BALL|TRAIL|ACH|DLC|GOAT)_[A-Z0-9_]+$")

STEAM_CLOUD_WARNING = (
    "Close Steam before applying a save edit.\n\n"
    "This game uses Steam Auto-Cloud. Steam re-syncs the save folder every time "
    "it launches the game, so if Steam is running -- or if cloud sync is still "
    "enabled -- your edited save can be silently replaced by the copy stored in "
    "the cloud, and it will look like nothing happened.\n\n"
    "Recommended order:\n"
    "  1. Steam \u2192 NBA BOUNCE \u2192 Properties \u2192 General \u2192\n"
    "     uncheck \"Keep game saves in the Steam Cloud\"\n"
    "  2. Exit Steam completely (check the tray AND Task Manager for\n"
    "     steam.exe and steamwebhelper.exe)\n"
    "  3. Apply the edit here\n"
    "  4. Start Steam, launch the game, confirm your unlocks\n"
    "  5. Re-enable cloud sync -- Steam uploads the new save as authoritative"
)


class SaveError(Exception):
    """Raised when a save cannot be parsed, or a safety gate fails."""


# ---------------------------------------------------------------------------
# BinaryFormatter primitives
# ---------------------------------------------------------------------------

_PRIM = {1: "Boolean", 2: "Byte", 3: "Char", 5: "Decimal", 6: "Double", 7: "Int16",
         8: "Int32", 9: "Int64", 10: "SByte", 11: "Single", 12: "TimeSpan",
         13: "DateTime", 14: "UInt16", 15: "UInt32", 16: "UInt64", 17: "Null",
         18: "String"}
_PRIM_INV = {v: k for k, v in _PRIM.items()}
_SZ = {"Boolean": 1, "Byte": 1, "SByte": 1, "Int16": 2, "Int32": 4,
       "Single": 4, "Int64": 8, "Double": 8}

_LIBRARY = ("Assembly-CSharp, Version=0.0.0.0, Culture=neutral, "
            "PublicKeyToken=null")


class _Reader:
    __slots__ = ("d", "p")

    def __init__(self, d):
        self.d = d
        self.p = 0

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def s7(self):
        n = 0
        sh = 0
        while True:
            b = self.u8()
            n |= (b & 0x7F) << sh
            if not (b & 0x80):
                break
            sh += 7
        v = self.d[self.p:self.p + n]
        self.p += n
        return v.decode("utf-8")


def _w7(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _wstr(s):
    b = s.encode("utf-8")
    return _w7(len(b)) + b


def load_save(path):
    """Parse a .sav into an editable model. Raises SaveError on bad input."""
    try:
        with open(path, "rb") as f:
            d = f.read()
    except OSError as e:
        raise SaveError(f"Could not read {path}: {e}") from e
    try:
        r = _Reader(d)
        r.u8(); r.i32(); r.i32(); r.i32(); r.i32()      # header
        r.u8(); r.i32(); r.s7()                          # BinaryLibrary
        r.u8(); r.i32()
        cls = r.s7()
        mc = r.i32()
        names = [r.s7() for _ in range(mc)]
        bt = [r.u8() for _ in range(mc)]
        addl = []
        for t in bt:
            if t == 0:
                addl.append(("prim", _PRIM[r.u8()]))
            elif t == 1:
                addl.append(("string", None))
            elif t == 4:
                addl.append(("class", (r.s7(), r.i32())))
            else:
                raise SaveError(f"unsupported member type {t}")
        r.i32()                                          # libraryId

        classdefs, enums, maxid, spans = {}, {}, [1], []
        strtab = {}

        def rec():
            rt = r.u8()
            if rt == 6:
                oid = r.i32(); strtab[oid] = r.s7()
                maxid[0] = max(maxid[0], abs(oid)); return
            if rt == 9:
                r.i32(); return
            if rt == 10:
                return
            if rt == 5:
                oid = r.i32(); nm = r.s7(); n = r.i32()
                maxid[0] = max(maxid[0], abs(oid))
                [r.s7() for _ in range(n)]
                b2 = [r.u8() for _ in range(n)]
                a2 = []
                for t in b2:
                    if t == 0:
                        a2.append(("prim", _PRIM[r.u8()]))
                    elif t == 1:
                        a2.append(("string", None))
                    elif t == 4:
                        a2.append(("class", (r.s7(), r.i32())))
                    else:
                        raise SaveError(f"unsupported enum member type {t}")
                r.i32()
                classdefs[nm] = (b2, a2); enums[oid] = nm
                for t, a in zip(b2, a2):
                    if t == 0:
                        r.p += _SZ[a[1]]
                    else:
                        rec()
                return
            if rt == 1:
                oid = r.i32(); mid = r.i32()
                maxid[0] = max(maxid[0], abs(oid))
                b2, a2 = classdefs[enums[mid]]
                for t, a in zip(b2, a2):
                    if t == 0:
                        r.p += _SZ[a[1]]
                    else:
                        rec()
                return
            raise SaveError(f"unknown record type {rt} at offset {r.p}")

        for t, a in zip(bt, addl):
            st = r.p
            if t == 0:
                r.p += _SZ[a[1]]
            else:
                rec()
            spans.append(d[st:r.p])

        if r.p + 1 != len(d) or d[r.p] != 0x0B:
            raise SaveError("trailer mismatch - file may be truncated")
    except SaveError:
        raise
    except Exception as e:
        raise SaveError(f"{os.path.basename(path)} is not a readable "
                        f"NBA Bounce save: {e}") from e

    return {"raw": d, "cls": cls, "names": names, "bt": bt,
            "addl": addl, "spans": spans, "maxid": maxid[0], "strtab": strtab}


def build_save(m):
    """Serialise a model back to bytes."""
    o = bytearray()
    o += b"\x00" + struct.pack("<iiii", 1, -1, 1, 0)
    o += b"\x0c" + struct.pack("<i", 2) + _wstr(_LIBRARY)
    o += b"\x05" + struct.pack("<i", 1) + _wstr(m["cls"])
    o += struct.pack("<i", len(m["names"]))
    for n in m["names"]:
        o += _wstr(n)
    o += bytes(m["bt"])
    for t, a in zip(m["bt"], m["addl"]):
        if t == 0:
            o.append(_PRIM_INV[a[1]])
        elif t == 4:
            o += _wstr(a[1][0]) + struct.pack("<i", a[1][1])
    o += struct.pack("<i", 2)
    for s in m["spans"]:
        o += s
    o += b"\x0b"
    return bytes(o)


def _index(m):
    return {n: i for i, n in enumerate(m["names"])}


def read_array(m, base):
    """Read a string array member as a list of str.

    Handles both record kinds: BinaryObjectString (0x06, inline text) and
    MemberReference (0x09, a pointer to a string emitted earlier). The writer
    uses references for any repeated string, so m_aoUnlocksToBeSeen -- which
    duplicates ids already in m_aoUnlockedUnlockables -- is stored almost
    entirely as references. Parsing those as inline text yields garbage.
    """
    idx = _index(m)
    strtab = m.get("strtab", {})
    key = base + "_COUNT_"
    if key not in idx:
        return []
    n = struct.unpack("<i", m["spans"][idx[key]])[0]
    out = []
    for j in range(n):
        sp = m["spans"][idx[f"{base}/{j}"]]
        if not sp:
            continue
        if sp[0] == 0x09:                        # MemberReference
            oid = struct.unpack_from("<i", sp, 1)[0]
            if oid in strtab:
                out.append(strtab[oid])
            continue
        if sp[0] == 0x0A:                        # ObjectNull
            continue
        if sp[0] != 0x06:
            continue
        p = 5
        ln = 0
        sh = 0
        while True:
            c = sp[p]; p += 1
            ln |= (c & 0x7F) << sh
            if not (c & 0x80):
                break
            sh += 7
        out.append(sp[p:p + ln].decode("utf-8"))
    return out


def set_array(m, base, ids, next_id):
    """Replace a contiguous string array with `ids`. Returns the next free id."""
    idx = _index(m)
    ci = idx[base + "_COUNT_"]
    old = struct.unpack("<i", m["spans"][ci])[0]
    first = idx[base + "/0"] if old else ci + 1
    if old:
        if first != ci + 1:
            raise SaveError(f"{base}: elements are not adjacent to the count")
        for j in range(old):
            if m["names"][first + j] != f"{base}/{j}":
                raise SaveError(f"{base}: element {j} is out of order")
    lo, hi = first, first + old
    spans = []
    for s in ids:
        spans.append(b"\x06" + struct.pack("<i", next_id) + _wstr(s))
        next_id += 1
    m["names"][lo:hi] = [f"{base}/{j}" for j in range(len(ids))]
    m["bt"][lo:hi] = [1] * len(ids)
    m["addl"][lo:hi] = [("string", None)] * len(ids)
    m["spans"][lo:hi] = spans
    m["spans"][ci] = struct.pack("<i", len(ids))
    return next_id


def validate(path):
    """Re-scan a built file. Object ids must be unique by ABSOLUTE value and
    every MemberReference must resolve. Returns (object_count, max_abs_id)."""
    m = load_save(path)
    d = m["raw"]
    r = _Reader(d)
    r.u8(); r.i32(); r.i32(); r.i32(); r.i32()
    r.u8(); r.i32(); r.s7()
    r.u8(); r.i32(); r.s7()
    mc = r.i32()
    [r.s7() for _ in range(mc)]
    bt = [r.u8() for _ in range(mc)]
    addl = []
    for t in bt:
        if t == 0:
            addl.append(("prim", _PRIM[r.u8()]))
        elif t == 1:
            addl.append(("string", None))
        elif t == 4:
            addl.append(("class", (r.s7(), r.i32())))
    r.i32()
    ids, refs, classdefs, enums = [], [], {}, {}

    def rec():
        rt = r.u8()
        if rt == 6:
            ids.append(r.i32()); r.s7(); return
        if rt == 9:
            refs.append(r.i32()); return
        if rt == 10:
            return
        if rt == 5:
            oid = r.i32(); nm = r.s7(); n = r.i32(); ids.append(oid)
            [r.s7() for _ in range(n)]
            b2 = [r.u8() for _ in range(n)]
            a2 = []
            for t in b2:
                if t == 0:
                    a2.append(("prim", _PRIM[r.u8()]))
                elif t == 1:
                    a2.append(("string", None))
                elif t == 4:
                    a2.append(("class", (r.s7(), r.i32())))
            r.i32(); classdefs[nm] = (b2, a2); enums[oid] = nm
            for t, a in zip(b2, a2):
                if t == 0:
                    r.p += _SZ[a[1]]
                else:
                    rec()
            return
        if rt == 1:
            oid = r.i32(); mid = r.i32(); ids.append(oid)
            b2, a2 = classdefs[enums[mid]]
            for t, a in zip(b2, a2):
                if t == 0:
                    r.p += _SZ[a[1]]
                else:
                    rec()
            return
        raise SaveError(f"unknown record {rt}")

    for t, a in zip(bt, addl):
        if t == 0:
            r.p += _SZ[a[1]]
        else:
            rec()

    absids = [abs(i) for i in ids]
    if len(set(absids)) != len(absids):
        seen, dup = set(), set()
        for i in absids:
            (dup if i in seen else seen).add(i)
        raise SaveError(
            f"{len(dup)} duplicate object ids (by absolute value): "
            f"{sorted(dup)[:8]}. Enum records use negative ids -- new ids must "
            f"start above max(abs(id)).")
    dangling = set(map(abs, refs)) - set(absids)
    if dangling:
        raise SaveError(f"dangling MemberReferences: {sorted(dangling)[:8]}")
    return len(ids), max(absids)


# ---------------------------------------------------------------------------
# Save discovery / backup
# ---------------------------------------------------------------------------

def localow_roots():
    """Every plausible ...\\AppData\\LocalLow on this machine, deduped.

    Unity writes persistentDataPath under LocalLow. USERPROFILE is the reliable
    anchor; LOCALAPPDATA's sibling is a second route in case the profile has
    been relocated. On non-Windows both simply won't exist.
    """
    out, seen = [], set()
    for cand in (
        os.path.join(os.environ.get("USERPROFILE", "") or os.path.expanduser("~"),
                     "AppData", "LocalLow"),
        os.path.join(os.path.dirname(os.environ.get("LOCALAPPDATA", "")), "LocalLow")
        if os.environ.get("LOCALAPPDATA") else "",
    ):
        if not cand or not os.path.isdir(cand):
            continue
        key = os.path.normcase(os.path.abspath(cand))
        if key not in seen:
            seen.add(key)
            out.append(cand)
    return out


def _child_ci(parent, name):
    """Case-insensitive child lookup."""
    exact = os.path.join(parent, name)
    if os.path.isdir(exact):
        return exact
    try:
        for entry in os.listdir(parent):
            if entry.lower() == name.lower():
                p = os.path.join(parent, entry)
                if os.path.isdir(p):
                    return p
    except OSError:
        pass
    return None


def _holds_save(d):
    try:
        return any(f.lower().startswith("nbabounce") and f.lower().endswith(".sav")
                   for f in os.listdir(d))
    except OSError:
        return False


def find_save_dir():
    """Locate ...\\AppData\\LocalLow\\UnfinishedPixel\\NBA Bounce, or None.

    Tries the documented path first, then falls back to scanning LocalLow for
    any folder that actually contains an NBABounce*.sav -- which covers a
    renamed company/product folder after a publisher change.
    """
    roots = localow_roots()
    fallback = None
    for root in roots:
        company = _child_ci(root, SAVE_COMPANY)
        if not company:
            continue
        product = _child_ci(company, SAVE_PRODUCT)
        if product:
            if _holds_save(product):
                return product
            fallback = fallback or product
    if fallback:
        return fallback
    # last resort: any LocalLow/<company>/<product> holding a save
    for root in roots:
        try:
            companies = os.listdir(root)
        except OSError:
            continue
        for c in companies:
            cp = os.path.join(root, c)
            if not os.path.isdir(cp):
                continue
            try:
                products = os.listdir(cp)
            except OSError:
                continue
            for p in products:
                pp = os.path.join(cp, p)
                if os.path.isdir(pp) and _holds_save(pp):
                    return pp
    return None


def find_save_files(save_dir=None):
    """Every NBABounce*.sav in the save folder, newest first."""
    d = save_dir or find_save_dir()
    if not d or not os.path.isdir(d):
        return []
    out = [os.path.join(d, f) for f in os.listdir(d)
           if f.lower().startswith("nbabounce") and f.lower().endswith(".sav")]
    out.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return out


def default_backup_dir():
    d = find_save_dir()
    if not d:
        return os.path.join(APP_DIR, "save_backups")
    # deliberately OUTSIDE the synced folder so backups don't burn cloud quota
    return os.path.join(os.path.dirname(os.path.dirname(d)),
                        "NBA_Bounce_Save_Backups")


def backup_save(path, backup_dir=None):
    """Timestamped copy, preserving mtime. Returns the backup path."""
    bd = backup_dir or default_backup_dir()
    os.makedirs(bd, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = os.path.splitext(os.path.basename(path))[0]
    dst = os.path.join(bd, f"{base}_BACKUP_{stamp}.sav")
    shutil.copy2(path, dst)
    return dst


def steam_cloud_enabled_hint():
    """Best-effort read of Steam's per-app cloud flag. None if undetermined.

    Steam stores this in localconfig.vdf as a binary blob, so this only reports
    whether the app has a cloud section at all -- treat a True as 'probably on'
    and always show STEAM_CLOUD_WARNING regardless.
    """
    for root in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        ud = os.path.join(root, "userdata")
        if not os.path.isdir(ud):
            continue
        for acct in os.listdir(ud):
            p = os.path.join(ud, acct, STEAM_APPID, "remotecache.vdf")
            if os.path.exists(p):
                return True
    return None


# ---------------------------------------------------------------------------
# Unlockable catalog (extracted from the game, filtered on m_bEnabled)
# ---------------------------------------------------------------------------

def _read_len_str(b, p):
    if p + 4 > len(b):
        return None, p
    ln = struct.unpack_from("<i", b, p)[0]
    if ln < 0 or ln > 200 or p + 4 + ln > len(b):
        return None, p
    try:
        return b[p + 4:p + 4 + ln].decode("ascii"), (p + 4 + ln + 3) & ~3
    except Exception:
        return None, (p + 4 + ln + 3) & ~3


def _records_from_blob(b):
    """Yield (id, m_bEnabled) for every Unlockable record in a MonoBehaviour.

    bounce.Unlockable is laid out m_bEnabled, m_sID, m_eUnlockType, m_iTeamId,
    m_iNumWin, m_iAchievementId, ... so the enabled flag is the aligned int
    immediately before the id string.
    """
    out = []
    p = 0
    while p < len(b) - 4:
        s, p2 = _read_len_str(b, p)
        if s and ID_PATTERN.match(s):
            en = struct.unpack_from("<i", b, p - 4)[0] if p >= 4 else 0
            out.append((s, en))
            p = p2
            continue
        p += 4
    return out


def extract_catalog(game_data_path, use_cache=True, progress=None):
    """Build {"main": [...], "goat": [...]} of ENABLED unlockable ids.

    Reads the game's level1 scene with UnityPy. Cached to
    unlockables_catalog.json keyed on level1's size+mtime, so a game update
    invalidates it automatically -- important, because a patch that flips an
    m_bEnabled flag would otherwise leave a stale id in the cache and put the
    loading-screen hang right back.
    """
    level1 = os.path.join(game_data_path, "level1")
    if not os.path.exists(level1):
        raise SaveError(f"level1 not found in {game_data_path}")
    st = os.stat(level1)
    key = f"{st.st_size}:{int(st.st_mtime)}"

    if use_cache and os.path.exists(CATALOG_CACHE):
        try:
            with open(CATALOG_CACHE, encoding="utf-8") as f:
                c = json.load(f)
            if c.get("key") == key and c.get("main"):
                return {"main": c["main"], "goat": c.get("goat", [])}
        except Exception:
            pass

    if progress:
        progress("Reading unlockable catalog from level1 ...")
    try:
        import UnityPy
    except ImportError as e:
        raise SaveError("UnityPy is required to read the unlockable catalog. "
                        "Run SETUP_AND_RUN.bat.") from e

    env = UnityPy.load(level1)
    best_main, best_goat = [], []
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            raw = obj.get_raw_data()
        except Exception:
            continue
        recs = _records_from_blob(raw)
        if len(recs) < 20:
            continue
        goatish = sum(1 for s, _ in recs if s.startswith("GOAT_"))
        if goatish > len(recs) / 2:
            if len(recs) > len(best_goat):
                best_goat = recs
        elif len(recs) > len(best_main):
            best_main = recs

    if not best_main:
        raise SaveError("Could not find the unlockables list in level1. "
                        "The game may have been updated.")

    main = [s for s, en in best_main if en == 1]
    goat = [s for s, en in best_goat if en == 1]
    if not main:
        raise SaveError("Found the unlockables list but every entry was "
                        "disabled -- refusing to produce an empty catalog.")

    try:
        with open(CATALOG_CACHE, "w", encoding="utf-8") as f:
            json.dump({"key": key, "main": main, "goat": goat,
                       "main_total": len(best_main),
                       "goat_total": len(best_goat)}, f, indent=1)
    except OSError:
        pass
    return {"main": main, "goat": goat}


# ---------------------------------------------------------------------------
# Read / write unlocks
# ---------------------------------------------------------------------------

def read_unlocks(save_path):
    m = load_save(save_path)
    return {"main": read_array(m, MAIN_ARRAY),
            "goat": read_array(m, GOAT_ARRAY),
            "seen": read_array(m, SEEN_ARRAY)}


def build_unlocked(src, dst, catalog, ids=None, include_goat=False,
                   clear_popups=True):
    """Write `dst` with the unlock arrays expanded.

    ids           explicit id list; defaults to the whole enabled main catalog
    include_goat  also expand the Mountain/GOAT array (union with what's there)
    clear_popups  empty the "new unlock!" queues so the game doesn't queue
                  hundreds of popups on first launch

    Safety gates, all fatal:
      * src must round-trip byte-identically before any edit
      * every id written must be in the enabled catalog (the m_bEnabled trap)
      * no id already present may be dropped
      * dst must pass validate()
    """
    m = load_save(src)
    if build_save(m) != m["raw"]:
        raise SaveError(
            "This save did not round-trip byte-for-byte, so it is not safe to "
            "edit. It may come from a different game version.")

    enabled_main = list(catalog.get("main") or [])
    enabled_goat = list(catalog.get("goat") or [])
    if not enabled_main:
        raise SaveError("Empty unlockable catalog.")

    before_main = read_array(m, MAIN_ARRAY)
    before_goat = read_array(m, GOAT_ARRAY)

    target_main = list(ids) if ids is not None else list(enabled_main)
    allowed = set(enabled_main)
    # Only ids we ADD have to be in the enabled catalog. Ids already in the save
    # are preserved verbatim whatever they are: the game mints its own at
    # runtime via calculateRandomUnlockable() (RANDOM_ARM_R_FULL and friends),
    # and those appear in no static catalog. Dropping them would lose cosmetics
    # the player actually owns.
    bad = [i for i in target_main if i not in allowed and i not in before_main]
    if bad:
        raise SaveError(
            f"{len(bad)} id(s) are not in the enabled catalog and would make "
            f"the game hang on the loading screen: {bad[:6]}")
    # never lose what the player already earned
    existing = set(target_main)
    for i in before_main:
        if i not in existing:
            target_main.append(i)
            existing.add(i)

    next_id = m["maxid"] + 1                      # maxid is the ABS high-water mark
    next_id = set_array(m, MAIN_ARRAY, target_main, next_id)
    if clear_popups:
        next_id = set_array(m, SEEN_ARRAY, [], next_id)
    if include_goat and enabled_goat:
        merged = list(dict.fromkeys(before_goat + enabled_goat))
        next_id = set_array(m, GOAT_ARRAY, merged, next_id)
        if clear_popups:
            next_id = set_array(m, GOAT_SEEN_ARRAY, [], next_id)

    out = build_save(m)
    tmp = dst + ".tmp"
    with open(tmp, "wb") as f:
        f.write(out)
    try:
        nobj, hi = validate(tmp)
        m2 = load_save(tmp)
        if build_save(m2) != out:
            raise SaveError("output failed its own round-trip")
        after_main = read_array(m2, MAIN_ARRAY)
        after_goat = read_array(m2, GOAT_ARRAY)
        if not set(before_main) <= set(after_main):
            raise SaveError("existing unlocks would be dropped")
        if not set(before_goat) <= set(after_goat):
            raise SaveError("existing GOAT unlocks would be dropped")
        added = [i for i in after_main if i not in before_main]
        if any(i not in allowed for i in added):
            raise SaveError("wrote an id outside the enabled catalog")
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, dst)

    return {"main_before": len(before_main), "main_after": len(after_main),
            "goat_before": len(before_goat), "goat_after": len(after_goat),
            "objects": nobj, "max_abs_id": hi, "bytes": len(out),
            "catalog_main": len(enabled_main), "catalog_goat": len(enabled_goat)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print(__doc__)
        print("\nusage: python save_manager.py <game_data_path> <in.sav> "
              "[out.sav] [--goat]")
        sys.exit(1)
    gd, src = sys.argv[1], sys.argv[2]
    dst = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") \
        else src + ".unlocked"
    cat = extract_catalog(gd, progress=print)
    print(f"catalog: {len(cat['main'])} enabled main, "
          f"{len(cat['goat'])} enabled GOAT")
    cur = read_unlocks(src)
    print(f"current: {len(cur['main'])} main, {len(cur['goat'])} GOAT")
    print(f"backup:  {backup_save(src)}")
    info = build_unlocked(src, dst, cat, include_goat="--goat" in sys.argv)
    print(f"main {info['main_before']} -> {info['main_after']}, "
          f"GOAT {info['goat_before']} -> {info['goat_after']}")
    print(f"validated {info['objects']} objects, wrote {dst}")
