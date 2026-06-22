"""
cogs/leaderboard_cog.py  —  Leaderboard commands.

Features
────────
  • Board-type dropdown: Catches / Shiny / Gigantamax / Box Openings / (Category)
  • Time-window dropdown: Today / All Time
  • "Today" = UTC midnight → now
  • "All Time" correctly reads all historical data — nothing is deleted
  • Dynamic Discord timestamp for window reset

Bug fixed vs previous version
──────────────────────────────
  SelectOption.default flags are now rebuilt fresh on every render rather than
  mutated in-place.  This prevents stale state causing the wrong window/board
  to display on the second+ interaction in a session.

Commands
────────
  a!leaderboard [category]
  a!lb          [category]
"""

import discord
from discord.ext import commands

import db
from categories import get_category, all_keys
from config import E


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _resolve_entries(bot, guild, raw_entries) -> list[dict]:
    """Attach display_name to each raw entry dict."""
    out = []
    for e in raw_entries:
        member = guild.get_member(e["user_id"])
        if member:
            display = member.display_name
        else:
            try:
                user    = await bot.fetch_user(e["user_id"])
                display = user.display_name
            except Exception:
                display = f"<@{e['user_id']}>"
        out.append({**e, "display_name": display})
    return out


# Board-type constants
BOARD_CATCHES    = "catches"
BOARD_SHINY      = "shiny"
BOARD_GIGANTAMAX = "gigantamax"
BOARD_BOX        = "box"
BOARD_CATEGORY   = "category"


def _build_lines(entries: list[dict], board: str) -> list[str]:
    lines = []
    for i, e in enumerate(entries, 1):
        rank = E.rank_emoji(i) if hasattr(E, "rank_emoji") else f"**#{i}**"
        name = e["display_name"]

        if board == BOARD_CATCHES:
            extras = []
            if e.get("shiny"):      extras.append(f"{E.shiny} {e['shiny']}")
            if e.get("gigantamax"): extras.append(f"{E.gigantamax} {e['gigantamax']}")
            suffix = f"  {'  '.join(extras)}" if extras else ""
            lines.append(
                f"{E.reply} {rank} **{name}** — **{e['total']}** caught{suffix}"
            )

        elif board == BOARD_SHINY:
            chain = e.get("chain_shiny", 0)
            parts = [f"{E.shiny} **{e.get('shiny', 0)}**"]
            if chain:
                parts.append(f"{E.chain_shiny} **{chain}**")
            lines.append(
                f"{E.reply} {rank} **{name}** — {' + '.join(parts)} shiny"
                f"  *(total: {e['total']})*"
            )

        elif board == BOARD_GIGANTAMAX:
            lines.append(
                f"{E.reply} {rank} **{name}** — {E.gigantamax} **{e['total']}**"
            )

        elif board == BOARD_BOX:
            extra = (
                f"  `🎴 {e.get('total_pokemon', 0)}`"
                f"  `✨ {e.get('total_shinies', 0)}`"
            )
            lines.append(
                f"{E.reply} {rank} **{name}** — 📦 **{e['boxes_opened']}** boxes{extra}"
            )

        else:  # category
            lines.append(
                f"{E.reply} {rank} **{name}** — **{e['total']}** caught"
            )

    return lines


# ── View ──────────────────────────────────────────────────────────────────────

