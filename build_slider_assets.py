"""
build_slider_assets.py -- generate sliders_catalog.json and presets.json from
the SHIPPED game data, so every slider endpoint is anchored to real values
rather than invented ones.

Anchoring scheme (this is the important idea):
    UI 25 == the game's Rookie difficulty
    UI 50 == the game's Advanced difficulty
    UI 75 == the game's Pro difficulty       (its hardest in-game option)
Values are piecewise-linear through those three anchors and linearly
extrapolated outside them, so the "Pro" PRESET lands EXACTLY on the game's
ADVANCED difficulty, and Hall of Fame extrapolates past the game's Pro.

Naming note: internally the game calls these skill sets Beginner / Advanced /
Legend, but the English menu shows Rookie / Advanced / Pro. All user-facing text
uses the menu names. The app's 2K-style preset names overlap with them without
meaning the same thing -- see DIFFICULTY_EXPLAINER in slider_tab.py.
Sliders with no per-tier variation get hand-specified endpoints instead.

2K convention is preserved throughout: a slider is "how much of this thing for
this side", higher = more. Difficulty presets then push CPU sliders up and User
sliders down.

Run:  python build_slider_assets.py "<...>\\NBA Bounce_Data"
"""
import json
import os
import sys

from slider_manager import SliderManager

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- CPU sliders
# (id, label, category, [field paths], clamp)  -- endpoints auto-derived
CPU = [
    ("cpu_3pt_frequency", "CPU 3PT Frequency", "Shooting",
     ["m_fShoot3PointProbability"], [0.0, 3.0]),
    ("cpu_shot_frequency", "CPU Shot Frequency", "Shooting",
     ["m_v2ShootCooldown"], [0.0, 60.0]),
    ("cpu_dunk_frequency", "CPU Dunk Frequency", "Shooting",
     ["m_fDunkProvability", "m_v2DunkTeamCooldown"], [0.0, 60.0]),
    ("cpu_alleyoop_frequency", "CPU Alley-Oop Frequency", "Shooting",
     ["m_fAlleyOopProvability", "m_v2AlleyOopTeamCooldown"], [0.0, 60.0]),
    ("cpu_shot_pass", "CPU Pass-Out-Of-Shot", "Passing",
     ["m_fShotPassProvability", "m_v2ShotPassTeamCooldown"], [0.0, 60.0]),
    ("cpu_sprint_with_ball", "CPU Sprint With Ball", "Movement",
     ["m_fSprintWithBallProbability"], [0.0, 1.0]),
    ("cpu_steal_success", "CPU Steal Success", "Defense",
     ["m_fStealSuccessProbability"], [0.0, 1.0]),
    ("cpu_steal_frequency", "CPU Steal Frequency", "Defense",
     ["m_v2StealCooldown", "m_v2RushToStealCooldown"], [0.0, 60.0]),
    ("cpu_steal_takeball", "CPU Take-Ball On Steal", "Defense",
     ["m_fStealTakeBallProbability"], [0.0, 1.0]),
    ("cpu_block_shot", "CPU Shot Block", "Defense",
     ["m_fJumpBlockShotProbability", "m_v2JumpBlockCooldown"], [0.0, 60.0]),
    ("cpu_block_dunk", "CPU Dunk Block", "Defense",
     ["m_fJumpBlockDunkProbability", "m_v2JumpBlockDunkCooldown"], [0.0, 60.0]),
    ("cpu_push_success", "CPU Push Success", "Defense",
     ["m_fPushSucessProbability", "m_v2PushCooldown"], [0.0, 60.0]),
    ("cpu_smack_frequency", "CPU Smack Frequency", "Defense",
     ["m_fSmackProbability", "m_v2SmackCooldown"], [0.0, 60.0]),
    ("cpu_intercept_pass", "CPU Pass Interception", "Defense",
     ["m_fInterceptPassProbability", "m_v2InterceptPassCooldown"], [0.0, 60.0]),
    ("cpu_rebound", "CPU Rebounding", "Rebounding",
     ["m_fReboundProbability", "m_v2ReboundCooldown", "m_fReboundMaximumOffset"], [0.0, 60.0]),
    ("cpu_rebound_pass", "CPU Outlet Pass After Rebound", "Rebounding",
     ["m_fPassOnReboundProvabiliy", "m_v2PassOnReboundCooldown"], [0.0, 60.0]),
    ("cpu_defensive_pressure", "CPU Defensive Pressure", "Defense",
     ["m_fIntensityPerSecond", "m_fIntensityPerSecondToBallOwner"], [0.0, 200.0]),
    ("cpu_cover_tightness", "CPU Coverage Tightness", "Defense",
     ["m_fCoverMaximumClosestDistanceOffset", "m_fCoverMaximumTargetAngleOffset",
      "m_v2CoverTargetUpdateTime"], [0.0, 90.0]),
    ("cpu_reaction_time", "CPU Reaction Speed", "Awareness",
     ["m_v2GoToBallSprintDelay", "m_fCoverTimeToStartSprintingDelay",
      "m_fCoverTimeToUpdateOpponentVelocity", "m_v2JumpBallOffsetFromBestTime"], [-2.0, 60.0]),
]

