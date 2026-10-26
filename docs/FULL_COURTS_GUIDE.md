# Full-Court Textures — Findings and Implementation Guide

One image replaces a team's entire court surface: wood, apron, keys, lines,
painted logos and the centre logo. This replaces the current approach, where a
tiled greyscale floor pattern is tinted by Court Colors.

Status: **shipped in v4.0.0** as the Full Courts tab (`modules/full_courts.py`,
`modules/full_courts_tab.py`). §6 below is resolved: one court per logo variant
works without game code changes. The rest of this file is the original
research, kept for reference.

Original status: **proof of concept verified in-game** (Knicks and Bulls, 2026-27 build,
2026-10-26). Working script: [`tools/full_court_poc.py`](../tools/full_court_poc.py) (the original PoC).
References are against public **v3.2.0 (`cb3a78c`)**. Re-check the line numbers
against your local branch, which has the camera and roster changes.

---

## 1. How a court is drawn (what we found)

All of this is in the court file: `sharedassets0.assets` on the 2026-27 build
(`floor_patterns.MAT_FILE`, resolved by `game_layout`).

### Meshes (in `sharedassets1.assets`, placed by the level scenes)

| Mesh | Size / notes |
|---|---|
| `mesh_stadium_floor01` | 12 verts, **±30.48 × ±21.97 m**, covers the whole floor including the apron and the area under the stands. Material slot = `mat_bounce_stadium_courtParquet_v2`, swapped at runtime for the team's `mat_bounce_stadium_parquet_<Team>` |
| `mesh_stadium_courtLines02` | Half court, x 0…14.4, z ±7.7. **The playing court is 28.8 × 15.4 m**, not regulation 28.65 × 15.24 |
| `mesh_stadium_courtLines01`, `…courtLinesExtension` | More line and area geometry; vertex colours pick colour slots |
| `mesh_stadium_teamLogo` | The centre logo. Uses a **single shared scene material** (see §4) |

### Materials, per team (`<Team>` = e.g. `ChicagoBulls`, `NewYorkKnicks`)

| Material | Shader | Role |
|---|---|---|
| `mat_bounce_stadium_parquet_<Team>` | `Shader Graphs/shd_bounce_courtLit_v2` | Floor. `_Court_Lines_ON_OFF` = 0 |
| `mat_bounce_stadium_lines_<Team>` | same | Lines. `_Court_Lines_ON_OFF` = 1 |
| `mat_bounce_stadium_decorations_<Team>` | `shd_bounce_courtDecorations` | **Not** the centre logo on NBA courts (changing it had no effect) |

Relevant `shd_bounce_courtLit_v2` properties (all size-neutral to patch):

| Property | Kind | Use |
|---|---|---|
| `_Texture_Parquet` | TexEnv PPtr | The floor texture. Point it at the full-court texture |
| `_Tiling_Parquet` | Color (r = x, g = y) | **Repeats per metre in world/object XZ** (stock 0.4 → one tile per 2.5 m) |
| `_Offset_Parquet` | Color (r, g) | UV at the court centre. Use 0.5, 0.5 for a centred image |
| `_Color_Parquet` | Color | Multiplies the texture. Set it to white (1, 1, 1) |
| `_Rotate_Parquet` | Float | Set to 0 |
| `_Court_Lines_ON_OFF` | Float | Set to 0 on the **lines** material to hide the game's lines |
| `_ONLY_PARQUET` | Float | Set to **1** on both materials. Removes the apron (`_Color_Outside_Court_R`) and key (`_Color_Area_G`) colour zones the shader paints from its court mask |
| `_OutsideTexture`, `_Outside_Texture_*`, `_GOAT_Textures_ON_OFF` | — | Unused by this approach (untested) |

### Mapping (verified with a grid test image)

```
uv = world_xz * _Tiling_Parquet + _Offset_Parquet
```

- Image **u (left→right)** runs along the court's length; **v (top→bottom)** runs
  across it, with the image top at the **far** sideline. The image is not mirrored.
