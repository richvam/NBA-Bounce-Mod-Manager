"""Sprite crop repair -- why an imported logo can come out cut off in-game.

Every logo texture in NBA Bounce is fronted by a Unity **Sprite**, and every one
of those sprites was imported with Mesh Type = Tight. That means two things were
baked at build time from the *original* artwork's opaque pixels:

  * ``m_RD.textureRect`` -- a tight crop box around the old logo. For
    ``GSWarriors_Global`` it is 513.9 x 618.9 at (259, 193) inside a 1024x1024
    texture. UGUI maps the sprite's ``m_Rect`` (the whole canvas) onto the
    on-screen box and pads the difference (``DataUtility.GetPadding``), so only
    the pixels inside that crop are ever sampled.
  * ``m_RD.m_VertexData`` / ``m_IndexBuffer`` -- a tight polygon hull of the old
    logo's silhouette (250 verts / 879 indices for GSWarriors_Global), used by
    SpriteRenderer and by UI Images with "Use Sprite Mesh".

Replacing the texture pixels leaves both of those describing the *old* logo. A
replacement that covers more of the canvas than the logo it replaced -- exactly
what happens when a wide wordmark replaces a round primary logo -- gets silently
clipped to the old logo's bounding box, even though it lined up perfectly in
Photoshop on top of the exported original.

The repair points the crop at the whole texture, zeroes the trim offset, clears
the Tight mesh-type bit and rewrites the baked hull as a single full-rect quad.
Every write either overwrites a fixed-width field or refills a buffer at its
existing length, so the reserialized Sprite comes out the same byte size and can
be spliced into the .assets file in place -- the same guarantee
``apply_single_mod()`` relies on.

Self-contained: imports nothing from app.py.
"""

import os
import struct

import UnityPy

BACKUP_SUFFIX = ".original_backup"

# m_RD.settingsRaw bit 6 (value 64) is meshType; 1 = Tight, 0 = FullRect.
MESHTYPE_BIT = 64

# Unity's tight rect sits ~1px inside the true alpha bounds, and the widths come
# out fractional (513.9469...), so "is this crop the whole texture?" needs slack.
_EDGE_SLACK = 1.5

# {assets_path: {texture_path_id: [crop dict, ...]}}
_index_cache = {}


def _pristine(assets_path):
    """Prefer the untouched backup: the crop we want to report is the original."""
    bak = assets_path + BACKUP_SUFFIX
    return bak if os.path.exists(bak) else assets_path


def _rect_tuple(r):
    return (float(r.x), float(r.y), float(r.width), float(r.height))


def _is_full(rect, tex_w, tex_h):
    x, y, w, h = rect
    return (abs(x) <= _EDGE_SLACK and abs(y) <= _EDGE_SLACK
            and w >= tex_w - _EDGE_SLACK and h >= tex_h - _EDGE_SLACK)


def sprite_index(assets_path, refresh=False):
    """Map every Texture2D path_id in this file to the Sprites that front it.

    Built from the pristine backup when one exists, so the reported crop is the
    one the game shipped rather than one a previous repair already widened.
    Cached per file -- sharedassets1.assets holds 350 sprites and each needs a
    full read for m_RD.
    """
    key = os.path.abspath(assets_path)
    if not refresh and key in _index_cache:
        return _index_cache[key]

    index = {}
    try:
        env = UnityPy.load(_pristine(assets_path))
    except Exception:
        _index_cache[key] = index
        return index

    textures, sprites = {}, []
    for obj in env.objects:
        if obj.type.name == "Texture2D":
            textures[obj.path_id] = obj
        elif obj.type.name == "Sprite":
            sprites.append(obj)

    # Read the sprites first, then only the textures they actually point at --
    # reading all 1,140 Texture2Ds in sharedassets1.assets would cost far more
    # than the ~350 sprites do.
    parsed = []
    wanted = set()
    for obj in sprites:
        try:
            data = obj.read()
        except Exception:
            continue
        tex_id = getattr(data.m_RD.texture, "path_id", 0)
        if tex_id:
            parsed.append((obj, data, tex_id))
            wanted.add(tex_id)

    tex_dims = {}
    for tex_id in wanted:
        tex_obj = textures.get(tex_id)
        if tex_obj is None:
            continue
        try:
            d = tex_obj.read()
            tex_dims[tex_id] = (int(d.m_Width), int(d.m_Height))
        except Exception:
            pass

    for obj, data, tex_id in parsed:
        try:
            rd = data.m_RD
            tex_w, tex_h = tex_dims.get(tex_id, (0, 0))
            rect = _rect_tuple(rd.textureRect)
            index.setdefault(tex_id, []).append({
                "sprite_path_id": obj.path_id,
                "name": data.m_Name,
                "rect": rect,
                "tight_mesh": bool(int(rd.settingsRaw) & MESHTYPE_BIT),
                "tex_w": tex_w,
                "tex_h": tex_h,
                "full": _is_full(rect, tex_w, tex_h) if tex_w else False,
            })
        except Exception:
            continue

    _index_cache[key] = index
    return index


