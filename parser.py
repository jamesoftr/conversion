"""
parser.py  —  Parse Pokétwo bot messages into structured data.

Catch message formatsz
─────────────────────
Standard:
  Congratulations <@USER_ID>! You caught a Level N NAME<:gender:ID> (IV%)!

Gigantamax (extra line after catch line):
  Woah! It seems that this pokémon has the Gigantamax Factor... <:_:ID>

Shiny (extra line, may appear with or without IV):
  These colors seem unusual... ✨

Chain / streak shiny (extra lines):
  Shiny streak reset. (**N**)
  These colors seem unusual... ✨

IV is optional; when hidden the parenthetical is absent.

Flee embed title format
────────────────────────
  Wild POKEMON fled. A new wild pokémon has appeared!
  (case-insensitive, may have leading/trailing whitespace)
"""

import re
from dataclasses import dataclass, field

# ── Regex patterns ────────────────────────────────────────────────────────────

# Matches the congratulations line:
#   Congratulations <@USER_ID>! You caught a Level N NAME<:...> (IV%)!
#   OR  …NAME<:...>!   (no IV)
_CATCH_RE = re.compile(
    r"Congratulations\s+<@!?(\d+)>!\s+"     # user mention
    r"You caught a Level \d+\s+"            # level (ignored)
    r"([\w\s\-'.:']+?)"                     # Pokémon name (lazy, stops before gender emoji)
    r"(?:<:[^>]+>)?"                        # optional gender emoji  <:male:ID>
    r"(?:\s*\((\d+(?:\.\d+)?)%\))?"        # optional (IV%)
    r"!",                                   # trailing bang
    re.IGNORECASE,
)

_GIGANTAMAX_RE = re.compile(
    r"Gigantamax Factor",
    re.IGNORECASE,
)

_SHINY_RE = re.compile(
    r"These colors seem unusual",
    re.IGNORECASE,
)

_CHAIN_SHINY_RE = re.compile(
    r"Shiny streak reset\.",
    re.IGNORECASE,
)

# Flee embed title:  "Wild Whismur fled. A new wild pokémon has appeared!"
_FLEE_RE = re.compile(
    r"^Wild\s+([\w\s\-'.:']+?)\s+fled\.",
    re.IGNORECASE,
)


@dataclass
class CatchEvent:
    user_id:     int
    pokemon:     str
    iv:          float | None   # None = hidden
    shiny:       bool = False
    gigantamax:  bool = False
    chain_shiny: bool = False   # shiny via streak chain


@dataclass
class FleeEvent:
    pokemon: str


# ── Public API ────────────────────────────────────────────────────────────────

def parse_catch(content: str) -> CatchEvent | None:
    """
    Parse the text content of a Pokétwo catch message.
    Returns a CatchEvent or None if not a catch message.

    `content` should be the full message text (all lines joined).
    """
    m = _CATCH_RE.search(content)
    if not m:
        return None

    user_id = int(m.group(1))
    pokemon = m.group(2).strip()
    iv_str  = m.group(3)
    iv      = float(iv_str) if iv_str is not None else None

    shiny       = bool(_SHINY_RE.search(content))
    gigantamax  = bool(_GIGANTAMAX_RE.search(content))
    chain_shiny = bool(_CHAIN_SHINY_RE.search(content))

    return CatchEvent(
        user_id=user_id,
        pokemon=pokemon,
        iv=iv,
        shiny=shiny,
        gigantamax=gigantamax,
        chain_shiny=chain_shiny,
    )


def parse_flee(embed_title: str) -> FleeEvent | None:
    """
    Parse the embed title of a flee notification.
    Returns a FleeEvent or None if not a flee message.
    """
    if not embed_title:
        return None
    m = _FLEE_RE.match(embed_title.strip())
    if not m:
        return None
    return FleeEvent(pokemon=m.group(1).strip())


# ── Test / demo ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    samples = [
        # Standard catch with IV
        "Congratulations <@1131217949672353832>! You caught a Level 23 Fletchling<:female:1207734084210532483> (49.46%)!",
        # Gigantamax
        "Congratulations <@738379789777371249>! You caught a Level 20 Eevee<:male:1207734081585152101> (43.01%)!\nWoah! It seems that this pokémon has the Gigantamax Factor... <:_:1242455099213877248>",
        # Shiny, no IV
        "Congratulations <@757852191338922025>! You caught a Level 11 Wooloo<:male:1207734081585152101>!\n\nThese colors seem unusual... ✨",
        # Chain shiny with IV
        "Congratulations <@738379789777371249>! You caught a Level 67 Beta Bellsprout<:female:1207734084210532483> (67.67%)!\n\nShiny streak reset. (**491**)\n\nThese colors seem unusual... ✨",
    ]
    flee_samples = [
        "Wild Whismur fled. A new wild pokémon has appeared!",
        "Wild Tapu Koko fled. A new wild pokémon has appeared!",
    ]

    print("=== Catch parsing ===")
    for s in samples:
        ev = parse_catch(s)
        print(f"  → {ev}")

    print("\n=== Flee parsing ===")
    for s in flee_samples:
        ev = parse_flee(s)
        print(f"  → {ev}")
