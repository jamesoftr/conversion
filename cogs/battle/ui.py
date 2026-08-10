"""
cogs/battle/ui.py
─────────────────────
All discord.ui Views/Buttons/Selects used by the battle cog: challenge
accept/decline, post-battle rematch, forced switch after a faint, the
private switch menu, and the combined per-turn action panel.
"""

import asyncio
from typing import Optional, TYPE_CHECKING

import discord

from .constants import LEVEL, TURN_TIMEOUT
from .trainer_ai import Trainer, bot_choose_action

if TYPE_CHECKING:
    from ..battle_cog import BattleCog


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


class RematchView(discord.ui.View):
    """Posted after a battle ends. Against another human, BOTH trainers
    must click before the rematch starts; against the bot, only the human
    needs to. Reuses the exact format/team size/BST filter of the battle
    that just finished."""

    def __init__(self, cog: "BattleCog", channel: discord.TextChannel,
                 p1: discord.abc.User, p2: discord.abc.User,
                 fmt: str, count: int, bst_filter: tuple, vs_bot: bool):
        super().__init__(timeout=60)
        self.cog = cog
        self.channel = channel
        self.p1 = p1
        self.p2 = p2
        self.fmt = fmt
        self.count = count
        self.bst_filter = bst_filter
        self.vs_bot = vs_bot
        self.agreed: set = set()
        self.agreed_needed = {p1.id} if vs_bot else {p1.id, p2.id}
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="🔁 Rematch", style=discord.ButtonStyle.primary)
    async def rematch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.agreed_needed:
            await interaction.response.send_message(
                "Only the trainers from this battle can start a rematch.", ephemeral=True)
            return
        if self.channel.id in self.cog.pending or self.channel.id in self.cog.active_battles:
            await interaction.response.send_message(
                "There's already a challenge or battle active in this channel.", ephemeral=True)
            return
        self.agreed.add(interaction.user.id)
        if not self.agreed_needed <= self.agreed:
            await interaction.response.send_message(
                f"✅ {interaction.user.display_name} wants a rematch — waiting on the other trainer.",
            )
            return
        for item in self.children:
            item.disabled = True
        self.stop()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await self.cog.start_rematch(self.channel, self.p1, self.p2,
                                      self.fmt, self.count, self.bst_filter, self.vs_bot)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


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


class MoveSelect(discord.ui.Select):
    def __init__(self, trainer: Trainer, panel: "BattlePanel", row: int):
        self.trainer = trainer
        self.panel = panel
        options = []
        usable = [(i, mv) for i, mv in enumerate(trainer.active.moves) if mv.get("current_pp", 1) > 0]
        if not usable:
            # Out of PP on every move — the only legal action is Struggle.
            options.append(discord.SelectOption(
                label="Struggle",
                description="No PP left! Recoil damage to yourself.",
                value="-1",
            ))
        else:
            for i, mv in usable:
                tag = "⚡Priority • " if mv.get("priority", 0) > 0 else ""
                pp_txt = f"{mv.get('current_pp')}/{mv.get('pp') or '—'} PP"
                desc = f"{tag}{mv.get('type', 'normal').title()} • {mv.get('power') or '—'} power • {pp_txt}"
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
        self.timed_out_ids: set = set()

        # A bot-controlled trainer gets no dropdown and no wait — its move
        # is decided immediately via bot_choose_action() instead of a
        # component interaction, since nobody is going to click for it.
        self.move_select_t1 = None
        self.move_select_t2 = None
        row = 0
        if t1.is_bot:
            self.actions[t1.user.id] = bot_choose_action(t1, t2)
        else:
            self.move_select_t1 = MoveSelect(t1, self, row=row)
            self.add_item(self.move_select_t1)
            row += 1
        if t2.is_bot:
            self.actions[t2.user.id] = bot_choose_action(t2, t1)
        else:
            self.move_select_t2 = MoveSelect(t2, self, row=row)
            self.add_item(self.move_select_t2)
            row += 1

        self.add_item(SwitchButton(self))
        self.add_item(PassButton(self))

        if len(self.actions) == 2 and not self.event.is_set():
            self.event.set()

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
        # Fill in any missing action with that trainer's strongest move that
        # still has PP left (moves are pre-sorted by power) instead of a
        # random one — or Struggle (-1) if every move is out of PP.
        self.timed_out_ids = {
            trainer.user.id for trainer in (self.t1, self.t2)
            if trainer.user.id not in self.actions
        }
        for trainer in (self.t1, self.t2):
            if trainer.user.id not in self.actions:
                usable = [i for i, mv in enumerate(trainer.active.moves) if mv.get("current_pp", 1) > 0]
                idx = usable[0] if usable else -1
                self.actions[trainer.user.id] = ("move", idx)
                sel = self._select_for(trainer)
                sel.disabled = True
                fallback_label = "auto-used strongest move" if idx != -1 else "out of PP — used Struggle"
                sel.placeholder = f"{trainer.user.display_name} ran out of time — {fallback_label}"
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        if not self.event.is_set():
            self.event.set()
      
