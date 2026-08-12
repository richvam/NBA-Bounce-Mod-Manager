"""
mesh_manager.py -- 3D mesh support for NBA Bounce Mod Manager.

Everything asset-side for the Meshes tab: finding Mesh objects inside the
game's .assets files, decoding their geometry, writing that geometry out as
OBJ / STL / PLY / GLB, reading a replacement back in, and patching it into the
game.

It imports nothing from app.py (same arrangement as save_manager.py), so it can
be unit-tested and used from a script without a Tk window. mesh_view.py draws
the geometry; mesh_tab.py is the UI on top of both.

--------------------------------------------------------------------------------
HOW A UNITY MESH IS STORED (Unity 6 / NBA Bounce)
--------------------------------------------------------------------------------
A Mesh object holds:

  m_VertexData   m_VertexCount + m_Channels (position / normal / tangent /
                 color / uv0..uv7 / blend weights / blend indices) + one
                 interleaved byte blob per "stream". Each channel says which
                 stream it lives in, its byte offset inside that stream's
                 per-vertex stride, its component format and its dimension --
                 so the blob is only decodable through the channel table.
  m_IndexBuffer  a flat uint16 (or uint32, per m_IndexFormat) triangle index
                 list, ALWAYS stored inline in the object.
  m_SubMeshes    fixed-length array; each entry is a (firstByte, indexCount)
                 window into m_IndexBuffer plus its own bounds. One submesh per
                 material slot on the renderer.
  m_StreamData   when non-empty, the vertex blob is NOT in the object at all --
                 it lives at (offset, size) inside a companion .resS file, and
                 m_VertexData.m_DataSize is an empty array.

--------------------------------------------------------------------------------
HOW A REPLACEMENT IS WRITTEN BACK
--------------------------------------------------------------------------------
Two paths, in this order of preference:

1. IN-PLACE (same technique as app.apply_single_mod, and just as safe).
   The rebuilt object must serialize to the exact same byte size as the one it
   replaces, so it can be spliced over the original bytes and every other
   object's offset in the file stays valid. That works out because:
     - vertex data goes to the .resS (append-only), so the vertex COUNT is
       free to change -- only an int field in the object changes;
     - the index buffer is reused at its original byte length, zero-padded past
       the end of the last submesh's window (padding is never indexed by any
       submesh, so the GPU never sees it);
     - the submesh array keeps its length; unused entries get indexCount = 0.
   The one hard limit is the index buffer: a replacement can have at most as
   many triangles as the mesh it replaces (mesh_write_info() reports that
   budget up front). Meshes whose vertex data is inline rather than streamed
   have a vertex budget too.

2. REBUILD (fallback, only when explicitly allowed).
   When the replacement needs a bigger index buffer, the whole .assets file is
   re-serialized by UnityPy with the new object size and every following
   object's offset recomputed. Objects this app never touched are carried over
   as raw bytes, so texture mods already patched into the file survive. It
   rewrites a multi-hundred-MB file wholesale, which is why it is opt-in.

--------------------------------------------------------------------------------
COORDINATE SPACE
--------------------------------------------------------------------------------
Unity is left-handed (+X right, +Y up, +Z forward); OBJ/glTF/STL/PLY tooling is
right-handed. Every exporter here negates X and reverses triangle winding --
the same convention UnityPy's own OBJ exporter uses -- and every importer
undoes it. The operation is its own inverse, so export -> import round-trips
byte-for-byte identically.
"""

import json
import math
import ntpath
import os
import shutil
import struct

try:
    import numpy as _np
except Exception:          # numpy is an accelerator here, never a requirement
    _np = None

BACKUP_SUFFIX   = ".original_backup"
MESH_META_FILE  = "mesh_mods.json"
MESH_SUBFOLDER  = "meshes"

# Formats offered in the export dialog, best-supported first.
EXPORT_FORMATS = [
    ("Wavefront OBJ",       "*.obj"),
    ("glTF binary",         "*.glb"),
    ("Stanford PLY",        "*.ply"),
    ("STL (binary)",        "*.stl"),
]
IMPORT_FORMATS = [
    ("3D models",           "*.obj *.ply *.stl"),
    ("Wavefront OBJ",       "*.obj"),
    ("Stanford PLY",        "*.ply"),
    ("STL",                 "*.stl"),
]


class MeshError(Exception):
    """Anything the user needs to read: unsupported mesh, over budget, bad file."""


