# Adding a 33rd team — proof of concept

The feasibility study's recommended next step, built: clone the Trail Blazers
under a new ID, name and colour, hang a 33rd pointer off the team manager, and
see what the game does with it.

Two developer scripts, both under `tools/` (nothing in the app runs them):

| Script | What it does |
| --- | --- |
| `team_slot_poc.py` | inspects, applies and reverts the change |
| `team_slot_selftest.py` | checks the byte-level logic without the game installed |

## Run it

```
python tools/team_slot_selftest.py      # sanity check, no game needed
python tools/team_slot_poc.py           # inspect only — writes nothing
python tools/team_slot_poc.py --apply   # do it
python tools/team_slot_poc.py --verify  # read the game files back — is it there?
python tools/team_slot_poc.py --revert  # put the backups back
```

Close NBA Bounce and quit Steam first — Windows won't let the rebuilt files be
swapped in while anything holds them open.

Inspect mode prints everything an apply would rely on: the source container, the
unique-ID field and the ID it read for every team, the name strings it would
rewrite, the colour overrides it would change, and the pointer array it would
grow. **Read that output before applying.** If any of it looks wrong, the apply
would be wrong too.

Useful flags: `--source` (which team to clone), `--name`, `--color RRGGBB`,
`--new-id`, `--game-data`.

## Nothing changed in game?

Run `--verify`. It reads the game files back and reports which half of the
change is on disk, so "no difference in game" splits into causes that look
different:

- **Neither half there** — the apply never ran, ran against a different folder,
  or the files were restored afterwards. Steam ▸ Properties ▸ Installed Files ▸
  *Verify integrity* undoes this mod completely, and so does a game update.
- **Clone there, no pointer** — the `level1` half didn't land; the game never
  sees the team.
- **Both there but the grid is stock** — that's a finding about the game, not a
  mistake: it's reading the team list from somewhere this script doesn't touch.

The team-select grid draws the bundled teams and the four classic teams in one
run, so a 33rd bundled team pushes Nets/Bobcats/Sonics/Grizzlies along by one
tile. If that row hasn't moved, the change isn't live — no need to hunt for a
new tile.

## What it changes

- **sharedassets1.assets** gains one new `MonoBehaviour`: a copy of the
  Blazers' `BasketTeamContainer` with a new unique ID (-101 by default), new
  name fields, and every `_Color_*` override set to the chosen colour.
- **level1** — the `BasketTeamManager`'s bundled-team array goes from 32 entries
  to 33, the new one pointing at the clone.

Both files are re-serialised by UnityPy rather than byte-patched, because both
grow. Both are backed up to `.teamslot_backup` first, and the rebuilt file is
read back and compared against the original object table before it is allowed
near the game folder: every object that was there must still be there at its
original size, or the rebuild is thrown away and the game file is left alone.

Every edit inside the container is length-preserving — a colour is 16 fixed
bytes, the ID is one int32, and a rename is written into the old string's
footprint (space-padded, and refused outright if the new name is longer). So the
clone serialises to exactly the same size as the container it came from, and no
offset inside it can rot.

## What it deliberately doesn't do

- **The logo artwork is still Portland's.** `--color` changes the team's colour
  table — the RGBA values the game reapplies to the court and the team's tint on
  every load. A genuinely new logo needs new `Texture2D` and `Sprite` objects
  plus ResourceManager name entries, which is the next piece of work.
- **Only the leading name fields are renamed.** Strings further into the
  container are resource names that jerseys and court textures are loaded by, so
  rewriting one would leave the new team with no art at all.
- **Nothing guards your save.** This is the study's biggest risk: the game looks
  up every saved team ID with no missing-key check, so a save written while the
  custom team exists may fail to load once the mod is gone. Back the save up
  yourself, and undo with `--revert` rather than deleting files by hand.
- **It isn't in the app.** Rebuilding `sharedassets1.assets` shifts byte offsets,
  which invalidates the offsets existing texture and audio patches rely on. Apply
  this first and re-apply other mods on top, not the other way round.

## What to look for in game

1. Does the game load at all — i.e. does an added object work?
2. Is there a 33rd tile on the team-select screen, and does the grid still
   navigate sanely with a controller? (`StandardMatrix.CreateMatrix` wires
   wrap-around to fixed tile indices, so an extra row is the open question.)
3. Does a Quick Match with the new team play through to the final buzzer?

If the select grid misbehaves, the fallback from the study still stands:
overwrite the East/West Special slots instead, which needs no new objects.
