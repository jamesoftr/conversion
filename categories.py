"""
categories.py  —  Define named Pokémon categories for fled-log tracking and stats.

Each entry in CATEGORIES is a dict with:
  name        : display name
  key         : short slug used in commands  (must be lowercase, no spaces)
  aliases     : list of alternative command aliases
  pokemon     : set of Pokémon names that belong to this category
"""

CATEGORIES: list[dict] = [
    {
        "name": "Rare Pokémon",
        "key": "rares",
        "aliases": ["rare", "allrare", "legendaries"],
        "pokemon": {
            "Registeel","Articuno","Regirock","Rayquaza","Moltres","Suicune",
            "Groudon","Jirachi","Zapdos","Mewtwo","Raikou","Celebi","Regice",
            "Latias","Latios","Kyogre","Entei","Lugia","Ho-Oh","Mew","Regigigas",
            "Cresselia","Terrakion","Giratina","Cobalion","Virizion","Tornadus",
            "Mesprit","Heatran","Manaphy","Darkrai","Shaymin","Victini","Deoxys",
            "Dialga","Palkia","Phione","Arceus","Azelf","Uxie","Type: Null",
            "Thundurus","Volcanion","Tapu Koko","Tapu Lele","Tapu Bulu","Tapu Fini",
            "Reshiram","Landorus","Meloetta","Genesect","Silvally","Xerneas",
            "Yveltal","Zygarde","Diancie","Zekrom","Kyurem","Keldeo","Hoopa",
            "Blacephalon","Celesteela","Pheromosa","Xurkitree","Marshadow",
            "Naganadel","Stakataka","Solgaleo","Nihilego","Buzzwole","Guzzlord",
            "Necrozma","Magearna","Cosmoem","Kartana","Poipole","Zeraora","Cosmog",
            "Lunala","Meltan","Zamazenta","Eternatus","Regieleki","Regidrago",
            "Glastrier","Spectrier","Chien-Pao","Melmetal","Enamorus","Wo-Chien",
            "Koraidon","Miraidon","Urshifu","Calyrex","Ting-Lu","Okidogi","Zacian",
            "Zarude","Chi-Yu","Kubfu","Pirouette Meloetta","Therian Thundurus",
            "Therian Tornadus","Therian Landorus","Origin Giratina","Resolute Keldeo",
            "Defense Deoxys","Mega Mewtwo X","Mega Mewtwo Y","Attack Deoxys",
            "Speed Deoxys","Black Kyurem","White Kyurem","Mega Latias","Sky Shaymin",
            "Fezandipiti","Munkidori","Terapagos","Pecharunt","Ogerpon",
            "Rapid Strike Urshifu","Shadow Rider Calyrex","Dawn Wings Necrozma",
            "Dusk Mane Necrozma","Galarian Articuno","Original Magearna",
            "Crowned Zamazenta","Ice Rider Calyrex","Galarian Moltres",
            "Complete Zygarde","Galarian Zapdos","Primal Groudon","Ultra Necrozma",
            "Crowned Zacian","Primal Kyogre","Mega Rayquaza","Hoopa Unbound",
            "Mega Diancie","Mega Latios","10% Zygarde",
            "Gigantamax Single Strike Urshifu","Gigantamax Rapid Strike Urshifu",
            "Sprinting Build Koraidon","Hearthflame Mask Ogerpon",
            "Cornerstone Mask Ogerpon","Wellspring Mask Ogerpon",
            "Gliding Build Koraidon","Gigantamax Melmetal","Eternamax Eternatus",
            "Drive Mode Miraidon","Glide Mode Miraidon","Terastal Terapagos",
            "Therian Enamorus","Neutral Xerneas","Origin Dialga","Origin Palkia",
            "Dragon Arceus","Dark Arceus","Dada Zarude","Bug Arceus",
            "Electric Silvally","Fighting Silvally","Electric Arceus",
            "Fighting Arceus","Dragon Silvally","Psychic Arceus","Flying Arceus",
            "Ground Arceus","Poison Arceus","Dark Silvally","Fire Silvally",
            "Ghost Arceus","Grass Arceus","Steel Arceus","Water Arceus",
            "Fairy Arceus","Bug Silvally","Fire Arceus","Rock Arceus","Ice Arceus",
            "High-speed Flight Configuration Genesect","Psychic Silvally",
            "Zenith Marshadow","Flying Silvally","Ground Silvally","Poison Silvally",
            "Ghost Silvally","Grass Silvally","Steel Silvally","Water Silvally",
            "Fairy Silvally","Rock Silvally","Zygarde Cell","Zygarde Core",
            "Ice Silvally",
        },
    },
    {
        "name": "Regional Pokémon",
        "key": "regionals",
        "aliases": ["regional", "reg", "regs"],
        "pokemon": {
            "Galarian Zen Darmanitan","Galarian Farfetch'd","Combat Breed Tauros",
            "Galarian Darmanitan","Blaze Breed Tauros","Hisuian Typhlosion",
            "Galarian Zigzagoon","Hisuian Growlithe","Galarian Rapidash",
            "Galarian Slowpoke","Hisuian Electrode","Galarian Mr. Mime",
            "Aqua Breed Tauros","Galarian Articuno","Galarian Slowking",
            "Hisuian Lilligant","Galarian Darumaka","Galarian Stunfisk",
            "Hisuian Decidueye","Alolan Sandshrew","Alolan Sandslash",
            "Alolan Ninetales","Hisuian Arcanine","Galarian Slowbro",
            "Alolan Exeggutor","Galarian Weezing","Galarian Moltres",
            "Hisuian Qwilfish","Galarian Corsola","Galarian Linoone",
            "Hisuian Samurott","Hisuian Braviary","Alolan Raticate",
            "Galarian Meowth","Alolan Graveler","Galarian Ponyta","Hisuian Voltorb",
            "Galarian Zapdos","Hisuian Sneasel","Galarian Yamask","Hisuian Zoroark",
            "Hisuian Sliggoo","Hisuian Avalugg","Alolan Rattata","Alolan Diglett",
            "Alolan Dugtrio","Alolan Persian","Alolan Geodude","Alolan Marowak",
            "Paldean Wooper","Hisuian Goodra","Alolan Raichu","Alolan Vulpix",
            "Alolan Meowth","Alolan Grimer","Hisuian Zorua","Alolan Golem",
            "Alolan Muk",
        },
    },
    {
        "name": "Event Pokémon",
        "key": "event",
        "aliases": ["ep", "ev", "emons"],
        "pokemon": {
            "Cicada Vikavolt","Swarming Ledyba",
        },
    },
]

# ── Lookup helpers ────────────────────────────────────────────────────────────

def get_category(key_or_alias: str) -> dict | None:
    """Return a category dict by its key or any alias (case-insensitive)."""
    needle = key_or_alias.lower()
    for cat in CATEGORIES:
        if cat["key"] == needle or needle in [a.lower() for a in cat["aliases"]]:
            return cat
    return None


def get_category_for_pokemon(pokemon_name: str) -> list[str]:
    """Return list of category keys that contain this Pokémon name."""
    result = []
    for cat in CATEGORIES:
        if pokemon_name in cat["pokemon"]:
            result.append(cat["key"])
    return result


def all_keys() -> list[str]:
    return [c["key"] for c in CATEGORIES]
