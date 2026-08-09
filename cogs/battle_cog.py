"""
cogs/battle_cog.py
───────────────────
Pokemon battling cog.

Data
----
Pokemon + move data is pulled live from PokeAPI (https://pokeapi.co) the
first time a given Pokemon/move is needed, then cached in MongoDB
(collections `pokedex_cache` and `move_cache`) so repeat battles don't
re-hit the API. Uses the same `db.get_db()` pattern as welcome_cog.py.

Commands (prefix, assumes bot already has a command_prefix like "!")
----------------------------------------------------------------------
!battle @user [format] [count]
    format : "random" (default) or "custom"
    count  : 1-6 pokemon per side (default 3)
    Posts a challenge with Accept / Decline buttons for the opponent.
    - random  → both teams are auto-rolled and the battle starts immediately
                on accept.
    - custom  → both trainers then build their own team with `!battle add`.

!battle add <name>, <name>, ...
    Add Pokemon (comma-separated, case-insensitive) to your team while a
    "custom" challenge is pending in the channel. Can be called multiple
    times. Once BOTH trainers have submitted `count` pokemon, the battle
    starts automatically.

!battle cancel
    Cancels a pending challenge/team-build, or force-ends an active battle
    in the current channel.

Battle flow / UI
-----------------
All Pokemon battle at LEVEL 100. Each round, ONE message is posted per
turn: a battle-scene image (both Pokemon with an HP bar rendered directly
above their sprite), the turn number, the *previous* turn's damage
results, and a single view containing:

    • a move dropdown for trainer 1
    • a move dropdown for trainer 2
    • a shared "🔄 Switch" button
    • a shared "⏭️ Pass Turn" button

Both dropdowns/buttons live on the same message (so the whole channel can
watch the battle), but each component checks `interaction.user` before
acting — a trainer can only ever submit their OWN action. Clicking Switch
opens a private (ephemeral) menu of your remaining team that only you can
see, so your bench isn't spoiled for your opponent while you're deciding.

Each trainer locks in exactly ONE action per turn: a move, a switch, or a
pass. Switching *consumes the whole turn* — a Pokemon that switches in
never also attacks that same turn. This is a deliberate fix for a bug in
the previous version where a switched-in Pokemon could end up executing a
move nobody selected; because switch and move are now mutually exclusive
per-turn actions resolved from a single locked-in dict (instead of two
separate sequential prompts), there's no code path left that can attack
with a Pokemon the trainer didn't choose to attack with. If a trainer
doesn't respond before the turn timer runs out, they no longer get a
*random* move — they auto-use their strongest available move instead
(moves are pre-sorted by power), and the panel says so.

How the 4 moves are chosen
---------------------------
`pick_moves()` fetches every damage-dealing (non-status) move in a
Pokemon's learnable move pool, sorts them by base power, and keeps the
top 4. So each Pokemon always has its four hardest-hitting moves
available — not a random sample.
"""

import asyncio
import io
import random
import sys
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    PIL_OK = True
except ImportError:
    PIL_OK = False
    print("[battle_cog] Pillow not installed — battle scene images disabled, "
          "falling back to text embeds.", file=sys.stderr)

import db as _db


def _col():
    return _db.get_db()


POKEAPI = "https://pokeapi.co/api/v2"
LEVEL = 100
TURN_TIMEOUT = 90  # seconds each turn's panel stays open

# ── Type effectiveness chart (attacking type -> {defending type: multiplier}) ──
# Only non-1.0 entries listed; anything missing defaults to 1.0.
TYPE_CHART = {
    "normal":   {"rock": 0.5, "ghost": 0.0, "steel": 0.5},
    "fire":     {"fire": 0.5, "water": 0.5, "grass": 2.0, "ice": 2.0, "bug": 2.0,
                 "rock": 0.5, "dragon": 0.5, "steel": 2.0},
    "water":    {"fire": 2.0, "water": 0.5, "grass": 0.5, "ground": 2.0,
                 "rock": 2.0, "dragon": 0.5},
    "electric": {"water": 2.0, "electric": 0.5, "grass": 0.5, "ground": 0.0,
                 "flying": 2.0, "dragon": 0.5},
    "grass":    {"fire": 0.5, "water": 2.0, "grass": 0.5, "poison": 0.5,
                 "ground": 2.0, "flying": 0.5, "bug": 0.5, "rock": 2.0,
                 "dragon": 0.5, "steel": 0.5},
    "ice":      {"fire": 0.5, "water": 0.5, "grass": 2.0, "ice": 0.5,
                 "ground": 2.0, "flying": 2.0, "dragon": 2.0, "steel": 0.5},
    "fighting": {"normal": 2.0, "ice": 2.0, "poison": 0.5, "flying": 0.5,
                 "psychic": 0.5, "bug": 0.5, "rock": 2.0, "ghost": 0.0,
                 "dark": 2.0, "steel": 2.0, "fairy": 0.5},
    "poison":   {"grass": 2.0, "poison": 0.5, "ground": 0.5, "rock": 0.5,
                 "ghost": 0.5, "steel": 0.0, "fairy": 2.0},
    "ground":   {"fire": 2.0, "electric": 2.0, "grass": 0.5, "poison": 2.0,
                 "flying": 0.0, "bug": 0.5, "rock": 2.0, "steel": 2.0},
    "flying":   {"electric": 0.5, "grass": 2.0, "fighting": 2.0, "bug": 2.0,
                 "rock": 0.5, "steel": 0.5},
    "psychic":  {"fighting": 2.0, "poison": 2.0, "psychic": 0.5, "dark": 0.0,
                 "steel": 0.5},
    "bug":      {"fire": 0.5, "grass": 2.0, "fighting": 0.5, "poison": 0.5,
                 "flying": 0.5, "psychic": 2.0, "ghost": 0.5, "dark": 2.0,
                 "steel": 0.5, "fairy": 0.5},
    "rock":     {"fire": 2.0, "ice": 2.0, "fighting": 0.5, "ground": 0.5,
                 "flying": 2.0, "bug": 2.0, "steel": 0.5},
    "ghost":    {"normal": 0.0, "psychic": 2.0, "ghost": 2.0, "dark": 0.5},
    "dragon":   {"dragon": 2.0, "steel": 0.5, "fairy": 0.0},
    "dark":     {"fighting": 0.5, "psychic": 2.0, "ghost": 2.0, "dark": 0.5,
                 "fairy": 0.5},
    "steel":    {"fire": 0.5, "water": 0.5, "electric": 0.5, "ice": 2.0,
                 "rock": 2.0, "steel": 0.5, "fairy": 2.0},
    "fairy":    {"fire": 0.5, "fighting": 2.0, "poison": 0.5, "dragon": 2.0,
                 "dark": 2.0, "steel": 0.5},
}