def crop_report(assets_path, texture_path_id):
    """Describe how a texture's sprite(s) crop it. Purely informational.

    status:
      "none"    -- no Sprite fronts this texture; the full image is used as-is.
      "full"    -- a single sprite that already shows the whole canvas.
      "trimmed" -- a single sprite cropping the canvas; repairable.
      "atlas"   -- several sprites share the texture; NOT repairable by widening
                   (each sprite is a real sub-region), so the artwork has to stay
                   inside the listed rects.
    """
    sprites = sprite_index(assets_path).get(texture_path_id, [])
    if not sprites:
        return {"status": "none", "sprites": []}
    if len(sprites) > 1:
        return {"status": "atlas", "sprites": sprites}
    s = sprites[0]
    return {"status": "full" if s["full"] and not s["tight_mesh"] else "trimmed",
            "sprites": sprites}


def describe(report):
    """One-line summary of crop_report() for the status bar / info label."""
    st = report["status"]
    if st == "none":
        return ""
    sprites = report["sprites"]
    if st == "atlas":
        return (f"⚠ shared by {len(sprites)} sprites (texture atlas) — "
                f"artwork must stay inside each sprite's region")
    x, y, w, h = sprites[0]["rect"]
    if st == "full":
        return "sprite uses the full canvas"
    return (f"⚠ sprite crops to {w:.0f}×{h:.0f} at ({x:.0f},{y:.0f}) — "
            f"will be widened to the full canvas on Apply")


# ── The repair ────────────────────────────────────────────────────────────────

def _stream_layout(vertex_data, vertex_count):
    """(byte offset, stride) per vertex stream. Streams run in index order, each
    padded up to a 16-byte boundary -- Unity's VertexData layout."""
    strides = {}
    for ch in vertex_data.m_Channels:
        dim = int(ch.dimension)
        if not dim:
            continue
        if int(ch.format) != 0:          # 0 = float32; anything else is unexpected
            raise ValueError(f"unsupported vertex channel format {ch.format}")
        strides[int(ch.stream)] = strides.get(int(ch.stream), 0) + dim * 4
    offsets, cur = {}, 0
    for stream in sorted(strides):
        offsets[stream] = cur
        cur = (cur + vertex_count * strides[stream] + 15) // 16 * 16
    return offsets, strides


