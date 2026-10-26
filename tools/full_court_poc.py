"""full_court_poc.py -- proof of concept: one texture over a whole court.

Today a court is a tiled greyscale wood pattern (_Texture_Parquet, repeated by
_Tiling_Parquet) multiplied by _Color_Parquet, with the lines and painted areas
drawn by separate meshes whose vertex colours pick one of the material's
_Color_* slots. This script tries to replace that with a single full-court
image:

  1. FLOORSLOT_8 (an unused slot texture added by Floor Patterns) is
     re-described as a big DXT1 texture -- width, height, mip count and stream
     pointer are fixed-size ints, so the object patches in place -- and its
     pixels are appended to the .resS.
  2. The team's parquet AND lines materials point _Texture_Parquet at it, with
     _Tiling_Parquet / _Offset_Parquet set so one tile spans the court, and
     _Color_Parquet set to white so the image shows in its own colours.
  3. --hide-lines turns _Court_Lines_ON_OFF off on the lines material, so the
     line meshes sample the same image and disappear into it; the image then
     has to carry its own lines.

Everything is patched size-neutrally. The first run copies the two files to
*.fullcourt_backup; --restore puts them back. Run --restore before using the
app's Apply Mods: that run rewinds the .resS, which would strand the slot.

    python full_court_poc.py --team BostonCeltics                  # grid test
    python full_court_poc.py --team BostonCeltics --hide-lines
    python full_court_poc.py --tiling 0.0349 0.0656 --offset 0.5 0.5
    python full_court_poc.py --image my_court.png --hide-lines
    python full_court_poc.py --restore
"""

import os, sys, shutil, argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "modules"))

import UnityPy
from PIL import Image, ImageDraw, ImageFont
from UnityPy.enums import TextureFormat
from UnityPy.export.Texture2DConverter import image_to_texture2d
from UnityPy.helpers.TypeTreeHelper import write_typetree
from UnityPy.streams.EndianBinaryWriter import EndianBinaryWriter

DEFAULT_GAME = r"C:\Program Files (x86)\Steam\steamapps\common\NBA BOUNCE\NBA Bounce_Data"
COURT_FILE = "sharedassets0.assets"
SLOT = "FLOORSLOT_8"
BACKUP = ".fullcourt_backup"
SIZE = (2048, 1024)


