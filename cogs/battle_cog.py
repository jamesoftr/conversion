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

During a battle, each round both trainers get a move-select View (60s to
choose, otherwise a random move is auto-picked). A fainted Pokemon triggers
a switch-select View for its owner. Only one pending challenge / battle is
allowed per channel at a time.
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
LEVEL = 50

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
    }
    await _col().move_cache.update_one({"_id": key}, {"$set": doc}, upsert=True)
    return doc


async def pick_moves(session: aiohttp.ClientSession, data: dict, count: int = 4) -> list:
    pool = data.get("move_pool", [])[:]
    random.shuffle(pool)
    chosen = []
    for name in pool:
        if len(chosen) >= count:
            break
        mv = await get_move_data(session, name)
        if mv and mv.get("power") and mv.get("damage_class") != "status":
            chosen.append(mv)
    if not chosen:
        tackle = await get_move_data(session, "tackle")
        chosen = [tackle] if tackle else [FALLBACK_MOVE]
    return chosen


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

    @property
    def fainted(self) -> bool:
        return self.hp <= 0


def calc_damage(attacker: BattlePokemon, defender: BattlePokemon, move: dict):
    power = move.get("power") or 0
    if power <= 0:
        return 0, 1.0, False
    if move.get("damage_class") == "physical":
        a, d = attacker.atk, defender.dfn
    else:
        a, d = attacker.spa, defender.spd
    stab = 1.5 if move.get("type") in attacker.types else 1.0
    eff = type_multiplier(move.get("type", "normal"), defender.types)
    crit = random.random() < 0.0625
    crit_mult = 1.5 if crit else 1.0
    rand = random.uniform(0.85, 1.0)
    base = (((2 * LEVEL / 5 + 2) * power * a / max(d, 1)) / 50 + 2)
    dmg = int(base * stab * eff * crit_mult * rand)
    return max(dmg, 1), eff, crit


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


def _draw_hp_plate(draw: "ImageDraw.ImageDraw", x: int, y: int, w: int,
                    name: str, hp: int, max_hp: int):
    draw.rounded_rectangle([x, y, x + w, y + 54], radius=10,
                            fill=(255, 255, 255, 235), outline=(40, 40, 40, 255), width=2)
    draw.text((x + 12, y + 6), f"{name.title()}  Lv.{LEVEL}",
               font=_font(16), fill=(20, 20, 20, 255))
    bar_x, bar_y, bar_w, bar_h = x + 12, y + 30, w - 24, 12
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                            radius=6, fill=(90, 90, 90, 255))
    frac = max(hp, 0) / max_hp if max_hp else 0
    fill_w = int(bar_w * frac)
    if fill_w > 0:
        draw.rounded_rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h],
                                radius=6, fill=_hp_color(frac))
    hp_text = f"{max(hp, 0)}/{max_hp}"
    draw.text((bar_x + bar_w - 6, bar_y + 14), hp_text, font=_font(13, bold=False),
               fill=(20, 20, 20, 255), anchor="ra")


async def render_battle_scene(session: aiohttp.ClientSession,
                               opponent_pokemon: "BattlePokemon",
                               player_pokemon: "BattlePokemon") -> Optional[discord.File]:
    """Classic side-on battle scene: opponent upper-right facing player,
    player's pokemon lower-left (mirrored) facing opponent, HP bars in green
    (shifting to yellow/red as HP drops) above each."""
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

    if opp_sprite:
        opp_sprite = opp_sprite.resize(
            (opp_sprite.width * SPRITE_SCALE, opp_sprite.height * SPRITE_SCALE), Image.NEAREST)
        canvas.alpha_composite(opp_sprite, (int(CANVAS_W * 0.60), 46))

    if player_sprite:
        player_sprite = ImageOps.mirror(player_sprite)
        player_sprite = player_sprite.resize(
            (player_sprite.width * SPRITE_SCALE, player_sprite.height * SPRITE_SCALE), Image.NEAREST)
        canvas.alpha_composite(player_sprite, (int(CANVAS_W * 0.06), CANVAS_H - 260))

    _draw_hp_plate(draw, 24, 20, 260, opponent_pokemon.name,
                    opponent_pokemon.hp, opponent_pokemon.max_hp)
    _draw_hp_plate(draw, CANVAS_W - 284, CANVAS_H - 140, 260, player_pokemon.name,
                    player_pokemon.hp, player_pokemon.max_hp)

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


# ── UI: move selection ──────────────────────────────────────────────────────

class MoveButton(discord.ui.Button):
    def __init__(self, label: str, idx: int):
        super().__init__(label=label[:80], style=discord.ButtonStyle.primary)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        view: "MoveView" = self.view
        for item in view.children:
            item.disabled = True
        await interaction.response.edit_message(view=view)
        if not view.future.done():
            view.future.set_result(self.idx)
        view.stop()


