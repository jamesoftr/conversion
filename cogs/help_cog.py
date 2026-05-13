"""
cogs/help_cog.py  —  Help command.

a!help                  — Overview page with a button for each section
a!help tracker          — Jump straight to Tracker
a!help leaderboard      — Jump straight to Leaderboard
a!help category         — Jump straight to Category Stats
a!help autopause        — Jump straight to Autopause
a!help converter        — Jump straight to Converter

Navigation
──────────
Overview page  : one button per section (clicks open that section page)
Section pages  : 🏠 Home button returns to overview
                 ◀ Prev / Next ▶ to walk between section pages
"""

import discord
from discord.ext import commands


# ─────────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────────

# PAGE_INDEX = 0  →  overview (generated from SECTIONS)
# PAGE_INDEX = 1  →  SECTIONS[0]   (Tracker)
# PAGE_INDEX = 2  →  SECTIONS[1]   (Leaderboard)
# etc.
# So section page index = section list index + 1

SECTIONS = [
    {
        "key":    "tracker",
        "title":  "📋 Tracker",
        "emoji":  "📋",
        "color":  discord.Color.gold(),
        "summary": "Catch & flee tracking, user profiles, fled-log routing.",
        "fields": [
            (
                "`a!profile` / `a!pf`  `[@user]`",
                "View your catch stats (today + all-time), type breakdown, region "
                "breakdown, and full Pokémon list. Mention a user to see theirs.",
            ),
            (
                "`a!check`  *(Admin)*",
                "Reply to a Pokétwo message to **manually record** a catch or flee "
                "that the bot missed.",
            ),
            (
                "`a!fled-logs <category> <#channel>`  *(Admin)*",
                "Route fled-alerts for a category to a specific channel.\n"
                "e.g. `a!fled-logs rares #rare-logs`",
            ),
            (
                "`a!fled-logs list`  *(Admin)*",
                "Show the current fled-log channel routing for this server.",
            ),
            (
                "`a!cleardata`  *(Bot owner)*",
                "Permanently delete **all** catch and flee records for this server. "
                "Asks for confirmation first.",
            ),
        ],
    },
    {
        "key":    "leaderboard",
        "title":  "🏆 Leaderboard",
        "emoji":  "🏆",
        "color":  discord.Color.blurple(),
        "summary": "Global and category catch leaderboards with time-window toggles.",
        "fields": [
            (
                "`a!leaderboard` / `a!lb`",
                "Show the **global** leaderboard. Use the dropdown to switch between "
                "Catches / Shiny / Gigantamax boards, and Today / All Time windows.",
            ),
            (
                "`a!leaderboard <category>` / `a!lb <category>`",
                "Show the leaderboard filtered to a specific Pokémon category.\n"
                "e.g. `a!lb rares`  •  `a!lb regionals`",
            ),
        ],
    },
    {
        "key":    "category",
        "title":  "📊 Category Stats",
        "emoji":  "📊",
        "color":  discord.Color.teal(),
        "summary": "Per-category spawn, catch, and flee statistics.",
        "fields": [
            (
                "`a!catstat <category>` / `a!cs` / `a!categorystat`",
                "Show spawned / caught / fled counts and catch rate for a Pokémon "
                "category, both today and all-time.\n"
                "e.g. `a!catstat rares`  •  `a!catstat regionals`",
            ),
        ],
    },
    {
        "key":    "autopause",
        "title":  "🔒 Autopause",
        "emoji":  "🔒",
        "color":  discord.Color.red(),
        "summary": "Auto-lock channels on rare/regional spawns, with reminders and manual unlock.",
        "fields": [
            (
                "`a!autopause enable` / `disable`  *(also: `a!ap`)*  *(Admin)*",
                "Toggle the entire autopause feature on or off for this server.",
            ),
            (
                "`a!autopause status`  *(Admin)*",
                "Show current config: enabled state, naming bot, delays, reminder roles.",
            ),
            (
                "`a!autopause setbot <user_id>`  *(Admin)*",
                "Set the **Naming Bot** user ID to listen to for rare/regional spawns.",
            ),
            (
                "`a!autopause setlock <seconds>`  *(Admin)*",
                "Delay before locking the channel after a spawn. `0` = instant.\n"
                "e.g. `a!ap setlock 30`",
            ),
            (
                "`a!autopause setunlock <seconds>`  *(Admin)*",
                "Delay before the channel auto-unlocks after being locked.\n"
                "e.g. `a!ap setunlock 300`  *(5 minutes)*",
            ),
            (
                "`a!autopause setreminder <seconds>`  *(Admin)*",
                "Send a role-ping this many seconds after spawn detection. Must be "
                "**between** lock and unlock delays. Requires both to be set first.\n"
                "e.g. `a!ap setreminder 120`",
            ),
            (
                "`a!autopause setrole rare <@role>`  *(Admin)*",
                "Role pinged in reminders for **Rare** spawns.",
            ),
            (
                "`a!autopause setrole regional <@role>`  *(Admin)*",
                "Role pinged in reminders for **Regional** spawns.",
            ),
            (
                "`a!unlock` / `a!u`",
                "Manually unlock the current channel. Usable by anyone — same as "
                "pressing the 🔓 Unlock Now button in the lock message.",
            ),
            (
                "`a!locked`",
                "View all currently locked channels. Rare / Regional tabs, pagination, "
                "and a **🔓 Unlock All** button. Each entry links to the spawn message.",
            ),
        ],
    },
    {
        "key":    "converter",
        "title":  "🔄 Converter",
        "emoji":  "🔄",
        "color":  discord.Color.og_blurple(),
        "summary": "Convert Components V2 messages into classic embeds.",
        "fields": [
            (
                "`a!convert`  *(reply to a message)*",
                "Manually convert a **Components V2** message into a classic embed. "
                "Reply to the target message then run this command.",
            ),
            (
                "`/convert`  *(slash — reply to a message)*",
                "Slash-command version of the converter.",
            ),
        ],
    },
]