FALLBACK_MOVE = {
    "name": "struggle", "power": 50, "accuracy": 100, "pp": 1,
    "type": "normal", "damage_class": "physical", "priority": 0,
    "effect_chance": None, "stat_changes": [], "drain": 0,
    "target": "selected-pokemon",
}


# ── PokeAPI fetch + Mongo cache ─────────────────────────────────────────────

async def get_pokemon_data(session: aiohttp.ClientSession, ident: str) -> Optional[dict]:
    key = str(ident).lower().strip().replace(" ", "-")
    doc = await _col().pokedex_cache.find_one({"_id": key})
    if doc:
        return doc
    try:
        async with session.get(f"{POKEAPI}/pokemon/{key}", timeout=10) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None

    stats = {s["stat"]["name"]: s["base_stat"] for s in data["stats"]}
    types = [t["type"]["name"] for t in data["types"]]
    move_pool = [m["move"]["name"] for m in data["moves"]]
    sprite = (data.get("sprites") or {}).get("front_default") or ""

    doc = {
        "_id": key,
        "name": data["name"],
        "dex_id": data["id"],
        "types": types,
        "stats": stats,
        "move_pool": move_pool,
        "sprite": sprite,
    }
    await _col().pokedex_cache.update_one({"_id": key}, {"$set": doc}, upsert=True)
    return doc