class MoveView(discord.ui.View):
    def __init__(self, trainer: Trainer, future: asyncio.Future):
        super().__init__(timeout=60)
        self.trainer = trainer
        self.future = future
        for i, mv in enumerate(trainer.active.moves):
            power = mv.get("power") or "-"
            label = f"{mv['name'].replace('-', ' ').title()} ({power} pow)"
            self.add_item(MoveButton(label, i))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.trainer.user.id:
            await interaction.response.send_message("Not your move to make.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if not self.future.done():
            self.future.set_result(random.randrange(len(self.trainer.active.moves)))


# ── UI: switch selection ────────────────────────────────────────────────────

class SwitchButton(discord.ui.Button):
    def __init__(self, label: str, idx: int):
        super().__init__(label=label[:80], style=discord.ButtonStyle.secondary)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        view: "SwitchView" = self.view
        for item in view.children:
            item.disabled = True
        await interaction.response.edit_message(view=view)
        if not view.future.done():
            view.future.set_result(self.idx)
        view.stop()


class SwitchView(discord.ui.View):
    def __init__(self, trainer: Trainer, future: asyncio.Future):
        super().__init__(timeout=60)
        self.trainer = trainer
        self.future = future
        self._alive_indices = []
        for i, p in enumerate(trainer.team):
            if not p.fainted:
                self._alive_indices.append(i)
                self.add_item(SwitchButton(p.name.title(), i))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.trainer.user.id:
            await interaction.response.send_message("Not your Pokemon to switch.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if not self.future.done() and self._alive_indices:
            self.future.set_result(self._alive_indices[0])


# ── Battle runner ────────────────────────────────────────────────────────────

class Battle:
    def __init__(self, cog: "BattleCog", channel: discord.TextChannel,
                 t1: Trainer, t2: Trainer):
        self.cog = cog
        self.channel = channel
        self.t1 = t1
        self.t2 = t2

    @staticmethod
    def _hp_bar(hp: int, max_hp: int, length: int = 12) -> str:
        filled = int(length * max(hp, 0) / max_hp) if max_hp else 0
        return "🟩" * filled + "⬜" * (length - filled)

    def status_embed(self) -> discord.Embed:
        """Text-only fallback, used if Pillow isn't installed."""
        e = discord.Embed(title="⚔️ Battle Status", colour=0x3498DB)
        for t in (self.t1, self.t2):
            p = t.active
            bar = self._hp_bar(p.hp, p.max_hp)
            e.add_field(
                name=f"{t.user.display_name} — {p.name.title()} (Lv.{LEVEL})",
                value=f"{bar}  {p.hp}/{p.max_hp} HP\nRemaining: {len(t.alive_team)}/{len(t.team)}",
                inline=False,
            )
        return e

    async def send_status(self):
        """Posts the battle scene image (opponent-vs-player, HP bars), or
        falls back to a text embed if Pillow isn't available."""
        image = await render_battle_scene(self.cog.session, self.t2.active, self.t1.active)
        if image is None:
            await self.channel.send(embed=self.status_embed())
            return
        caption = (f"**{self.t1.user.display_name}**'s {self.t1.active.name.title()} "
                   f"vs **{self.t2.user.display_name}**'s {self.t2.active.name.title()}")
        await self.channel.send(content=caption, file=image)

    async def get_move_choice(self, trainer: Trainer) -> int:
        future = asyncio.get_event_loop().create_future()
        view = MoveView(trainer, future)
        await self.channel.send(
            f"{trainer.user.mention}, choose **{trainer.active.name.title()}**'s move:",
            view=view,
        )
        return await future

    async def get_switch_choice(self, trainer: Trainer) -> int:
        future = asyncio.get_event_loop().create_future()
        view = SwitchView(trainer, future)
        await self.channel.send(
            f"{trainer.user.mention}, **{trainer.active.name.title()}** fainted! "
            f"Choose your next Pokemon:",
            view=view,
        )
        return await future

    async def _execute_move(self, attacker: Trainer, defender: Trainer, move: dict):
        acc = move.get("accuracy")
        if acc is not None and random.uniform(0, 100) > acc:
            await self.channel.send(
                f"❌ {attacker.active.name.title()}'s "
                f"{move['name'].replace('-', ' ').title()} missed!"
            )
            return
        dmg, eff, crit = calc_damage(attacker.active, defender.active, move)
        defender.active.hp = max(0, defender.active.hp - dmg)
        text = (f"➡️ {attacker.active.name.title()} used "
                f"**{move['name'].replace('-', ' ').title()}**! ({dmg} dmg)")
        if crit:
            text += " 💫 Critical hit!"
        if eff > 1:
            text += " It's super effective!"
        elif 0 < eff < 1:
            text += " It's not very effective..."
        elif eff == 0:
            text += " It had no effect!"
        await self.channel.send(text)

    async def run(self):
        await self.send_status()
        while self.t1.alive_team and self.t2.alive_team:
            idx1, idx2 = await asyncio.gather(
                self.get_move_choice(self.t1), self.get_move_choice(self.t2)
            )
            move1 = self.t1.active.moves[idx1]
            move2 = self.t2.active.moves[idx2]

            order = [(self.t1, self.t2, move1), (self.t2, self.t1, move2)]
            order.sort(key=lambda o: (o[2].get("priority", 0), o[0].active.spe), reverse=True)

            for attacker, defender, move in order:
                if attacker.active.fainted or defender.active.fainted:
                    continue
                await self._execute_move(attacker, defender, move)
                if defender.active.fainted:
                    await self.channel.send(f"💥 {defender.active.name.title()} fainted!")
                    if defender.alive_team:
                        new_idx = await self.get_switch_choice(defender)
                        defender.active_idx = new_idx
                        await self.channel.send(
                            f"{defender.user.display_name} sent out "
                            f"{defender.active.name.title()}!"
                        )
                        await self.send_status()
                    else:
                        break

            if not self.t1.alive_team or not self.t2.alive_team:
                break
            await self.send_status()

        winner = self.t1 if self.t1.alive_team else self.t2
        loser = self.t2 if winner is self.t1 else self.t1
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
            f"**{fmt}** battle ({count} pokemon each)! "
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
