"""Retro court era labels.

The game stores one set of court colors per *court variant* inside each
BasketTeamContainer: a "default" entry for the team's modern court, plus one
entry per unlockable retro/throwback logo (keys like ``LOGO_4_TEAM_11``).

Those keys are meaningless to a user, so retro_court_eras.json maps each one to
the real-world era it represents -- derived by matching the colors the game
bakes into each variant against known NBA logo/uniform history. This module is
the read side of that file.

Nothing here writes. Editing retro_court_eras.json is enough to relabel a court
everywhere in the UI.
"""

import json
import os

ERAS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "retro_court_eras.json")

DEFAULT_VARIANT = "default"
ALL_VARIANTS = "__all__"
DEFAULT_LABEL = "Modern (default court)"
ALL_LABEL = "All courts (modern + retro)"

_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            with open(ERAS_FILE, encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {"teams": {}}
    return _cache


def reload():
    """Drop the cache so an edited retro_court_eras.json is picked up."""
    global _cache
    _cache = None
    return _load()


def describe(container_name, variant):
    """Return {label, years, confidence, evidence, ...} for one variant.

    Falls back to a usable record for unknown keys rather than raising, so a
    game update that adds a variant can't break the dropdown.
    """
    if variant == DEFAULT_VARIANT:
        return {"label": _load().get("_default_label", DEFAULT_LABEL),
                "years": "", "confidence": "high", "evidence": ""}
    if variant == ALL_VARIANTS:
        return {"label": ALL_LABEL, "years": "", "confidence": "high", "evidence": ""}
    rec = _load().get("teams", {}).get(container_name, {}).get(variant)
    if rec:
        return rec
    return {"label": variant.replace("_", " ").title(), "years": "",
            "confidence": "low", "evidence": "No era mapping for this variant yet."}


def display_name(container_name, variant):
    """The string shown in the dropdown, e.g. 'Mod Green & Blue (1970-1972)'."""
    rec = describe(container_name, variant)
    label, years = rec.get("label", variant), rec.get("years", "")
    if years and years != "unknown":
        return f"{label} ({years})"
    return label


def variants_for(container_data):
    """Ordered variant keys present in a container: default first, then retros.

    Reads the live container rather than the JSON, so the dropdown only ever
    offers courts that actually exist in the installed game.
    """
    if not container_data:
        return []
    seen = []
    profiles = container_data.get("profiles", {})
    values = profiles.values() if isinstance(profiles, dict) else profiles
    for variants in values:
        for key in variants:
            if key not in seen:
                seen.append(key)
    seen.sort(key=lambda v: (v != DEFAULT_VARIANT, v))
    return seen


def choices_for(container_name, container_data, include_all=True):
    """Build the dropdown model: list of (variant_key, display_string).

    'All courts' is only offered when the team actually has retro variants --
    otherwise it would be a confusing no-op duplicate of the default entry.
    """
    keys = variants_for(container_data)
    if not keys:
        return []
    out = [(k, display_name(container_name, k)) for k in keys]
    if include_all and len(keys) > 1:
        out.append((ALL_VARIANTS, ALL_LABEL))
    return out


def _norm(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def resolve_container(team_token):
    """Map a Material-derived team name ('AtlantaHawks') to a container name
    ('11_Atlanta Hawks').

    The Floor Patterns tab only knows teams by their material suffix, so it
    needs this to look anything up. Returns None when the team has no retro
    courts at all.
    """
    if not team_token:
        return None
    want = _norm(team_token)
    for cname in _load().get("teams", {}):
        # container names are '11_Atlanta Hawks'; drop the numeric prefix
        bare = cname.split("_", 1)[-1]
        if _norm(bare) == want or _norm(cname) == want:
            return cname
    return None


def variant_keys_from_json(container_name):
    """Retro variant keys for a container, read from the JSON alone.

    Used where live container data isn't available. Metadata keys (leading
    underscore) are skipped.
    """
    team = _load().get("teams", {}).get(container_name, {})
    return [k for k in team if not k.startswith("_")]


def choices_from_json(team_token):
    """(variant_key, display_string) list for a team known only by material name.

    Always leads with the modern court so the caller can offer it as the
    default selection, even for teams with no retro variants.
    """
    cname = resolve_container(team_token)
    out = [(DEFAULT_VARIANT, _load().get("_default_label", DEFAULT_LABEL))]
    if not cname:
        return cname, out
    for key in variant_keys_from_json(cname):
        out.append((key, display_name(cname, key)))
    return cname, out


# Court colors recorded per variant, in the order they read best as a palette.
# NOTE: none of these is the parquet tint. The wood texture is multiplied by the
# *material's* _Color_Parquet, which is per-team and not stored here. Tinting a
# floor-pattern preview with any of these produces a black or flat-colour swatch
# -- these are court surface/marking colours, not wood tones.
PALETTE_ORDER = [
    ("Court surface", "_Vertex_Color_R_Main"),
    ("Sideline / apron", "_Color_Outside_Court_R"),
    ("Key / paint", "_Color_Area_G"),
    ("Outer surround", "_Color_Floor_Court_B"),
]


def colors_for(container_name, variant):
    """Raw {param: '#RRGGBB'} recorded for one court variant."""
    team = _load().get("teams", {}).get(container_name, {})
    if variant in (DEFAULT_VARIANT, ALL_VARIANTS):
        return dict(team.get("_default_colors", {}))
    return dict((team.get(variant) or {}).get("colors", {}))


def palette_for(container_name, variant):
    """[(label, '#RRGGBB')] for one court variant, for showing swatches."""
    colors = colors_for(container_name, variant)
    out = []
    for label, param in PALETTE_ORDER:
        hexval = colors.get(param)
        if hexval and hexval.startswith("#") and len(hexval) == 7:
            out.append((label, hexval))
    return out


def texture_token(container_name):
    """The TeamName token used in texture filenames, e.g. 'AtlantaHawks'."""
    return (_load().get("teams", {}).get(container_name, {}) or {}).get("_texture_token")


def court_texture_for(container_name, variant):
    """Name of the centre-court logo decal for one court variant, or None.

    The modern court's decal has no retroLogo suffix, so it's derived from the
    team token rather than stored per-variant.
    """
    if not container_name:
        return None
    if variant == ALL_VARIANTS:
        variant = DEFAULT_VARIANT
    if variant == DEFAULT_VARIANT:
        token = texture_token(container_name)
        return f"txt_bounce_court_{token}_D" if token else None
    rec = _load().get("teams", {}).get(container_name, {}).get(variant) or {}
    return rec.get("court_texture")


def floor_color_for(container_name, variant, fallback=(0.10, 0.10, 0.10)):
    """The court's outer-surround color as 0-1 floats.

    Used as the backdrop when previewing a mostly-transparent logo decal, so the
    logo is shown against the colour it actually sits on in game.
    """
    team = _load().get("teams", {}).get(container_name, {})
    if variant in (DEFAULT_VARIANT, ALL_VARIANTS):
        colors = team.get("_default_colors", {})
    else:
        colors = (team.get(variant) or {}).get("colors", {})
    # The centre-court logo sits on the main playing surface, so the main court
    # tint is the right backdrop -- and it varies far more between eras than the
    # outer surround does.
    hexval = colors.get("_Vertex_Color_R_Main") or colors.get("_Color_Floor_Court_B")
    if not hexval or not hexval.startswith("#") or len(hexval) != 7:
        return fallback
    try:
        return tuple(int(hexval[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    except ValueError:
        return fallback


def confidence_note(container_name, variant):
    """Short caveat for inferred eras, or '' when the mapping is evidenced.

    Shown in the UI so a low-confidence label never reads as fact.
    """
    rec = describe(container_name, variant)
    if rec.get("confidence") == "low" and variant not in (DEFAULT_VARIANT, ALL_VARIANTS):
        return ("Era name is inferred from logo history — this court's colors match the "
                "modern one, so the label may be off. Edit retro_court_eras.json to correct it.")
    return ""
