# Changelog

## v2.2.0

### What's New

- **New Meshes tab: view, export and replace the game's 3D models.**

Every `Mesh` in the game -- players, the ball, the hoop, the arena, the court --
is now browsable, viewable in 3D, exportable, and replaceable, the same way
textures and audio already were.

### Added

- **3D viewport.** Orbit (drag), pan (right-drag), zoom (wheel), re-frame
  (double-click), with shaded / textured / wireframe / shaded+wire / points /
  normals / submesh display modes, a ground grid, an axis gizmo, a turntable
  spin toggle and a render snapshot button. It's a software renderer built on
  PIL -- vertices are transformed in Python (numpy-accelerated when numpy is
  installed, plain lists when it isn't), triangles are depth-sorted and painted
  back to front -- so it needs no GPU stack and no new hard dependency. Dense
  meshes drop to a point cloud while you drag and return to full quality when
  you let go.

- **Export to OBJ, GLB, PLY and STL.** OBJ is the round-trip format: UVs,
  normals and one group per submesh, so an export re-imports onto the same
  material slots. GLB is single-file glTF 2.0 that opens directly in Blender and
  Windows 3D Viewer. Coordinates are converted from Unity's left-handed axes on
  the way out and converted back on the way in, so a round trip is lossless.
  "Export List" dumps every mesh currently in the list at once.

- **Import replacements from OBJ, PLY or STL.** The import dialog compares the
  model against the original and against the slot's budget before anything is
  queued, and offers a one-click scale factor to match the original's size plus
  optional re-centring and normal recalculation.

- **Skinned meshes keep animating.** Player and mascot meshes carry per-vertex
  bone indices and weights that a model made in Blender won't have; each new
  vertex borrows them from the closest original vertex, so the replacement still
  follows the skeleton.

- `tools_mesh_selftest.py`, 68 checks that need no copy of the game: every
  exporter/importer round-trip, the vertex buffer this app writes decoded back
  by UnityPy's own mesh reader over a full Unity 6 channel layout (packed
  colours and bone weights included), index padding, submesh windows, the
  .resS append/rewind bookkeeping against real files on disk, and a full
  round trip for a streamed skinned mesh -- original vertex data read out
  of a .resS, an unrigged replacement written back, decoded again, then
  reverted from the backup.

- `MESH_MODDING_GUIDE.md` with the modelling rules, the budget explanation and a
  troubleshooting table.

### How mesh replacement is written

Same in-place binary patch as everything else in this app. A mesh's vertex data
is streamed from the companion `.resS` file, so new vertex data is appended
there and only an offset changes -- the vertex count of a replacement is
unlimited. The triangle index buffer, though, is stored inside the object at a
fixed size, so it is reused at its original byte length and zero-padded past the
end of the last submesh window (padding no submesh indexes, so the GPU never
sees it). The rewritten object therefore serializes to exactly the same number
of bytes as the one it replaces and splices in place, which caps a replacement
at the original's triangle count and is reported as a budget in the UI before
you import.

An opt-in **rebuild fallback** lifts that cap by re-serializing the whole
`.assets` file. Since this README has warned since v1 that UnityPy's full
re-serialization can drop data, the rebuilt file is verified object-by-object
against the original -- every other object present, and the same size -- before
it is allowed to replace anything; if the check fails, the write is abandoned
and the game file is left untouched.

### Changed

- **"Apply Texture Mods" is now "Apply Mods", and applies meshes too.** Textures
  and meshes in the same `.assets` file share one `.resS` companion, and both
  append their bulk data to it after rewinding it to its backup once per run.
  Applying one kind on its own would rewind that file out from under the other
  kind's offsets, leaving those objects pointing past the end of the file. They
  now always go together, from one pipeline with one shared rewind set.
  "Restore Textures" is likewise now "Restore Game Files".

- `revert_object_from_backup()` locates the object in the backup file rather
  than assuming the backup and the live file share a layout. Every texture patch
  is size-neutral so the two normally do agree, but the mesh rebuild fallback
  can shift every object after the one it grew, and reverting a texture mod
  afterwards would have spliced in the wrong object's bytes.

## v2.1.1

### What's New

- Fixed issues with cropped custom logo imports
- Fixed court floor patterns not reaching the game
- Fixed the court preview showing the same pattern for every court
- Removed the exported stock court floor, which displayed without local game
  files present

Two bugs that both showed up the same way: a replacement texture coming out cut
off, cropped, or garbled in-game even though the PNG looked right in the app.

### Fixed