# ------------------------------------------------------------- USER sliders
# Applied to m_oHumanDifficulty. Endpoints are the human stock value scaled.
# (id, label, category, [paths], at0_mult, at100_mult, clamp)
USER = [
    ("user_steal_success", "User Steal Success", "Defense",
     ["m_fStealSuccessProbability"], 0.3, 1.8, [0.0, 1.0]),
    ("user_steal_takeball", "User Take-Ball On Steal", "Defense",
     ["m_fStealTakeBallProbability"], 0.3, 1.8, [0.0, 1.0]),
    ("user_steal_frequency", "User Steal Frequency", "Defense",
     ["m_v2StealCooldown", "m_v2RushToStealCooldown"], 2.0, 0.25, [0.0, 60.0]),
    ("user_block_frequency", "User Block Frequency", "Defense",
     ["m_v2JumpBlockCooldown"], 2.0, 0.25, [0.0, 60.0]),
    ("user_push_frequency", "User Push Frequency", "Defense",
     ["m_v2PushCooldown"], 2.0, 0.25, [0.0, 60.0]),
    ("user_intercept_frequency", "User Pass Interception", "Defense",
     ["m_v2InterceptPassCooldown"], 2.0, 0.25, [0.0, 60.0]),
    ("user_rebound_frequency", "User Rebound Frequency", "Rebounding",
     ["m_v2ReboundCooldown"], 2.0, 0.25, [0.0, 60.0]),
    ("user_sprint_with_ball", "User Sprint With Ball", "Movement",
     ["m_fSprintWithBallProbability"], 0.5, 1.2, [0.0, 1.0]),
]

# ------------------------------------------------------------ SHARED sliders
SHARED = [
    ("player_speed", "Player Speed", "Movement", "playerstats",
     ["m_fMaxSpeed", "m_fMaxSpeedSprint", "m_fMaxSpeedBall", "m_fMaxSpeedBallSprint",
      "m_fMaxSpeedContact", "m_fMaxSpeedSprintContact", "m_fMaxSpeedBallContact",
      "m_fMaxSpeedBallSprintContact"], 0.7, 1.3, [0.5, 20.0]),
    ("player_acceleration", "Player Acceleration", "Movement", "playerstats",
     ["m_fAcceleration", "m_fAccelerationSprint"], 0.5, 1.5, [1.0, 200.0]),
    ("pass_speed", "Pass Speed", "Passing", "playerstats",
     ["m_fPassSpeed", "m_fPassSpeedAllieOop"], 0.6, 1.5, [1.0, 60.0]),
    ("input_memory", "Input Buffer Window", "Controls", "playerstats",
     ["m_fInputMemoryTime"], 0.4, 2.0, [0.05, 2.0]),
]

