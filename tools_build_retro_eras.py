"""Generate retro_court_eras.json from the curated era table + live container data.

Guarantees every variant in team_container_colors.json gets an entry, so the
dropdown can never show a raw LOGO_4_TEAM_11 key to the user.
"""
import json, os, sys

SRC = sys.argv[1]
OUT = sys.argv[2]

# (label, years, confidence, evidence)
# confidence: "high"  = retro variant carries colors that are unmistakably that era
#             "medium"= colors are suggestive and match known era palette
#             "low"   = variant colors are identical/near-identical to the modern
#                       court, so the era is inferred from logo history alone.
#                       These are the ones worth confirming visually.
CURATED = {
    "01_Boston Celtics": {
        "LOGO_4_TEAM_1": ("Boston Garden Classic", "1976-1996", "low",
            "Celtics never changed colors; retro variant is identical green #008348 to modern."),
        "LOGO_5_TEAM_1": ("Original Leprechaun", "1950-1968", "low",
            "Identical green #008348 to modern - era inferred from logo history only."),
    },
    "03_New York Knicks": {
        "LOGO_4_TEAM_3": ("Knickerbockers Roundel", "1964-1983", "medium",
            "Apron and key drop to pure blue #005FAB with no orange - matches the all-blue roundel era."),
        "LOGO_5_TEAM_3": ("Classic Blue & Orange", "1992-2011", "medium",
            "Orange apron #F58426 identical to modern - the long-running classic roundel."),
    },
    "04_Philadelphia 76ers": {
        "LOGO_4_TEAM_4": ("Black & Gold Era", "1997-2009", "medium",
            "Near-black floor #0C181C with blue key #0061AE - matches the black-uniform rebrand."),
        "LOGO_5_TEAM_4": ("Stars & Stripes", "1977-1997", "medium",
            "Navy floor with royal blue #014B96 - the classic red/white/blue identity."),
    },
    "05_Toronto Raptors": {
        "LOGO_4_TEAM_5": ("Purple Dino", "1995-2006", "medium",
            "Red #C10431 with indigo #373697 - the original purple/red/black expansion identity."),
        "LOGO_5_TEAM_5": ("Silver & Purple", "2008-2015", "low",
            "Silver floor #3F3F3F with purple key #793EBB."),
    },
    "06_Chicago Bulls": {
        "LOGO_4_TEAM_6": ("Chicago Stadium Classic", "1966-1994", "low",
            "Red apron #CE1141; Bulls logo/colors are unchanged since 1966, so era is inferred."),
    },
    "07_Cleveland Cavaliers": {
        "LOGO_4_TEAM_7": ("Blue & Black", "1994-2003", "high",
            "Black main #1A1A1A with light blue #4B82C3 - unmistakably the Gund Arena blue/black rebrand."),
        "LOGO_5_TEAM_7": ("Orange & Blue", "1983-1994", "high",
            "Orange main #F1491B with navy #013D7D - the classic orange/blue Cavs."),
    },
    "08_Detroit Pistons": {
        "LOGO_4_TEAM_8": ("Bad Boys", "1979-1996", "high",
            "Blue #0461A5 with red #ED154E - the red/white/blue era that covers the Bad Boys titles."),
        "LOGO_5_TEAM_8": ("Teal Horse", "1996-2001", "high",
            "Teal #007C8B with red #B71B37 - the teal/black rebrand, unmistakable."),
    },
    "09_Indiana Pacers": {
        "LOGO_4_TEAM_9": ("ABA Blue & Gold", "1967-1976", "medium",
            "Bright blue floor #0B4499 with gold #F5D130 - the ABA-era palette."),
    },
    "10_Milwaukee Bucks": {
        "LOGO_4_TEAM_10": ("Purple & Green", "1993-2006", "high",
            "Purple floor #20102B and apron #5D307E over green - the purple/green era."),
        "LOGO_5_TEAM_10": ("Classic Green (Bango)", "1968-1993", "medium",
            "Identical green #00471B to modern - the original green/red identity."),
    },
    "11_Atlanta Hawks": {
        "LOGO_4_TEAM_11": ("Pac-Man Hawks", "1972-1995", "high",
            "Red main #DD042C - the red/gold hawk-head era introduced July 1972."),
        "LOGO_5_TEAM_11": ("Mod Green & Blue", "1970-1972", "high",
            "Blue #0D489B with lime green #5ABF34 - the short-lived 1970-72 'mod' scheme. Verified."),
    },
    "12_Charlotte Hornets": {
        "LOGO_4_TEAM_12": ("Original Hornets", "1988-2002", "high",
            "Purple #1D1160 with teal #00788C - the original Hugo-era teal/purple."),
    },
    "13_Miami Heat": {
        "LOGO_4_TEAM_13": ("Original Heat", "1988-1999", "medium",
            "Deep red main #98002E on black - the original red/black/orange identity."),
        "LOGO_5_TEAM_13": ("Miami Floridians Tribute", "1968-1972", "low",
            "Brighter magenta-red #D20D44 - possibly the ABA Floridians throwback the Heat wear."),
    },
    "14_Orlando Magic": {
        "LOGO_4_TEAM_14": ("Original Magic (Pinstripes)", "1989-2000", "high",
            "Blue apron #0050B5 on navy floor #001C3F - the pinstripe expansion identity."),
    },
    "16_Denver Nuggets": {
        "LOGO_4_TEAM_16": ("Rainbow Skyline", "1981-1993", "medium",
            "Bright red/pink #F1124B over a silver floor #3F3F3F - the multicolor skyline era."),
        "LOGO_5_TEAM_16": ("Early Nuggets Blue", "1974-1981", "low",
            "Blue #0161AA over a silver floor - the pre-skyline identity."),
    },
    "17_Minnesota Timberwolves": {
        "LOGO_4_TEAM_17": ("Original Wolves", "1989-1996", "high",
            "Blue #1E59AB with green #00A162 on a dark green floor - the original blue/green expansion look."),
    },
    "19_Portland Trail Blazers": {
        "LOGO_4_TEAM_19": ("Original Blazers", "1970-1990", "low",
            "Red #D0043C; Blazers colors are essentially unchanged, so era is inferred from logo history."),
        "LOGO_5_TEAM_19": ("Pinwheel Era", "1990-2002", "low",
            "Red #DD042B, near-identical to the other retro - era inferred from logo history."),
    },
    "20_Utah Jazz": {
        "LOGO_4_TEAM_20": ("New Orleans Jazz", "1974-1979", "high",
            "Green main #006937 with purple #522B5A - the New Orleans purple/green/gold original."),
        "LOGO_5_TEAM_20": ("Purple Mountain", "1996-2004", "high",
            "Teal #066685 with purple #3C2E87 - the purple/teal/copper mountain era."),
    },
    "21_Golden State Warriors": {
        "LOGO_4_TEAM_21": ("The City", "1966-1971", "medium",
            "Orange-red main #DB452C on navy #0D2954 - the Golden Gate Bridge 'The City' jerseys."),
        "LOGO_5_TEAM_21": ("Run TMC", "1988-1997", "medium",
            "Royal blue #1D428A with bright yellow #F3D600 on a blue floor #002F6D."),
    },
    "23_Los Angeles Lakers": {
        "LOGO_4_TEAM_23": ("Classic Forum", "1967-1999", "low",
            "Pale violet floor #7D76B7 with gold #F0C33B - a stylized Forum-era purple/gold court."),
    },
    "24_Phoenix Suns": {
        "LOGO_4_TEAM_24": ("Original Suns", "1968-1992", "medium",
            "Purple #422C8F with orange #F59110 - the original purple/orange identity."),
        "LOGO_5_TEAM_24": ("Streaking Sun", "1992-2000", "medium",
            "Purple #5D2787 with orange #EE722C - the Barkley-era streaking sun."),
    },
    "25_Sacramento Kings": {
        "LOGO_4_TEAM_25": ("Kansas City Kings", "1975-1985", "high",
            "Navy #08357A with red key #F90031 - the blue/red Kansas City identity, not Sacramento purple."),
    },
    "26_Dallas Mavericks": {
        "LOGO_4_TEAM_26": ("Original Mavs", "1980-2001", "high",
            "Green main #05724E with navy #002858 - the original blue/green Mavericks."),
    },
    "27_Houston Rockets": {
        "LOGO_4_TEAM_27": ("Classic Red", "1972-1995", "medium",
            "Flat red #DB0029 on black - the red/gold Rockets through the championship years."),
        "LOGO_5_TEAM_27": ("Pinwheel Navy", "1995-2003", "high",
            "Red #C90539 with navy #02295A on a navy floor - the navy/pinwheel rebrand."),
    },
    "30_San Antonio Spurs": {
        "LOGO_4_TEAM_30": ("Fiesta", "1989-2002", "high",
            "Orange #F9A139, teal #00ABA3 and pink #F16578 - unmistakably the Fiesta colors."),
        "LOGO_5_TEAM_30": ("Original Spurs", "1976-1989", "low",
            "Black/silver identical to modern - era inferred from logo history only."),
    },
    "33_New Jersey Nets": {
        "LOGO_1_TEAM_33": ("Nets Navy", "1997-2012", "medium",
            "Navy #21456A with red key - identical to the legacy team's default court."),
        "LOGO_2_TEAM_33": ("Royal Blue Nets", "1990-1997", "medium",
            "Royal blue #0045AD throughout - the tie-dye/royal era."),
        "LOGO_3_TEAM_33": ("Classic Nets", "1977-1990", "medium",
            "Lighter blue #1B65B7 with red #EB1444 - the red/white/blue arrival-in-New-Jersey era."),
    },
    "35_Seattle Supersonics": {
        "LOGO_1_TEAM_35": ("Classic Sonics Green", "1975-1995", "low",
            "Identical green #005931 to the legacy default court - era inferred from logo history."),
    },
    "37_GoatTeamLeft": {
        "LOGO_4_TEAM_1": ("Boston Garden Classic", "1976-1996", "low",
            "GOAT team reuses the Celtics container profile."),
        "LOGO_5_TEAM_1": ("Original Leprechaun", "1950-1968", "low",
            "GOAT team reuses the Celtics container profile."),
    },
    "38_GoatTeamRight": {
        "LOGO_4_TEAM_1": ("Boston Garden Classic", "1976-1996", "low",
            "GOAT team reuses the Celtics container profile."),
        "LOGO_5_TEAM_1": ("Original Leprechaun", "1950-1968", "low",
            "GOAT team reuses the Celtics container profile."),
    },
}