async def get_move_data(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    key = name.lower().strip()
    doc = await _col().move_cache.find_one({"_id": key})
    if doc:
        return doc
    try:
        async with session.get(f"{POKEAPI}/move/{key}", timeout=10) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None

    doc = {
        "_id": key,
        "name": data["name"],
        "power": data.get("power"),
        "accuracy": data.get("accuracy"),
        "pp": data.get("pp"),
        "type": data["type"]["name"],
        "damage_class": data["damage_class"]["name"],
        "priority": data.get("priority", 0),
        # Secondary-effect data, used to apply stat drops/boosts and
        # recoil/drain on top of raw damage.
        "effect_chance": data.get("effect_chance"),
        "stat_changes": [
            {"stat": sc["stat"]["name"], "change": sc["change"]}
            for sc in (data.get("stat_changes") or [])
        ],
        "drain": (data.get("meta") or {}).get("drain", 0),
        "target": (data.get("target") or {}).get("name", "selected-pokemon"),
    }
    await _col().move_cache.update_one({"_id": key}, {"$set": doc}, upsert=True)
    return doc


PRIORITY_MOVE_MIN_POWER = 40  # e.g. Quick Attack/Aqua Jet/Mach Punch-tier or better


async def pick_moves(session: aiohttp.ClientSession, data: dict, count: int = 4) -> list:
    """Return the `count` best damage-dealing moves this Pokemon can learn
    (status moves excluded), deterministically — never a random sample.

    Priority is given to raw power, EXCEPT that if the Pokemon can learn a
    decent-power priority move (priority > 0, power >= PRIORITY_MOVE_MIN_POWER
    — e.g. Quick Attack, Aqua Jet, Mach Punch, Extreme Speed, Sucker Punch),
    the single best one of those is guaranteed a slot even if it wouldn't
    otherwise crack the top `count` by power alone. Priority moves are
    strategically important (they can strike first regardless of Speed), so
    a pure power sort would frequently throw them away.
    """
    pool = data.get("move_pool", [])
    if not pool:
        tackle = await get_move_data(session, "tackle")
        return [tackle] if tackle else [FALLBACK_MOVE]

    results = await asyncio.gather(
        *[get_move_data(session, name) for name in pool],
        return_exceptions=True,
    )
    candidates = [
        mv for mv in results
        if isinstance(mv, dict) and mv.get("power") and mv.get("damage_class") != "status"
    ]
    if not candidates:
        tackle = await get_move_data(session, "tackle")
        return [tackle] if tackle else [FALLBACK_MOVE]

    candidates.sort(key=lambda m: m.get("power") or 0, reverse=True)

    chosen = []
    priority_candidates = [
        m for m in candidates
        if m.get("priority", 0) > 0 and (m.get("power") or 0) >= PRIORITY_MOVE_MIN_POWER
    ]
    if priority_candidates:
        best_priority = max(priority_candidates, key=lambda m: m.get("power") or 0)
        chosen.append(best_priority)

    for mv in candidates:
        if len(chosen) >= count:
            break
        if mv in chosen:
            continue
        chosen.append(mv)

    return chosen[:count]


async def build_random_pokemon(session: aiohttp.ClientSession) -> "BattlePokemon":
    for _ in range(6):
        dex_id = random.randint(1, 1025)
        data = await get_pokemon_data(session, str(dex_id))
        if data:
            moves = await pick_moves(session, data)
            return BattlePokemon(data, moves)
    data = await get_pokemon_data(session, "pikachu")
    moves = await pick_moves(session, data)
    return BattlePokemon(data, moves)


# ── Battle math ──────────────────────────────────────────────────────────────

def _calc_stat(base: int, is_hp: bool = False, level: int = LEVEL) -> int:
    if is_hp:
        return int(((2 * base + 31) * level) / 100) + level + 10
    return int(((2 * base + 31) * level) / 100) + 5


def type_multiplier(move_type: str, defender_types: list) -> float:
    mult = 1.0
    for t in defender_types:
        mult *= TYPE_CHART.get(move_type, {}).get(t, 1.0)
    return mult


# Maps PokeAPI stat names -> the short attribute names BattlePokemon uses.
# (accuracy/evasion aren't modeled as separate battle stats here, so
# stat-changing effects that target those are simply ignored.)
STAT_KEY_MAP = {
    "attack": "atk", "defense": "dfn",
    "special-attack": "spa", "special-defense": "spd",
    "speed": "spe",
}
STAT_DISPLAY = {"atk": "Attack", "dfn": "Defense", "spa": "Sp. Atk",
                "spd": "Sp. Def", "spe": "Speed"}

SELF_KO_MOVES = {"explosion", "self-destruct"}


def _stage_multiplier(stage: int) -> float:
    stage = max(-6, min(6, stage))
    if stage >= 0:
        return (2 + stage) / 2
    return 2 / (2 - stage)


class BattlePokemon:
    def __init__(self, data: dict, moves: list):
        self.name = data["name"]
        self.types = data["types"]
        self.sprite = data.get("sprite")
        s = data.get("stats", {})
        self.max_hp = _calc_stat(s.get("hp", 50), is_hp=True)
        self.hp = self.max_hp
        self.atk = _calc_stat(s.get("attack", 50))
        self.dfn = _calc_stat(s.get("defense", 50))
        self.spa = _calc_stat(s.get("special-attack", 50))
        self.spd = _calc_stat(s.get("special-defense", 50))
        self.spe = _calc_stat(s.get("speed", 50))
        self.moves = moves or [FALLBACK_MOVE]
        # Battle-only stat boosts/drops (-6..+6 stages), reset per battle —
        # these are what stat-lowering/raising secondary effects modify.
        self.stat_stages = {"atk": 0, "dfn": 0, "spa": 0, "spd": 0, "spe": 0}

    @property
    def fainted(self) -> bool:
        return self.hp <= 0

    def effective_stat(self, key: str) -> float:
        base = getattr(self, key)
        return base * _stage_multiplier(self.stat_stages.get(key, 0))


def calc_damage(attacker: BattlePokemon, defender: BattlePokemon, move: dict):
    power = move.get("power") or 0
    if power <= 0:
        return 0, 1.0, False
    if move.get("damage_class") == "physical":
        a, d = attacker.effective_stat("atk"), defender.effective_stat("dfn")
    else:
        a, d = attacker.effective_stat("spa"), defender.effective_stat("spd")
    stab = 1.5 if move.get("type") in attacker.types else 1.0
    eff = type_multiplier(move.get("type", "normal"), defender.types)
    crit = random.random() < 0.0625
    crit_mult = 1.5 if crit else 1.0
    rand = random.uniform(0.85, 1.0)
    base = (((2 * LEVEL / 5 + 2) * power * a / max(d, 1)) / 50 + 2)
    dmg = int(base * stab * eff * crit_mult * rand)
    return max(dmg, 1), eff, crit


def _apply_secondary_effects(attacker: BattlePokemon, defender: BattlePokemon, move: dict) -> list:
    """Applies a move's secondary stat-change effect (e.g. Acid lowering Sp.
    Def, Superpower lowering the user's own Atk/Def), if any, and returns
    flavor-text lines describing what happened. Not every move has one —
    only moves with a `stat_changes` entry in their PokeAPI data do."""
    stat_changes = move.get("stat_changes") or []
    if not stat_changes:
        return []

    chance = move.get("effect_chance")
    if chance is not None and random.uniform(0, 100) > chance:
        return []  # secondary effect didn't proc this time

    target_self = move.get("target") == "user"
    target = attacker if target_self else defender

    lines = []
    for sc in stat_changes:
        key = STAT_KEY_MAP.get(sc.get("stat"))
        if not key:
            continue  # accuracy/evasion changes aren't modeled
        delta = sc.get("change", 0)
        old = target.stat_stages.get(key, 0)
        new = max(-6, min(6, old + delta))
        target.stat_stages[key] = new
        if new == old:
            continue
        verb = "rose" if delta > 0 else "fell"
        emphasis = "sharply " if abs(delta) >= 2 else ""
        lines.append(f"📉 {target.name.title()}'s {STAT_DISPLAY[key]} {emphasis}{verb}!"
                      if delta < 0 else
                      f"📈 {target.name.title()}'s {STAT_DISPLAY[key]} {emphasis}{verb}!")
    return lines


def _apply_drain_recoil(attacker: BattlePokemon, dmg: int, move: dict) -> Optional[str]:
    """Applies HP-drain (e.g. Giga Drain, positive %) or recoil (e.g. Flare
    Blitz/Double-Edge/Brave Bird, negative %) based on the move's `drain`
    percentage, and returns a flavor-text line, or None if the move has
    neither."""
    drain = move.get("drain") or 0
    if not drain or dmg <= 0:
        return None
    amount = max(1, int(abs(dmg) * abs(drain) / 100))
    if drain > 0:
        healed = min(amount, attacker.max_hp - attacker.hp)
        if healed <= 0:
            return None
        attacker.hp += healed
        return f"🩸 {attacker.name.title()} drained **{healed}** HP!"
    else:
        attacker.hp = max(0, attacker.hp - amount)
        return f"💢 {attacker.name.title()} is hit by recoil! (**{amount}** dmg)"


# ── Battle scene image rendering ────────────────────────────────────────────

CANVAS_W, CANVAS_H = 720, 380
SKY_COLOR = (135, 206, 235, 255)
GROUND_COLOR = (150, 210, 90, 255)
SPRITE_SCALE = 3

_FONT_CACHE: dict = {}


def _font(size: int, bold: bool = True):
    if not PIL_OK:
        return None
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for name in (("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
                 ("Arial Bold.ttf" if bold else "Arial.ttf")):
        try:
            f = ImageFont.truetype(name, size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            continue
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _hp_color(frac: float):
    if frac > 0.5:
        return (60, 200, 70, 255)
    if frac > 0.2:
        return (240, 190, 40, 255)
    return (220, 60, 60, 255)


async def _fetch_sprite(session: aiohttp.ClientSession, url: str):
    if not url or not PIL_OK:
        return None
    try:
        async with session.get(url, timeout=10) as r:
            if r.status != 200:
                return None
            raw = await r.read()
        return Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None


def _draw_hp_bar_above(draw: "ImageDraw.ImageDraw", center_x: float, sprite_top_y: float,
                        name: str, hp: int, max_hp: int, width: int = 190):
    """Draws a compact name+HP-bar plate directly above a sprite's position."""
    bar_h = 10
    plate_h = 40
    x = int(center_x - width / 2)
    x = max(6, min(x, CANVAS_W - width - 6))
    y = int(max(4, sprite_top_y - plate_h - 6))

    draw.rounded_rectangle([x, y, x + width, y + plate_h], radius=9,
                            fill=(255, 255, 255, 235), outline=(40, 40, 40, 255), width=2)
    draw.text((x + 10, y + 4), f"{name.title()}  Lv.{LEVEL}",
               font=_font(14), fill=(20, 20, 20, 255))

    bar_x, bar_y, bar_w = x + 10, y + 22, width - 20
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                            radius=5, fill=(90, 90, 90, 255))
    frac = max(hp, 0) / max_hp if max_hp else 0
    fill_w = int(bar_w * frac)
    if fill_w > 0:
        draw.rounded_rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h],
                                radius=5, fill=_hp_color(frac))