def _rewrite_as_quad(rd, rect_w, rect_h, pivot_x, pivot_y, ppu):
    """Replace the baked tight hull with a full-rect quad, in place.

    Vertex and index buffers keep their exact byte length: the first four
    vertices become the rect's corners, every later vertex is a copy of the
    first, the first six indices draw the quad and every remaining index is
    zeroed into a degenerate (zero-area, never rasterised) triangle.
    """
    vd = rd.m_VertexData
    n = int(vd.m_VertexCount)
    if n < 4:
        raise ValueError(f"sprite has only {n} vertices")

    channels = list(vd.m_Channels)
    pos = channels[0] if channels else None
    uv = channels[4] if len(channels) > 4 else None
    if pos is None or int(pos.dimension) != 3 or int(pos.offset) != 0:
        raise ValueError("unexpected position channel layout")
    if uv is None or int(uv.dimension) != 2 or int(uv.offset) != 0:
        raise ValueError("unexpected UV channel layout")

    offsets, strides = _stream_layout(vd, n)
    pos_off, pos_stride = offsets[int(pos.stream)], strides[int(pos.stream)]
    uv_off, uv_stride = offsets[int(uv.stream)], strides[int(uv.stream)]

    raw = bytearray(bytes(vd.m_DataSize))
    need = max(pos_off + n * pos_stride, uv_off + n * uv_stride)
    if len(raw) < need:
        raise ValueError(f"vertex buffer too short: {len(raw)} < {need}")

    # Sprite mesh space is units (pixels / pixelsToUnits), origin at the pivot,
    # +y up -- matching UV (0,0) at the bottom-left of the texture.
    w_u, h_u = rect_w / ppu, rect_h / ppu
    left, right = -pivot_x * w_u, (1.0 - pivot_x) * w_u
    bottom, top = -pivot_y * h_u, (1.0 - pivot_y) * h_u
    corners = ((left, bottom), (right, bottom), (right, top), (left, top))
    uvs = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))

    for i in range(n):
        px, py = corners[i] if i < 4 else corners[0]
        u, v = uvs[i] if i < 4 else uvs[0]
        struct.pack_into("<3f", raw, pos_off + i * pos_stride, px, py, 0.0)
        struct.pack_into("<2f", raw, uv_off + i * uv_stride, u, v)
    vd.m_DataSize = bytes(raw)

    idx = bytearray(bytes(rd.m_IndexBuffer))
    if len(idx) < 12 or (len(idx) // 2) % 3:
        raise ValueError(f"unexpected index buffer length {len(idx)}")
    for i in range(len(idx) // 2):
        struct.pack_into("<H", idx, i * 2, (0, 1, 2, 0, 2, 3)[i] if i < 6 else 0)
    rd.m_IndexBuffer = list(idx)


def uncrop_texture_sprites(assets_path, texture_path_id):
    """Widen the sprite fronting this texture to the full canvas, in place.

    Returns (patched_count, notes). Idempotent: a sprite already showing the
    whole canvas with a full-rect mesh is left alone. Multi-sprite (atlas)
    textures are never widened -- their sub-rects are real -- and come back as a
    note instead.
    """
    notes = []
    sprites = sprite_index(assets_path).get(texture_path_id, [])
    if not sprites:
        return 0, notes
    if len(sprites) > 1:
        names = ", ".join(s["name"] for s in sprites[:4])
        notes.append(f"texture is a sprite atlas shared by {len(sprites)} sprites "
                     f"({names}{'…' if len(sprites) > 4 else ''}); crop left alone — "
                     f"keep artwork inside each sprite's region")
        return 0, notes

    target_id = sprites[0]["sprite_path_id"]
    env = UnityPy.load(assets_path)

    sprite_obj = None
    tex_dims = None
    for obj in env.objects:
        if obj.path_id == target_id and obj.type.name == "Sprite":
            sprite_obj = obj
        elif obj.path_id == texture_path_id and obj.type.name == "Texture2D":
            try:
                d = obj.read()
                tex_dims = (int(d.m_Width), int(d.m_Height))
            except Exception:
                pass
    if sprite_obj is None or tex_dims is None:
        notes.append("sprite or texture not found; crop left alone")
        return 0, notes

    tex_w, tex_h = tex_dims
    data = sprite_obj.read()
    rd = data.m_RD

    rect_w, rect_h = float(data.m_Rect.width), float(data.m_Rect.height)
    if abs(rect_w - tex_w) > 1.0 or abs(rect_h - tex_h) > 1.0:
        # Sub-rect of a larger sheet -- widening would move the artwork.
        notes.append(f"sprite rect {rect_w:.0f}×{rect_h:.0f} doesn't match the "
                     f"texture {tex_w}×{tex_h}; crop left alone")
        return 0, notes

    already_full = (_is_full(_rect_tuple(rd.textureRect), tex_w, tex_h)
                    and not int(rd.settingsRaw) & MESHTYPE_BIT)
    if already_full:
        return 0, notes

    rd.textureRect.x = 0.0
    rd.textureRect.y = 0.0
    rd.textureRect.width = float(tex_w)
    rd.textureRect.height = float(tex_h)
    rd.textureRectOffset.x = 0.0
    rd.textureRectOffset.y = 0.0
    rd.settingsRaw = int(rd.settingsRaw) & ~MESHTYPE_BIT

    ppu = float(data.m_PixelsToUnits) or 100.0
    try:
        _rewrite_as_quad(rd, rect_w, rect_h,
                         float(data.m_Pivot.x), float(data.m_Pivot.y), ppu)
    except ValueError as e:
        # The textureRect widening alone already fixes UGUI Simple images, which
        # is how these logos are drawn; only the sprite-mesh path stays clipped.
        notes.append(f"kept the baked sprite mesh ({e})")

    from UnityPy.streams.EndianBinaryWriter import EndianBinaryWriter
    from UnityPy.helpers.TypeTreeHelper import write_typetree

    writer = EndianBinaryWriter(endian=sprite_obj.reader.endian)
    write_typetree(data, sprite_obj._get_typetree_node(), writer, env.file)
    new_bytes = writer.bytes
    if len(new_bytes) != sprite_obj.byte_size:
        raise ValueError(
            f"sprite '{data.m_Name}' reserialized to {len(new_bytes)}b, "
            f"expected {sprite_obj.byte_size}b — refusing to patch")

    start = sprite_obj.byte_start
    with open(assets_path, "rb") as f:
        blob = bytearray(f.read())
    blob[start:start + sprite_obj.byte_size] = new_bytes
    with open(assets_path, "wb") as f:
        f.write(blob)

    return 1, notes