# Container name -> the TeamName token used in texture filenames.
TEXTURE_TOKEN = {
    "01_Boston Celtics": "BostonCeltics",       "03_New York Knicks": "NewYorkKnicks",
    "04_Philadelphia 76ers": "Philadelphia76ers", "05_Toronto Raptors": "TorontoRaptors",
    "06_Chicago Bulls": "ChicagoBulls",         "07_Cleveland Cavaliers": "ClevelandCavaliers",
    "08_Detroit Pistons": "DetroitPistons",     "09_Indiana Pacers": "IndianaPacers",
    "10_Milwaukee Bucks": "MilwaukeeBucks",     "11_Atlanta Hawks": "AtlantaHawks",
    "12_Charlotte Hornets": "CharlotteHornets", "13_Miami Heat": "MiamiHeat",
    "14_Orlando Magic": "OrlandoMagic",         "16_Denver Nuggets": "DenverNuggets",
    "17_Minnesota Timberwolves": "MinnesotaTimberwolves",
    "19_Portland Trail Blazers": "PortlandTrailBlazers", "20_Utah Jazz": "UtahJazz",
    "21_Golden State Warriors": "GoldenStateWarriors", "23_Los Angeles Lakers": "LosAngelesLakers",
    "24_Phoenix Suns": "PhoenixSuns",           "25_Sacramento Kings": "SacramentoKings",
    "26_Dallas Mavericks": "DallasMavericks",   "27_Houston Rockets": "HoustonRockets",
    "30_San Antonio Spurs": "SanAntonioSpurs",  "33_New Jersey Nets": "NewJerseyNets",
    # The Sonics' court decal uses a shorter token than their parquet material
    # (mat_..._SeattleSuperSonics vs txt_bounce_court_SeattleSonics_D).
    "35_Seattle Supersonics": "SeattleSonics",
}