- **Replacement logos were cut off by the old logo's sprite crop.** Every logo
  texture in the game is drawn through a Unity Sprite imported with Mesh Type =
  Tight, so a crop box and a polygon hull of the *original* artwork's opaque
  pixels are baked into the sprite at build time. `GSWarriors_Global`, for
  instance, crops to 514×619 at (259, 193) inside its 1024×1024 canvas, and UGUI
  only ever samples inside that box. Replacing the texture pixels left the crop
  describing the logo that used to be there, so a replacement covering more of
  the canvas than its predecessor — a wide wordmark over a round primary logo —
  was silently clipped, even though it lined up perfectly in Photoshop over the
  exported original. This affected 341 of 350 sprites in `sharedassets1.assets`,
  255 of 326 in `sharedassets2.assets` and 57 of 62 in `resources.assets`;
  jerseys, court decals and arena signage have no sprite and were never hit.

  Apply now widens the sprite to the whole canvas: `textureRect` to the full
  texture, `textureRectOffset` to zero, the Tight mesh-type bit cleared, and the
  baked hull rewritten as a single full-rect quad. Both buffers are refilled at
  their existing length, so the Sprite reserializes to the same byte size and
  splices in place like every other patch this app makes.

- **Applying more than one texture mod to the same file corrupted all but the
  last.** Every Texture2D in this game is streamed, so `apply_single_mod()`
  appends the new pixels to the companion `.resS` and repoints
  `m_StreamData.offset`. The `.resS` was being rewound to its backup once *per
  mod* rather than once per Apply run, which discarded the bytes of every mod
  already written to that file and left those textures pointing at a stale
  offset, often past the new end of file. Reproduced with two 1024×1024 mods in
  `sharedassets1.assets`: both objects ended up at offset 122,221,152 and both
  rendered the second mod's image. Since a full team reskin touches ~17 textures
  across two files, this hit essentially every multi-texture mod.

- **The Preview showed the last-clicked library pattern for every court, so one
  assignment looked like it had changed all 36.** Selecting a court re-rendered
  whatever pattern was selected in the library, tinted for that court, and
  captioned it with the court's name — so clicking down the court list showed the
  same pattern on every one of them, while the list itself still (correctly) read
  "Stock parquet". Selecting a court now previews *that court's* floor and
  captions it `GoldenStateWarriors floor: Basketweave`; auditioning a library
  pattern captions it `Basketweave -- previewing on DenverNuggets, not assigned to
  it yet`. Nothing about what gets written changed: `apply()` only ever patched
  the materials of teams in the assignment map, which the game files confirm — of
  72 court material regions, 70 still point at the untouched stock texture.

- **"Apply to game" could be pushed off the bottom of the Floor Patterns window,
  so assignments were never written.** The right-hand panel packed top-down, and
  the notes above the buttons wrap to a variable number of lines -- the
  retro-court note runs four lines for a team like the Warriors. On a display
  where that overflows the window, tkinter simply doesn't draw the last two
  buttons, and the visible panel ends at "Import custom PNG ...". Assigning a
  pattern still updated the court list and the `1 / 8` slot counter, so it looked
  applied, while `floor_patterns.json` stayed empty and no journal was ever
  written -- and the game of course loaded the stock floor. The action buttons are
  now packed `side="bottom"` and claim their space *before* the notes, so the
  notes are what clip on a short window, never the buttons. Assigning also says
  "Not written yet -- click 'Apply to game'" in the status line, and closing with
  unapplied assignments now offers to apply them. The
  exported `stock_parquet.png` used to ship with the app, so the Floor Patterns
  gallery offered "Stock Parquet (game original)" even with no game files
  present — and it was game art being redistributed. It is no longer tracked;
  instead `extract_stock_pattern()` decodes `txt_bounce_parquet01_D` out of
  `sharedassets1.assets` (the pristine backup when one exists) and caches it on
  first open, in ~0.1s. It's deliberately standalone, so the stock floor is
  offered even when the donor probe can't complete — previously extraction ran
  only at the *end* of a successful `discover()`, so any earlier failure, such as
  `resources.assets not reachable`, silently cost you the template. Matches on
  path ID 1760 and falls back to the texture name if a game patch renumbers
  objects. With no game folder set, the entry is omitted rather than faked, and
  asking for it explains why. Verified pixel-identical to the PNG that used to
  ship, and it still encodes to exactly 174,776 bytes.

- **The `I:\SteamLibrary\...` developer fallback was still in the code.** v2.1.0
  claimed it was gone; in fact `slider_tab.py` and `build_slider_assets.py` both
  still fell back to it when no path was given. They now exit with a usage
  message instead.

### Added

- **Sprite crop is visible before you author anything.** Selecting a texture
  outlines its sprite's crop box on the **Original** preview with a dashed
  rectangle and states it in the info line, so it's obvious up front when the old
  logo only used part of its canvas. Apply reports how many sprites it widened.
- **Multi-sprite atlases are detected on import.** Where several sprites share
  one texture — `txt_assets_decorationAnimationGOAT_D` carries five — each
  `textureRect` is a real sub-region, so widening one would move the others'
  artwork. Those are left alone and the regions the replacement must stay inside
  are listed instead.
- **Remove Mod now reverts the game file.** The texture and its sprite go back to
  stock byte-for-byte from the backup, without disturbing any other mod in the
  same file. Previously the texture kept pointing at appended `.resS` bytes that
  the next Apply rewound away.