DESCRIPTIONS = {
    # ---- CPU: shooting ----------------------------------------------------
    "cpu_3pt_frequency":
        "How willing the CPU is to take a three. Rolled through the team's shot "
        "decision check (TeamAI) every time it looks for a shot. This is a raw "
        "weight, not a percentage \u2014 the game's own Advanced difficulty sits "
        "at 2.5.",
    "cpu_shot_frequency":
        "Minimum and maximum delay before a CPU player will look for another "
        "shot. Higher = shorter cooldown = the CPU shoots more often. On the "
        "game's hardest difficulty (Pro) there is no cooldown at all.",
    "cpu_shooting_accuracy":
        "Where the CPU aims on its own shot bar, for each shot distance. Blends "
        "the full 7x7 release-profile matrix through the game's own Rookie, "
        "Advanced and Pro release profiles. This is the single biggest lever "
        "on CPU shooting percentage.",
    "cpu_dunk_frequency":
        "Chance the CPU commits to a dunk when one is available, plus the "
        "team-wide cooldown before the next dunk attempt. Read by "
        "TeamAI.updateDunk.",
    "cpu_alleyoop_frequency":
        "Chance the CPU goes for an alley-oop, plus the team cooldown between "
        "attempts. Read by TeamAI.updateAlleyOop. Even on the game's hardest "
        "difficulty (Pro) the cooldown is 30-40 seconds, so these stay rare.",
    # ---- CPU: passing -----------------------------------------------------
    "cpu_shot_pass":
        "Chance the CPU bails out of a shot into a pass instead, plus the "
        "cooldown between such passes. Read by TeamAI.updateShotPass. Higher = "
        "the CPU gives up contested shots more readily.",
    # ---- CPU: defense -----------------------------------------------------
    "cpu_steal_success":
        "Chance a CPU steal attempt succeeds once it is in range. Read by "
        "PlayerSteal.getSuccessRate \u2014 the same code path your own steals use.",
    "cpu_steal_frequency":
        "Cooldowns before the CPU may try another steal, and before it may rush "
        "at the ball handler to force one. Higher = shorter waits = constant "
        "ball pressure.",
    "cpu_steal_takeball":
        "When a CPU steal succeeds, the chance it comes away with the ball "
        "rather than only knocking it loose. Read by "
        "PlayerSteal.computeStealOrHitRate.",
    "cpu_block_shot":
        "Chance the CPU goes up to block a jump shot, plus the cooldown between "
        "block attempts. Read by TeamAI.updateJumpBlockShot.",
    "cpu_block_dunk":
        "Chance the CPU contests a dunk at the rim, plus its own cooldown. Read "
        "by TeamAI.updateJumpBlockDunk. Tracked separately from shot blocking.",
    "cpu_push_success":
        "Chance a CPU push knocks you off balance, plus the cooldown between "
        "pushes. Read by PlayerPush.getSuccessRate.",
    "cpu_smack_frequency":
        "Chance the CPU palms the ball out of your hands at the rim, plus its "
        "cooldown. Read by PlayerJumpBlock.canAIPalm.",
    "cpu_intercept_pass":
        "Chance the CPU jumps a passing lane, plus the cooldown between "
        "interception attempts. Read by TeamAI.updateInterceptPass.",
    "cpu_defensive_pressure":
        "How fast CPU defenders build coverage intensity while marking, and "
        "separately while marking the ball handler. Read by "
        "PlayerCover.computeAddedIntensityOverTime. Higher = they close you "
        "down faster.",
    "cpu_cover_tightness":
        "How sloppy CPU marking is allowed to be: how far off the ideal spot, "
        "how far off the ideal angle, and how often the marking target is "
        "recomputed. Higher = tighter, better-angled, more frequently corrected "
        "coverage.",
    # ---- CPU: rebounding --------------------------------------------------
    "cpu_rebound":
        "Chance the CPU wins a rebound, the cooldown between attempts, and how "
        "far off the ideal landing spot it is allowed to be. Higher = better "
        "odds, shorter waits and more precise positioning. Read by "
        "TeamAI.updateReboundData.",
    "cpu_rebound_pass":
        "Chance the CPU immediately outlets the ball after grabbing a rebound, "
        "plus the cooldown. Read by TeamAI.updateReboundPass.",
    # ---- CPU: movement / awareness ---------------------------------------
    "cpu_sprint_with_ball":
        "How readily a CPU ball handler sprints. Feeds AI option scoring "
        "(AIOptionForward.calculateScoreAndRisk) and the 'sprint to shake "
        "defenders' behaviour.",
    "cpu_reaction_time":
        "CPU delay before reacting: sprinting for a loose ball, starting to "
        "sprint while covering, refreshing its estimate of your velocity, and "
        "its timing offset on the jump ball. Higher = faster, sharper reactions.",
    # ---- User: shot windows ----------------------------------------------
    "shot_window_100":
        "Width of the green (guaranteed make) release window on your shot bar, "
        "as a fraction of the width the game ships. The single biggest lever on "
        "how hard shooting feels. At 100 the window is exactly as shipped; "
        "lower narrows it at every distance. Bands the game ships with no green "
        "window at all (0 m, 12 m, 15 m) stay that way.",
    "shot_window_30":
        "Width of the yellow 30%-make zones on either side of the green window, "
        "as a fraction of the shipped width.",
    "shot_window_10":
        "Width of the orange 10%-make zones, as a fraction of the shipped "
        "width. Anything outside these falls into the 5% zone.",
    # ---- User: defense ----------------------------------------------------
    "user_steal_success":
        "Chance your steal attempt succeeds once you are in range. Read by "
        "PlayerSteal.getSuccessRate for the player you control.",
    "user_steal_takeball":
        "When your steal succeeds, the chance you come away with the ball "
        "instead of only knocking it loose.",
    "user_steal_frequency":
        "Cooldown before another steal attempt on your side. NOTE: only AI "
        "decision code reads these cooldowns, so this governs assisted and "
        "automatic attempts by your side rather than gating your own button "
        "presses.",
    "user_block_frequency":
        "Cooldown before another block attempt on your side. NOTE: only AI "
        "decision code reads this cooldown, so it governs assisted attempts "
        "rather than gating your own button presses.",
    "user_push_frequency":
        "Cooldown before another push attempt on your side. NOTE: only AI "
        "decision code reads this cooldown, so it governs assisted attempts "
        "rather than gating your own button presses.",
    "user_intercept_frequency":
        "Cooldown before your side may jump another passing lane. NOTE: only AI "
        "decision code reads this cooldown, so it governs assisted attempts "
        "rather than gating your own button presses.",
    "user_rebound_frequency":
        "Cooldown before your side may contest another rebound. NOTE: only AI "
        "decision code reads this cooldown, so it governs assisted attempts "
        "rather than gating your own button presses.",
    "user_sprint_with_ball":
        "How readily your side commits to sprinting with the ball in AI option "
        "scoring. Affects assisted movement, not your own sprint button.",
    # ---- Both -------------------------------------------------------------
    "player_speed":
        "Top speed across all eight movement states (walk and sprint, with and "
        "without the ball, in and out of contact). Affects BOTH teams \u2014 "
        "PlayerStats is shared by every player on the floor and cannot be split "
        "by side.",
    "player_acceleration":
        "How quickly players reach top speed, walking and sprinting. Affects "
        "BOTH teams.",
    "pass_speed":
        "How fast the ball travels on normal passes and on alley-oop passes. "
        "Affects BOTH teams \u2014 faster passes are harder to intercept, which "
        "cuts both ways.",
    "input_memory":
        "How long a queued action is remembered while a player is receiving a "
        "pass, before it is discarded (PlayerReceiving.update). Higher is more "
        "forgiving on catch-and-shoot timing. Affects BOTH teams.",
}