# Flat key → index+1 map for direct jumping
_KEY_TO_PAGE: dict[str, int] = {s["key"]: i + 1 for i, s in enumerate(SECTIONS)}


# ─────────────────────────────────────────────────────────────────────────────
# Overview embed (page 0)
# ─────────────────────────────────────────────────────────────────────────────

def _overview_embed() -> discord.Embed:
    e = discord.Embed(
        title="📖 Help — Command Overview",
        description=(
            "Click a button below to open a section, "
            "or use `a!help <section>` to jump directly.\n\u200b"
        ),
        color=discord.Color.dark_grey(),
    )
    for sec in SECTIONS:
        e.add_field(
            name=f"{sec['emoji']}  {sec['title'].split(' ', 1)[1]}",
            value=sec["summary"],
            inline=False,
        )
    e.set_footer(text="Prefix: a!  or  !")
    return e


# ─────────────────────────────────────────────────────────────────────────────
# Section embed (pages 1+)
# ─────────────────────────────────────────────────────────────────────────────

def _section_embed(sec: dict, page_num: int) -> discord.Embed:
    e = discord.Embed(title=sec["title"], color=sec["color"])
    for name, value in sec["fields"]:
        e.add_field(name=name, value=value, inline=False)
    e.set_footer(
        text=(
            f"Section {page_num}/{len(SECTIONS)}  •  "
            "Prefix: a!  or  !  •  "
            "a!help <section> to jump directly"
        )
    )
    return e


# ─────────────────────────────────────────────────────────────────────────────
# View
# ─────────────────────────────────────────────────────────────────────────────