# Decal names that don't follow the usual retroLogo_{slot}_team_{n} pattern.
# Verified against the texture catalog -- these are shipped names, not guesses.
DECAL_OVERRIDES = {
    # The game misspells the Pacers' decal: 'logoRetro' and 'team9', not
    # 'retroLogo' and 'team_9'. Both the court and signage textures are affected.
    ("09_Indiana Pacers", "LOGO_4_TEAM_9"): (
        "txt_bounce_court_IndianaPacers_D_logoRetro_4_team9",
        "txt_bounce_publi_IndianaPacers_D_logoRetro_4_team9"),
    # Legacy teams' LOGO_1 is their own primary logo, so it has no separate
    # retro decal -- it reuses the base court texture.
    ("33_New Jersey Nets", "LOGO_1_TEAM_33"): (
        "txt_bounce_court_NewJerseyNets_D", "txt_bounce_publi_NewJerseyNets_D"),
    ("35_Seattle Supersonics", "LOGO_1_TEAM_35"): (
        "txt_bounce_court_SeattleSonics_D", "txt_bounce_publi_SeattleSonics_D"),
}


def variant_order(container):
    seen = []
    for prof in container["profiles"]:
        for v in prof["variants"]:
            if v["variant"] not in seen:
                seen.append(v["variant"])
    return seen


