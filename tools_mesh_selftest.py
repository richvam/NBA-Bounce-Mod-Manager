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
  4. the .resS append/rewind bookkeeping, against real files on disk -- the
     shared stream file is rewound once per apply run, not once per mod;
  5. bone weights transfer to a replacement that has none, end to end: a
     skinned mesh's original data is read out of a .resS, an unrigged
     replacement is written back, and the result is decoded again.

Sections 2-5 need UnityPy; they're skipped with a note if it isn't installed.
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


class _FakeReader:
    endian = "<"


class _FakeObject:
    """Stands in for a UnityPy ObjectReader over a mesh we built in memory."""

    def __init__(self, mesh, path_id, byte_start, byte_size):
        self.mesh = mesh
        self.path_id = path_id
        self.byte_start = byte_start
        self.byte_size = byte_size
        self.version = VERSION
        self.reader = _FakeReader()
        self.assets_file = None      # only read when resolving streamed data
        self.type = type("T", (), {"name": "Mesh"})()

    def read(self):
        return self.mesh

    def peek_name(self):
        return self.mesh.m_Name

    def _get_typetree_node(self):
        return None


def test_stream_writeback():
    """The .resS dance: rewind once per run, append, record the right offset.

    This is the failure mode the app already hit once with textures -- rewinding
    the shared .resS per mod instead of per run silently strands every mod
    applied before it -- so it gets tested with real files on disk rather than
    reasoned about.
    """
    print("\nStreamed writeback (.resS offsets)")
    try:
        import UnityPy
        from UnityPy.helpers import TypeTreeHelper
    except ImportError:
        SKIP.append("streamed writeback")
        print("  SKIP  UnityPy isn't installed")
        return

    folder = tempfile.mkdtemp(prefix="mesh_selftest_ress_")
    assets = os.path.join(folder, "sharedassets1.assets")
    ress = assets + ".resS"
    original_assets = bytes(range(256)) * 40           # 10 KB of recognizable filler
    original_ress = b"PIXELS!!" * 128                  # stock streamed data
    for path, blob in ((assets, original_assets), (ress, original_ress)):
        with open(path, "wb") as f:
            f.write(blob)
        with open(path + mm.BACKUP_SUFFIX, "wb") as f:
            f.write(blob)
    # a previous apply already appended texture bytes to the live .resS
    with open(ress, "ab") as f:
        f.write(b"OLDMOD" * 20)

    geo = sample_sphere(segments=10, rings=8)
    meshes = [_fake_unity_mesh(geo, skinned=False) for _ in range(2)]
    for mesh in meshes:
        mesh.m_StreamData.path = "archive:/CAB-fake/sharedassets1.assets.resS"
        mesh.m_StreamData.offset = 0
        mesh.m_StreamData.size = 0
        mesh.m_IndexBuffer = b"\0" * (geo.triangle_count * 3 * 2 + 128)

    objects = [_FakeObject(meshes[0], 11, 512, 96),
               _FakeObject(meshes[1], 22, 4096, 96)]
    env = type("Env", (), {"objects": objects, "file": None})()

    real_load, real_write = UnityPy.load, TypeTreeHelper.write_typetree
    UnityPy.load = lambda *a, **k: env
    TypeTreeHelper.write_typetree = lambda tree, node, writer, af: writer.write_bytes(
        b"M" * 96)
    try:
        shared = set()
        first = mm.apply_mesh_mod(assets, 11, geo, ress_reset=shared)
        second = mm.apply_mesh_mod(assets, 22, geo, ress_reset=shared)

        check("both meshes patched in place",
              first["method"] == "in-place" and second["method"] == "in-place")
        check("the .resS was rewound exactly once", len(shared) == 1)

        blob = open(ress, "rb").read()
        check("the .resS starts from its backup, not the modded copy",
              blob[:len(original_ress)] == original_ress
              and b"OLDMOD" not in blob,
              "stale mod bytes were left in the stream file")

        # float3 pos + float3 normal + float4 tangent + UNorm8 colour + 2 float2 UVs
        stride = 12 + 12 + 16 + 4 + 8 + 8
        for mesh, label in zip(meshes, ("first", "second")):
            start = mesh.m_StreamData.offset
            size = mesh.m_StreamData.size
            check(f"{label} mesh's vertex data is where its offset says",
                  start + size <= len(blob) and size == geo.vertex_count * stride,
                  f"offset {start}, size {size}, file {len(blob)} bytes")
            check(f"{label} mesh's blob is 16-byte aligned", start % 16 == 0)
        check("the two meshes don't overlap in the .resS",
              meshes[0].m_StreamData.offset + meshes[0].m_StreamData.size
              <= meshes[1].m_StreamData.offset)
        check("appending never shrinks the .resS",
              len(blob) >= len(original_ress))

        patched = open(assets, "rb").read()
        check("each object was spliced at its own byte_start",
              patched[512:512 + 96] == b"M" * 96
              and patched[4096:4096 + 96] == b"M" * 96)
        check("everything outside those objects is untouched",
              patched[:512] == original_assets[:512]
              and patched[608:4096] == original_assets[608:4096]
              and len(patched) == len(original_assets))

        # over-budget replacement, rebuild not allowed -> refused, file untouched
        before = open(assets, "rb").read()
        big = sample_sphere(segments=40, rings=30)
        try:
            mm.apply_mesh_mod(assets, 11, big, ress_reset=shared)
            check("an over-budget replacement is refused", False)
        except mm.MeshError as exc:
            check("an over-budget replacement is refused",
                  "triangle" in str(exc) or "rebuild" in str(exc), str(exc))
        check("a refused write leaves the .assets file alone",
              open(assets, "rb").read() == before)

        # object that would change size, rebuild not allowed -> refused
        TypeTreeHelper.write_typetree = lambda tree, node, writer, af: \
            writer.write_bytes(b"M" * 128)
        try:
            mm.apply_mesh_mod(assets, 11, geo, ress_reset=shared)
            check("a size change without the rebuild option is refused", False)
        except mm.MeshError:
            check("a size change without the rebuild option is refused", True)

        # compressed meshes are read-only
        meshes[0].m_MeshCompression = 1
        try:
            mm.apply_mesh_mod(assets, 11, geo, ress_reset=shared)
            check("compressed meshes are refused", False)
        except mm.MeshError:
            check("compressed meshes are refused", True)
        meshes[0].m_MeshCompression = 0
    finally:
        UnityPy.load, TypeTreeHelper.write_typetree = real_load, real_write