- Image centre = court centre when the offset is (0.5, 0.5).
- For an image that spans exactly the court lines:
  `tiling = (1/28.8, 1/15.4)`. The black-border calibration lined up with the game's
  sidelines and halfway line.
- For an image that includes an apron, measure the court rectangle inside it
  (`court_px`), then:
  - `m_per_px = 28.8 / court_w_px` (x), `15.4 / court_h_px` (y)
  - `tiling = 1 / (image_w_px * m_per_px_x)`, `1 / (image_h_px * m_per_px_y)`
  - `offset = court_centre_px / image_size_px`
  - Wrap mode is repeat: anything past the image edge tiles. Keep aprons at least
    about 1.6 m (sidelines) and 3.2 m (baselines) wide, or extend the edge colour.
- Measured examples:

| Team | Source | Court rect (px) | Tiling | Offset |
|---|---|---|---|---|
| Knicks | 1932×1010 | x 178–1757, y 89–929 | 0.028377, 0.054000 | 0.5008, 0.5040 |
| Bulls | 1834×920, top 9 px cropped → 1834×911 | x 208–1624, y 76–831 (after crop) | 0.026808, 0.053820 | 0.4995, 0.4978 |

Auto-detecting the court rect worked on both: wood mask (R > 180, G > 140,
100 < B < 200, R − B > 40), first and last row/column ≥ 30 % wood, ± 2 px for the
boundary line.

---

## 2. Texture rules (each one hung the game when broken)

Symptom of a bad texture: the game **hangs at launch** (needs a force close), and
`%USERPROFILE%\AppData\LocalLow\UnfinishedPixel\NBA Bounce\Player.log` contains
`async texture load: failed to load <TextureName> from …resS`.

| Rule | Detail |
|---|---|
| **Full mip chain down to 1×1** | A chain that stopped at 4×4 failed even though the byte count matched Unity's expectation. 2048×1024 → **12 mips**, 1,398,120 bytes DXT1 |
| **Power-of-two dimensions** | 1932×1010 failed. Resize to **2048×1024** (tiling is in metres, so resizing doesn't move anything) |
| **16-byte-aligned stream offset** | Pad the `.resS` before appending. Not proven to be required on its own (the mip fix was the decisive one), but every working texture is aligned, and it costs nothing |
| Format | DXT1 (10) for the court; DXT5 (12) when alpha is needed (logos) |

Header fields changed in place, all fixed-size ints, so the object stays the same
size: `m_Width`, `m_Height`, `m_TextureFormat`, `m_MipCount`,
`m_CompleteImageSize`, `m_StreamData.offset`, `m_StreamData.size`.

---

## 3. Writing it (what the PoC does)

1. **Slot texture.** Re-describe an unused `FLOORSLOT_n` Texture2D (clone of the
   stock floor, see `floor_patterns.create_slots`) as 2048×1024 DXT1, 12 mips.
   Append pixels to `sharedassets0.assets.resS` (aligned) and point
   `m_StreamData` at them. This is the same technique as
   `floor_patterns._write_slot_pixels`.
2. **Materials.** In `parquet_<Team>` **and** `lines_<Team>`, patch
   `_Texture_Parquet` → slot path_id (fileID 0), plus the tiling, offset, white
   tint, rotation 0, `_ONLY_PARQUET` = 1, and `_Court_Lines_ON_OFF` = 0. Both
   materials get the same texture and tiling, so line meshes render as the same
   image and vanish into it.
3. Each object is re-serialized with `write_typetree` and spliced in place only if
   it is the same byte length (`patch_object`).

The PoC uses `FLOORSLOT_8`, so only **one team at a time**. Its `--restore` copies
`*.fullcourt_backup` back.

---

## 4. The centre logo

- Drawn by `mesh_stadium_teamLogo` with **one scene material shared by all teams**
  (`mat_bounce_stadium_decorationLogo`, level1, stored with `AHawks_Global`). At
  match load the game copies the team's **currently selected logo** texture into
  its `_Decoration_Texture`.
- "Currently selected" = the save's per-team `iCurrentLogoIndex`, which picks one
  of the logo sprites in the team's `BasketTeamContainer` logo run. For the Bulls:
  `CBulls_Global`, `CHIBulls_Unlock_01/02/03`, `txt_classicLogos_chicago001`.
  `CHIBulls_Unlock_01` looks the same as `CBulls_Global`, and the user's Bulls were
  on it. Blanking `CBulls_Global` alone changed nothing, while blanking both worked.
- The same selected logo also drives the **big team-select panel** and the
  **scoreboard logo**. The small grid tile uses `<X>_Global_Icon` and is
  unaffected.
- What did **not** work: `_Alpha_Clipping` on `decorations_<Team>` (alpha clip
  is compiled out, `m_ValidKeywords` is empty), and a zero `_Decoration_Texture`
  scale on `decorations_<Team>` (wrong material).
- What worked: making the selected logo's Texture2D fully transparent (DXT5, full
  mips, streamed, header patched in place). The centre logo disappeared.