def anchored(b, l):
    """UI25 == Beginner, UI75 == Legend -> endpoints at UI0 and UI100."""
    if isinstance(b, list):
        return ([b[i] - 0.5 * (l[i] - b[i]) for i in range(len(b))],
                [l[i] + 0.5 * (l[i] - b[i]) for i in range(len(b))])
    return b - 0.5 * (l - b), l + 0.5 * (l - b)


def main(root):
    sm = SliderManager.__new__(SliderManager)      # bypass catalog load
    import UnityPy
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.0.37f1"
    sm.root = root
    from slider_manager import Assembly, Walker
    sm.asm = Assembly(os.path.join(root, "Managed", "Assembly-CSharp.dll"))
    sm.walker = Walker(sm.asm)
    sm.objs = {}
    for f in ("level2", "sharedassets2.assets"):
        # ALWAYS read the stock backup when one exists. Reading the live files
        # would bake whatever preset is currently applied in as the "shipped"
        # anchors -- the catalog would then treat modified values as ground
        # truth and every preset would collapse onto the same numbers.
        live = os.path.join(root, f)
        bak = live + ".sliders_backup"
        if os.path.exists(bak):
            import filecmp
            if not filecmp.cmp(live, bak, shallow=False):
                print("  note: %s differs from its stock backup; reading the "
                      "backup for anchors" % f)
            sm.objs[f] = sm._read_file(f, use_backup=True)
        else:
            print("  note: no backup for %s -- assuming the live file is stock. "
                  "If a preset is currently applied, restore first." % f)
            sm.objs[f] = sm._read_file(f)
    sm._map_roles()

    t0 = sm.roles["tier0"].data          # Beginner
    t1 = sm.roles["tier1"].data          # Advanced
    t2 = sm.roles["tier2"].data          # Legend
    hu = sm.roles["human"].data
    ps = sm.roles["playerstats"][0].data
    b0 = sm.roles["bars0"].data          # Beginner release profile
    b1 = sm.roles["bars1"].data          # Advanced release profile
    b2 = sm.roles["bars2"].data          # Legend release profile

    sliders = {}

    for sid, label, cat, paths, clamp in CPU:
        fields = []
        for p in paths:
            fields.append({"role": "tiers", "path": p, "clamp": clamp,
                           "anchors": {"25": t0[p], "50": t1[p], "75": t2[p]}})
        sliders[sid] = {"label": label, "category": cat, "side": "CPU",
                        "description": DESCRIPTIONS.get(sid, ""), "fields": fields}

    for sid, label, cat, paths, m0, m1, clamp in USER:
        fields = []
        for p in paths:
            v = hu[p]
            a = [x * m0 for x in v] if isinstance(v, list) else v * m0
            z = [x * m1 for x in v] if isinstance(v, list) else v * m1
            fields.append({"role": "human", "path": p, "at0": a, "at100": z, "clamp": clamp})
        sliders[sid] = {"label": label, "category": cat, "side": "User",
                        "description": DESCRIPTIONS.get(sid, ""), "fields": fields}

    for sid, label, cat, role, paths, m0, m1, clamp in SHARED:
        fields = []
        for p in paths:
            v = ps[p]
            fields.append({"role": role, "path": p, "at0": v * m0, "at100": v * m1,
                           "clamp": clamp})
        sliders[sid] = {"label": label, "category": cat, "side": "Both",
                        "description": DESCRIPTIONS.get(sid, ""), "fields": fields}

    # CPU shooting accuracy: blend the whole 7x7 release-profile matrix
    fields = []
    for i, (rb, rm, rl) in enumerate(zip(b0["m_aoChargingBarChunksProbabilities"],
                                         b1["m_aoChargingBarChunksProbabilities"],
                                         b2["m_aoChargingBarChunksProbabilities"])):
        for j, (cb, cm, cl) in enumerate(zip(rb["m_afChunkProbabilities"],
                                             rm["m_afChunkProbabilities"],
                                             rl["m_afChunkProbabilities"])):
            fields.append({"role": "bars",
                           "path": "m_aoChargingBarChunksProbabilities[%d].m_afChunkProbabilities[%d].m_fValue" % (i, j),
                           "clamp": [0.0, 1.0],
                           "anchors": {"25": cb["m_fValue"], "50": cm["m_fValue"], "75": cl["m_fValue"]}})
    sliders["cpu_shooting_accuracy"] = {
        "label": "CPU Shooting Accuracy", "category": "Shooting", "side": "CPU",
        "description": DESCRIPTIONS["cpu_shooting_accuracy"], "fields": fields}

    # User shot-window sliders are geometry, handled by _shot_window_edits
    for sid, label in (("shot_window_100", "User Green Window"),
                       ("shot_window_30", "User Yellow Zones"),
                       ("shot_window_10", "User Orange Zones")):
        sliders[sid] = {"label": label, "category": "Shooting", "side": "User",
                        "description": DESCRIPTIONS[sid], "fields": []}

    catalog = {
        "unity": "6000.0.37f1",
        "anchoring": "UI 25 = the game's Rookie difficulty, UI 50 = Advanced, UI 75 = Pro (the hardest in-game option). Piecewise, extrapolated beyond.",
        "convention": "Higher UI = more of that thing for that side (NBA 2K convention)",
        "categories": ["Shooting", "Passing", "Defense", "Rebounding", "Movement",
                       "Awareness", "Controls"],
        "shot_window": {
            "_model": "Each zone width = shipped_width * fraction. UI 100 = shipped width (easiest the game ships), UI 0 = a sliver.",
            "slider_100": "shot_window_100", "frac100_at0": 0.03, "frac100_at100": 1.0,
            "slider_30": "shot_window_30", "frac30_at0": 0.10, "frac30_at100": 1.0,
            "slider_10": "shot_window_10", "frac10_at0": 0.15, "frac10_at100": 1.0,
            "note": "Fractions <= 1.0 mean no distance band can become easier than the shipped game, and zones that are zero-width in stock stay zero-width."
        },
        "sliders": sliders,
    }
    missing = [k for k, v in sliders.items() if not v.get("description")]
    if missing:
        raise SystemExit("Sliders missing a description: %s" % ", ".join(missing))
    json.dump(catalog, open(os.path.join(OUT_DIR, "sliders_catalog.json"), "w",
                            encoding="utf-8"), indent=1)
    print("sliders_catalog.json: %d sliders, all with descriptions" % len(sliders))

    # ------------------------------------------------------------ presets
    # base intensity per preset; CPU sliders use it directly, User sliders are
    # mirrored (100 - base) so the user gets weaker as the CPU gets stronger.
    BASE = {"Rookie": 8, "Pro": 50, "All-Star": 63, "Superstar": 76, "Hall of Fame": 92}
    # shot window follows the USER curve but is deliberately gentler at the top
    WINDOW = {"Rookie": 92, "Pro": 62, "All-Star": 48, "Superstar": 33, "Hall of Fame": 16}
    NEUTRAL = {"player_speed": 50, "player_acceleration": 50, "pass_speed": 50,
               "input_memory": {"Rookie": 70, "Pro": 55, "All-Star": 50,
                                "Superstar": 45, "Hall of Fame": 38}}

    presets = {}
    for name, base in BASE.items():
        vals = {}
        for sid, s in sliders.items():
            if sid.startswith("shot_window"):
                vals[sid] = WINDOW[name]
            elif sid in NEUTRAL:
                n = NEUTRAL[sid]
                vals[sid] = n[name] if isinstance(n, dict) else n
            elif s["side"] == "CPU":
                vals[sid] = base
            elif s["side"] == "User":
                vals[sid] = round(100 - base * 0.85)
            else:
                vals[sid] = 50
        presets[name] = {
            "order": list(BASE).index(name),
            "sliders": vals,
        }
    blurbs = {
        "Rookie": "Easier than the game's own Rookie difficulty, with a very "
                  "forgiving shot window. For learning the controls.",
        "Pro": "Matches the game's Advanced difficulty exactly -- stock NBA "
               "Bounce as the developers balanced it. (NOTE: this is the app "
               "preset called Pro; the game's own difficulty named Pro is its "
               "HARDEST setting, closer to the Superstar preset below.)",
        "All-Star": "Between the game's Advanced and Pro difficulties. The CPU "
                    "contests more and your green window narrows.",
        "Superstar": "Roughly the game's Pro difficulty -- its hardest in-game "
                     "option -- with your own cooldowns lengthened on top.",
        "Hall of Fame": "Beyond anything in the retail game. Past the game's Pro "
                        "difficulty on every axis, with a sliver of a green window.",
    }
    for k, v in blurbs.items():
        presets[k]["description"] = v
    json.dump({"unity": "6000.0.37f1", "presets": presets},
              open(os.path.join(OUT_DIR, "presets.json"), "w", encoding="utf-8"), indent=1)
    print("presets.json: %s" % ", ".join(presets))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1
         else r"I:\SteamLibrary\steamapps\common\NBA BOUNCE\NBA Bounce_Data")
