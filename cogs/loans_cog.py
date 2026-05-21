"""
cogs/loans_cog.py  —  PokéCoin / PC loan tracker.

Features
────────
  • Issue loans with optional interest (flat or daily-compound) and due dates
  • Attach proof (message link / image URL) at creation or later
  • Record partial and full repayments with per-payment notes
  • Cancel a loan (lender only)
  • View any loan by ID with full payment history
  • Personal loan dashboard (lent / borrowed, active / all)
  • Server-wide loan list (mod-only)
  • Overdue alert on every loan embed

Commands  (prefix a!)
─────────────────────
  a!loan give  @user <amount> [pc|pokecoins] [--rate 5] [--type flat|compound]
               [--due YYYY-MM-DD] [--proof <url>] [--note "text"]
  a!loan pay   <LOAN-ID> <amount> [--note "text"]
  a!loan cancel <LOAN-ID>
  a!loan info   <LOAN-ID>
  a!loan proof  <LOAN-ID> <url>
  a!loan list  [lent|borrowed|all]   (defaults to both active)
  a!loan server [active|paid|all]    (mod-only)
  a!loan summary [@user]
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import commands

import db
from config import E   # your emoji config; adjust if needed


# ── Constants ─────────────────────────────────────────────────────────────────

CURRENCY_ALIASES = {
    "pc":         "pc",
    "pcs":        "pc",
    "pokecoin":   "pokecoins",
    "pokecoins":  "pokecoins",
    "coins":      "pokecoins",
    "coin":       "pokecoins",
}

CURRENCY_EMOJI = {
    "pc":        "🪙",
    "pokecoins": "<:pokecoin:0>",   # replace <:pokecoin:0> with your real emoji
}

STATUS_EMOJI = {
    "active":    "🟢",
    "partial":   "🟡",
    "paid":      "✅",
    "cancelled": "❌",
}

PAGE_SIZE = 5   # loans per page in list views


# ── Formatting helpers ────────────────────────────────────────────────────────

def _cur(loan: dict) -> str:
    return CURRENCY_EMOJI.get(loan["currency"], "🪙")


def _fmt_amount(amount: float, loan: dict) -> str:
    v = int(amount) if amount == int(amount) else f"{amount:,.2f}"
    return f"{_cur(loan)} **{v:,}**" if isinstance(v, int) else f"{_cur(loan)} **{v}**"


def _interest_label(loan: dict) -> str:
    rate = loan["interest_rate"]
    if rate == 0 or loan["interest_type"] == "none":
        return "None (interest-free)"
    pct = f"{rate * 100:.4g}%"
    if loan["interest_type"] == "flat":
        return f"Flat {pct}"
    return f"Daily compound {pct}"


def _status_line(loan: dict) -> str:
    emoji  = STATUS_EMOJI.get(loan["status"], "❓")
    label  = loan["status"].capitalize()
    return f"{emoji} {label}"


def _overdue_tag(loan: dict) -> str:
    if loan.get("due_date") and loan["status"] in ("active", "partial"):
        due = loan["due_date"]
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > due:
            return "  ⚠️ **OVERDUE**"
    return ""


def _amount_due_now(loan: dict) -> float:
    """For compound loans, recalculate live."""
    if loan["interest_type"] == "compound" and loan["interest_rate"] > 0:
        return db.compute_compound_due(
            loan["principal"], loan["interest_rate"], loan["created_at"]
        )
    return loan["amount_due"]


def _remaining(loan: dict) -> float:
    return max(0.0, round(_amount_due_now(loan) - loan["amount_paid"], 2))


# ── Embed builders ────────────────────────────────────────────────────────────

def _loan_embed(loan: dict, guild: discord.Guild) -> discord.Embed:
    """Full detail embed for a single loan."""
    status      = loan["status"]
    color_map   = {
        "active":    discord.Color.green(),
        "partial":   discord.Color.yellow(),
        "paid":      discord.Color.blurple(),
        "cancelled": discord.Color.red(),
    }
    embed = discord.Embed(
        title=f"Loan {loan['loan_id']}{_overdue_tag(loan)}",
        color=color_map.get(status, discord.Color.greyple()),
    )

    lender   = guild.get_member(loan["lender_id"])
    borrower = guild.get_member(loan["borrower_id"])
    lender_s   = lender.display_name   if lender   else f"<@{loan['lender_id']}>"
    borrower_s = borrower.display_name if borrower else f"<@{loan['borrower_id']}>"

    amount_due = _amount_due_now(loan)
    remaining  = max(0.0, round(amount_due - loan["amount_paid"], 2))

    embed.add_field(name="👤 Lender",   value=lender_s,   inline=True)
    embed.add_field(name="👤 Borrower", value=borrower_s, inline=True)
    embed.add_field(name="Status",      value=_status_line(loan) + _overdue_tag(loan), inline=True)

    embed.add_field(name="Principal",   value=_fmt_amount(loan["principal"],  loan), inline=True)
    embed.add_field(name="Interest",    value=_interest_label(loan),                 inline=True)
    embed.add_field(name="Total Due",   value=_fmt_amount(amount_due,         loan), inline=True)

    embed.add_field(name="Paid So Far", value=_fmt_amount(loan["amount_paid"], loan), inline=True)
    embed.add_field(name="Remaining",   value=_fmt_amount(remaining,           loan), inline=True)

    created_ts = int(loan["created_at"].replace(tzinfo=timezone.utc).timestamp()
                     if loan["created_at"].tzinfo is None
                     else loan["created_at"].timestamp())
    embed.add_field(name="Issued", value=f"<t:{created_ts}:D>", inline=True)

    if loan.get("due_date"):
        due = loan["due_date"]
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        due_ts = int(due.timestamp())
        embed.add_field(name="Due Date", value=f"<t:{due_ts}:D> (<t:{due_ts}:R>)", inline=True)

    if loan.get("note"):
        embed.add_field(name="📝 Note", value=loan["note"], inline=False)

    if loan.get("proof_url"):
        embed.add_field(name="🔗 Proof", value=f"[View message / proof]({loan['proof_url']})", inline=False)

    # Payment history (last 5)
    payments = loan.get("payments", [])
    if payments:
        lines = []
        for p in payments[-5:]:
            ts = p["timestamp"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            unix = int(ts.timestamp())
            line = f"• <t:{unix}:d> — {_fmt_amount(p['amount'], loan)}"
            if p.get("note"):
                line += f" *(_{p['note']}_)*"
            lines.append(line)
        if len(payments) > 5:
            lines.append(f"*…and {len(payments) - 5} earlier payment(s)*")
        embed.add_field(name=f"💸 Payments ({len(payments)})", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"Loan ID: {loan['loan_id']}")
    return embed


def _loan_row(loan: dict, guild: discord.Guild) -> str:
    """Single-line summary for list views."""
    borrower = guild.get_member(loan["borrower_id"])
    lender   = guild.get_member(loan["lender_id"])
    b_name   = borrower.display_name if borrower else f"<@{loan['borrower_id']}>"
    l_name   = lender.display_name   if lender   else f"<@{loan['lender_id']}>"

    remaining = _remaining(loan)
    status    = STATUS_EMOJI.get(loan["status"], "❓")
    overdue   = "⚠️" if _overdue_tag(loan) else ""

    due = ""
    if loan.get("due_date"):
        d = loan["due_date"]
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        due = f" · due <t:{int(d.timestamp())}:d>"

    return (
        f"{status}{overdue} **{loan['loan_id']}** — "
        f"**{l_name}** → **{b_name}** — "
        f"{_fmt_amount(remaining, loan)} remaining{due}"
    )


# ── Argument parser for a!loan give ──────────────────────────────────────────

def _parse_give_args(raw: str) -> dict:
    """
    Parse:  @mention <amount> [currency] [--rate N] [--type flat|compound]
            [--due YYYY-MM-DD] [--proof URL] [--note "text"]
    Returns a dict of parsed values; raises ValueError on bad input.
    """
    # Use shlex to handle quoted strings
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"Could not parse arguments: {exc}") from exc

    # Pop --flag value pairs first
    kwargs = {}
    remaining = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            key = t[2:].lower()
            if i + 1 >= len(tokens):
                raise ValueError(f"Flag `--{key}` needs a value.")
            kwargs[key] = tokens[i + 1]
            i += 2
        else:
            remaining.append(t)
            i += 1

    # remaining[0] is the mention or user ID (already resolved by Discord)
    # remaining[1] is the amount
    # remaining[2] (optional) is the currency

    if len(remaining) < 2:
        raise ValueError("Usage: `a!loan give @user <amount> [pc|pokecoins] [--flags…]`")

    # Amount
    try:
        amount = int(remaining[1].replace(",", ""))
        if amount <= 0:
            raise ValueError("Amount must be positive.")
    except (ValueError, IndexError):
        raise ValueError(f"`{remaining[1]}` is not a valid amount.")

    # Currency (default pc)
    currency_raw = remaining[2].lower() if len(remaining) > 2 else "pc"
    currency = CURRENCY_ALIASES.get(currency_raw)
    if not currency:
        raise ValueError(f"Unknown currency `{currency_raw}`. Use `pc` or `pokecoins`.")

    # --rate (percent, stored as decimal)
    interest_rate = 0.0
    if "rate" in kwargs:
        try:
            interest_rate = float(kwargs["rate"]) / 100
            if interest_rate < 0:
                raise ValueError()
        except ValueError:
            raise ValueError(f"`--rate {kwargs['rate']}` must be a non-negative number (e.g. `--rate 5` = 5%).")

    # --type
    interest_type = "flat" if interest_rate > 0 else "none"
    if "type" in kwargs:
        t = kwargs["type"].lower()
        if t not in ("flat", "compound", "none"):
            raise ValueError("`--type` must be `flat`, `compound`, or `none`.")
        interest_type = t

    if interest_rate > 0 and interest_type == "none":
        interest_type = "flat"   # sensible default

    # --due
    due_date = None
    if "due" in kwargs:
        try:
            due_date = datetime.strptime(kwargs["due"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"`--due {kwargs['due']}` must be `YYYY-MM-DD` format.")

    return {
        "amount":        amount,
        "currency":      currency,
        "interest_rate": interest_rate,
        "interest_type": interest_type,
        "due_date":      due_date,
        "proof_url":     kwargs.get("proof"),
        "note":          kwargs.get("note"),
    }


# ── Paginated list view ───────────────────────────────────────────────────────

class LoanListView(discord.ui.View):

    def __init__(self, loans: list[dict], guild: discord.Guild, title: str, invoker_id: int):
        super().__init__(timeout=300)
        self.loans      = loans
        self.guild      = guild
        self.title      = title
        self.invoker_id = invoker_id
        self.page       = 0
        self.total_pages = max(1, (len(loans) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def _build_embed(self) -> discord.Embed:
        start  = self.page * PAGE_SIZE
        chunk  = self.loans[start : start + PAGE_SIZE]
        lines  = [_loan_row(l, self.guild) for l in chunk] or ["*No loans found.*"]
        embed  = discord.Embed(
            title       = self.title,
            description = "\n".join(lines),
            color       = discord.Color.gold(),
        )
        embed.set_footer(text=f"Page {self.page + 1}/{self.total_pages}  •  {len(self.loans)} loans total")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.page -= 1
        self._update_buttons()
        await interaction.message.edit(embed=self._build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.page += 1
        self._update_buttons()
        await interaction.message.edit(embed=self._build_embed(), view=self)


# ── Cog ───────────────────────────────────────────────────────────────────────

class LoansCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Group ────────────────────────────────────────────────────────────────

    @commands.group(name="loan", aliases=["loans"], invoke_without_command=True)
    async def loan(self, ctx: commands.Context):
        """Pokécoin / PC loan tracker. Use `a!loan help` for subcommands."""
        await ctx.reply(
            "**Loan commands:**\n"
            "`a!loan give @user <amount> [currency] [--rate N] [--type flat|compound] [--due YYYY-MM-DD] [--proof URL] [--note text]`\n"
            "`a!loan pay <LOAN-ID> <amount> [--note text]`\n"
            "`a!loan cancel <LOAN-ID>`\n"
            "`a!loan info <LOAN-ID>`\n"
            "`a!loan proof <LOAN-ID> <url>`\n"
            "`a!loan list [lent|borrowed|all]`\n"
            "`a!loan server [active|paid|all]`  *(mod only)*\n"
            "`a!loan summary [@user]`",
            mention_author=False,
        )

    # ── a!loan give ──────────────────────────────────────────────────────────

    @loan.command(name="give")
    async def loan_give(self, ctx: commands.Context, borrower: discord.Member, *, args: str = ""):
        """
        Issue a loan to another user.

        Examples:
          a!loan give @User 5000
          a!loan give @User 5000 pc --rate 5 --type flat --due 2025-09-01
          a!loan give @User 10000 pokecoins --rate 1 --type compound --note "3rd loan" --proof https://discord.com/channels/…
        """
        if borrower.bot:
            await ctx.reply("❌ You can't loan to a bot.", mention_author=False)
            return
        if borrower == ctx.author:
            await ctx.reply("❌ You can't loan to yourself.", mention_author=False)
            return

        # Inject amount placeholder so parser always sees remaining[0]=mention, [1]=amount
        # Since Discord already resolved the mention into `borrower`, args starts after it.
        # We prepend a dummy token so _parse_give_args sees the expected positional layout.
        full_args = f"_placeholder_ {args.strip()}"
        try:
            parsed = _parse_give_args(full_args)
        except ValueError as exc:
            await ctx.reply(f"❌ {exc}", mention_author=False)
            return

        loan_doc = await db.create_loan(
            guild_id      = ctx.guild.id,
            lender_id     = ctx.author.id,
            borrower_id   = borrower.id,
            principal     = parsed["amount"],
            currency      = parsed["currency"],
            interest_rate = parsed["interest_rate"],
            interest_type = parsed["interest_type"],
            due_date      = parsed["due_date"],
            proof_url     = parsed["proof_url"],
            note          = parsed["note"],
        )

        embed = _loan_embed(loan_doc, ctx.guild)
        embed.set_author(name="✅ Loan Created")
        await ctx.reply(embed=embed, mention_author=False)

    # ── a!loan pay ───────────────────────────────────────────────────────────

    @loan.command(name="pay")
    async def loan_pay(self, ctx: commands.Context, loan_id: str, amount: str, *, args: str = ""):
        """
        Record a repayment on a loan.

        Examples:
          a!loan pay L-00001 2500
          a!loan pay L-00001 2500 --note "first half"
        """
        # parse amount
        try:
            pay_amount = float(amount.replace(",", ""))
            if pay_amount <= 0:
                raise ValueError()
        except ValueError:
            await ctx.reply(f"❌ `{amount}` is not a valid amount.", mention_author=False)
            return

        # parse optional --note
        note = None
        m = re.search(r"--note\s+(.+)", args)
        if m:
            note = m.group(1).strip().strip('"\'')

        loan_doc = await db.get_loan(loan_id.upper())
        if not loan_doc:
            await ctx.reply(f"❌ Loan `{loan_id}` not found.", mention_author=False)
            return

        # Only lender or borrower may record payments
        if ctx.author.id not in (loan_doc["lender_id"], loan_doc["borrower_id"]):
            # Allow mods too
            if not ctx.author.guild_permissions.manage_guild:
                await ctx.reply("❌ Only the lender or borrower can record payments.", mention_author=False)
                return

        if loan_doc["status"] in ("paid", "cancelled"):
            await ctx.reply(f"❌ This loan is already **{loan_doc['status']}**.", mention_author=False)
            return

        updated = await db.record_payment(loan_id.upper(), pay_amount, note)
        embed   = _loan_embed(updated, ctx.guild)
        embed.set_author(name="💸 Payment Recorded")
        await ctx.reply(embed=embed, mention_author=False)

    # ── a!loan cancel ────────────────────────────────────────────────────────

    @loan.command(name="cancel")
    async def loan_cancel(self, ctx: commands.Context, loan_id: str):
        """Cancel a loan (lender or mod only)."""
        loan_doc = await db.get_loan(loan_id.upper())
        if not loan_doc:
            await ctx.reply(f"❌ Loan `{loan_id}` not found.", mention_author=False)
            return

        is_lender = ctx.author.id == loan_doc["lender_id"]
        is_mod    = ctx.author.guild_permissions.manage_guild

        if not (is_lender or is_mod):
            await ctx.reply("❌ Only the lender (or a mod) can cancel a loan.", mention_author=False)
            return

        if loan_doc["status"] in ("paid", "cancelled"):
            await ctx.reply(f"❌ This loan is already **{loan_doc['status']}**.", mention_author=False)
            return

        updated = await db.cancel_loan(loan_id.upper())
        embed   = _loan_embed(updated, ctx.guild)
        embed.set_author(name="❌ Loan Cancelled")
        await ctx.reply(embed=embed, mention_author=False)

    # ── a!loan info ──────────────────────────────────────────────────────────

    @loan.command(name="info")
    async def loan_info(self, ctx: commands.Context, loan_id: str):
        """Show full details and payment history for a loan."""
        loan_doc = await db.get_loan(loan_id.upper())
        if not loan_doc:
            await ctx.reply(f"❌ Loan `{loan_id}` not found.", mention_author=False)
            return

        # Only those involved or mods
        involved = ctx.author.id in (loan_doc["lender_id"], loan_doc["borrower_id"])
        is_mod   = ctx.author.guild_permissions.manage_guild
        if not (involved or is_mod):
            await ctx.reply("❌ You're not involved in this loan.", mention_author=False)
            return

        await ctx.reply(embed=_loan_embed(loan_doc, ctx.guild), mention_author=False)

    # ── a!loan proof ─────────────────────────────────────────────────────────

    @loan.command(name="proof")
    async def loan_proof(self, ctx: commands.Context, loan_id: str, url: str):
        """Attach or update the proof URL on a loan (lender or mod)."""
        loan_doc = await db.get_loan(loan_id.upper())
        if not loan_doc:
            await ctx.reply(f"❌ Loan `{loan_id}` not found.", mention_author=False)
            return

        is_lender = ctx.author.id == loan_doc["lender_id"]
        is_mod    = ctx.author.guild_permissions.manage_guild
        if not (is_lender or is_mod):
            await ctx.reply("❌ Only the lender (or a mod) can update proof.", mention_author=False)
            return

        updated = await db.update_loan_proof(loan_id.upper(), url)
        embed   = _loan_embed(updated, ctx.guild)
        embed.set_author(name="🔗 Proof Updated")
        await ctx.reply(embed=embed, mention_author=False)

    # ── a!loan list ──────────────────────────────────────────────────────────

    @loan.command(name="list")
    async def loan_list(self, ctx: commands.Context, mode: str = "active"):
        """
        Show your own loans.
          a!loan list            — active loans (both lent and borrowed)
          a!loan list lent       — only loans you gave
          a!loan list borrowed   — only loans you received
          a!loan list all        — every loan regardless of status
        """
        mode     = mode.lower()
        user_id  = ctx.author.id
        guild_id = ctx.guild.id

        status_filter = None if mode == "all" else "active"

        if mode in ("lent", "active", "all"):
            lent = await db.get_loans_as_lender(guild_id, user_id, status_filter)
        else:
            lent = []

        if mode in ("borrowed", "active", "all"):
            borrowed = await db.get_loans_as_borrower(guild_id, user_id, status_filter)
        else:
            borrowed = []

        # Merge, deduplicate, sort newest first
        seen = set()
        combined = []
        for l in lent + borrowed:
            lid = l["loan_id"]
            if lid not in seen:
                seen.add(lid)
                combined.append(l)
        combined.sort(key=lambda x: x["created_at"], reverse=True)

        if not combined:
            await ctx.reply("📭 No loans found.", mention_author=False)
            return

        title = f"📋 Your Loans — {mode.capitalize()}"
        view  = LoanListView(combined, ctx.guild, title, ctx.author.id)
        await ctx.reply(embed=view._build_embed(), view=view, mention_author=False)

    # ── a!loan server ────────────────────────────────────────────────────────

    @loan.command(name="server")
    @commands.has_permissions(manage_guild=True)
    async def loan_server(self, ctx: commands.Context, mode: str = "active"):
        """
        (Mod only) View all server loans.
          a!loan server           — active loans
          a!loan server paid      — paid loans
          a!loan server all       — every loan
        """
        mode   = mode.lower()
        status = None
        if mode == "active":
            status = "active"
        elif mode == "paid":
            status = "paid"

        loans = await db.get_all_guild_loans(ctx.guild.id, status)

        if not loans:
            await ctx.reply("📭 No loans found.", mention_author=False)
            return

        title = f"🏦 Server Loans — {mode.capitalize()}"
        view  = LoanListView(loans, ctx.guild, title, ctx.author.id)
        await ctx.reply(embed=view._build_embed(), view=view, mention_author=False)

    # ── a!loan summary ───────────────────────────────────────────────────────

    @loan.command(name="summary")
    async def loan_summary(self, ctx: commands.Context, member: discord.Member = None):
        """
        Show a loan activity summary for yourself or another user.
          a!loan summary
          a!loan summary @User
        """
        target = member or ctx.author

        # Non-mods can only check themselves
        if target != ctx.author and not ctx.author.guild_permissions.manage_guild:
            await ctx.reply("❌ You can only view your own summary.", mention_author=False)
            return

        stats = await db.get_loan_summary(ctx.guild.id, target.id)

        embed = discord.Embed(
            title=f"💼 Loan Summary — {target.display_name}",
            color=discord.Color.gold(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.add_field(
            name="Currently Lending",
            value=f"🟢 **{stats['lent_active']:,}** across active loans",
            inline=True,
        )
        embed.add_field(
            name="Currently Borrowing",
            value=f"🟡 **{stats['borrowed_active']:,}** outstanding",
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        embed.add_field(
            name="All-Time Lent",
            value=f"**{stats['lent_total']:,}** across **{stats['loans_given']}** loan(s)",
            inline=True,
        )
        embed.add_field(
            name="All-Time Borrowed",
            value=f"**{stats['borrowed_total']:,}** across **{stats['loans_received']}** loan(s)",
            inline=True,
        )

        await ctx.reply(embed=embed, mention_author=False)

    # ── Error handlers ───────────────────────────────────────────────────────

    @loan_server.error
    async def loan_server_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("❌ You need the **Manage Server** permission to use this.", mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(LoansCog(bot))