**Agreed default: "Replace selected logo".** Write a user-supplied logo (or one
cut from the court image's centre circle) into the texture(s) of the team's logo
run, so the centre logo and menus match and nothing goes blank. Per-team options:
*Hide* (transparent; panel and scoreboard go blank) and *Keep game logo*.
Simplest robust approach: apply to **every** sprite texture in the team's logo
run, because which index is selected is a save-file setting. Alternatively, read
`iCurrentLogoIndex` through `save_manager` and apply only that one, with a warning
that changing the logo in-game changes the centre.

---

## 5. Agreed design

| Decision | Choice |
|---|---|
| Placement | **New "Full Courts" tab** plus a Home card. Teams with a full court have Floor Patterns and Court Colors disabled |
| Input | **Template + any image.** Export a 2048×1024 template (court rect and recommended apron marked, with the 28.8 × 15.4 m geometry). Also accept any image, auto-detect the court rect, and let the user adjust the edges in a preview |
| Centre logo | **Replace selected logo** by default; Hide and Keep as options |
| Slots | **Open question:** one court per *logo variant* (see §6). Otherwise one slot per team, created on demand |

---

## 6. One court per logo variant (resolved)

Decompiled `CourtManager.setTeam()` (Assembly-CSharp, 2026-27 build):

```csharp
bool flag = team.getExtraLogoTextures().Any(r => r.m_sUnlockID == team.getLogo().m_sUnlockID);
string sColorID = flag ? team.getLogo().m_sUnlockID : "default";
...
foreach part: foreach MaterialProfile p in m_aoCourtMaterialColors[part]:
    if (p.m_aoRetroExtraColorParams.TryGetValue(sColorID, out _))
        material.SetColor(p.m_sMaterialColorParam, p.m_aoRetroExtraColorParams[sColorID]);
```

The parameter name is data, and `_Tiling_Parquet` / `_Offset_Parquet` are
colour properties. Full Courts therefore:

- builds an **atlas** per team (one 2048×1024 court per row, row count a power
  of two, at most 8),
- adds a `_Tiling_Parquet` and an `_Offset_Parquet` profile to the Parquet (1)
  and Lines (2) parts of `m_aoCourtMaterialColors`, with a value for every
  variant key, so each variant selects its own row,
- `offset.y = (oy + rows − 1 − row) / rows`, `tiling.y /= rows`, where
  `oy = 1 − centre_row / height` (the texture's v runs bottom-up).

Container serialization (SerializableDictionary → list of pairs):
`int32 nPairs; { int32 part; int32 nProfiles; { string param; int32 n;
{ string key; float4 value } } }`, strings being int32 length + bytes padded to 4.

Limits: variants are the game's colour IDs, so alternate logos without
`ExtraLogosTextureReferences` share the `default` court. Past the edge of its row,
a court shows the neighbouring row instead of repeating itself, so keep aprons
painted to the image edge.

Verified in game (2026-10-26): Bulls home game shows the default-row picture
with the game's lines hidden, and the scoreboard and team-select panel show the
replaced logo. The retro row has been checked by readback but not yet seen in game.

## 7. Implementation plan for the app

1. **`modules/full_courts.py`** (new), modelled on `floor_patterns.py`:
   - `discover()`: team list from `parquet_<Team>` materials; logo runs from the
     containers (reuse `team_manager._team_art` / `_object_catalog`).
   - Slot creation: add `FULLCOURT_<n>` Texture2D objects (clone of the stock
     floor, re-described 2048×1024 DXT1, 12 mips) in one verified rebuild
     (`team_manager._rebuild`, as in `create_slots`). Allocate one per assigned
     team (or per variant, depending on §6).
   - `encode_court(img)`: resize to 2048×1024, full DXT1 mip chain to 1×1.
   - `apply(assignments, ress_reset)`: write pixels (aligned) and patch both
     materials as in §3. Patch the logo textures per §4.
   - `revert(team)`: restore the material bytes from the backup (texture PPtr,
     tiling, offset, tint, `_ONLY_PARQUET`, `_Court_Lines_ON_OFF`) and the logo
     textures.
   - State in `user/full_courts.json`: team → {image copy path, court rect,
     tiling, offset, logo mode, logo image}.
2. **`reapply_full_courts(game_path, ress_reset)`**, called from app.py's Apply
   Mods next to `floor_patterns.reapply_slot_pixels`. Apply Mods rewinds the
   `.resS` to its backup, so the court and logo pixels must be re-appended on
   every run.
3. **UI (`full_courts_tab.py`):** team list, image import (template or any),
   preview with the court rect overlay and draggable edges, an "Export template"
   button, a logo mode radio, and Apply/Revert. Grey out the team in Floor
   Patterns and Court Colors when it has a full court.
4. **Restore Game Files** already rolls back `.assets`/`.resS`. Make sure the tab
   then shows "not applied", as Floor Patterns does.
5. **Validation before writing:** power-of-two, mips to 1×1, aligned offset,
   same-size object patches. After writing, read back with UnityPy (dims, format,
   offset % 16, pixel extrema).
6. **Self-test (`--selftest`), like floor_patterns:** discover, encode, and
   check the expected sizes (2048×1024 DXT1 = 1,398,120 bytes; 1024² DXT5 =
   1,398,128 bytes).

### Related fix in existing code

`floor_patterns._write_slot_pixels` appends at the current `.resS` size without
padding. One pattern is 174,776 bytes (≡ 8 mod 16), so a second simultaneous
pattern lands misaligned. Pad to 16 bytes before appending, as the PoC does.

---

## 8. Testing approach that worked

- Back up `sharedassets0.assets(.resS)` before every write; each run starts from
  that backup so changes never stack up.
- **Automated boot test** (no screenshots needed for load errors): launch with
  `steam://rungameid/3115130`, wait for `Player.log` to update, wait about 25 s,
  grep for `fail|error|exception`, then `taskkill /IM "NBA BOUNCE.exe"`.
- Visual checks need a match. Start a **home game for the team being tested**
  (one-slot PoC: other teams show their stock court). Use the default camera.
- Grid image (coloured quadrants, labelled corners, 10 % grid, black border at the
  court lines) to calibrate orientation and scale: `--save-grid out.png`.

---

## 9. PoC script reference

```
python tools/full_court_poc.py --team BostonCeltics                       # grid test
python tools/full_court_poc.py --team NewYorkKnicks --image courts/knicks_source.webp \
    --tiling 0.028377 0.054000 --offset 0.5008 0.5040 --hide-lines
python tools/full_court_poc.py --team ChicagoBulls --image courts/bulls_court.png \
    --tiling 0.026808 0.053820 --offset 0.4995 0.4978 --hide-lines \
    --blank-logo CBulls_Global CHIBulls_Unlock_01
python tools/full_court_poc.py --restore
```

`--hide-lines` also sets `_ONLY_PARQUET` and a zero decoration scale on
`decorations_<Team>`. That second change is harmless but ineffective; drop it in
the real implementation.

**Run `--restore` before using the app's Apply Mods.** The PoC isn't registered
with the app, and Apply Mods rewinds the `.resS`, which would leave the slot
pointing past the end of the file.