async def render_battle_scene(session: aiohttp.ClientSession,
                               opponent_pokemon: "BattlePokemon",
                               player_pokemon: "BattlePokemon") -> Optional[discord.File]:
    """Classic side-on battle scene: opponent upper-right facing player,
    player's pokemon lower-left (mirrored) facing opponent, with an HP bar
    rendered directly above each sprite (name, Lv.100, colour-shifting bar)."""
    if not PIL_OK:
        return None

    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), SKY_COLOR)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, CANVAS_H - 120, CANVAS_W, CANVAS_H], fill=GROUND_COLOR)
    draw.ellipse([CANVAS_W * 0.55 - 150, CANVAS_H - 150, CANVAS_W * 0.55 + 150, CANVAS_H - 90],
                 fill=(130, 190, 75, 255))
    draw.ellipse([CANVAS_W * 0.14 - 120, CANVAS_H - 95, CANVAS_W * 0.14 + 120, CANVAS_H - 45],
                 fill=(130, 190, 75, 255))

    opp_sprite = await _fetch_sprite(session, opponent_pokemon.sprite)
    player_sprite = await _fetch_sprite(session, player_pokemon.sprite)

    opp_pos = (int(CANVAS_W * 0.60), 68)
    player_pos = (int(CANVAS_W * 0.06), CANVAS_H - 232)

    opp_w = 96 * SPRITE_SCALE
    if opp_sprite:
        opp_sprite = opp_sprite.resize(
            (opp_sprite.width * SPRITE_SCALE, opp_sprite.height * SPRITE_SCALE), Image.NEAREST)
        canvas.alpha_composite(opp_sprite, opp_pos)
        opp_w = opp_sprite.width

    player_w = 96 * SPRITE_SCALE
    if player_sprite:
        player_sprite = ImageOps.mirror(player_sprite)
        player_sprite = player_sprite.resize(
            (player_sprite.width * SPRITE_SCALE, player_sprite.height * SPRITE_SCALE), Image.NEAREST)
        canvas.alpha_composite(player_sprite, player_pos)
        player_w = player_sprite.width

    _draw_hp_bar_above(draw, opp_pos[0] + opp_w / 2, opp_pos[1],
                        opponent_pokemon.name, opponent_pokemon.hp, opponent_pokemon.max_hp)
    _draw_hp_bar_above(draw, player_pos[0] + player_w / 2, player_pos[1],
                        player_pokemon.name, player_pokemon.hp, player_pokemon.max_hp)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="battle.png")