class HelpView(discord.ui.View):
    """
    page == 0           →  overview
    page == 1..N        →  SECTIONS[page-1]
    """

    def __init__(self, page: int = 0):
        super().__init__(timeout=180)
        self.page = page
        self._rebuild_buttons()

    # ── button factory ────────────────────────────────────────────────────────

    def _rebuild_buttons(self):
        self.clear_items()

        if self.page == 0:
            # Overview: one button per section (row 0 and 1, up to 5 per row)
            for i, sec in enumerate(SECTIONS):
                btn = discord.ui.Button(
                    label=sec["title"],
                    emoji=sec["emoji"],
                    style=discord.ButtonStyle.primary,
                    row=i // 5,
                    custom_id=f"help_sec_{i}",
                )
                # Capture i in closure
                btn.callback = self._make_section_callback(i + 1)
                self.add_item(btn)
        else:
            # Section page: Home + Prev + Next
            home_btn = discord.ui.Button(
                label="🏠 Home",
                style=discord.ButtonStyle.secondary,
                row=0,
                custom_id="help_home",
            )
            home_btn.callback = self._go_home
            self.add_item(home_btn)

            prev_btn = discord.ui.Button(
                label="◀ Prev",
                style=discord.ButtonStyle.secondary,
                disabled=(self.page == 1),
                row=0,
                custom_id="help_prev",
            )
            prev_btn.callback = self._go_prev
            self.add_item(prev_btn)

            next_btn = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self.page == len(SECTIONS)),
                row=0,
                custom_id="help_next",
            )
            next_btn.callback = self._go_next
            self.add_item(next_btn)

    # ── callbacks ─────────────────────────────────────────────────────────────

    def _make_section_callback(self, target_page: int):
        async def callback(interaction: discord.Interaction):
            self.page = target_page
            self._rebuild_buttons()
            sec = SECTIONS[self.page - 1]
            await interaction.response.edit_message(
                embed=_section_embed(sec, self.page), view=self
            )
        return callback

    async def _go_home(self, interaction: discord.Interaction):
        self.page = 0
        self._rebuild_buttons()
        await interaction.response.edit_message(embed=_overview_embed(), view=self)

    async def _go_prev(self, interaction: discord.Interaction):
        self.page -= 1
        self._rebuild_buttons()
        sec = SECTIONS[self.page - 1]
        await interaction.response.edit_message(
            embed=_section_embed(sec, self.page), view=self
        )

    async def _go_next(self, interaction: discord.Interaction):
        self.page += 1
        self._rebuild_buttons()
        sec = SECTIONS[self.page - 1]
        await interaction.response.edit_message(
            embed=_section_embed(sec, self.page), view=self
        )

    # ── embed helper (used externally) ────────────────────────────────────────

    def current_embed(self) -> discord.Embed:
        if self.page == 0:
            return _overview_embed()
        return _section_embed(SECTIONS[self.page - 1], self.page)


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────

class HelpCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="help")
    async def help_cmd(self, ctx: commands.Context, *, section: str = None):
        """
        Show all bot commands.

        a!help                  — overview with section buttons
        a!help tracker          — Tracker commands
        a!help leaderboard      — Leaderboard commands
        a!help category         — Category Stats commands
        a!help autopause        — Autopause commands
        a!help converter        — Converter commands
        """
        start_page = 0

        if section:
            key = section.strip().lower()

            # Exact key match first
            page = _KEY_TO_PAGE.get(key)

            # Fuzzy: check if the query appears in any key or title
            if page is None:
                match = next(
                    (s for s in SECTIONS
                     if key in s["key"] or key in s["title"].lower()),
                    None,
                )
                if match:
                    page = _KEY_TO_PAGE[match["key"]]

            if page is None:
                names = list(_KEY_TO_PAGE.keys())
                await ctx.reply(
                    f"❌ Unknown section `{section}`.\n"
                    f"Available: `{'`, `'.join(names)}`"
                )
                return

            start_page = page

        view = HelpView(page=start_page)
        await ctx.reply(embed=view.current_embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