- New `sprite_crop.py`, self-contained — it imports nothing from `app.py`. The
  mechanism, the survey numbers and the serialization layout are written up in
  the Team Identity Texture Guide.

## v2.1.0

### Added

**Saves tool** — a new `💾 Saves` tab that unlocks the cosmetics in your save
file.

- The unlockable list is extracted from the game's `level1` rather than
  hardcoded, and cached to `unlockables_catalog.json` keyed on that file's size
  and modification time, so a game patch invalidates it automatically.
- Save discovery tries the documented LocalLow path, then a case-insensitive
  match, then scans LocalLow for any folder holding an `NBABounce*.sav` — which
  covers installs where the publisher folder has been renamed. Saves list
  newest-first and **Browse** opens in the save folder.
- The save is backed up first, outside the Steam Cloud folder so it doesn't
  consume cloud quota.
- Four gates run on every apply, any failure aborting the write: the input must
  round-trip byte-identically before editing; every added ID must have
  `m_bEnabled == 1`; nothing already owned may be dropped; the output must have
  no duplicate object IDs and no dangling references. Writes go to a temp file
  and move into place with `os.replace`, so an abort leaves the original
  untouched. Applying twice is a no-op.
- Runtime-generated `RANDOM_*` cosmetics are preserved verbatim even though they
  aren't in the static catalog.
- Reads `level1` and writes the `.sav` only — it never opens a `.assets` file.

**Retro court eras** — 48 throwback courts across 28 teams identified and named
after the real NBA era they represent, derived from the court colours the game
bakes into each variant. Ships as `retro_court_eras.json`; labels are free text
and safe to edit.

**Per-era colour editing** — Court Colors gained a **Court variant** dropdown,
replacing the old "Also apply to retro / throwback courts" checkbox. A single
throwback court can now be recoloured on its own; *All courts* reproduces the
previous behaviour.

**Centre-court logo preview** — selecting a court variant loads that era's real
logo decal and composites it over the era's own court colour.

**Court palette reference** in Floor Patterns, showing each era's court colours
beside the pattern preview.

**`tools_build_retro_eras.py`** — regenerates the era mapping by scanning the
installed game.

### Fixed

- **Floor pattern previews rendered black or flat red.** The preview tinted the
  wood with the container's court-surface colour (`#1A1A1A` on several teams)
  instead of the material's `_Color_Parquet` wood tone. Mean preview luminance
  went from 20/255 to 136/255.
- **Three centre-court decals failed to load** because their names were wrong:
  the game misspells Indiana's as `logoRetro_4_team9` rather than
  `retroLogo_4_team_9`; Seattle's token is `SeattleSonics`, not
  `SeattleSuperSonics`; and legacy teams' `LOGO_1` has no separate decal, reusing
  the base court texture. All 70 decal names are now verified against the game's
  texture table.
- **Unreadable text in light mode** on the Saves tab. `accent` is the same navy
  in both themes while `text` and `gold` flip to near-black, so the warning
  banner was painting at a 1.61:1 contrast ratio and the status bar at 1.92:1.
  Foreground colours are now chosen by measuring WCAG contrast against the
  surface they sit on; everything clears AA (4.5:1) in both themes.
- **`read_array` misparsed repeated strings.** BinaryFormatter emits duplicates
  as `0x09 MemberReference` records rather than inline — and 100% of
  `m_aoUnlocksToBeSeen` entries are references, so the old code returned garbage
  there.
- **Hardcoded developer paths.** `slider_tab.py` and `build_slider_assets.py`
  shipped with an `I:\SteamLibrary\...` fallback. Both now read `config.json` or
  take a path argument, and exit with a clear message otherwise.
- **The era mapping couldn't be rebuilt.** `tools_build_retro_eras.py` depended
  on a dump file that no longer shipped; it now scans the game directly through
  the app's own container parser.

### Changed

- The Floor Patterns slot counter reads **Pattern slots used: N / 8** and
  explains that a slot holds one *distinct pattern*, not one court — courts
  sharing a pattern share a slot, and courts on the stock floor use none, so all
  40 courts can be reskinned with 8 or fewer patterns.
- `floor_patterns/` is no longer tracked in git. It is a generated cache, and
  `stock_parquet.png` inside it is extracted from the user's copy of the game.
- `config.json` and `floor_patterns.json` are untracked; they hold per-user
  state.

### Known limitations

- Floor patterns cannot be set per era. A team's wood texture and tint both live
  on a single per-team material shared by all of its courts; only court *colours*
  are stored per era.
- Retro slot numbers are not chronological — for the Hawks and Cavaliers `LOGO_4`
  is the newer era, for the Pistons it's the older one.
- 15 of the 48 era labels are marked `low` confidence: those courts reuse the
  modern palette, so the era is inferred from logo history rather than evidenced
  by the game's own colours.

---

## v2.0.0

Initial public release: Textures, Audio, Floor Patterns, Gameplay Sliders and
Court Colors tools.