# ---------------------------------------------------------------------------
def test_grid(size=SIZE):
    """Orientation/mapping target: coloured quadrants, a 10% grid, labelled
    corners and a centre cross, so one screenshot shows where u/v land."""
    w, h = size
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    quads = [((0, 0), (200, 60, 60)), ((w // 2, 0), (60, 160, 60)),
             ((0, h // 2), (60, 90, 200)), ((w // 2, h // 2), (210, 190, 50))]
    for (x, y), col in quads:
        d.rectangle([x, y, x + w // 2, y + h // 2], fill=col)
    for i in range(11):
        x, y = round(i * (w - 1) / 10), round(i * (h - 1) / 10)
        lw = 9 if i in (0, 5, 10) else 3
        d.line([(x, 0), (x, h)], fill="white", width=lw)
        d.line([(0, y), (w, y)], fill="white", width=lw)
    try:
        font = ImageFont.truetype("arialbd.ttf", 90)
    except OSError:
        font = ImageFont.load_default()
    for (x, y), text in [((60, 50), "TOP-LEFT  u0 v0"), ((w - 760, 50), "TOP-RIGHT  u1"),
                         ((60, h - 150), "BOTTOM-LEFT  v1"), ((w - 760, h - 150), "BOTTOM-RIGHT")]:
        d.text((x, y), text, fill="black", font=font)
    d.ellipse([w / 2 - 120, h / 2 - 120, w / 2 + 120, h / 2 + 120], outline="black", width=14)
    d.polygon([(w * .85, h * .5 - 60), (w * .95, h * .5), (w * .85, h * .5 + 60)], fill="black")
    # Calibration: where the game's own lines should sit if the image spans
    # exactly the court-lines mesh (border = baselines/sidelines).
    d.rectangle([0, 0, w - 1, h - 1], outline="black", width=24)
    d.line([(w / 2, 0), (w / 2, h)], fill="black", width=16)
    return img


def dxt1_mipchain(img):
    return mipchain(img, TextureFormat.DXT1)


def mipchain(img, fmt):
    """Full mip chain down to 1x1. The game's loader rejects a partial
    chain (one that stops at 4x4) even when the byte count matches."""
    out, cur, mips = b"", img.convert("RGBA"), 0
    while True:
        data, _ = image_to_texture2d(cur, fmt)
        out += data
        mips += 1
        w, h = cur.size
        if w == 1 and h == 1:
            return out, mips
        cur = cur.resize((max(1, w // 2), max(1, h // 2)), Image.LANCZOS)


# ---------------------------------------------------------------------------
def patch_object(path, obj, d):
    """Re-serialize a typetree and splice it over the original bytes -- only
    if it is exactly the same size, so no other object moves."""
    w = EndianBinaryWriter(endian=obj.reader.endian)
    write_typetree(d, obj._get_typetree_node(), w, obj.assets_file)
    blob = w.bytes
    if len(blob) != obj.byte_size:
        raise RuntimeError(f"{d.get('m_Name')} changed size ({obj.byte_size} -> {len(blob)})")
    return obj.byte_start, blob


def set_pair(pairs, key, value):
    for i, p in enumerate(pairs):
        if p[0] == key:
            pairs[i] = (key, value)
            return
    raise KeyError(key)


def apply(game, team, img, tiling, offset, hide_lines, blank_logo=None):
    assets = os.path.join(game, COURT_FILE)
    env = UnityPy.load(assets)
    patches = []

    slot = next((o for o in env.objects if o.type.name == "Texture2D" and o.peek_name() == SLOT), None)
    if slot is None:
        raise SystemExit(f"{SLOT} not found -- open Floor Patterns and Apply once so the slots exist.")
    data, mips = dxt1_mipchain(img)
    t = slot.read_typetree()
    ress = os.path.join(game, os.path.basename(t["m_StreamData"]["path"]))
    t.update(m_Width=img.width, m_Height=img.height, m_MipCount=mips,
             m_TextureFormat=int(TextureFormat.DXT1), m_CompleteImageSize=len(data))
    # Unity's async texture loader fails (and the game hangs at launch) when a
    # stream doesn't start on a 16-byte boundary, so pad the .resS first.
    end = os.path.getsize(ress)
    pad = -end % 16
    t["m_StreamData"]["offset"] = end + pad
    t["m_StreamData"]["size"] = len(data)
    patches.append(patch_object(assets, slot, t))

    names = {f"mat_bounce_stadium_parquet_{team}", f"mat_bounce_stadium_lines_{team}"}
    found = set()
    for o in env.objects:
        if o.type.name != "Material" or o.peek_name() not in names:
            continue
        d = o.read_typetree()
        props = d["m_SavedProperties"]
        tex = dict((k, v) for k, v in props["m_TexEnvs"])["_Texture_Parquet"]["m_Texture"]
        tex["m_FileID"], tex["m_PathID"] = 0, int(slot.path_id)
        set_pair(props["m_Colors"], "_Tiling_Parquet", {"r": tiling[0], "g": tiling[1], "b": 0.0, "a": 0.0})
        set_pair(props["m_Colors"], "_Offset_Parquet", {"r": offset[0], "g": offset[1], "b": 0.0, "a": 0.0})
        set_pair(props["m_Colors"], "_Color_Parquet", {"r": 1.0, "g": 1.0, "b": 1.0, "a": 0.0})
        set_pair(props["m_Floats"], "_Rotate_Parquet", 0.0)
        if hide_lines:
            # Lines off, and _ONLY_PARQUET drops the colour zones (apron,
            # keys) the shader paints over the parquet from its court mask.
            set_pair(props["m_Floats"], "_Court_Lines_ON_OFF", 0.0)
            set_pair(props["m_Floats"], "_ONLY_PARQUET", 1.0)
        patches.append(patch_object(assets, o, d))
        found.add(d["m_Name"])
    if found != names:
        raise SystemExit(f"Missing materials: {sorted(names - found)}")
    if hide_lines:
        # The centre logo is the decorations material, its texture set at
        # runtime (alpha clipping is compiled out, so _Alpha_Clipping does
        # nothing). A zero texture scale samples one corner texel -- the
        # transparent margin of the logo -- so the whole decal vanishes.
        deco = f"mat_bounce_stadium_decorations_{team}"
        o = next((o for o in env.objects if o.type.name == "Material" and o.peek_name() == deco), None)
        if o is not None:
            d = o.read_typetree()
            env_ = dict(d["m_SavedProperties"]["m_TexEnvs"])["_Decoration_Texture"]
            env_["m_Scale"] = {"x": 0.0, "y": 0.0}
            env_["m_Offset"] = {"x": 0.002, "y": 0.002}
            patches.append(patch_object(assets, o, d))
    extra = b""
    # The centre logo isn't per-team: the arena scene has one shared logo
    # material, and the game copies the team's CURRENTLY SELECTED logo (the
    # save's iCurrentLogoIndex picks one of the container's logo sprites) into
    # it. Making that texture transparent hides it -- and also wherever else
    # the game shows that logo.
    for name in blank_logo or ():
        o = next((o for o in env.objects if o.type.name == "Texture2D" and o.peek_name() == name), None)
        if o is None:
            raise SystemExit(f"Texture {name} not found")
        t = o.read_typetree()
        clear, lmips = mipchain(Image.new("RGBA", (t["m_Width"], t["m_Height"]), (0, 0, 0, 0)), TextureFormat.DXT5)
        at = end + pad + len(data) + len(extra)
        lpad = -at % 16
        t.update(m_TextureFormat=int(TextureFormat.DXT5), m_MipCount=lmips, m_CompleteImageSize=len(clear))
        t["m_StreamData"]["offset"] = at + lpad
        t["m_StreamData"]["size"] = len(clear)
        patches.append(patch_object(assets, o, t))
        extra += b"\0" * lpad + clear
    env = None

    with open(ress, "ab") as f:
        f.write(b"\0" * pad + data + extra)
    with open(assets, "r+b") as f:
        for start, blob in patches:
            f.seek(start)
            f.write(blob)
    print(f"{team}: {img.width}x{img.height} DXT1, {mips} mips, {len(data):,} bytes -> {SLOT}; "
          f"tiling {tiling}, offset {offset}, lines {'hidden' if hide_lines else 'kept'}")


def backup_paths(game):
    a = os.path.join(game, COURT_FILE)
    return [a, a + ".resS"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", default=DEFAULT_GAME, help="the NBA Bounce_Data folder")
    ap.add_argument("--team", default="BostonCeltics")
    ap.add_argument("--image", help="full-court PNG (default: generated test grid)")
    ap.add_argument("--tiling", type=float, nargs=2, default=(1 / 28.8, 1 / 15.4))  # court-lines mesh is 28.8 x 15.4 m
    ap.add_argument("--offset", type=float, nargs=2, default=(0.5, 0.5))
    ap.add_argument("--size", type=int, nargs=2, default=SIZE, help="test grid size")
    ap.add_argument("--hide-lines", action="store_true")
    ap.add_argument("--blank-logo", metavar="TEXTURE", nargs="+", help="make this logo texture transparent, e.g. CBulls_Global")
    ap.add_argument("--save-grid", help="write the test grid PNG here and exit")
    ap.add_argument("--restore", action="store_true")
    a = ap.parse_args()

    if a.save_grid:
        test_grid().save(a.save_grid)
        return
    if a.restore:
        for p in backup_paths(a.game):
            if os.path.exists(p + BACKUP):
                shutil.copy2(p + BACKUP, p)
                print("restored", os.path.basename(p))
        return
    # Each run starts from the pre-PoC files, so runs never stack up.
    for p in backup_paths(a.game):
        if os.path.exists(p + BACKUP):
            shutil.copy2(p + BACKUP, p)
        else:
            shutil.copy2(p, p + BACKUP)
    img = Image.open(a.image).convert("RGB") if a.image else test_grid(tuple(a.size))
    # The game only loads power-of-two textures; tiling is in metres, so a
    # resize changes resolution, not where the image lands.
    img = img.resize(SIZE, Image.LANCZOS) if a.image else img
    apply(a.game, a.team, img, a.tiling, a.offset, a.hide_lines, a.blank_logo)


if __name__ == "__main__":
    main()