# ══════════════════════════════════════════════════════════════════════════════
#  Geometry container
# ══════════════════════════════════════════════════════════════════════════════
class MeshGeometry:
    """Plain, engine-agnostic triangle mesh.

    Vertices/normals/tangents are tuples of floats, uvs are (u, v), colors are
    (r, g, b, a) in 0..1, triangles are (a, b, c) index tuples. `submeshes` is a
    list of {"name", "first", "count"} triangle windows, in triangle units, and
    always covers the whole triangle list in order.
    """

    def __init__(self, name="mesh", vertices=None, triangles=None, normals=None,
                 uvs=None, uvs2=None, colors=None, tangents=None,
                 bone_indices=None, bone_weights=None, submeshes=None, source=""):
        self.name         = name or "mesh"
        self.vertices     = vertices  if vertices  is not None else []
        self.triangles    = triangles if triangles is not None else []
        self.normals      = normals
        self.uvs          = uvs
        self.uvs2         = uvs2
        self.colors       = colors
        self.tangents     = tangents
        self.bone_indices = bone_indices
        self.bone_weights = bone_weights
        self.submeshes    = submeshes if submeshes is not None else []
        self.source       = source
        if not self.submeshes:
            self.submeshes = [{"name": self.name, "first": 0,
                               "count": len(self.triangles)}]

    # ── basics ────────────────────────────────────────────────────────────────
    @property
    def vertex_count(self):
        return len(self.vertices)

    @property
    def triangle_count(self):
        return len(self.triangles)

    def bounds(self):
        """(min_xyz, max_xyz); (0,0,0)/(0,0,0) for an empty mesh."""
        if not self.vertices:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        xs = [v[0] for v in self.vertices]
        ys = [v[1] for v in self.vertices]
        zs = [v[2] for v in self.vertices]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    def center_extent(self):
        lo, hi = self.bounds()
        center = tuple((lo[i] + hi[i]) / 2.0 for i in range(3))
        extent = tuple((hi[i] - lo[i]) / 2.0 for i in range(3))
        return center, extent

    def size(self):
        lo, hi = self.bounds()
        return tuple(hi[i] - lo[i] for i in range(3))

    def validate(self):
        """Raise MeshError if the index list can't address the vertex list."""
        n = len(self.vertices)
        if n == 0:
            raise MeshError("Mesh has no vertices.")
        if not self.triangles:
            raise MeshError("Mesh has no triangles.")
        for tri in self.triangles:
            if len(tri) != 3:
                raise MeshError("Mesh contains a non-triangular face.")
            for idx in tri:
                if idx < 0 or idx >= n:
                    raise MeshError(
                        f"Face references vertex {idx}, but the mesh only has "
                        f"{n} vertices.")
        for attr, label in ((self.normals, "normals"), (self.uvs, "UVs"),
                            (self.uvs2, "UV2s"), (self.colors, "colors"),
                            (self.tangents, "tangents")):
            if attr is not None and len(attr) != n:
                raise MeshError(f"Mesh has {n} vertices but {len(attr)} {label}.")
        return True

    # ── derived data ──────────────────────────────────────────────────────────
    def compute_normals(self):
        """Area-weighted vertex normals. Used when an import brings none."""
        acc = [[0.0, 0.0, 0.0] for _ in self.vertices]
        for a, b, c in self.triangles:
            va, vb, vc = self.vertices[a], self.vertices[b], self.vertices[c]
            ux, uy, uz = vb[0] - va[0], vb[1] - va[1], vb[2] - va[2]
            wx, wy, wz = vc[0] - va[0], vc[1] - va[1], vc[2] - va[2]
            nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
            for i in (a, b, c):
                acc[i][0] += nx
                acc[i][1] += ny
                acc[i][2] += nz
        out = []
        for nx, ny, nz in acc:
            length = math.sqrt(nx * nx + ny * ny + nz * nz)
            out.append((nx / length, ny / length, nz / length) if length > 1e-12
                       else (0.0, 1.0, 0.0))
        self.normals = out
        return out

    def ensure_normals(self):
        if not self.normals:
            self.compute_normals()
        return self.normals

    def compute_tangents(self):
        """Per-vertex tangents from UV0, Unity-style xyzw with a handedness w.

        Only needed when writing back into a mesh whose vertex layout has a
        tangent channel and the imported file had none -- normal-mapped shaders
        render badly with a zeroed tangent.
        """
        n = len(self.vertices)
        if not self.uvs:
            self.tangents = [(1.0, 0.0, 0.0, 1.0)] * n
            return self.tangents
        tan = [[0.0, 0.0, 0.0] for _ in range(n)]
        for a, b, c in self.triangles:
            p0, p1, p2 = self.vertices[a], self.vertices[b], self.vertices[c]
            w0, w1, w2 = self.uvs[a], self.uvs[b], self.uvs[c]
            x1, y1, z1 = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
            x2, y2, z2 = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
            s1, t1 = w1[0] - w0[0], w1[1] - w0[1]
            s2, t2 = w2[0] - w0[0], w2[1] - w0[1]
            det = s1 * t2 - s2 * t1
            if abs(det) < 1e-12:
                continue
            r = 1.0 / det
            tx, ty, tz = ((t2 * x1 - t1 * x2) * r,
                          (t2 * y1 - t1 * y2) * r,
                          (t2 * z1 - t1 * z2) * r)
            for i in (a, b, c):
                tan[i][0] += tx
                tan[i][1] += ty
                tan[i][2] += tz
        normals = self.ensure_normals()
        out = []
        for i in range(n):
            tx, ty, tz = tan[i]
            nx, ny, nz = normals[i]
            # Gram-Schmidt against the normal, then normalize
            dot = nx * tx + ny * ty + nz * tz
            tx, ty, tz = tx - nx * dot, ty - ny * dot, tz - nz * dot
            length = math.sqrt(tx * tx + ty * ty + tz * tz)
            if length < 1e-12:
                out.append((1.0, 0.0, 0.0, 1.0))
            else:
                out.append((tx / length, ty / length, tz / length, 1.0))
        self.tangents = out
        return out

    # ── transforms ────────────────────────────────────────────────────────────
    def flipped_handedness(self):
        """Copy with X negated and winding reversed -- Unity <-> OBJ/glTF/STL/PLY.

        Its own inverse, so the same call converts in both directions.
        """
        geo = MeshGeometry(
            name=self.name,
            vertices=[(-v[0], v[1], v[2]) for v in self.vertices],
            triangles=[(c, b, a) for (a, b, c) in self.triangles],
            normals=([(-n[0], n[1], n[2]) for n in self.normals]
                     if self.normals else None),
            uvs=list(self.uvs) if self.uvs else None,
            uvs2=list(self.uvs2) if self.uvs2 else None,
            colors=list(self.colors) if self.colors else None,
            tangents=([(-t[0], t[1], t[2]) + tuple(t[3:]) for t in self.tangents]
                      if self.tangents else None),
            bone_indices=list(self.bone_indices) if self.bone_indices else None,
            bone_weights=list(self.bone_weights) if self.bone_weights else None,
            submeshes=[dict(sm) for sm in self.submeshes],
            source=self.source,
        )
        return geo

    def copy(self):
        # flipping twice is a no-op, so it doubles as a copy constructor
        return self.flipped_handedness().flipped_handedness()

    def scaled(self, factor):
        """Uniform scale -- Blender exports in metres, Unity meshes rarely are."""
        if factor == 1.0:
            return self
        geo = self.copy()
        geo.vertices = [(v[0] * factor, v[1] * factor, v[2] * factor)
                        for v in self.vertices]
        return geo

    def recentered(self, target_center):
        """Move the mesh so its bounding box centre sits on `target_center` --
        the usual fix when a replacement lands off to the side in-game."""
        center, _ = self.center_extent()
        delta = tuple(target_center[i] - center[i] for i in range(3))
        if max(abs(d) for d in delta) < 1e-9:
            return self
        geo = self.copy()
        geo.vertices = [(v[0] + delta[0], v[1] + delta[1], v[2] + delta[2])
                        for v in self.vertices]
        return geo

    # ── reporting ─────────────────────────────────────────────────────────────
    def summary(self):
        lo, hi = self.bounds()
        size = tuple(hi[i] - lo[i] for i in range(3))
        return {
            "name":       self.name,
            "vertices":   self.vertex_count,
            "triangles":  self.triangle_count,
            "submeshes":  len(self.submeshes),
            "has_normals": bool(self.normals),
            "has_uvs":    bool(self.uvs),
            "has_colors": bool(self.colors),
            "skinned":    bool(self.bone_weights),
            "bounds_min": lo,
            "bounds_max": hi,
            "size":       size,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Exporters -- all take Unity-space geometry and flip it on the way out
# ══════════════════════════════════════════════════════════════════════════════
def export_geometry(geo, path, fmt=None):
    """Write `geo` to `path`. Format from the extension unless `fmt` is given."""
    fmt = (fmt or os.path.splitext(path)[1].lstrip(".")).lower()
    writer = {
        "obj":  _write_obj,
        "stl":  _write_stl,
        "ply":  _write_ply,
        "glb":  _write_glb,
        "gltf": _write_glb,     # always the binary container; .gltf is just a name
    }.get(fmt)
    if writer is None:
        raise MeshError(f"Don't know how to export '{fmt}' files.")
    geo.validate()
    writer(geo.flipped_handedness(), path)
    return path


def _write_obj(geo, path):
    """OBJ + a matching .mtl stub. One `g` group per submesh, so the group
    layout survives a round-trip back into the same submesh slots."""
    name  = _safe_name(geo.name)
    lines = [f"# Exported by NBA Bounce Mod Manager\n",
             f"# {geo.vertex_count} vertices, {geo.triangle_count} triangles\n",
             f"mtllib {name}.mtl\n",
             f"o {name}\n"]
    for v in geo.vertices:
        lines.append("v %.6f %.6f %.6f\n" % v)
    if geo.uvs:
        for uv in geo.uvs:
            lines.append("vt %.6f %.6f\n" % (uv[0], uv[1]))
    if geo.normals:
        for n in geo.normals:
            lines.append("vn %.6f %.6f %.6f\n" % (n[0], n[1], n[2]))

    has_uv, has_n = bool(geo.uvs), bool(geo.normals)
    for si, sm in enumerate(geo.submeshes):
        if not sm["count"]:
            continue
        lines.append(f"g {sm.get('name') or f'{name}_{si}'}\n")
        lines.append(f"usemtl material_{si}\n")
        for tri in geo.triangles[sm["first"]: sm["first"] + sm["count"]]:
            parts = []
            for idx in tri:
                i = idx + 1
                if has_uv and has_n:
                    parts.append(f"{i}/{i}/{i}")
                elif has_uv:
                    parts.append(f"{i}/{i}")
                elif has_n:
                    parts.append(f"{i}//{i}")
                else:
                    parts.append(str(i))
            lines.append("f " + " ".join(parts) + "\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    mtl = os.path.join(os.path.dirname(path), f"{name}.mtl")
    try:
        with open(mtl, "w", encoding="utf-8") as f:
            for si in range(len(geo.submeshes)):
                f.write(f"newmtl material_{si}\nKd 0.800 0.800 0.800\nd 1.0\n"
                        f"illum 2\n\n")
    except OSError:
        pass    # the .mtl is a convenience; a read-only folder shouldn't fail the export


def _write_stl(geo, path):
    """Binary STL. Positions only -- no UVs, no submeshes, no vertex sharing."""
    tri_count = geo.triangle_count
    with open(path, "wb") as f:
        header = b"NBA Bounce Mod Manager - " + _safe_name(geo.name).encode("ascii", "replace")
        f.write(header[:80].ljust(80, b"\0"))
        f.write(struct.pack("<I", tri_count))
        for a, b, c in geo.triangles:
            va, vb, vc = geo.vertices[a], geo.vertices[b], geo.vertices[c]
            ux, uy, uz = vb[0] - va[0], vb[1] - va[1], vb[2] - va[2]
            wx, wy, wz = vc[0] - va[0], vc[1] - va[1], vc[2] - va[2]
            nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
            length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            f.write(struct.pack("<12fH",
                                nx / length, ny / length, nz / length,
                                va[0], va[1], va[2],
                                vb[0], vb[1], vb[2],
                                vc[0], vc[1], vc[2], 0))


def _write_ply(geo, path):
    """Binary little-endian PLY with normals and UVs (s/t, the common spelling)."""
    has_n, has_uv = bool(geo.normals), bool(geo.uvs)
    head = ["ply", "format binary_little_endian 1.0",
            "comment Exported by NBA Bounce Mod Manager",
            f"element vertex {geo.vertex_count}",
            "property float x", "property float y", "property float z"]
    if has_n:
        head += ["property float nx", "property float ny", "property float nz"]
    if has_uv:
        head += ["property float s", "property float t"]
    head += [f"element face {geo.triangle_count}",
             "property list uchar int vertex_indices", "end_header"]
    with open(path, "wb") as f:
        f.write(("\n".join(head) + "\n").encode("ascii"))
        for i, v in enumerate(geo.vertices):
            f.write(struct.pack("<3f", v[0], v[1], v[2]))
            if has_n:
                n = geo.normals[i]
                f.write(struct.pack("<3f", n[0], n[1], n[2]))
            if has_uv:
                uv = geo.uvs[i]
                f.write(struct.pack("<2f", uv[0], uv[1]))
        for tri in geo.triangles:
            f.write(struct.pack("<B3i", 3, tri[0], tri[1], tri[2]))


def _write_glb(geo, path):
    """Single-file glTF 2.0 (.glb) -- opens in Blender, Windows 3D Viewer, and
    every browser-based model viewer without a sidecar file.

    glTF puts the UV origin top-left where OBJ/Unity put it bottom-left, so V is
    flipped here (and only here).
    """
    n_verts, n_tris = geo.vertex_count, geo.triangle_count
    lo, hi = geo.bounds()

    blobs, views, accessors, attributes = [], [], [], {}
    offset = 0

    def add(data, target, count, comp_type, type_str, minmax=None):
        nonlocal offset
        pad = (-len(data)) % 4
        blobs.append(data + b"\0" * pad)
        views.append({"buffer": 0, "byteOffset": offset,
                      "byteLength": len(data), "target": target})
        offset += len(data) + pad
        acc = {"bufferView": len(views) - 1, "componentType": comp_type,
               "count": count, "type": type_str}
        if minmax:
            acc["min"], acc["max"] = minmax
        accessors.append(acc)
        return len(accessors) - 1

    pos = b"".join(struct.pack("<3f", *v[:3]) for v in geo.vertices)
    attributes["POSITION"] = add(pos, 34962, n_verts, 5126, "VEC3",
                                 ([lo[0], lo[1], lo[2]], [hi[0], hi[1], hi[2]]))
    if geo.normals:
        nrm = b"".join(struct.pack("<3f", *n[:3]) for n in geo.normals)
        attributes["NORMAL"] = add(nrm, 34962, n_verts, 5126, "VEC3")
    if geo.uvs:
        uvs = b"".join(struct.pack("<2f", uv[0], 1.0 - uv[1]) for uv in geo.uvs)
        attributes["TEXCOORD_0"] = add(uvs, 34962, n_verts, 5126, "VEC2")
    if geo.colors:
        col = b"".join(struct.pack("<4f", *(tuple(c) + (1.0,))[:4]) for c in geo.colors)
        attributes["COLOR_0"] = add(col, 34962, n_verts, 5126, "VEC4")

    primitives = []
    for si, sm in enumerate(geo.submeshes):
        if not sm["count"]:
            continue
        idx = b"".join(struct.pack("<3I", *tri)
                       for tri in geo.triangles[sm["first"]: sm["first"] + sm["count"]])
        acc = add(idx, 34963, sm["count"] * 3, 5125, "SCALAR")
        primitives.append({"attributes": attributes, "indices": acc,
                           "material": 0, "mode": 4})
    if not primitives:
        raise MeshError("Mesh has no triangles to export.")

    gltf = {
        "asset": {"version": "2.0", "generator": "NBA Bounce Mod Manager"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": _safe_name(geo.name)}],
        "meshes": [{"name": _safe_name(geo.name), "primitives": primitives}],
        "materials": [{"name": "material_0",
                       "pbrMetallicRoughness": {
                           "baseColorFactor": [0.8, 0.8, 0.8, 1.0],
                           "metallicFactor": 0.0, "roughnessFactor": 0.7}}],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": offset}],
    }
    bin_chunk  = b"".join(blobs)
    json_chunk = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)

    total = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
    with open(path, "wb") as f:
        f.write(struct.pack("<4sII", b"glTF", 2, total))
        f.write(struct.pack("<II", len(json_chunk), 0x4E4F534A)); f.write(json_chunk)
        f.write(struct.pack("<II", len(bin_chunk),  0x004E4942)); f.write(bin_chunk)


def _safe_name(name):
    keep = "-_.() "
    cleaned = "".join(ch for ch in (name or "mesh") if ch.isalnum() or ch in keep)
    return cleaned.strip() or "mesh"


# ══════════════════════════════════════════════════════════════════════════════
#  Importers -- read a file in right-handed space, flip it back to Unity space
# ══════════════════════════════════════════════════════════════════════════════
def import_geometry(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    reader = {"obj": _read_obj, "stl": _read_stl, "ply": _read_ply}.get(ext)
    if reader is None:
        raise MeshError(
            f"'{ext or path}' isn't a format this app can import.\n\n"
            f"Import OBJ, PLY or STL. (Blender: File > Export > Wavefront OBJ.)")
    geo = reader(path)
    if not geo.vertices or not geo.triangles:
        raise MeshError(f"No triangles found in {os.path.basename(path)}.")
    geo.source = path
    geo.validate()
    return geo.flipped_handedness()


def _read_obj(path):
    """Wavefront OBJ.

    OBJ indexes position / uv / normal separately, so a vertex is really the
    (v, vt, vn) triple -- each distinct triple becomes one GPU vertex here,
    which is exactly what Unity needs. `g`/`o`/`usemtl` starts a new submesh.
    """
    positions, uvs, normals = [], [], []
    verts, uv_out, n_out = [], [], []
    lookup = {}
    triangles, submeshes = [], []
    current = None

    def start_group(name):
        nonlocal current
        if current is not None and len(triangles) == current["first"]:
            current["name"] = name          # empty group, just rename it
            return
        current = {"name": name, "first": len(triangles), "count": 0}
        submeshes.append(current)

    def resolve(token):
        """'v/vt/vn' -> index into the de-duplicated vertex arrays."""
        if token in lookup:
            return lookup[token]
        bits = token.split("/")
        vi = int(bits[0])
        vi = vi - 1 if vi > 0 else len(positions) + vi
        ti = ni = None
        if len(bits) > 1 and bits[1]:
            ti = int(bits[1]); ti = ti - 1 if ti > 0 else len(uvs) + ti
        if len(bits) > 2 and bits[2]:
            ni = int(bits[2]); ni = ni - 1 if ni > 0 else len(normals) + ni
        if vi < 0 or vi >= len(positions):
            raise MeshError(f"OBJ face references vertex {bits[0]}, which doesn't exist.")
        verts.append(positions[vi])
        uv_out.append(uvs[ti] if ti is not None and 0 <= ti < len(uvs) else (0.0, 0.0))
        n_out.append(normals[ni] if ni is not None and 0 <= ni < len(normals) else None)
        lookup[token] = len(verts) - 1
        return len(verts) - 1

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line or line[0] == "#":
                continue
            parts = line.split()
            if not parts:
                continue
            tag = parts[0]
            if tag == "v":
                positions.append(tuple(float(x) for x in parts[1:4]))
            elif tag == "vt":
                u = float(parts[1]) if len(parts) > 1 else 0.0
                v = float(parts[2]) if len(parts) > 2 else 0.0
                uvs.append((u, v))
            elif tag == "vn":
                normals.append(tuple(float(x) for x in parts[1:4]))
            elif tag == "f":
                idx = [resolve(tok) for tok in parts[1:]]
                if len(idx) < 3:
                    continue
                if current is None:
                    start_group("group_0")
                # fan-triangulate n-gons; convex quads (the common case) are exact
                for k in range(1, len(idx) - 1):
                    triangles.append((idx[0], idx[k], idx[k + 1]))
                    current["count"] += 1
            elif tag in ("g", "o", "usemtl"):
                start_group(" ".join(parts[1:]) or f"group_{len(submeshes)}")

    submeshes = [sm for sm in submeshes if sm["count"]]
    has_normals = any(n is not None for n in n_out)
    geo = MeshGeometry(
        name=os.path.splitext(os.path.basename(path))[0],
        vertices=verts, triangles=triangles,
        normals=[n or (0.0, 1.0, 0.0) for n in n_out] if has_normals else None,
        uvs=uv_out if uvs else None,
        submeshes=submeshes or None,
    )
    if not has_normals:
        geo.compute_normals()
    return geo


def _read_stl(path):
    """Binary or ASCII STL. STL has no shared vertices, so identical positions
    are welded back together -- otherwise a 5k-triangle model would import as
    15k vertices and blow its budget for no reason."""
    with open(path, "rb") as f:
        head = f.read(84)
        raw  = head + f.read()
    ascii_stl = head[:5].lower() == b"solid" and b"facet" in raw[:2048].lower()

    faces = []
    if ascii_stl:
        verts = []
        for line in raw.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "vertex":
                verts.append(tuple(float(x) for x in parts[1:4]))
        faces = [tuple(verts[i:i + 3]) for i in range(0, len(verts) - 2, 3)]
    else:
        count = struct.unpack("<I", raw[80:84])[0]
        need  = 84 + count * 50
        if len(raw) < need:
            raise MeshError("STL file is truncated.")
        for i in range(count):
            base = 84 + i * 50
            vals = struct.unpack("<12f", raw[base:base + 48])
            faces.append((vals[3:6], vals[6:9], vals[9:12]))

    verts, lookup, triangles = [], {}, []
    for tri in faces:
        idx = []
        for v in tri:
            key = (round(v[0], 6), round(v[1], 6), round(v[2], 6))
            if key not in lookup:
                lookup[key] = len(verts)
                verts.append(tuple(float(c) for c in v))
            idx.append(lookup[key])
        if idx[0] != idx[1] and idx[1] != idx[2] and idx[0] != idx[2]:
            triangles.append(tuple(idx))

    geo = MeshGeometry(name=os.path.splitext(os.path.basename(path))[0],
                       vertices=verts, triangles=triangles)
    geo.compute_normals()
    return geo


def _read_ply(path):
    """PLY, ascii or binary_little_endian, with the usual property spellings."""
    with open(path, "rb") as f:
        raw = f.read()
    marker = raw.find(b"end_header")
    if marker < 0:
        raise MeshError("Not a PLY file (no end_header).")
    line_end   = raw.find(b"\n", marker) + 1
    header     = raw[:marker].decode("ascii", "replace")
    body       = raw[line_end:]

    fmt, elements, current = "ascii", [], None
    for line in header.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "format":
            fmt = parts[1]
        elif parts[0] == "element":
            current = {"name": parts[1], "count": int(parts[2]), "props": []}
            elements.append(current)
        elif parts[0] == "property" and current is not None:
            if parts[1] == "list":
                current["props"].append(("list", parts[2], parts[3], parts[4]))
            else:
                current["props"].append(("scalar", parts[1], parts[2]))
    if fmt not in ("ascii", "binary_little_endian"):
        raise MeshError(f"PLY format '{fmt}' isn't supported (use ascii or "
                        f"binary little endian).")

    PLY_STRUCT = {"char": "b", "int8": "b", "uchar": "B", "uint8": "B",
                  "short": "h", "int16": "h", "ushort": "H", "uint16": "H",
                  "int": "i", "int32": "i", "uint": "I", "uint32": "I",
                  "float": "f", "float32": "f", "double": "d", "float64": "d"}

    verts, normals, uvs, triangles = [], [], [], []
    if fmt == "ascii":
        tokens = body.decode("ascii", "replace").split()
        pos = 0
        for el in elements:
            names = [p[2] if p[0] == "scalar" else None for p in el["props"]]
            for _ in range(el["count"]):
                if el["name"] == "vertex":
                    row = {}
                    for name in names:
                        row[name] = float(tokens[pos]); pos += 1
                    _ply_row(row, verts, normals, uvs)
                elif el["name"] == "face":
                    n = int(float(tokens[pos])); pos += 1
                    idx = [int(float(tokens[pos + k])) for k in range(n)]
                    pos += n
                    _ply_faces(idx, triangles)
                else:
                    pos += len(el["props"])
    else:
        pos = 0
        for el in elements:
            if el["name"] == "vertex":
                chars = [PLY_STRUCT[p[1]] for p in el["props"] if p[0] == "scalar"]
                names = [p[2] for p in el["props"] if p[0] == "scalar"]
                st = struct.Struct("<" + "".join(chars))
                for _ in range(el["count"]):
                    vals = st.unpack_from(body, pos); pos += st.size
                    _ply_row(dict(zip(names, vals)), verts, normals, uvs)
            elif el["name"] == "face":
                # ("list", count_type, index_type, name)
                prop = next(p for p in el["props"] if p[0] == "list")
                cchar, ichar = PLY_STRUCT[prop[1]], PLY_STRUCT[prop[2]]
                csize, isize = struct.calcsize(cchar), struct.calcsize(ichar)
                for _ in range(el["count"]):
                    n = struct.unpack_from("<" + cchar, body, pos)[0]; pos += csize
                    idx = struct.unpack_from(f"<{n}{ichar}", body, pos)
                    pos += n * isize
                    _ply_faces(list(idx), triangles)
            else:
                for p in el["props"]:
                    if p[0] == "scalar":
                        pos += struct.calcsize(PLY_STRUCT[p[1]]) * el["count"]

    geo = MeshGeometry(name=os.path.splitext(os.path.basename(path))[0],
                       vertices=verts, triangles=triangles,
                       normals=normals if len(normals) == len(verts) else None,
                       uvs=uvs if len(uvs) == len(verts) else None)
    geo.ensure_normals()
    return geo


def _ply_row(row, verts, normals, uvs):
    verts.append((row.get("x", 0.0), row.get("y", 0.0), row.get("z", 0.0)))
    if "nx" in row:
        normals.append((row["nx"], row.get("ny", 0.0), row.get("nz", 0.0)))
    if "s" in row or "u" in row or "texture_u" in row:
        u = row.get("s", row.get("u", row.get("texture_u", 0.0)))
        v = row.get("t", row.get("v", row.get("texture_v", 0.0)))
        uvs.append((u, v))


def _ply_faces(idx, triangles):
    for k in range(1, len(idx) - 1):
        triangles.append((idx[0], idx[k], idx[k + 1]))


# ══════════════════════════════════════════════════════════════════════════════
#  Unity side: reading meshes out of .assets files
# ══════════════════════════════════════════════════════════════════════════════
# Channel index -> what that channel means. Unity renumbered these in 2018;
# NBA Bounce is Unity 6, so the modern table is what actually runs, but the
# legacy one keeps this module honest against older files.
_SEMANTICS_2018 = {0: "position", 1: "normal", 2: "tangent", 3: "color",
                   4: "uv0", 5: "uv1", 6: "uv2", 7: "uv3", 8: "uv4",
                   9: "uv5", 10: "uv6", 11: "uv7",
                   12: "bone_weights", 13: "bone_indices"}
_SEMANTICS_LEGACY = {0: "position", 1: "normal", 2: "color", 3: "uv0",
                     4: "uv1", 5: "uv2", 6: "uv3", 7: "tangent"}


def _channel_semantic(index, version):
    if version[0] >= 2018:
        return _SEMANTICS_2018.get(index)
    if version[0] < 5 and index == 5:
        return "tangent"
    return _SEMANTICS_LEGACY.get(index)


def find_mesh_files(game_data_path):
    """Every file in the game data folder that can hold Mesh objects.

    Same rule as app.find_assets_files(): the .assets files plus the extensionless
    levelN scene files.
    """
    result = []
    for f in sorted(os.listdir(game_data_path)):
        full = os.path.join(game_data_path, f)
        if f.endswith(".assets"):
            result.append(full)
        elif f.startswith("level") and "." not in f:
            result.append(full)
    return result


def _source_file(assets_path, prefer_backup=True):
    """Read the ORIGINAL bytes when a backup exists, so 'Original' really is."""
    bak = assets_path + BACKUP_SUFFIX
    return bak if (prefer_backup and os.path.exists(bak)) else assets_path


def list_meshes_in_file(assets_path, progress=None):
    """Summarize every Mesh in one .assets file, without decoding vertex data.

    Vertex/triangle counts come straight off the header fields, so this stays
    fast enough to index the whole game -- decoding is deferred to load_mesh().
    """
    import UnityPy
    meshes = []
    env = UnityPy.load(assets_path)
    for obj in env.objects:
        if obj.type.name != "Mesh":
            continue
        entry = {"name": f"mesh_{obj.path_id}", "path_id": obj.path_id,
                 "assets_file": assets_path, "vertices": 0, "triangles": 0,
                 "submeshes": 0, "streamed": False, "skinned": False,
                 "compressed": False, "readable": True}
        try:
            entry["name"] = obj.peek_name() or entry["name"]
            mesh = obj.read()
            entry.update(_mesh_stats(mesh, obj))
        except Exception:
            entry["readable"] = False
        meshes.append(entry)
        if progress:
            progress(len(meshes))
    return meshes


def _mesh_stats(mesh, obj):
    vertex_data = getattr(mesh, "m_VertexData", None)
    verts = int(getattr(vertex_data, "m_VertexCount", 0) or 0)
    compressed = bool(getattr(mesh, "m_MeshCompression", 0))
    if not verts and compressed:
        try:
            verts = mesh.m_CompressedMesh.m_Vertices.m_NumItems // 3
        except Exception:
            verts = 0
    submeshes = list(getattr(mesh, "m_SubMeshes", None) or [])
    tri = 0
    for sm in submeshes:
        topology = getattr(sm, "topology", 0) or 0
        count = getattr(sm, "indexCount", 0) or 0
        tri += count // 3 if topology == 0 else max(0, count - 2)
    stream = getattr(mesh, "m_StreamData", None)
    version = getattr(obj, "version", (2018, 0, 0, 0))
    skinned = bool(getattr(mesh, "m_Skin", None))
    if not skinned and vertex_data is not None:
        for ci, ch in enumerate(getattr(vertex_data, "m_Channels", None) or []):
            if ch.dimension and _channel_semantic(ci, version) == "bone_weights":
                skinned = True
                break
    return {"vertices": verts, "triangles": tri, "submeshes": len(submeshes),
            "streamed": bool(stream and stream.path), "skinned": skinned,
            "compressed": compressed}


def load_mesh(assets_path, path_id, prefer_backup=True):
    """Decode one Mesh into a MeshGeometry (Unity space) plus an info dict."""
    import UnityPy
    from UnityPy.helpers.MeshHelper import MeshHandler

    env = UnityPy.load(_source_file(assets_path, prefer_backup))
    obj = next((o for o in env.objects if o.path_id == path_id), None)
    if obj is None:
        raise MeshError(f"No mesh with path_id {path_id} in "
                        f"{os.path.basename(assets_path)}.")
    if obj.type.name != "Mesh":
        raise MeshError(f"Object {path_id} is a {obj.type.name}, not a Mesh.")

    mesh    = obj.read()
    info    = _mesh_stats(mesh, obj)
    info["name"]        = obj.peek_name() or f"mesh_{path_id}"
    info["path_id"]     = path_id
    info["assets_file"] = assets_path

    # pass the version explicitly: MeshHandler otherwise reads it back off the
    # object reader, which isn't attached on every code path
    handler = MeshHandler(mesh, version=obj.version)
    handler.process()
    if not handler.m_Vertices:
        raise MeshError(f"'{info['name']}' has no readable vertex data.")

    verts = [tuple(v[:3]) for v in handler.m_Vertices]
    triangles, submeshes = [], []
    for si, tris in enumerate(handler.get_triangles()):
        first = len(triangles)
        for tri in tris:
            if len(tri) == 3:
                triangles.append(tuple(tri))
        submeshes.append({"name": f"{info['name']}_{si}", "first": first,
                          "count": len(triangles) - first})

    def maybe(data, width):
        if not data:
            return None
        return [tuple(row[:width]) if len(row) >= width
                else tuple(row) + (0.0,) * (width - len(row)) for row in data]

    geo = MeshGeometry(
        name=info["name"], vertices=verts, triangles=triangles,
        normals=maybe(handler.m_Normals, 3), uvs=maybe(handler.m_UV0, 2),
        uvs2=maybe(handler.m_UV1, 2), colors=maybe(handler.m_Colors, 4),
        tangents=maybe(handler.m_Tangents, 4),
        bone_indices=maybe(handler.m_BoneIndices, 4),
        bone_weights=maybe(handler.m_BoneWeights, 4),
        submeshes=[sm for sm in submeshes if sm["count"]] or None,
        source=assets_path,
    )
    geo.ensure_normals()
    info["geometry"] = geo
    return geo, info


def mesh_write_info(assets_path, path_id):
    """What a replacement for this mesh is allowed to be.

    Returns the in-place budget (max_triangles / max_vertices), plus the flags
    the UI needs to warn about before an import: compressed meshes can't be
    written at all, blend shapes and baked collision won't follow the new
    geometry, and a 16-bit index buffer caps the vertex count at 65535.
    """
    import UnityPy
    env = UnityPy.load(assets_path)
    obj = next((o for o in env.objects if o.path_id == path_id), None)
    if obj is None:
        raise MeshError(f"No mesh with path_id {path_id} in "
                        f"{os.path.basename(assets_path)}.")
    mesh    = obj.read()
    version = obj.version
    stats   = _mesh_stats(mesh, obj)

    index_bytes  = len(bytes(mesh.m_IndexBuffer or b""))
    use_16bit    = _uses_16bit_indices(mesh, version)
    index_stride = 2 if use_16bit else 4
    stream       = getattr(mesh, "m_StreamData", None)
    streamed     = bool(stream and stream.path)

    inline_bytes = len(bytes(getattr(mesh.m_VertexData, "m_DataSize", b"") or b""))
    stride_total = 0
    channels     = list(getattr(mesh.m_VertexData, "m_Channels", None) or [])
    for ch in channels:
        if ch.dimension:
            stride_total += (ch.dimension & 0xF) * _component_size(ch.format, version)

    shapes = getattr(mesh, "m_Shapes", None)
    has_shapes = bool(getattr(shapes, "vertices", None)) if shapes is not None else False

    info = {
        "name":          obj.peek_name() or f"mesh_{path_id}",
        "path_id":       path_id,
        "assets_file":   assets_path,
        "streamed":      streamed,
        "compressed":    stats["compressed"],
        "skinned":       stats["skinned"],
        "legacy_skin":   bool(getattr(mesh, "m_Skin", None)),
        "blend_shapes":  has_shapes,
        "baked_collision": bool(getattr(mesh, "m_BakedTriangleCollisionMesh", None)),
        "submesh_count": stats["submeshes"],
        "vertices":      stats["vertices"],
        "triangles":     stats["triangles"],
        "index_stride":  index_stride,
        "index_bytes":   index_bytes,
        "max_triangles": index_bytes // (3 * index_stride),
        "max_vertices":  (65535 if use_16bit else 2 ** 31),
        "vertex_stride": stride_total,
        "channels":      [(ci, _channel_semantic(ci, version), ch.dimension, ch.format)
                          for ci, ch in enumerate(channels) if ch.dimension],
    }
    if not streamed and stride_total:
        # Inline vertex data can only shrink, never grow, without a rebuild.
        info["max_vertices"] = min(info["max_vertices"], inline_bytes // stride_total)
    return info


def _uses_16bit_indices(mesh, version):
    use_16bit = getattr(mesh, "m_Use16BitIndices", None)
    if use_16bit is not None:
        return bool(use_16bit)
    index_format = getattr(mesh, "m_IndexFormat", None)
    if index_format is not None and version >= (2017, 4):
        return index_format == 0
    return True


def _format_maps(version):
    from UnityPy.enums.VertexFormat import (
        VERTEX_CHANNEL_FORMAT_STRUCT_TYPE_MAP, VERTEX_FORMAT_2017_STRUCT_TYPE_MAP,
        VERTEX_FORMAT_STRUCT_TYPE_MAP, VertexChannelFormat, VertexFormat,
        VertexFormat2017)
    if version[0] < 2017:
        return VertexChannelFormat, VERTEX_CHANNEL_FORMAT_STRUCT_TYPE_MAP
    if version[0] < 2019:
        return VertexFormat2017, VERTEX_FORMAT_2017_STRUCT_TYPE_MAP
    return VertexFormat, VERTEX_FORMAT_STRUCT_TYPE_MAP


def _struct_char(format_id, version):
    enum, table = _format_maps(version)
    return table[enum(format_id)]


def _component_size(format_id, version):
    return struct.calcsize(_struct_char(format_id, version))


def _component_kind(format_id, version):
    """How a float value has to be quantized for this channel format.

    'float' writes as-is, 'unorm'/'snorm' scale into the integer range, 'int'
    rounds (bone indices, packed IDs).
    """
    enum, _ = _format_maps(version)
    name = enum(format_id).name
    if "Float" in name:
        return "float", 1.0
    if "UNorm8" in name or name.endswith("Color"):
        return "unorm", 255.0
    if "SNorm8" in name:
        return "snorm", 127.0
    if "UNorm16" in name:
        return "unorm", 65535.0
    if "SNorm16" in name:
        return "snorm", 32767.0
    if "kChannelFormatColor" in name or "kChannelFormatByte" in name:
        return "unorm", 255.0
    return "int", 1.0


# ══════════════════════════════════════════════════════════════════════════════
#  Unity side: writing a replacement mesh back in
# ══════════════════════════════════════════════════════════════════════════════
def _find_ress_file(assets_path, stream_path):
    """Resolve an m_StreamData.path ('archive:/CAB-x/foo.resS') to a real file."""
    assets_dir = os.path.dirname(assets_path)
    candidate = os.path.join(assets_dir, ntpath.basename(stream_path))
    if os.path.exists(candidate):
        return candidate
    for part in stream_path.replace("\\", "/").split("/"):
        candidate = os.path.join(assets_dir, part)
        if os.path.exists(candidate):
            return candidate
    return None


def _nearest_indices(src_points, dst_points):
    """For each dst point, the index of the closest src point.

    numpy path is chunked so a 50k x 50k distance matrix is never allocated;
    the fallback buckets src points into a uniform grid and walks outward, which
    keeps skin transfer usable on a machine without numpy.
    """
    if not src_points or not dst_points:
        return [0] * len(dst_points)

    if _np is not None:
        src = _np.asarray(src_points, dtype="float32")
        dst = _np.asarray(dst_points, dtype="float32")
        out = _np.empty(len(dst), dtype="int64")
        chunk = max(1, int(4_000_000 / max(1, len(src))))
        for start in range(0, len(dst), chunk):
            block = dst[start:start + chunk]
            d = ((block[:, None, :] - src[None, :, :]) ** 2).sum(-1)
            out[start:start + len(block)] = d.argmin(1)
        return out.tolist()

    lo = [min(p[i] for p in src_points) for i in range(3)]
    hi = [max(p[i] for p in src_points) for i in range(3)]
    span = max(max(hi[i] - lo[i] for i in range(3)), 1e-6)
    divisions = max(1, min(64, int(round(len(src_points) ** (1 / 3.0)))))
    cell = span / divisions

    grid = {}
    def key_of(p):
        return tuple(int((p[i] - lo[i]) / cell) for i in range(3))
    for i, p in enumerate(src_points):
        grid.setdefault(key_of(p), []).append(i)

    out = []
    for p in dst_points:
        kx, ky, kz = key_of(p)
        best, best_d = 0, float("inf")
        radius = 0
        while radius <= divisions + 1:
            found = False
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    for dz in range(-radius, radius + 1):
                        if radius and max(abs(dx), abs(dy), abs(dz)) != radius:
                            continue
                        for i in grid.get((kx + dx, ky + dy, kz + dz), ()):
                            q = src_points[i]
                            d = ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
                                 + (p[2] - q[2]) ** 2)
                            if d < best_d:
                                best_d, best = d, i
                            found = True
            # one extra ring past the first hit: the true nearest can sit in it
            if found and radius > 0:
                break
            radius += 1
        out.append(best)
    return out


def _channel_values(semantic, geo, dimension, original, nearest, warnings):
    """Per-vertex source data for one vertex channel, always `dimension` wide."""
    n = geo.vertex_count

    def pad(rows, fill):
        out = []
        for row in rows:
            row = tuple(row)
            if len(row) >= dimension:
                out.append(row[:dimension])
            else:
                out.append(row + fill[len(row):dimension])
        return out

    if semantic == "position":
        return pad(geo.vertices, (0.0, 0.0, 0.0, 1.0))
    if semantic == "normal":
        return pad(geo.ensure_normals(), (0.0, 1.0, 0.0, 0.0))
    if semantic == "tangent":
        if not geo.tangents:
            geo.compute_tangents()
        return pad(geo.tangents, (1.0, 0.0, 0.0, 1.0))
    if semantic == "color":
        colors = geo.colors or [(1.0, 1.0, 1.0, 1.0)] * n
        return pad(colors, (1.0, 1.0, 1.0, 1.0))
    if semantic == "uv0":
        return pad(geo.uvs or [(0.0, 0.0)] * n, (0.0, 0.0, 0.0, 0.0))
    if semantic == "uv1":
        return pad(geo.uvs2 or geo.uvs or [(0.0, 0.0)] * n, (0.0, 0.0, 0.0, 0.0))
    if semantic and semantic.startswith("uv"):
        return pad(geo.uvs or [(0.0, 0.0)] * n, (0.0, 0.0, 0.0, 0.0))
    if semantic in ("bone_weights", "bone_indices"):
        mine = geo.bone_weights if semantic == "bone_weights" else geo.bone_indices
        if mine:
            return pad(mine, (0.0, 0.0, 0.0, 0.0))
        source = (original.bone_weights if semantic == "bone_weights"
                  else original.bone_indices)
        if source and nearest is not None:
            return pad([source[i] for i in nearest], (0.0, 0.0, 0.0, 0.0))
        if semantic == "bone_weights":
            warnings.append("This mesh is skinned but the replacement has no bone "
                            "weights and none could be transferred -- it may not "
                            "animate correctly.")
            return pad([(1.0, 0.0, 0.0, 0.0)] * n, (0.0,) * 4)
        return pad([(0, 0, 0, 0)] * n, (0,) * 4)
    return pad([(0.0,) * dimension] * n, (0.0,) * 4)


def _build_vertex_buffer(geo, channels, streams, version, original, nearest,
                         warnings):
    """Interleave the replacement's per-vertex data into Unity's stream layout."""
    n = geo.vertex_count
    total = 0
    for st in streams:
        total = max(total, st.offset + n * st.stride)
    buf = bytearray(total)

    for ci, ch in enumerate(channels):
        dim = ch.dimension & 0xF
        if not dim:
            continue
        semantic = _channel_semantic(ci, version)
        values   = _channel_values(semantic, geo, dim, original, nearest, warnings)
        char     = _struct_char(ch.format, version)
        kind, scale = _component_kind(ch.format, version)
        packer   = struct.Struct("<" + char * dim)
        st       = streams[ch.stream]
        base     = st.offset + ch.offset
        stride   = st.stride

        for i in range(n):
            row = values[i]
            if kind == "float":
                out = row
            elif kind == "unorm":
                out = tuple(int(round(min(1.0, max(0.0, float(v))) * scale)) for v in row)
            elif kind == "snorm":
                out = tuple(int(round(min(1.0, max(-1.0, float(v))) * scale)) for v in row)
            else:
                out = tuple(int(round(float(v))) for v in row)
            pos = base + i * stride
            buf[pos:pos + packer.size] = packer.pack(*out)
    return bytes(buf)


def _assign_submeshes(mesh, geo, index_stride, warnings):
    """Point the existing submesh slots at the replacement's triangle groups.

    The array length is fixed (changing it would change the object's size), so
    extra groups in the imported file are merged into the last slot and unused
    slots are emptied with indexCount = 0.
    """
    slots  = list(mesh.m_SubMeshes)
    groups = [sm for sm in geo.submeshes if sm["count"]] or [
        {"name": geo.name, "first": 0, "count": geo.triangle_count}]

    if len(groups) > len(slots):
        merged = groups[:len(slots) - 1]
        tail_first = groups[len(slots) - 1]["first"]
        tail_count = sum(g["count"] for g in groups[len(slots) - 1:])
        merged.append({"name": "merged", "first": tail_first, "count": tail_count})
        groups = merged
        warnings.append(
            f"The replacement has more groups than this mesh has material slots "
            f"({len(slots)}); the extra groups were merged into the last slot.")

    for si, slot in enumerate(slots):
        if si < len(groups):
            g     = groups[si]
            tris  = geo.triangles[g["first"]: g["first"] + g["count"]]
            first = g["first"] * 3
            used  = sorted({i for tri in tris for i in tri}) or [0]
            slot.firstByte   = first * index_stride
            slot.indexCount  = g["count"] * 3
            slot.firstVertex = used[0]
            slot.vertexCount = used[-1] - used[0] + 1
            pts = [geo.vertices[i] for i in used]
        else:
            slot.firstByte   = 0
            slot.indexCount  = 0
            slot.firstVertex = 0
            slot.vertexCount = 0
            pts = []
        if getattr(slot, "topology", None) is not None:
            slot.topology = 0                 # triangles
        if getattr(slot, "baseVertex", None) is not None:
            slot.baseVertex = 0
        if getattr(slot, "triangleCount", None) is not None:
            slot.triangleCount = (groups[si]["count"] if si < len(groups) else 0)
        _set_aabb(slot.localAABB, pts)


def _set_aabb(aabb, points):
    if aabb is None:
        return
    if points:
        lo = [min(p[i] for p in points) for i in range(3)]
        hi = [max(p[i] for p in points) for i in range(3)]
    else:
        lo = hi = [0.0, 0.0, 0.0]
    aabb.m_Center.x = (lo[0] + hi[0]) / 2.0
    aabb.m_Center.y = (lo[1] + hi[1]) / 2.0
    aabb.m_Center.z = (lo[2] + hi[2]) / 2.0
    aabb.m_Extent.x = (hi[0] - lo[0]) / 2.0
    aabb.m_Extent.y = (hi[1] - lo[1]) / 2.0
    aabb.m_Extent.z = (hi[2] - lo[2]) / 2.0


def apply_mesh_mod(assets_path, path_id, geo, allow_rebuild=False,
                   ress_reset=None, transfer_skin=True):
    """Write `geo` over the Mesh at `path_id`. Returns a report dict.

    ress_reset: shared set of .resS paths already rewound to their backup during
    this apply run -- exactly the same contract as app.apply_single_mod(). The
    .resS is append-only within a run: rewinding it twice would strip the bytes
    of everything already applied, and the textures and meshes in one file share
    the same .resS, which is why the app applies both from a single pipeline.
    """
    import UnityPy
    from UnityPy.helpers.MeshHelper import MeshHandler
    from UnityPy.streams.EndianBinaryWriter import EndianBinaryWriter
    from UnityPy.helpers.TypeTreeHelper import write_typetree

    geo.validate()
    warnings = []

    env = UnityPy.load(assets_path)
    obj = next((o for o in env.objects if o.path_id == path_id), None)
    if obj is None:
        raise MeshError(f"No mesh with path_id {path_id} in "
                        f"{os.path.basename(assets_path)}.")
    mesh    = obj.read()
    version = obj.version
    name    = obj.peek_name() or f"mesh_{path_id}"

    if getattr(mesh, "m_MeshCompression", 0):
        raise MeshError(
            f"'{name}' is stored as a compressed mesh, which this app can read "
            f"but not rewrite. Export still works; import doesn't.")

    vertex_data = mesh.m_VertexData
    channels    = list(getattr(vertex_data, "m_Channels", None) or [])
    if not channels:
        raise MeshError(f"'{name}' has no vertex channel table to write into.")

    if version[0] < 5:
        # get_streams() below reconstructs the stream table the way Unity 5+
        # lays it out; older files carry an explicit m_Streams array with
        # different rules, and this game isn't one of them.
        raise MeshError(f"'{name}' comes from a pre-Unity-5 file, which this app "
                        f"can read but not rewrite.")

    use_16bit    = _uses_16bit_indices(mesh, version)
    index_stride = 2 if use_16bit else 4
    if use_16bit and geo.vertex_count > 65535:
        raise MeshError(
            f"'{name}' uses 16-bit indices, so it can address at most 65,535 "
            f"vertices; the replacement has {geo.vertex_count:,}. Decimate it in "
            f"Blender (Modifier > Decimate) and try again.")

    # ── original geometry, needed for skin transfer ────────────────────────────
    original = None
    nearest  = None
    needs_bones = any(_channel_semantic(ci, version) in ("bone_weights", "bone_indices")
                      for ci, ch in enumerate(channels) if ch.dimension)
    if needs_bones and transfer_skin and not geo.bone_weights:
        try:
            handler = MeshHandler(mesh, version=version)
            handler.process()
            original = MeshGeometry(
                name=name,
                vertices=[tuple(v[:3]) for v in (handler.m_Vertices or [])],
                triangles=[(0, 0, 0)],
                bone_indices=[tuple(r[:4]) for r in (handler.m_BoneIndices or [])],
                bone_weights=[tuple(r[:4]) for r in (handler.m_BoneWeights or [])],
            )
            if original.vertices and original.bone_weights:
                nearest = _nearest_indices(original.vertices, geo.vertices)
                warnings.append(
                    f"Bone weights were transferred from the original mesh's "
                    f"nearest vertices ({len(original.vertices):,} of them) so the "
                    f"replacement still animates. Check it in-game.")
        except Exception as exc:
            warnings.append(f"Couldn't read the original bone weights ({exc}).")
        # re-read: MeshHandler.process() swaps the streamed blob into m_DataSize
        env = UnityPy.load(assets_path)
        obj = next(o for o in env.objects if o.path_id == path_id)
        mesh = obj.read()
        vertex_data = mesh.m_VertexData
        channels = list(vertex_data.m_Channels)
    if original is None:
        original = MeshGeometry(vertices=[(0.0, 0.0, 0.0)], triangles=[(0, 0, 0)])

    # ── vertex buffer ─────────────────────────────────────────────────────────
    streams  = MeshHandler(mesh, version=version).get_streams(channels, geo.vertex_count)
    new_data = _build_vertex_buffer(geo, channels, streams, version, original,
                                    nearest, warnings)

    stream_info  = getattr(mesh, "m_StreamData", None)
    streamed     = bool(stream_info and stream_info.path)
    inline_bytes = len(bytes(getattr(vertex_data, "m_DataSize", b"") or b""))

    if streamed:
        ress_path = _find_ress_file(assets_path, stream_info.path)
        if not ress_path:
            raise MeshError(
                f"'{name}' streams its vertex data from '{stream_info.path}', "
                f"which isn't next to the .assets file.")
        if ress_reset is None:
            ress_reset = set()
        key = os.path.abspath(ress_path)
        if key not in ress_reset:
            bak = ress_path + BACKUP_SUFFIX
            if os.path.exists(bak):
                shutil.copy2(bak, ress_path)
            ress_reset.add(key)
        size_before = os.path.getsize(ress_path)
        pad = (-size_before) % 16          # keep every vertex blob 16-byte aligned
        with open(ress_path, "ab") as rf:
            if pad:
                rf.write(b"\0" * pad)
            rf.write(new_data)
        offset = size_before + pad
        stream_info.offset  = offset
        stream_info.size    = len(new_data)
        vertex_data.m_DataSize = b""
    else:
        if len(new_data) > inline_bytes and not allow_rebuild:
            raise MeshError(
                f"'{name}' stores its vertex data inside the .assets file, so a "
                f"replacement can use at most {inline_bytes:,} bytes of vertex "
                f"data; this one needs {len(new_data):,}. Use fewer vertices, or "
                f"turn on the rebuild fallback.")
        # Pad, never shrink: a shorter array would change the object's size.
        vertex_data.m_DataSize = (new_data.ljust(inline_bytes, b"\0")
                                  if len(new_data) < inline_bytes else new_data)
    vertex_data.m_VertexCount = geo.vertex_count

    # ── index buffer ──────────────────────────────────────────────────────────
    original_index_bytes = len(bytes(mesh.m_IndexBuffer or b""))
    flat = [i for tri in geo.triangles for i in tri]
    index_bytes = struct.pack(f"<{len(flat)}{'H' if use_16bit else 'I'}", *flat)
    if len(index_bytes) > original_index_bytes and not allow_rebuild:
        raise MeshError(
            f"'{name}' has room for {original_index_bytes // (3 * index_stride):,} "
            f"triangles and the replacement has {geo.triangle_count:,}.\n\n"
            f"Either decimate the model (Blender: Modifier > Decimate) or enable "
            f"the rebuild fallback in the Meshes tab, which lifts the limit by "
            f"rewriting the whole .assets file.")
    mesh.m_IndexBuffer = (index_bytes.ljust(original_index_bytes, b"\0")
                          if len(index_bytes) < original_index_bytes else index_bytes)

    # ── submeshes, bounds, leftovers ──────────────────────────────────────────
    _assign_submeshes(mesh, geo, index_stride, warnings)
    _set_aabb(mesh.m_LocalAABB, geo.vertices)
    if getattr(mesh, "m_IsReadable", None) is not None:
        mesh.m_IsReadable = True

    legacy_skin = list(getattr(mesh, "m_Skin", None) or [])
    if legacy_skin and len(legacy_skin) != geo.vertex_count:
        raise MeshError(
            f"'{name}' uses the legacy per-vertex skin array, which only matches a "
            f"replacement with exactly {len(legacy_skin):,} vertices.")
    if getattr(mesh, "m_Shapes", None) is not None and \
            getattr(mesh.m_Shapes, "vertices", None):
        warnings.append("This mesh has blend shapes (facial//morph animation). They "
                        "still point at the old vertex order and won't match the "
                        "replacement.")
    if getattr(mesh, "m_BakedTriangleCollisionMesh", None):
        warnings.append("The mesh's baked collision data is left untouched, so "
                        "physics still uses the original shape.")

    # ── serialize: in place if the size matches, else rebuild ─────────────────
    writer = EndianBinaryWriter(endian=obj.reader.endian)
    write_typetree(mesh, obj._get_typetree_node(), writer, env.file)
    new_bytes = writer.bytes

    if len(new_bytes) == obj.byte_size:
        with open(assets_path, "r+b") as f:
            f.seek(obj.byte_start)
            f.write(new_bytes)
        method = "in-place"
    elif allow_rebuild:
        # Objects this app never read are re-emitted from their raw bytes, so a
        # rebuild carries existing texture patches over -- but the README's
        # warning about full re-serialization is worth respecting, so the
        # rebuilt file is verified against the original object table BEFORE it
        # replaces anything. Anything missing, added or resized and the write is
        # abandoned with the game file untouched.
        expected = {o.path_id: o.byte_size for o in env.objects}
        mesh.save()
        rebuilt = env.file.save()
        tmp = assets_path + ".rebuild_tmp"
        with open(tmp, "wb") as f:
            f.write(rebuilt)
        try:
            actual = {o.path_id: o.byte_size for o in UnityPy.load(tmp).objects}
        except Exception as exc:
            os.remove(tmp)
            raise MeshError(f"The rebuilt '{os.path.basename(assets_path)}' could "
                            f"not be read back ({exc}); your game file was left "
                            f"alone.")
        lost    = set(expected) - set(actual)
        gained  = set(actual) - set(expected)
        resized = [pid for pid in expected
                   if pid != path_id and pid in actual
                   and actual[pid] != expected[pid]]
        if lost or gained or resized:
            os.remove(tmp)
            raise MeshError(
                f"Rebuilding '{os.path.basename(assets_path)}' would have changed "
                f"{len(lost)} lost / {len(gained)} added / {len(resized)} resized "
                f"other object(s), so it was abandoned and your game file was left "
                f"alone. Reduce the replacement's triangle count to fit the "
                f"in-place budget instead.")
        os.replace(tmp, assets_path)
        method = "rebuild"
        warnings.append(
            f"'{os.path.basename(assets_path)}' was rebuilt rather than patched in "
            f"place, because the replacement needed a bigger object "
            f"({len(new_bytes):,}b vs {obj.byte_size:,}b). All {len(expected):,} "
            f"other objects in the file were verified unchanged afterwards.")
    else:
        raise MeshError(
            f"'{name}' would serialize to {len(new_bytes):,} bytes instead of "
            f"{obj.byte_size:,}, so it can't be patched in place. Enable the "
            f"rebuild fallback to write it anyway.")

    return {"name": name, "method": method, "warnings": warnings,
            "vertices": geo.vertex_count, "triangles": geo.triangle_count,
            "streamed": streamed, "assets_file": assets_path, "path_id": path_id}


def revert_mesh_from_backup(assets_path, path_id):
    """Put one mesh back to stock without disturbing the rest of the file.

    Both files are read with UnityPy rather than assuming a shared layout, so
    this still works after a rebuild has moved every object's offset.
    """
    import UnityPy
    bak = assets_path + BACKUP_SUFFIX
    if not os.path.exists(bak):
        return False
    live = next((o for o in UnityPy.load(assets_path).objects
                 if o.path_id == path_id), None)
    orig = next((o for o in UnityPy.load(bak).objects
                 if o.path_id == path_id), None)
    if live is None or orig is None or live.byte_size != orig.byte_size:
        return False
    with open(bak, "rb") as f:
        f.seek(orig.byte_start)
        data = f.read(orig.byte_size)
    if len(data) != live.byte_size:
        return False
    with open(assets_path, "r+b") as f:
        f.seek(live.byte_start)
        f.write(data)
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  Mod bookkeeping -- mesh_mods.json next to the texture/audio metadata
# ══════════════════════════════════════════════════════════════════════════════
def mesh_key(assets_file, path_id):
    return f"{os.path.basename(assets_file)}:{path_id}"


def load_mesh_meta(mods_folder):
    path = os.path.join(mods_folder, MESH_META_FILE)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_mesh_meta(mods_folder, meta):
    os.makedirs(mods_folder, exist_ok=True)
    with open(os.path.join(mods_folder, MESH_META_FILE), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


def store_replacement(mods_folder, source_path, mesh_name, path_id=None):
    """Copy an imported model into the mods folder so the mod survives the
    original file being moved or deleted. Returns the stored path.

    path_id is part of the filename because mesh names are not unique in this
    game -- without it, replacing two different meshes that happen to share a
    name would silently overwrite one with the other.
    """
    dest_dir = os.path.join(mods_folder, MESH_SUBFOLDER)
    os.makedirs(dest_dir, exist_ok=True)
    ext  = os.path.splitext(source_path)[1].lower() or ".obj"
    stem = _safe_name(mesh_name) + (f"_{path_id}" if path_id is not None else "")
    dest = os.path.join(dest_dir, stem + ext)
    if os.path.abspath(dest) != os.path.abspath(source_path):
        shutil.copy2(source_path, dest)
    return dest