# Params worth recording per variant: the main court tint plus the three filled
# areas. These are what make one era visually distinct from another.
SNAPSHOT = ["_Vertex_Color_R_Main", "_Color_Floor_Court_B",
            "_Color_Outside_Court_R", "_Color_Area_G"]


def hexify(rgba):
    return "#%02X%02X%02X" % tuple(
        min(255, max(0, round(c * 255))) for c in rgba[:3])


def snapshot(container, variant):
    """{param: '#RRGGBB'} for one variant, used for the era-accurate preview."""
    out = {}
    for prof in container["profiles"]:
        if prof["param"] not in SNAPSHOT:
            continue
        for v in prof["variants"]:
            if v["variant"] == variant:
                out[prof["param"]] = hexify(v["rgba"])
    return out


def main():
    data = json.load(open(SRC, encoding="utf-8"))
    out = {
        "_note": ("Maps each BasketTeamContainer court variant to the real-world NBA era it "
                  "represents. Edit 'label'/'years' freely - the app reads this file at startup "
                  "and nothing else depends on the wording. 'confidence' is only shown as a hint "
                  "in the UI for entries that are inferred rather than evidenced."),
        "_confidence_legend": {
            "high": "Variant's baked court colors are unmistakably that era's palette.",
            "medium": "Colors are suggestive and consistent with that era.",
            "low": "Variant colors match the modern court, so the era comes from logo history alone. Worth confirming visually.",
        },
        "_default_label": "Modern (default court)",
        "teams": {},
    }

    missing = []
    for cname, cdata in data["teams"].items():
        variants = variant_order(cdata)
        retro = [v for v in variants if v != "default"]
        if not retro:
            continue
        token = TEXTURE_TOKEN.get(cname)
        entry = {"_default_colors": snapshot(cdata, "default"),
                 "_texture_token": token}
        for v in retro:
            cur = CURATED.get(cname, {}).get(v)
            if cur is None:
                missing.append(f"{cname}/{v}")
                cur = (v.replace("_", " ").title(), "unknown", "low",
                       "No curated entry - falls back to the raw variant key.")
            label, years, conf, evidence = cur
            rec = {"label": label, "years": years, "confidence": conf, "evidence": evidence,
                   "colors": snapshot(cdata, v)}
            override = DECAL_OVERRIDES.get((cname, v))
            if override:
                rec["court_texture"], rec["signage_texture"] = override
            elif token:
                # slot number, e.g. LOGO_4_TEAM_11 -> 4
                slot, team_no = v.split("_")[1], v.split("_")[-1]
                rec["court_texture"] = f"txt_bounce_court_{token}_D_retroLogo_{slot}_team_{team_no}"
                rec["signage_texture"] = f"txt_bounce_publi_{token}_D_retroLogo_{slot}_team_{team_no}"
            entry[v] = rec
        out["teams"][cname] = entry

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")

    n_variants = sum(len([k for k in v if not k.startswith("_")]) for v in out["teams"].values())
    print(f"wrote {OUT}: {len(out['teams'])} teams, {n_variants} retro variants")
    if missing:
        print("UNCURATED:", missing)
    else:
        print("every retro variant has a curated era label")


main()