class LeaderboardView(discord.ui.View):

    def __init__(
        self,
        bot,
        guild:       discord.Guild,
        guild_id:    int,
        invoker_id:  int,
        category:    dict | None,
        reset_unix:  int,
        today_label: str,
    ):
        super().__init__(timeout=300)
        self.bot         = bot
        self.guild       = guild
        self.guild_id    = guild_id
        self.invoker_id  = invoker_id
        self.category    = category
        self.reset_unix  = reset_unix
        self.today_label = today_label

        # State — track current selections by value, not by option object
        self._board  = BOARD_CATEGORY if category else BOARD_CATCHES
        self._window = "today"

        # Board-type select hidden when a category is active
        if not category:
            self._add_board_select()
        self._add_window_select()

    # ── Select builders (always fresh options — no mutation) ──────────────────

    def _add_board_select(self):
        select = discord.ui.Select(
            custom_id="board_select",
            placeholder="Board type…",
            row=0,
            options=self._board_options(),
        )
        select.callback = self._board_callback
        self._board_select = select
        self.add_item(select)

    def _add_window_select(self):
        select = discord.ui.Select(
            custom_id="window_select",
            placeholder="Time window…",
            row=1,
            options=self._window_options(),
        )
        select.callback = self._window_callback
        self._window_select = select
        self.add_item(select)

    def _board_options(self) -> list[discord.SelectOption]:
        """Return fresh SelectOption list each time, with correct defaults."""
        opts = [
            discord.SelectOption(
                label="Catches",      value=BOARD_CATCHES,
                emoji="📋",           default=(self._board == BOARD_CATCHES),
            ),
            discord.SelectOption(
                label="Shiny",        value=BOARD_SHINY,
                emoji=E.shiny,        default=(self._board == BOARD_SHINY),
            ),
            discord.SelectOption(
                label="Gigantamax",   value=BOARD_GIGANTAMAX,
                emoji=E.gigantamax,   default=(self._board == BOARD_GIGANTAMAX),
            ),
            discord.SelectOption(
                label="Box Openings", value=BOARD_BOX,
                emoji="📦",           default=(self._board == BOARD_BOX),
            ),
        ]
        return opts

    def _window_options(self) -> list[discord.SelectOption]:
        """Return fresh SelectOption list each time, with correct defaults."""
        return [
            discord.SelectOption(
                label="Today",    value="today",
                emoji="📅",       default=(self._window == "today"),
            ),
            discord.SelectOption(
                label="All Time", value="alltime",
                emoji="🏅",       default=(self._window == "alltime"),
            ),
        ]

    def _refresh_selects(self):
        """
        Replace option lists on both selects with freshly built ones.
        Called after every state change so dropdowns always reflect reality.
        """
        if hasattr(self, "_board_select"):
            self._board_select.options = self._board_options()
        self._window_select.options = self._window_options()

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _board_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        chosen = interaction.data["values"][0]
        if chosen == self._board:
            await interaction.followup.send(
                "Already showing that board.", ephemeral=True
            )
            return
        self._board = chosen
        self._refresh_selects()
        await interaction.message.edit(embed=await self._build_embed(), view=self)

    async def _window_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        chosen = interaction.data["values"][0]
        if chosen == self._window:
            await interaction.followup.send(
                "Already showing that window.", ephemeral=True
            )
            return
        self._window = chosen
        self._refresh_selects()
        await interaction.message.edit(embed=await self._build_embed(), view=self)

    # ── Embed builder ─────────────────────────────────────────────────────────

    async def _build_embed(self) -> discord.Embed:
        is_today = self._window == "today"
        gid      = self.guild_id
        board    = self._board

        # ── Fetch correct data ────────────────────────────────────────────────
        if board == BOARD_CATEGORY and self.category:
            raw = (
                await db.get_category_leaderboard(gid, self.category["pokemon"])
                if is_today else
                await db.get_category_leaderboard_alltime(gid, self.category["pokemon"])
            )
            title = f"{E.leaderboard} {self.category['name']} Leaderboard"

        elif board == BOARD_SHINY:
            raw = (
                await db.get_shiny_leaderboard(gid)
                if is_today else
                await db.get_shiny_leaderboard_alltime(gid)
            )
            title = f"{E.leaderboard} Shiny Leaderboard"

        elif board == BOARD_GIGANTAMAX:
            raw = (
                await db.get_gigantamax_leaderboard(gid)
                if is_today else
                await db.get_gigantamax_leaderboard_alltime(gid)
            )
            title = f"{E.leaderboard} {E.gigantamax} Gigantamax Leaderboard"

        elif board == BOARD_BOX:
            raw = (
                await db.get_box_leaderboard(gid)
                if is_today else
                await db.get_box_leaderboard_alltime(gid)
            )
            title = f"{E.leaderboard} 📦 Box Openings Leaderboard"

        else:  # BOARD_CATCHES
            raw = (
                await db.get_leaderboard(gid)
                if is_today else
                await db.get_leaderboard_alltime(gid)
            )
            title = f"{E.leaderboard} Global Leaderboard"

        entries = await _resolve_entries(self.bot, self.guild, raw)
        lines   = _build_lines(entries, board)

        # ── Window header ─────────────────────────────────────────────────────
        if is_today:
            window_header = (
                f"> 📅 **Today — {self.today_label}**\n"
                f"> Resets <t:{self.reset_unix}:R> — <t:{self.reset_unix}:t>"
            )
        else:
            window_header = "> 🏅 **All Time** — complete history"

        body        = "\n".join(lines) if lines else "*No data recorded yet.*"
        description = f"{window_header}\n\u200b\n{body}"

        return discord.Embed(
            title=title,
            description=description,
            color=discord.Color.gold(),
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class LeaderboardCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="leaderboard", aliases=["lb"])
    async def leaderboard(self, ctx: commands.Context, category: str = None):
        """
        Show the leaderboard with board-type and time-window dropdowns.

        Usage:
          a!leaderboard              — global (catches / shiny / gigantamax / boxes)
          a!leaderboard rares        — rares category leaderboard
        """
        guild_id = ctx.guild.id

        cat = None
        if category:
            cat = get_category(category)
            if not cat:
                await ctx.reply(
                    f"❌ Unknown category `{category}`.\n"
                    f"Available: `{'`, `'.join(all_keys())}`",
                    mention_author=False,
                )
                return

        reset_info = await db.get_window_reset_info(guild_id)

        view = LeaderboardView(
            bot         = self.bot,
            guild       = ctx.guild,
            guild_id    = guild_id,
            invoker_id  = ctx.author.id,
            category    = cat,
            reset_unix  = reset_info["reset_unix"],
            today_label = reset_info["today_label"],
        )
        embed = await view._build_embed()
        await ctx.reply(embed=embed, view=view, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