# ── Trainer / pending challenge ─────────────────────────────────────────────

@dataclass
class Trainer:
    user: discord.abc.User
    team: list = field(default_factory=list)
    active_idx: int = 0

    @property
    def active(self) -> BattlePokemon:
        return self.team[self.active_idx]

    @property
    def alive_team(self) -> list:
        return [p for p in self.team if not p.fainted]


@dataclass
class PendingChallenge:
    challenger: discord.Member
    opponent: discord.Member
    fmt: str
    count: int
    accepted: bool = False
    teams: dict = field(default_factory=dict)


# ── UI: challenge accept/decline ────────────────────────────────────────────

class ChallengeView(discord.ui.View):
    def __init__(self, cog: "BattleCog", challenger: discord.Member,
                 opponent: discord.Member, fmt: str, count: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.challenger = challenger
        self.opponent = opponent
        self.fmt = fmt
        self.count = count

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message(
                "This challenge isn't addressed to you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        self.cog.pending.pop(getattr(self, "_channel_id", None), None)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="⚔️")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            content=f"✅ {self.opponent.mention} accepted the challenge!", view=None)
        await self.cog.start_challenge(interaction.channel, self.challenger,
                                        self.opponent, self.fmt, self.count)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="🚫")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        self.cog.pending.pop(interaction.channel.id, None)
        await interaction.response.edit_message(
            content=f"❌ {self.opponent.mention} declined the challenge.", view=None)


# ── UI: forced switch after a faint (visible message, restricted to owner) ──

class ForcedSwitchButton(discord.ui.Button):
    def __init__(self, label: str, idx: int):
        super().__init__(label=label[:80], style=discord.ButtonStyle.secondary)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        view: "ForcedSwitchView" = self.view
        for item in view.children:
            item.disabled = True
        await interaction.response.edit_message(view=view)
        if not view.future.done():
            view.future.set_result(self.idx)
        view.stop()


class ForcedSwitchView(discord.ui.View):
    """Shown publicly (mentioning the trainer) when their active Pokemon
    faints mid-turn and they must send out a replacement. Visible to
    everyone, but only the owning trainer can press a button."""

    def __init__(self, trainer: Trainer, future: asyncio.Future):
        super().__init__(timeout=60)
        self.trainer = trainer
        self.future = future
        self._alive_indices = []
        for i, p in enumerate(trainer.team):
            if not p.fainted:
                self._alive_indices.append(i)
                self.add_item(ForcedSwitchButton(p.name.title(), i))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.trainer.user.id:
            await interaction.response.send_message("Not your Pokemon to switch.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if not self.future.done() and self._alive_indices:
            self.future.set_result(self._alive_indices[0])


# ── UI: private (ephemeral) switch menu, opened from the turn panel ────────

class SwitchSelectView(discord.ui.View):
    """Sent as an ephemeral response when a trainer presses the panel's
    Switch button — only that trainer ever sees this menu, so their bench
    isn't revealed to the opponent while they decide."""

    def __init__(self, panel: "BattlePanel", trainer: Trainer, bench: list):
        super().__init__(timeout=TURN_TIMEOUT)
        self.panel = panel
        self.trainer = trainer
        select = discord.ui.Select(
            placeholder="Choose your next Pokémon",
            options=[
                discord.SelectOption(
                    label=f"{p.name.title()} (Lv.{LEVEL})",
                    description=f"{p.hp}/{p.max_hp} HP",
                    value=str(i),
                )
                for i, p in bench
            ],
        )
        select.callback = self._callback
        self.add_item(select)
        self._select = select

    async def _callback(self, interaction: discord.Interaction):
        idx = int(self._select.values[0])
        for item in self.children:
            item.disabled = True
        mon = self.trainer.team[idx].name.title()
        await interaction.response.edit_message(
            content=f"✅ You'll send out **{mon}** this turn (this uses your whole turn).",
            view=self,
        )
        await self.panel.set_action(interaction, self.trainer, ("switch", idx),
                                     via_separate_message=True)
        self.stop()


# ── UI: the single combined turn panel ──────────────────────────────────────

class MoveSelect(discord.ui.Select):
    def __init__(self, trainer: Trainer, panel: "BattlePanel", row: int):
        self.trainer = trainer
        self.panel = panel
        options = []
        for i, mv in enumerate(trainer.active.moves):
            tag = "⚡Priority • " if mv.get("priority", 0) > 0 else ""
            desc = f"{tag}{mv.get('type', 'normal').title()} • {mv.get('power') or '—'} power"
            options.append(discord.SelectOption(
                label=mv["name"].replace("-", " ").title()[:100],
                description=desc[:100],
                value=str(i),
            ))
        super().__init__(
            placeholder=f"{trainer.user.display_name}: choose {trainer.active.name.title()}'s move",
            min_values=1, max_values=1, options=options, row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.trainer.user.id:
            await interaction.response.send_message(
                "🚫 That's not your Pokémon to command.", ephemeral=True)
            return
        if self.trainer.user.id in self.panel.actions:
            await interaction.response.send_message(
                "You've already locked in your action this turn.", ephemeral=True)
            return
        idx = int(self.values[0])
        await self.panel.set_action(interaction, self.trainer, ("move", idx))


class SwitchButton(discord.ui.Button):
    def __init__(self, panel: "BattlePanel"):
        super().__init__(label="Switch", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        trainer = self.panel.trainer_for(interaction.user.id)
        if trainer is None:
            await interaction.response.send_message("You're not part of this battle.", ephemeral=True)
            return
        if trainer.user.id in self.panel.actions:
            await interaction.response.send_message(
                "You've already locked in your action this turn.", ephemeral=True)
            return
        bench = [(i, p) for i, p in enumerate(trainer.team)
                 if not p.fainted and i != trainer.active_idx]
        if not bench:
            await interaction.response.send_message(
                "You have no other healthy Pokémon to switch to!", ephemeral=True)
            return
        await interaction.response.send_message(
            "Choose a Pokémon to switch in — only you can see this menu:",
            view=SwitchSelectView(self.panel, trainer, bench),
            ephemeral=True,
        )


class PassButton(discord.ui.Button):
    def __init__(self, panel: "BattlePanel"):
        super().__init__(label="Pass Turn", emoji="⏭️", style=discord.ButtonStyle.secondary, row=2)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        trainer = self.panel.trainer_for(interaction.user.id)
        if trainer is None:
            await interaction.response.send_message("You're not part of this battle.", ephemeral=True)
            return
        if trainer.user.id in self.panel.actions:
            await interaction.response.send_message(
                "You've already locked in your action this turn.", ephemeral=True)
            return
        await self.panel.set_action(interaction, trainer, ("pass", None))


class BattlePanel(discord.ui.View):
    """The one combined view posted each turn: two move dropdowns (one per
    trainer), a shared Switch button, and a shared Pass Turn button.

    Each trainer locks in exactly one action per turn, stored in
    `self.actions[user_id] = (kind, value)` where kind is "move", "switch",
    or "pass". Switch and move are mutually exclusive for a given trainer
    in a given turn — a switched-in Pokemon is never also made to attack,
    which is what previously caused the "random move on switch" bug.
    """

    def __init__(self, t1: Trainer, t2: Trainer):
        super().__init__(timeout=TURN_TIMEOUT)
        self.t1 = t1
        self.t2 = t2
        self.actions: dict = {}
        self.event = asyncio.Event()
        self.message: Optional[discord.Message] = None

        self.move_select_t1 = MoveSelect(t1, self, row=0)
        self.move_select_t2 = MoveSelect(t2, self, row=1)
        self.add_item(self.move_select_t1)
        self.add_item(self.move_select_t2)
        self.add_item(SwitchButton(self))
        self.add_item(PassButton(self))

    def trainer_for(self, user_id: int) -> Optional[Trainer]:
        if user_id == self.t1.user.id:
            return self.t1
        if user_id == self.t2.user.id:
            return self.t2
        return None

    def _select_for(self, trainer: Trainer) -> MoveSelect:
        return self.move_select_t1 if trainer is self.t1 else self.move_select_t2

    async def set_action(self, interaction: discord.Interaction, trainer: Trainer,
                          action: tuple, via_separate_message: bool = False):
        self.actions[trainer.user.id] = action
        verb = {"move": "chose a move", "switch": "will switch", "pass": "passed"}[action[0]]
        sel = self._select_for(trainer)
        sel.disabled = True
        sel.placeholder = f"{trainer.user.display_name} {verb} ✅"

        ready = len(self.actions) == 2
        if ready:
            for item in self.children:
                item.disabled = True

        if via_separate_message:
            # This action came from the private ephemeral switch menu, not
            # from a component on the panel message itself — edit the panel
            # message directly instead of trying to ack this interaction.
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass
        else:
            await interaction.response.edit_message(view=self)

        if ready and not self.event.is_set():
            self.event.set()

    async def on_timeout(self):
        # Fill in any missing action with that trainer's strongest move
        # (moves are pre-sorted by power) instead of a random one.
        for trainer in (self.t1, self.t2):
            if trainer.user.id not in self.actions:
                self.actions[trainer.user.id] = ("move", 0)
                sel = self._select_for(trainer)
                sel.disabled = True
                sel.placeholder = f"{trainer.user.display_name} ran out of time — auto-used strongest move"
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        if not self.event.is_set():
            self.event.set()


# ── Battle runner ────────────────────────────────────────────────────────────

class Battle:
    def __init__(self, cog: "BattleCog", channel: discord.TextChannel,
                 t1: Trainer, t2: Trainer):
        self.cog = cog
        self.channel = channel
        self.t1 = t1
        self.t2 = t2

    async def build_embed(self, turn: int, last_summary: Optional[str],
                           final: bool = False, winner: Optional[Trainer] = None):
        file = await render_battle_scene(self.cog.session, self.t2.active, self.t1.active)

        embed = discord.Embed(
            title=("🏆 Battle Complete" if final else f"⚔️ Turn {turn}"),
            colour=(0xF1C40F if final else 0x3498DB),
        )
        if winner is not None:
            embed.description = f"**{winner.user.display_name} wins the battle!**"
        if last_summary:
            embed.add_field(name="📋 Last Turn's Results", value=last_summary[:1024], inline=False)

        for t in (self.t1, self.t2):
            p = t.active
            embed.add_field(
                name=t.user.display_name,
                value=(f"{p.name.title()} (Lv.{LEVEL})\n"
                       f"❤️ {p.hp}/{p.max_hp} HP\n"
                       f"Team remaining: {len(t.alive_team)}/{len(t.team)}"),
                inline=True,
            )

        if file is not None:
            embed.set_image(url="attachment://battle.png")
        else:
            embed.add_field(name="⚠️ Note", value="Pillow isn't installed — image disabled.",
                             inline=False)
        return embed, file

    async def _send_embed(self, embed: discord.Embed, file: Optional[discord.File], **kwargs):
        if file is not None:
            return await self.channel.send(embed=embed, file=file, **kwargs)
        return await self.channel.send(embed=embed, **kwargs)

    def _execute_move(self, attacker: Trainer, defender: Trainer, move: dict) -> list:
        """Resolves one move: accuracy check, damage, secondary stat
        effects, recoil/drain, and self-KO moves. Returns a list of
        flavor-text lines (usually 1-3) describing everything that
        happened."""
        move_name = move["name"].replace("-", " ").title()

        acc = move.get("accuracy")
        if acc is not None and random.uniform(0, 100) > acc:
            return [f"❌ {attacker.active.name.title()}'s {move_name} missed!"]

        dmg, eff, crit = calc_damage(attacker.active, defender.active, move)
        defender.active.hp = max(0, defender.active.hp - dmg)

        text = f"➡️ {attacker.active.name.title()} used **{move_name}**! (**{dmg}** dmg)"
        if crit:
            text += " 💫 Critical hit!"
        if eff > 1:
            text += " It's super effective!"
        elif 0 < eff < 1:
            text += " It's not very effective..."
        elif eff == 0:
            text += " It had no effect!"
        lines = [text]

        # Self-KO moves: Explosion / Self-Destruct faint the user outright.
        if move["name"] in SELF_KO_MOVES:
            attacker.active.hp = 0
            lines.append(f"💥 {attacker.active.name.title()} was consumed by the blast!")
        else:
            recoil_msg = _apply_drain_recoil(attacker.active, dmg, move)
            if recoil_msg:
                lines.append(recoil_msg)

        lines.extend(_apply_secondary_effects(attacker.active, defender.active, move))
        return lines

    async def get_forced_switch(self, trainer: Trainer) -> int:
        future = asyncio.get_event_loop().create_future()
        view = ForcedSwitchView(trainer, future)
        await self.channel.send(
            f"{trainer.user.mention}, **{trainer.active.name.title()}** fainted! "
            f"Choose your next Pokemon:",
            view=view,
        )
        return await future

    async def run(self):
        await self.channel.send(
            f"⚔️ **Battle start!** {self.t1.user.mention} vs {self.t2.user.mention} "
            f"— all Pokémon are Level {LEVEL}."
        )

        turn = 1
        last_summary: Optional[str] = None

        while self.t1.alive_team and self.t2.alive_team:
            panel = BattlePanel(self.t1, self.t2)
            embed, file = await self.build_embed(turn, last_summary)
            msg = await self._send_embed(
                embed, file,
                content=f"{self.t1.user.mention} {self.t2.user.mention} — choose your action.",
                view=panel,
            )
            panel.message = msg

            await panel.event.wait()

            lines: list = []

            # 1) Switches resolve first and consume the whole turn for that
            #    trainer — a switched-in Pokemon never also attacks.
            for trainer in (self.t1, self.t2):
                action = panel.actions.get(trainer.user.id)
                if action and action[0] == "switch":
                    old_name = trainer.active.name.title()
                    trainer.active_idx = action[1]
                    lines.append(
                        f"🔄 {trainer.user.display_name} withdrew {old_name} and sent out "
                        f"**{trainer.active.name.title()}**!"
                    )
                elif action and action[0] == "pass":
                    lines.append(f"⏭️ {trainer.user.display_name} passed the turn.")

            # 2) Moves resolve in priority/speed order. Only trainers whose
            #    locked-in action was "move" attack this turn.
            movers = []
            for trainer, opponent in ((self.t1, self.t2), (self.t2, self.t1)):
                action = panel.actions.get(trainer.user.id)
                if action and action[0] == "move":
                    move = trainer.active.moves[action[1]]
                    movers.append((trainer, opponent, move))
            # Priority moves always go first; ties within the same priority
            # bracket go to the faster Pokemon (using effective, stage-
            # boosted Speed); true speed ties are broken randomly each turn
            # rather than always favoring the same trainer. Whichever
            # Pokemon moves first and knocks out the other's active Pokemon
            # denies it a turn entirely — the slower Pokemon's move is
            # skipped if it's already fainted by the time its turn comes up.
            movers.sort(
                key=lambda o: (o[2].get("priority", 0), o[0].active.effective_stat("spe"), random.random()),
                reverse=True,
            )

            for attacker, defender, move in movers:
                if attacker.active.fainted or defender.active.fainted:
                    continue
                move_name = move["name"]
                lines.extend(self._execute_move(attacker, defender, move))

                if attacker.active.fainted and move_name not in SELF_KO_MOVES:
                    # Fainted from its own recoil.
                    lines.append(f"💥 {attacker.active.name.title()} fainted from recoil!")
                if attacker.active.fainted:
                    if attacker.alive_team:
                        new_idx = await self.get_forced_switch(attacker)
                        attacker.active_idx = new_idx
                        lines.append(
                            f"{attacker.user.display_name} sent out "
                            f"**{attacker.active.name.title()}**!"
                        )

                if defender.active.fainted:
                    lines.append(f"💥 {defender.active.name.title()} fainted!")
                    if defender.alive_team:
                        new_idx = await self.get_forced_switch(defender)
                        defender.active_idx = new_idx
                        lines.append(
                            f"{defender.user.display_name} sent out "
                            f"**{defender.active.name.title()}**!"
                        )

            last_summary = "\n".join(lines) if lines else "No actions were taken."
            turn += 1

        winner = self.t1 if self.t1.alive_team else self.t2
        loser = self.t2 if winner is self.t1 else self.t1

        final_embed, final_file = await self.build_embed(turn, last_summary,
                                                           final=True, winner=winner)
        await self._send_embed(final_embed, final_file)
        await self.channel.send(
            f"🏆 **{winner.user.display_name} wins the battle!** GG {loser.user.mention}."
        )
        self.cog.active_battles.pop(self.channel.id, None)


# ── Cog ───────────────────────────────────────────────────────────────────

class BattleCog(commands.Cog, name="Battle"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.pending: dict = {}          # channel_id -> PendingChallenge
        self.active_battles: dict = {}   # channel_id -> Battle

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    @commands.group(name="battle", invoke_without_command=True)
    async def battle(self, ctx: commands.Context,
                      opponent: Optional[discord.Member] = None,
                      fmt: str = "random", count: int = 3):
        if opponent is None:
            await ctx.send("Usage: `!battle @user [random|custom] [count 1-6]`")
            return
        if opponent.bot or opponent.id == ctx.author.id:
            await ctx.send("Pick a real opponent (not yourself or a bot).")
            return
        if ctx.channel.id in self.pending or ctx.channel.id in self.active_battles:
            await ctx.send(
                "There's already a pending challenge or active battle in this "
                "channel. Finish it or run `!battle cancel` first."
            )
            return

        fmt = fmt.lower()
        if fmt not in ("random", "custom"):
            await ctx.send("Format must be `random` or `custom`.")
            return
        count = max(1, min(6, count))

        self.pending[ctx.channel.id] = PendingChallenge(ctx.author, opponent, fmt, count)

        view = ChallengeView(self, ctx.author, opponent, fmt, count)
        view._channel_id = ctx.channel.id
        await ctx.send(
            f"⚔️ {ctx.author.mention} has challenged {opponent.mention} to a "
            f"**{fmt}** battle ({count} pokemon each, Level {LEVEL})! "
            f"{opponent.mention}, do you accept?",
            view=view,
        )

    async def start_challenge(self, channel, challenger, opponent, fmt, count):
        pending = self.pending.get(channel.id)
        if not pending:
            return

        if fmt == "random":
            await channel.send("🎲 Rolling random teams...")
            t1, t2 = Trainer(challenger), Trainer(opponent)
            for _ in range(count):
                t1.team.append(await build_random_pokemon(self.session))
                t2.team.append(await build_random_pokemon(self.session))
            self.pending.pop(channel.id, None)
            battle = Battle(self, channel, t1, t2)
            self.active_battles[channel.id] = battle
            await battle.run()
        else:
            pending.accepted = True
            await channel.send(
                f"📋 Custom battle! Both trainers build your team with:\n"
                f"`!battle add pikachu, charizard, ...` (up to {count} each)\n"
                f"{challenger.mention} and {opponent.mention}, go ahead."
            )

    @battle.command(name="add")
    async def battle_add(self, ctx: commands.Context, *, names: str):
        pending = self.pending.get(ctx.channel.id)
        if not pending or not pending.accepted or pending.fmt != "custom":
            await ctx.send("There's no pending custom battle here to add Pokemon to.")
            return
        if ctx.author.id not in (pending.challenger.id, pending.opponent.id):
            await ctx.send("You're not part of this battle.")
            return

        team = pending.teams.setdefault(ctx.author.id, [])
        if len(team) >= pending.count:
            await ctx.send(f"You already have your full team of {pending.count}.")
            return

        requested = [n.strip() for n in names.split(",") if n.strip()]
        added, failed = [], []
        async with ctx.typing():
            for nm in requested:
                if len(team) >= pending.count:
                    failed.append(f"{nm} (team already full)")
                    continue
                data = await get_pokemon_data(self.session, nm)
                if not data:
                    failed.append(f"{nm} (not found)")
                    continue
                moves = await pick_moves(self.session, data)
                team.append(BattlePokemon(data, moves))
                added.append(data["name"].title())

        msg = ""
        if added:
            msg += f"✅ Added: {', '.join(added)} ({len(team)}/{pending.count})\n"
        if failed:
            msg += f"⚠️ Skipped: {', '.join(failed)}"
        await ctx.send(msg or "Nothing added.")

        challenger_team = pending.teams.get(pending.challenger.id, [])
        opponent_team = pending.teams.get(pending.opponent.id, [])
        if len(challenger_team) >= pending.count and len(opponent_team) >= pending.count:
            self.pending.pop(ctx.channel.id, None)
            t1 = Trainer(pending.challenger, team=challenger_team)
            t2 = Trainer(pending.opponent, team=opponent_team)
            battle = Battle(self, ctx.channel, t1, t2)
            self.active_battles[ctx.channel.id] = battle
            await ctx.send("Both teams are ready — battle starting!")
            await battle.run()

    @battle.command(name="cancel")
    async def battle_cancel(self, ctx: commands.Context):
        if ctx.channel.id in self.pending:
            del self.pending[ctx.channel.id]
            await ctx.send("Challenge cancelled.")
        elif ctx.channel.id in self.active_battles:
            del self.active_battles[ctx.channel.id]
            await ctx.send("Battle force-ended.")
        else:
            await ctx.send("Nothing to cancel here.")


async def setup(bot: commands.Bot):
    await bot.add_cog(BattleCog(bot))