def test_streamed_skin_replacement():
    """The whole trip for a skinned, streamed mesh: original vertex data read
    out of a .resS, a replacement written back into it, and the result decoded
    again -- which is what actually happens to a player model."""
    print("\nStreamed skinned mesh, end to end")
    try:
        import UnityPy
        from UnityPy.helpers import MeshHelper, TypeTreeHelper
        from UnityPy.helpers.MeshHelper import MeshHandler
    except ImportError:
        SKIP.append("streamed skinned replacement")
        print("  SKIP  UnityPy isn't installed")
        return

    folder = tempfile.mkdtemp(prefix="mesh_selftest_skin_")
    assets = os.path.join(folder, "sharedassets1.assets")
    ress = assets + ".resS"

    geo = sample_sphere(segments=16, rings=12)
    original = geo.copy()
    original.bone_weights = [(1.0, 0.0, 0.0, 0.0)] * geo.vertex_count
    original.bone_indices = [(2, 5, 0, 0)] * geo.vertex_count

    mesh = _fake_unity_mesh(geo, skinned=True)
    mesh.m_Name = "player_body"
    channels = list(mesh.m_VertexData.m_Channels)
    streams = MeshHandler(mesh, version=VERSION).get_streams(channels, geo.vertex_count)
    vertex_blob = mm._build_vertex_buffer(original, channels, streams, VERSION,
                                          original, None, [])

    filler = b"STOCKPIX" * 128
    pad = (-len(filler)) % 16
    stock_ress = filler + b"\0" * pad + vertex_blob
    stock_assets = bytes(range(256)) * 40
    for path, blob in ((assets, stock_assets), (ress, stock_ress)):
        with open(path, "wb") as f:
            f.write(blob)
        with open(path + mm.BACKUP_SUFFIX, "wb") as f:
            f.write(blob)

    mesh.m_StreamData.path = "archive:/CAB-fake/sharedassets1.assets.resS"
    mesh.m_StreamData.offset = len(filler) + pad
    mesh.m_StreamData.size = len(vertex_blob)
    mesh.m_VertexData.m_DataSize = b""
    mesh.m_VertexData.m_VertexCount = geo.vertex_count
    mesh.m_IndexBuffer = struct.pack(f"<{geo.triangle_count * 3}H",
                                     *[i for tri in geo.triangles for i in tri])
    mm._assign_submeshes(mesh, geo, 2, [])

    obj = _FakeObject(mesh, 77, 1024, 96)
    mesh.set_object_reader(obj)
    env = type("Env", (), {"objects": [obj], "file": None})()

    real_load = UnityPy.load
    real_write = TypeTreeHelper.write_typetree
    real_res = MeshHelper.get_resource_data
    UnityPy.load = lambda *a, **k: env
    TypeTreeHelper.write_typetree = lambda tree, node, writer, af: writer.write_bytes(
        b"M" * 96)
    MeshHelper.get_resource_data = lambda path, af, offset, size: \
        open(ress, "rb").read()[offset:offset + size]
    try:
        loaded, info = mm.load_mesh(assets, 77)
        check("streamed vertex data is read back out of the .resS",
              loaded.vertex_count == geo.vertex_count
              and max(max(abs(a - b) for a, b in zip(v1, v2))
                      for v1, v2 in zip(loaded.vertices, geo.vertices)) < 1e-5)
        check("the mesh reports itself as streamed and skinned",
              info["streamed"] and info["skinned"])

        budget = mm.mesh_write_info(assets, 77)
        check("the triangle budget matches the original",
              budget["max_triangles"] == geo.triangle_count,
              f"{budget['max_triangles']} vs {geo.triangle_count}")

        # a replacement with no rigging at all, half the size
        replacement = geo.scaled(0.5)
        report = mm.apply_mesh_mod(assets, 77, replacement, ress_reset=set())
        check("a skinned mesh accepts an unrigged replacement",
              report["method"] == "in-place")
        check("the transfer is reported to the user",
              any("Bone weights were transferred" in w for w in report["warnings"]))

        decoded = MeshHandler(mesh, version=VERSION)
        decoded.process()
        check("the written mesh decodes back to the replacement",
              decoded.m_VertexCount == replacement.vertex_count
              and max(max(abs(a - b) for a, b in zip(got[:3], want))
                      for got, want in zip(decoded.m_Vertices,
                                           replacement.vertices)) < 1e-5)
        check("bone indices survived the replacement",
              tuple(decoded.m_BoneIndices[0]) == (2, 5, 0, 0),
              f"got {tuple(decoded.m_BoneIndices[0])}")
        check("bone weights survived the replacement",
              close(decoded.m_BoneWeights[0][0], 1.0))
        triangles = [tuple(t) for sub in decoded.get_triangles() for t in sub]
        check("triangles survived the replacement",
              triangles == [tuple(t) for t in replacement.triangles])

        check("removing the mod puts the object back to stock",
              mm.revert_mesh_from_backup(assets, 77)
              and open(assets, "rb").read()[1024:1120] == stock_assets[1024:1120])
    finally:
        UnityPy.load = real_load
        TypeTreeHelper.write_typetree = real_write
        MeshHelper.get_resource_data = real_res


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


