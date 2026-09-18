"""
team_slot_selftest.py -- exercise team_slot_poc.py's byte-level logic without
the game installed.

The proof-of-concept script edits serialized Unity bytes by finding values
inside them, so the parts worth testing are exactly the parts that don't need
UnityPy: reading length-prefixed strings, finding the unique-ID field by what
it means rather than where it sits, locating the color overrides, rewriting a
string without moving anything, and spotting a PPtr array.

This builds synthetic MonoBehaviour blobs with the same layout the real
containers use, runs the real functions over them, and checks the results. It
proves the logic, not the game files -- the only test that can prove those is
launching the game after --apply.

    python tools/team_slot_selftest.py
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import team_slot_poc as poc                                     # noqa: E402


def pstr(text):
    """A Unity length-prefixed, 4-byte-aligned string."""
    b = text.encode("utf-8")
    return struct.pack("<i", len(b)) + b + b"\0" * ((-len(b)) % 4)


def color(r, g, b, a=1.0):
    return struct.pack("<4f", r, g, b, a)


def make_container(name, city, nickname, abbrev, team_id, script_pid=555,
                   rgb=(0.8, 0.1, 0.1)):
    """A blob shaped like a real BasketTeamContainer: the fixed MonoBehaviour
    header, m_Name at 28, then strings, the unique ID, and color overrides."""
    raw = b"\0" * 12                                   # m_GameObject PPtr
    raw += b"\x01" + b"\0" * 3                         # m_Enabled + padding
    raw += struct.pack("<iq", 0, script_pid)           # m_Script PPtr
    assert len(raw) == 28
    raw += pstr(name)                                  # m_Name
    raw += pstr(city) + pstr(nickname) + pstr(abbrev)
    raw += struct.pack("<i", team_id)                  # m_iUniqueId
    raw += struct.pack("<ii", 3, 1)                    # conference, division
    for param in ("_Color_Outside_Court_R", "_Color_Area_G"):
        raw += pstr(param)
        raw += struct.pack("<i", 2)                    # two variants
        raw += pstr("default") + color(*rgb)
        raw += pstr("retro_1977") + color(*rgb, a=0.5)
    return raw


TEAMS = [
    ("Boston Celtics",     "Boston",   "Celtics",      "BOS", -1),
    ("Portland Blazers",   "Portland", "Trail Blazers", "POR", -5),
    ("Los Angeles Lakers", "Los Angeles", "Lakers",    "LAL", -23),
    ("East Special",       "East",     "Special",      "EST", -31),
]


def check(label, got, want):
    if got != want:
        raise AssertionError(f"{label}: got {got!r}, wanted {want!r}")
    print(f"  ok  {label}")


def main():
    containers = {name: {"path_id": 1000 + i,
                         "raw": make_container(name, city, nick, ab, tid)}
                  for i, (name, city, nick, ab, tid) in enumerate(TEAMS)}

    print("strings and the unique-ID field")
    n_strings, ids = poc.locate_unique_id(containers)
    check("ids read back", {n: ids[n] for n in ids},
          {t[0]: t[4] for t in TEAMS})
    raw = containers["Portland Blazers"]["raw"]
    id_off = poc.id_offset_for(raw, n_strings)
    check("id offset points at the id",
          struct.unpack_from("<i", raw, id_off)[0], -5)
    check("m_Name", poc.read_str(raw, 28)[0], "Portland Blazers")
    check("script path_id", poc.script_path_id(raw), 555)

    print("color overrides")
    overrides = poc.find_color_overrides(raw)
    check("override count", len(overrides), 4)
    check("properties", sorted({p for _o, p, _v, _c in overrides}),
          ["_Color_Area_G", "_Color_Outside_Court_R"])
    check("variants", sorted({v for _o, _p, v, _c in overrides}),
          ["default", "retro_1977"])

    print("length-preserving rename")
    off = next(o for o, t, _e in poc.find_strings(raw) if t == "Portland")
    renamed = poc.replace_string_in_place(raw, off, "Pythons")
    check("same size", len(renamed), len(raw))
    check("padded with a space", poc.read_str(renamed, off)[0], "Pythons ")
    try:
        poc.replace_string_in_place(raw, off, "Portlandia Pythons")
    except poc.PocError:
        print("  ok  a too-long name is refused")
    else:
        raise AssertionError("a too-long name should have been refused")

    print("building the clone")
    report = []
    clone = poc.build_clone_bytes(raw, id_off, -101, "Portland",
                                  "Pythons", poc.hex_to_rgba("00c853"), report)
    check("clone size", len(clone), len(raw))
    check("new unique id", struct.unpack_from("<i", clone, id_off)[0], -101)
    check("renamed m_Name", poc.read_str(clone, 28)[0].strip(), "Pythons")
    greens = {tuple(round(c, 3) for c in rgba[:3])
              for _o, _p, _v, rgba in poc.find_color_overrides(clone)}
    check("recolored", greens, {(0.0, round(0xc8 / 255, 3), round(0x53 / 255, 3))})
    check("alphas kept",
          sorted(round(rgba[3], 2) for _o, _p, _v, rgba in
                 poc.find_color_overrides(clone)), [0.5, 0.5, 1.0, 1.0])
    for line in report:
        print("      " + line.strip())

    print("finding the pointer array")
    pids = {c["path_id"] for c in containers.values()}
    body = b"\x11" * 16 + struct.pack("<i", len(pids))
    for pid in sorted(pids):
        body += struct.pack("<iq", 2, pid)
    body += b"\x22" * 32
    arrays = poc.scan_pointer_arrays(body, pids)
    check("one array found", len(arrays), 1)
    check("offset/count/fileid", arrays[0], (16, 4, 2))
    check("an array with an unknown pointer is ignored",
          poc.scan_pointer_arrays(body, pids - {1000}), [])

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
