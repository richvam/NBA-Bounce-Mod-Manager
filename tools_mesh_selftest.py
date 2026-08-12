"""
tools_mesh_selftest.py -- checks the mesh pipeline without a copy of the game.

    python tools_mesh_selftest.py

Covers the parts that are easy to get quietly wrong:

  1. every exporter/importer round-trips a mesh with UVs, normals and multiple
     submeshes, in the right handedness and winding;
  2. the vertex buffer this app WRITES is read back correctly by UnityPy's own
     mesh decoder -- the same code path the game's assets go through -- for a
     Unity 6 channel layout including packed colors and bone weights;
  3. the index buffer, submesh windows and bounds land where the decoder
     expects them, including the zero-padding that keeps an in-place patch the
     same byte size as the object it replaces;
  4. bone weights transfer to a replacement that has none.

Sections 2-4 need UnityPy; they're skipped with a note if it isn't installed.
The 3D viewer isn't exercised here (it needs a display); run `python mesh_tab.py`
for that.
"""

import math
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mesh_manager as mm

VERSION = (6000, 0, 37, 1)          # NBA Bounce is Unity 6000.0.37f1
PASS, FAIL, SKIP = [], [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{('  -- ' + detail) if detail and not condition else ''}")
    return condition


def close(a, b, tol=1e-4):
    return abs(a - b) <= tol


# ── sample geometry ──────────────────────────────────────────────────────────
def sample_sphere(segments=20, rings=14, radius=0.75):
    verts, uvs, tris = [], [], []
    for i in range(rings + 1):
        theta = math.pi * i / rings
        for j in range(segments + 1):
            phi = 2 * math.pi * j / segments
            verts.append((radius * math.sin(theta) * math.cos(phi),
                          radius * math.cos(theta),
                          radius * math.sin(theta) * math.sin(phi)))
            uvs.append((j / segments, i / rings))
    for i in range(rings):
        for j in range(segments):
            a = i * (segments + 1) + j
            b = a + segments + 1
            tris.append((a, b, a + 1))
            tris.append((a + 1, b, b + 1))
    half = len(tris) // 2
    geo = mm.MeshGeometry(
        "selftest_ball", verts, tris, uvs=uvs,
        submeshes=[{"name": "upper", "first": 0, "count": half},
                   {"name": "lower", "first": half, "count": len(tris) - half}])
    geo.compute_normals()
    # inside 0..1: vertex colors are a normalized channel, and anything outside
    # that range is legitimately clamped on the way into the vertex buffer
    geo.colors = [(v[0] / (2 * radius) + 0.5, v[1] / (2 * radius) + 0.5,
                   v[2] / (2 * radius) + 0.5, 1.0) for v in verts]
    return geo


def corners(geo, attr):
    """Per-triangle-corner values, so two meshes can be compared even when
    their vertices were renumbered by a format's own indexing rules."""
    data = getattr(geo, attr)
    return [tuple(c for v in tri for c in data[v]) for tri in geo.triangles]


def max_delta(a, b):
    return max(max(abs(x - y) for x, y in zip(r1, r2)) for r1, r2 in zip(a, b))


# ── 1. file formats ──────────────────────────────────────────────────────────
def test_formats():
    print("\nFile formats")
    geo = sample_sphere()
    folder = tempfile.mkdtemp(prefix="mesh_selftest_")

    for ext in ("obj", "stl", "ply", "glb"):
        path = os.path.join(folder, "sample." + ext)
        mm.export_geometry(geo, path)
        check(f"export .{ext}", os.path.getsize(path) > 0)

    for ext in ("obj", "ply"):
        back = mm.import_geometry(os.path.join(folder, "sample." + ext))
        check(f".{ext} keeps every vertex", back.vertex_count == geo.vertex_count,
              f"{back.vertex_count} vs {geo.vertex_count}")
        check(f".{ext} keeps positions and winding",
              max_delta(corners(back, "vertices"), corners(geo, "vertices")) < 1e-4)
        check(f".{ext} keeps UVs",
              back.uvs is not None
              and max_delta(corners(back, "uvs"), corners(geo, "uvs")) < 1e-4)
        check(f".{ext} keeps normals",
              back.normals is not None
              and max_delta(corners(back, "normals"), corners(geo, "normals")) < 1e-3)
    obj_back = mm.import_geometry(os.path.join(folder, "sample.obj"))
    check("OBJ keeps submesh groups", len(obj_back.submeshes) == 2,
          f"{len(obj_back.submeshes)} groups")

    # STL stores loose triangles, so the importer welds identical positions back
    # together. On a UV sphere that also collapses the pole fans -- their three
    # corners share a position -- and those degenerate triangles are dropped.
    stl_back = mm.import_geometry(os.path.join(folder, "sample.stl"))
    solid = [row for row in corners(geo, "vertices")
             if len({tuple(round(c, 6) for c in row[i:i + 3])
                     for i in (0, 3, 6)}) == 3]
    check("STL welds shared vertices",
          stl_back.vertex_count < geo.triangle_count * 3
          and stl_back.triangle_count == len(solid),
          f"{stl_back.triangle_count} triangles, expected {len(solid)}")
    # STL stores float32, so round before comparing (and round BEFORE sorting:
    # near-equal float32/float64 pairs can otherwise sort into a different order)
    def rounded(rows):
        return sorted(tuple(round(c, 5) for c in row) for row in rows)
    check("STL keeps positions",
          rounded(corners(stl_back, "vertices")) == rounded(solid))

    raw = open(os.path.join(folder, "sample.glb"), "rb").read()
    magic, version, total = struct.unpack("<4sII", raw[:12])
    json_len, json_type = struct.unpack("<II", raw[12:20])
    bin_len, bin_type = struct.unpack("<II", raw[20 + json_len:28 + json_len])
    check("GLB header is valid glTF 2.0",
          magic == b"glTF" and version == 2 and total == len(raw))
    check("GLB chunks are JSON + BIN, 4-byte aligned",
          json_type == 0x4E4F534A and bin_type == 0x004E4942
          and json_len % 4 == 0 and bin_len % 4 == 0)

    scaled = geo.scaled(2.0)
    check("scale multiplies the bounding box",
          close(max(scaled.size()), max(geo.size()) * 2))
    moved = geo.recentered((5.0, 0.0, 0.0))
    check("recenter moves the bounding box centre",
          close(moved.center_extent()[0][0], 5.0))

    round_trip = geo.flipped_handedness().flipped_handedness()
    check("handedness flip is its own inverse",
          round_trip.vertices == geo.vertices
          and round_trip.triangles == geo.triangles)


# ── 2-4. Unity vertex buffer ─────────────────────────────────────────────────
def _fake_unity_mesh(geo, skinned=True, use_16bit=True):
    """A Mesh object shaped like one out of the game, built in memory.

    Channel layout mirrors a skinned Unity 6 character mesh: float3 position,
    float3 normal, float4 tangent, UNorm8 color, two float2 UV sets, float4
    bone weights and UInt8 bone indices.
    """
    from UnityPy.classes.generated import (AABB, ChannelInfo, CompressedMesh, Mesh,
                                           PackedBitVector, StreamingInfo, SubMesh,
                                           Vector3f, VertexData)

    def pbv():
        return PackedBitVector(m_Data=b"", m_NumItems=0, m_BitSize=0, m_Range=0.0,
                               m_Start=0.0)

    def box():
        return AABB(m_Center=Vector3f(x=0.0, y=0.0, z=0.0),
                    m_Extent=Vector3f(x=0.0, y=0.0, z=0.0))

    compressed = CompressedMesh(
        **{f"m_{n}": pbv() for n in ("BoneIndices", "NormalSigns", "Normals",
                                     "TangentSigns", "Tangents", "Triangles", "UV",
                                     "Vertices", "Weights")},
        m_BindPoses=pbv(), m_Colors=pbv(), m_FloatColors=pbv(), m_UVInfo=0)

    channels = [ChannelInfo(dimension=0, format=0, offset=0, stream=0) for _ in range(14)]
    layout = [(0, 3, 0), (1, 3, 0), (2, 4, 0), (3, 4, 2), (4, 2, 0), (5, 2, 0)]
    if skinned:
        layout += [(12, 4, 0), (13, 4, 6)]      # weights float32, indices UInt8
    offset = 0
    for index, dimension, fmt in layout:
        channels[index] = ChannelInfo(dimension=dimension, format=fmt,
                                      offset=offset, stream=0)
        offset += dimension * mm._component_size(fmt, VERSION)

    vertex_data = VertexData(m_DataSize=b"", m_VertexCount=0, m_Channels=channels,
                             m_CurrentChannels=0)
    submeshes = [SubMesh(firstByte=0, firstVertex=0, indexCount=0, localAABB=box(),
                         vertexCount=0, baseVertex=0, topology=0)
                 for _ in geo.submeshes]
    return Mesh(m_BindPose=[], m_CompressedMesh=compressed, m_IndexBuffer=b"",
                m_LocalAABB=box(), m_MeshCompression=0, m_Name="selftest",
                m_SubMeshes=submeshes, m_VertexData=vertex_data,
                m_StreamData=StreamingInfo(offset=0, path="", size=0),
                m_Use16BitIndices=1 if use_16bit else 0,
                m_IndexFormat=0 if use_16bit else 1, m_IsReadable=True,
                m_MeshUsageFlags=0, m_Skin=[], m_Shapes=None)


def _encode_into(mesh, geo, original=None, nearest=None, index_padding=0,
                 use_16bit=True):
    """Run this app's writer over `geo` and store the result on `mesh`."""
    from UnityPy.helpers.MeshHelper import MeshHandler

    warnings = []
    channels = list(mesh.m_VertexData.m_Channels)
    streams = MeshHandler(mesh, version=VERSION).get_streams(channels, geo.vertex_count)
    blob = mm._build_vertex_buffer(geo, channels, streams, VERSION,
                                   original or mm.MeshGeometry(vertices=[(0, 0, 0)],
                                                               triangles=[(0, 0, 0)]),
                                   nearest, warnings)
    mesh.m_VertexData.m_DataSize = blob
    mesh.m_VertexData.m_VertexCount = geo.vertex_count

    flat = [i for tri in geo.triangles for i in tri]
    packed = struct.pack(f"<{len(flat)}{'H' if use_16bit else 'I'}", *flat)
    mesh.m_IndexBuffer = packed + b"\0" * index_padding
    mm._assign_submeshes(mesh, geo, 2 if use_16bit else 4, warnings)
    mm._set_aabb(mesh.m_LocalAABB, geo.vertices)
    return blob, warnings


def test_unity_roundtrip():
    print("\nUnity vertex buffer (written here, decoded by UnityPy)")
    try:
        from UnityPy.helpers.MeshHelper import MeshHandler
    except ImportError:
        SKIP.append("unity round-trip")
        print("  SKIP  UnityPy isn't installed")
        return

    geo = sample_sphere()
    geo.compute_tangents()
    count = geo.vertex_count
    geo.bone_weights = [(0.6, 0.4, 0.0, 0.0)] * count
    geo.bone_indices = [(3, 7, 0, 0)] * count

    mesh = _fake_unity_mesh(geo)
    blob, _ = _encode_into(mesh, geo)

    handler = MeshHandler(mesh, version=VERSION)
    handler.process()

    check("vertex count survives", handler.m_VertexCount == count,
          f"{handler.m_VertexCount} vs {count}")
    check("positions survive",
          max(max(abs(a - b) for a, b in zip(got[:3], want))
              for got, want in zip(handler.m_Vertices, geo.vertices)) < 1e-5)
    check("normals survive",
          max(max(abs(a - b) for a, b in zip(got[:3], want))
              for got, want in zip(handler.m_Normals, geo.normals)) < 1e-5)
    check("UV0 survives",
          max(max(abs(a - b) for a, b in zip(got[:2], want))
              for got, want in zip(handler.m_UV0, geo.uvs)) < 1e-5)
    check("UNorm8 colors survive quantization",
          max(max(abs(a - b) for a, b in zip(got[:3], want[:3]))
              for got, want in zip(
                  [tuple(c / 255.0 for c in row) for row in handler.m_Colors],
                  geo.colors)) < 1.0 / 255,
          "packed color channel decoded wrong")
    check("bone weights survive",
          handler.m_BoneWeights is not None
          and close(handler.m_BoneWeights[0][0], 0.6))
    check("bone indices survive",
          handler.m_BoneIndices is not None
          and tuple(handler.m_BoneIndices[0]) == (3, 7, 0, 0))

    decoded = handler.get_triangles()
    check("submesh count preserved", len(decoded) == 2, f"{len(decoded)} submeshes")
    flat = [tri for sub in decoded for tri in sub]
    check("every triangle decodes identically",
          [tuple(t) for t in flat] == [tuple(t) for t in geo.triangles],
          "index buffer or submesh windows are wrong")
    check("local bounds match the geometry",
          close(mesh.m_LocalAABB.m_Extent.y, max(v[1] for v in geo.vertices), 1e-4))
    check("stream layout size matches the encoder",
          len(blob) == count * (3 + 3 + 4 + 2 + 2) * 4 + count * 4 + count * 16 + count * 4,
          f"{len(blob)} bytes for {count} vertices")


def test_index_padding():
    """A replacement with fewer triangles must keep the object's byte size by
    padding the index buffer past the last submesh window."""
    print("\nIn-place padding")
    try:
        from UnityPy.helpers.MeshHelper import MeshHandler
    except ImportError:
        SKIP.append("index padding")
        print("  SKIP  UnityPy isn't installed")
        return

    geo = sample_sphere(segments=8, rings=6)
    mesh = _fake_unity_mesh(geo, skinned=False)
    padding = 600                                     # bytes of dead space
    _encode_into(mesh, geo, index_padding=padding)

    handler = MeshHandler(mesh, version=VERSION)
    handler.process()
    decoded = [tuple(t) for sub in handler.get_triangles() for t in sub]
    check("padding is never indexed by a submesh",
          decoded == [tuple(t) for t in geo.triangles],
          f"{len(decoded)} triangles decoded, expected {geo.triangle_count}")
    check("padded buffer keeps the original byte size",
          len(bytes(mesh.m_IndexBuffer)) == geo.triangle_count * 3 * 2 + padding)

    # unused submesh slots must be emptied, not left pointing at old triangles
    single = mm.MeshGeometry("one", geo.vertices, geo.triangles[:10])
    spare = _fake_unity_mesh(geo, skinned=False)      # two submesh slots
    _encode_into(spare, single)
    check("spare submesh slots are emptied",
          spare.m_SubMeshes[1].indexCount == 0)
    check("first slot covers the whole replacement",
          spare.m_SubMeshes[0].indexCount == 30)

    # 32-bit index path
    wide = _fake_unity_mesh(geo, skinned=False, use_16bit=False)
    _encode_into(wide, geo, use_16bit=False)
    handler = MeshHandler(wide, version=VERSION)
    handler.process()
    decoded = [tuple(t) for sub in handler.get_triangles() for t in sub]
    check("32-bit index buffers decode too",
          decoded == [tuple(t) for t in geo.triangles])


def test_skin_transfer():
    print("\nSkin weight transfer")
    original = mm.MeshGeometry(
        "orig",
        vertices=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        triangles=[(0, 1, 2)],
        bone_indices=[(1, 0, 0, 0), (2, 0, 0, 0), (3, 0, 0, 0)],
        bone_weights=[(1.0, 0.0, 0.0, 0.0)] * 3)
    replacement = [(0.9, 0.05, 0.0), (0.02, 0.98, 0.0), (0.01, 0.0, 0.0)]
    nearest = mm._nearest_indices(original.vertices, replacement)
    check("nearest-vertex lookup finds the right source", nearest == [1, 2, 0],
          f"got {nearest}")

    new_geo = mm.MeshGeometry("new", replacement, [(0, 1, 2)])
    warnings = []
    values = mm._channel_values("bone_indices", new_geo, 4, original, nearest, warnings)
    check("bone indices come from the nearest original vertex",
          [v[0] for v in values] == [2, 3, 1], f"got {[v[0] for v in values]}")

    values = mm._channel_values("bone_weights", new_geo, 4, original, None, warnings)
    check("a skinned mesh with no transferable weights warns", bool(warnings))

    if mm._np is not None:
        # the grid fallback has to agree with the numpy path
        saved, mm._np = mm._np, None
        try:
            fallback = mm._nearest_indices(original.vertices, replacement)
        finally:
            mm._np = saved
        check("numpy and pure-Python lookups agree", fallback == nearest,
              f"{fallback} vs {nearest}")
    else:
        SKIP.append("numpy/pure-python agreement")
        print("  SKIP  numpy isn't installed")


def test_validation():
    print("\nGuard rails")
    geo = mm.MeshGeometry("bad", [(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 9)])
    try:
        geo.validate()
        check("out-of-range face index is rejected", False)
    except mm.MeshError:
        check("out-of-range face index is rejected", True)

    try:
        mm.import_geometry("model.fbx")
        check("unknown format is rejected", False)
    except mm.MeshError:
        check("unknown format is rejected", True)

    empty = mm.MeshGeometry("empty", [], [])
    try:
        empty.validate()
        check("empty mesh is rejected", False)
    except mm.MeshError:
        check("empty mesh is rejected", True)


def main():
    print("NBA Bounce Mod Manager -- mesh pipeline self-test")
    test_formats()
    test_unity_roundtrip()
    test_index_padding()
    test_skin_transfer()
    test_validation()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
    if FAIL:
        print("Failures: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
