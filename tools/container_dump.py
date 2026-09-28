"""container_dump.py -- what is actually inside a BasketTeamContainer.

Tier 1 ("borrow another team's logo / jerseys") needs two things this answers:

  1. WHICH POINTER IS THE LOGO. A container holds a run of PPtrs -- 12 fixed
     bytes each (int32 m_FileID, int64 m_PathID). Repointing one is an in-place,
     length-preserving write. But we can only repoint the right one if we know
     what each points AT, so every PPtr is resolved to its target's type and
     name (ico_..., txt_..., mat_...). Names identify fields far more reliably
     than byte offsets, which shift with every variable-length string in front
     of them.

  2. WHICH STRING IS THE JERSEY. Jerseys and courts are loaded by name through
     Resources.Load, so borrowing means rewriting a string. A length-prefixed
     string occupies 4 + len, padded to a 4-byte boundary, so a replacement only
     fits without moving anything if it lands in the same alignment bucket. Each
     string is reported with its length and the exact range of lengths that can
     replace it.

Dumps several teams so the fields can be told apart by what differs between
them. Read-only; writes container_dump.txt and container_dump.json.
"""
import json
import os
import struct
import sys

GAME_DATA = sys.argv[1] if len(sys.argv) > 1 else \
    r"I:\SteamLibrary\steamapps\common\NBA BOUNCE\NBA Bounce_Data"
HERE = os.path.dirname(os.path.abspath(__file__))
WANT = [a for a in sys.argv[2:]] or ["Portland", "Boston", "Chicago"]
SCRIPT_CLASS = "BasketTeamContainer"

lines = []


def p(s=""):
    print(s)
    lines.append(s)


def read_str(raw, off):
    (n,) = struct.unpack_from("<i", raw, off)
    if not (0 <= n <= 4096) or off + 4 + n > len(raw):
        raise ValueError
    return raw[off + 4:off + 4 + n].decode("utf-8", "replace"), (off + 4 + n + 3) & ~3


def script_path_id(raw):
    return struct.unpack_from("<iq", raw, 16)[1]


def close_env(env):
    import gc
    if env is None:
        return
    for holder in ("files", "cabs"):
        t = getattr(env, holder, None)
        if isinstance(t, dict):
            for sf in list(t.values()):
                for o in (sf, getattr(sf, "reader", None)):
                    for a in ("Stream", "stream"):
                        st = getattr(o, a, None)
                        if st is not None and hasattr(st, "close"):
                            try:
                                st.close()
                            except Exception:
                                pass
            t.clear()
    gc.collect()


def find_strings(raw, min_len=2, max_len=96):
    out, n = [], len(raw)
    for i in range(0, n - 4, 4):
        (slen,) = struct.unpack_from("<i", raw, i)
        if not (min_len <= slen <= max_len and i + 4 + slen <= n):
            continue
        cand = raw[i + 4:i + 4 + slen]
        if not all(32 <= c < 127 for c in cand):
            continue
        out.append((i, cand.decode("ascii"), slen))
    return out


import UnityPy

assets = os.path.join(GAME_DATA, "sharedassets1.assets")
ggm = os.path.join(GAME_DATA, "globalgamemanagers.assets")

script_pid = None
env = UnityPy.load(ggm)
try:
    for obj in env.objects:
        if obj.type.name == "MonoScript":
            try:
                if getattr(obj.read(), "m_ClassName", "") == SCRIPT_CLASS:
                    script_pid = obj.path_id
                    break
            except Exception:
                pass
finally:
    close_env(env)
p(f"BasketTeamContainer script path_id: {script_pid}")