def test_file_locks():
    """Windows won't rename over a file anything still has open.

    UnityPy keeps the .assets stream open for the life of an Environment, so a
    rebuild that didn't close its readers first failed with WinError 32 every
    time on Windows while passing on Linux, where the rename is legal. These
    checks pin both halves of the fix: handles get released, and a file the
    game or Steam is still holding fails with an explanation instead of a
    traceback -- with the game file untouched.
    """
    print("\nFile handles and locked files")
    folder = tempfile.mkdtemp(prefix="mesh_selftest_lock_")
    target = os.path.join(folder, "sharedassets1.assets")
    with open(target, "wb") as f:
        f.write(b"STOCK" * 100)

    def holder(handle):
        return type("R", (), {"stream": handle})()

    handles = [open(target, "rb") for _ in range(3)]
    env = type("Env", (), {
        "files": {"sharedassets1.assets": holder(handles[0])},
        "objects": [type("O", (), {"reader": holder(handles[1])})()],
        "file": type("F", (), {"reader": holder(handles[2])})(),
    })()
    mm.close_env(env)
    check("close_env releases every reader an environment holds",
          all(h.closed for h in handles),
          f"{sum(not h.closed for h in handles)} handle(s) still open")

    try:
        mm.close_env(None)
        mm.close_env(type("Bare", (), {})())
        check("close_env tolerates an environment with nothing to close", True)
    except Exception as exc:
        check("close_env tolerates an environment with nothing to close", False,
              str(exc))

    # browsing a file must not leave a handle behind for a later rebuild
    try:
        import UnityPy
    except ImportError:
        SKIP.append("browse closes its handle")
        print("  SKIP  UnityPy isn't installed")
    else:
        browsing = open(target, "rb")
        real_load = UnityPy.load
        UnityPy.load = lambda *a, **k: type("Env", (), {
            "files": {"x": holder(browsing)}, "objects": [], "file": None})()
        try:
            mm.list_meshes_in_file(target)
        finally:
            UnityPy.load = real_load
        check("listing meshes closes the file it opened", browsing.closed)

    # the swap itself
    tmp = target + ".rebuild_tmp"
    with open(tmp, "wb") as f:
        f.write(b"REBUILT" * 50)
    mm._replace_game_file(tmp, target, "mesh")
    check("an unlocked file is replaced normally",
          open(target, "rb").read() == b"REBUILT" * 50
          and not os.path.exists(tmp))

    stock = open(target, "rb").read()
    with open(tmp, "wb") as f:
        f.write(b"NEVER" * 50)
    real_replace, real_sleep = os.replace, mm.time.sleep

    def locked(src, dst):
        raise PermissionError(32, "The process cannot access the file because "
                                  "it is being used by another process")

    os.replace, mm.time.sleep = locked, lambda s: None
    try:
        mm._replace_game_file(tmp, target, "mesh")
        check("a locked game file is reported, not raised as WinError 32", False)
    except mm.MeshError as exc:
        check("a locked game file is reported, not raised as WinError 32",
              "Steam" in str(exc) and "left untouched" in str(exc), str(exc))
    except PermissionError:
        check("a locked game file is reported, not raised as WinError 32", False,
              "the raw PermissionError reached the caller")
    finally:
        os.replace, mm.time.sleep = real_replace, real_sleep
    check("a locked swap leaves the game file exactly as it was",
          open(target, "rb").read() == stock)
    check("a locked swap doesn't leave a .rebuild_tmp behind",
          not os.path.exists(tmp))


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
    test_stream_writeback()
    test_streamed_skin_replacement()
    test_skin_transfer()
    test_file_locks()
    test_validation()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
    if FAIL:
        print("Failures: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