# every object in the file, so a pointer can be resolved to a real thing
catalog, containers = {}, {}
env = UnityPy.load(assets)
try:
    for obj in env.objects:
        tname = obj.type.name
        name = ""
        try:
            raw0 = obj.get_raw_data()
            if tname != "MonoBehaviour":
                name, _ = read_str(raw0, 0)      # NamedObject opens with m_Name
        except Exception:
            raw0 = b""
        catalog[obj.path_id] = (tname, name)
        if tname == "MonoBehaviour" and raw0:
            try:
                if script_path_id(raw0) == script_pid:
                    cname, _ = read_str(raw0, 28)
                    containers[cname] = {"path_id": obj.path_id, "raw": raw0,
                                         "byte_start": getattr(obj, "byte_start", None)}
                    catalog[obj.path_id] = (tname, cname)
            except Exception:
                pass
finally:
    close_env(env)

p(f"objects in sharedassets1.assets: {len(catalog):,}")
p(f"team containers: {len(containers)}")

targets = []
for want in WANT:
    hit = [n for n in containers if want.lower() in n.lower()]
    targets += hit[:1]
if not targets:
    targets = sorted(containers)[:3]
p(f"dumping: {', '.join(targets)}\n")

report = {}
for name in targets:
    c = containers[name]
    raw = c["raw"]
    p("=" * 74)
    p(f"{name}   path_id {c['path_id']}   {len(raw)} bytes   "
      f"file offset {c['byte_start']}")
    p("=" * 74)

    p("\n-- POINTERS (12 bytes each: int32 m_FileID, int64 m_PathID) ----------")
    p("   Repointing one of these is an in-place write; nothing moves.")
    ptrs = []
    for i in range(0, len(raw) - 12, 4):
        fid, pid = struct.unpack_from("<iq", raw, i)
        if fid < 0 or fid > 8 or pid == 0:
            continue
        if fid == 0 and pid not in catalog:
            continue
        ttype, tname = catalog.get(pid, ("(other file)", ""))
        if fid != 0 and pid not in catalog:
            ttype, tname = "(external)", ""
        ptrs.append({"offset": i, "file_id": fid, "path_id": pid,
                     "type": ttype, "name": tname})
        p(f"   +{i:<5} fileID {fid}  pathID {pid:<12} {ttype:<16} {tname}")
    if not ptrs:
        p("   (none resolved -- pointers may target another assets file)")

    p("\n-- STRINGS (length-prefixed, padded to 4) ----------------------------")
    p("   'fits' is the length range a replacement can have without moving")
    p("   any byte after it.")
    strs = []
    for off, text, slen in find_strings(raw):
        total = (4 + slen + 3) & ~3
        lo = max(1, total - 4 + 1 - 4 + 1)
        lo = total - 4 - 3 + 1 if total - 4 - 3 + 1 > 0 else 1
        hi = total - 4
        lo = max(1, hi - 3)
        strs.append({"offset": off, "text": text, "len": slen,
                     "fits_min": lo, "fits_max": hi})
        p(f"   +{off:<5} len {slen:<3} (fits {lo}-{hi})  {text!r}")
    report[name] = {"path_id": c["path_id"], "byte_start": c["byte_start"],
                    "size": len(raw), "pointers": ptrs, "strings": strs}
    p("")

# what differs between the teams is what identifies a field
if len(targets) >= 2:
    p("=" * 74)
    p("WHICH POINTER SLOTS DIFFER BETWEEN TEAMS (by position in the run)")
    p("=" * 74)
    runs = [report[t]["pointers"] for t in targets]
    for i in range(min(len(r) for r in runs)):
        row = [r[i] for r in runs]
        same = len({(x["path_id"]) for x in row}) == 1
        mark = "same " if same else "DIFFER"
        p(f"  slot {i:<3} {mark}  " + " | ".join(
            f"{t}: {x['type']}/{x['name'] or x['path_id']}"
            for t, x in zip(targets, row)))

with open(os.path.join(HERE, "container_dump.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
with open(os.path.join(HERE, "container_dump.json"), "w", encoding="utf-8") as f:
    json.dump(report, f, indent=1)
print(f"\nwritten: {os.path.join(HERE, 'container_dump.txt')}")
print(f"written: {os.path.join(HERE, 'container_dump.json')}")
