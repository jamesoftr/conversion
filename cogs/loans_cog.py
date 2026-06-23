"""
cogs/loans_cog.py  —  PokéCoin / PC loan tracker  (v2)

Features
────────
  • Issue loans with optional interest (flat or daily-compound) and due dates
  • Modal input for loan creation and payment proof (optional — classic args still work)
  • Attach payment proof with URL + paid date via modal
  • Record partial and full repayments with per-payment notes
  • Cancel a loan (lender or mod only)
  • View any loan by ID with full payment history
  • Personal loan dashboard with pagination (lent / borrowed / active / all)
  • Server-wide loan list (mod-only)
  • Overdue alert on every loan embed
  • Reset loans: all users or one user (bot owner only)

Commands  (prefix a!)
─────────────────────
  a!loan give  @user <amount> [pc|pokecoins] [--rate N] [--type flat|compound]
               [--due YYYY-MM-DD] [--proof <url>] [--note "text"]
  a!loan mgive @user              ← opens a modal (issue date | due date field)
  a!loan pay   <LOAN-ID> <amount> [--note "text"]
  a!loan cancel <LOAN-ID>
  a!loan info   <LOAN-ID>
  a!loan proof  <LOAN-ID>         ← opens a modal (URL + paid date)
  a!loan list  [lent|borrowed|active|all]
  a!loan server [active|paid|all]    (mod-only)
  a!loan summary [@user]
  a!loan reset all                   (bot owner only)
  a!loan reset @user                 (bot owner only)
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import commands

import db

# ── Constants ─────────────────────────────────────────────────────────────────

CURRENCY_ALIASES = {
    "pc":        "pc",
    "pcs":       "pc",
    "pokecoin":  "pokecoins",
    "pokecoins": "pokecoins",
    "coins":     "pokecoins",
    "coin":      "pokecoins",
}

CURRENCY_EMOJI = {
    "pc":        "🪙",
    "pokecoins": "<:pokecoin:0>",  # replace with your real emoji ID
}

STATUS_EMOJI = {
    "active":    "🟢",
    "partial":   "🟡",
    "paid":      "✅",
    "cancelled": "❌",
}

PAGE_SIZE = 5
ACTIVE_STATUSES = ("active", "partial")


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _cur(loan: dict) -> str:
    return CURRENCY_EMOJI.get(loan["currency"], "🪙")


def _fmt_amount(amount: float, loan: dict) -> str:
    v = int(amount) if amount == int(amount) else round(amount, 2)
    return f"{_cur(loan)} **{v:,}**"


def _interest_label(loan: dict) -> str:
    rate = loan["interest_rate"]
    if rate == 0 or loan["interest_type"] == "none":
        return "None (interest-free)"
    pct = f"{rate * 100:.4g}%"
    if loan["interest_type"] == "flat":
        return f"Flat {pct}"
    return f"Daily compound {pct}"


def _status_line(loan: dict) -> str:
    emoji = STATUS_EMOJI.get(loan["status"], "❓")
    return f"{emoji} {loan['status'].capitalize()}"


def _overdue_tag(loan: dict) -> str:
    if loan.get("due_date") and loan["status"] in ACTIVE_STATUSES:
        due = loan["due_date"]
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > due:
            return "  ⚠️ **OVERDUE**"
    return ""


def _amount_due_now(loan: dict) -> float:
    if loan["interest_type"] == "compound" and loan["interest_rate"] > 0:
        return db.compute_compound_due(
            loan["principal"], loan["interest_rate"], loan["created_at"]
        )
    return loan["amount_due"]


def _remaining(loan: dict) -> float:
    return max(0.0, round(_amount_due_now(loan) - loan["amount_paid"], 2))


def _unix(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


# ── Embed builders ─────────────────────────────────────────────────────────────

def _loan_embed(loan: dict, guild: discord.Guild) -> discord.Embed:
    status = loan["status"]
    color_map = {
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
    embed.add_field(name="Issued",      value=f"<t:{_unix(loan['created_at'])}:D>",  inline=True)

    if loan.get("due_date"):
        ts = _unix(loan["due_date"])
        embed.add_field(name="Due Date", value=f"<t:{ts}:D> (<t:{ts}:R>)", inline=True)

    if loan.get("note"):
        embed.add_field(name="📝 Note", value=loan["note"], inline=False)

    if loan.get("proof_url"):
        embed.add_field(name="🔗 Proof", value=f"[View proof]({loan['proof_url']})", inline=False)

    # Payment history (last 5)
    payments = loan.get("payments", [])
    if payments:
        lines = []
        for p in payments[-5:]:
            ts   = p["timestamp"]
            unix = _unix(ts)
            line = f"• <t:{unix}:d> — {_fmt_amount(p['amount'], loan)}"
            if p.get("paid_date"):
                line += f" *(paid: {p['paid_date']})*"
            if p.get("proof_url"):
                line += f" [[proof]]({p['proof_url']})"
            if p.get("note"):
                line += f" *— {p['note']}*"
            lines.append(line)
        if len(payments) > 5:
            lines.append(f"*…and {len(payments) - 5} earlier payment(s)*")
        embed.add_field(
            name=f"💸 Payments ({len(payments)})",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text=f"Loan ID: {loan['loan_id']}")
    return embed


def _loan_row(loan: dict, guild: discord.Guild) -> str:
    borrower = guild.get_member(loan["borrower_id"])
    lender   = guild.get_member(loan["lender_id"])
    b_name   = borrower.display_name if borrower else f"<@{loan['borrower_id']}>"
    l_name   = lender.display_name   if lender   else f"<@{loan['lender_id']}>"

    remaining = _remaining(loan)
    status    = STATUS_EMOJI.get(loan["status"], "❓")
    overdue   = "⚠️" if _overdue_tag(loan) else ""

    due = ""
    if loan.get("due_date"):
        due = f" · due <t:{_unix(loan['due_date'])}:d>"

    return (
        f"{status}{overdue} **{loan['loan_id']}** — "
        f"**{l_name}** → **{b_name}** — "
        f"{_fmt_amount(remaining, loan)} remaining{due}"
    )


# ── Arg parser for classic a!loan give ────────────────────────────────────────

def _parse_give_args(raw: str) -> dict:
    """
    Parse: <amount> [currency] [--rate N] [--type flat|compound]
           [--due YYYY-MM-DD] [--proof URL] [--note "text"]
    The caller must strip the borrower mention before passing raw.
    """
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"Could not parse arguments: {exc}") from exc

    kwargs    = {}
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

    if not remaining:
        raise ValueError("Usage: `a!loan give @user <amount> [pc|pokecoins] [--flags…]`")

    try:
        amount = int(remaining[0].replace(",", ""))
        if amount <= 0:
            raise ValueError()
    except ValueError:
        raise ValueError(f"`{remaining[0]}` is not a valid amount.")

    currency_raw = remaining[1].lower() if len(remaining) > 1 else "pc"
    currency = CURRENCY_ALIASES.get(currency_raw)
    if not currency:
        raise ValueError(f"Unknown currency `{currency_raw}`. Use `pc` or `pokecoins`.")

    interest_rate = 0.0
    if "rate" in kwargs:
        try:
            interest_rate = float(kwargs["rate"]) / 100
            if interest_rate < 0:
                raise ValueError()
        except ValueError:
            raise ValueError(f"`--rate {kwargs['rate']}` must be a non-negative number.")

    interest_type = "flat" if interest_rate > 0 else "none"
    if "type" in kwargs:
        t = kwargs["type"].lower()
        if t not in ("flat", "compound", "none"):
            raise ValueError("`--type` must be `flat`, `compound`, or `none`.")
        interest_type = t

    if interest_rate > 0 and interest_type == "none":
        interest_type = "flat"

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


# ── Modals ─────────────────────────────────────────────────────────────────────

class LoanGiveModal(discord.ui.Modal, title="Give a Loan"):
    # Discord hard limit: 5 components per modal.
    # Currency defaults to "pc"; grant date defaults to now.
    amount = discord.ui.TextInput(
        label="Amount",
        placeholder="e.g. 5000",
        required=True,
        max_length=20,
    )
    proof_url = discord.ui.TextInput(
        label="Proof Link (optional)",
        placeholder="https://discord.com/channels/...",
        required=False,
        max_length=500,
    )
    interest = discord.ui.TextInput(
        label="Interest — rate% type (optional)",
        placeholder="e.g.  5 flat   or   1 compound   or leave blank",
        required=False,
        max_length=40,
    )
    dates = discord.ui.TextInput(
        label="Issue Date | Due Date (optional)",
        placeholder="e.g.  2025-06-01 | 2025-12-31   or just  | 2025-12-31",
        required=False,
        max_length=25,
    )
    note = discord.ui.TextInput(
        label="Note (optional)",
        placeholder="Reason for loan, terms, etc.",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=200,
    )

    def __init__(self, borrower: discord.Member):
        super().__init__()
        self.borrower = borrower

    async def on_submit(self, interaction: discord.Interaction):
        # Parse amount
        try:
            amount = int(self.amount.value.replace(",", "").strip())
            if amount <= 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message(
                "❌ Amount must be a positive integer.", ephemeral=True
            )
            return

        # Currency — always "pc" in modal (use classic args for pokecoins)
        currency = "pc"

        # Interest  — parse "5 flat" / "1 compound" / "5" / ""
        interest_rate = 0.0
        interest_type = "none"
        raw_interest  = (self.interest.value or "").strip()
        if raw_interest:
            parts = raw_interest.split()
            try:
                interest_rate = float(parts[0]) / 100
                if interest_rate < 0:
                    raise ValueError()
            except ValueError:
                await interaction.response.send_message(
                    "❌ Interest rate must be a non-negative number.", ephemeral=True
                )
                return
            if len(parts) >= 2:
                itype = parts[1].lower()
                if itype not in ("flat", "compound"):
                    await interaction.response.send_message(
                        "❌ Interest type must be `flat` or `compound`.", ephemeral=True
                    )
                    return
                interest_type = itype
            elif interest_rate > 0:
                interest_type = "flat"

        # Issue date | Due date  — split on "|"
        issue_date = None
        due_date   = None
        raw_dates  = (self.dates.value or "").strip()
        if raw_dates:
            parts = [p.strip() for p in raw_dates.split("|", 1)]
            raw_issue = parts[0]
            raw_due   = parts[1] if len(parts) > 1 else ""

            if raw_issue:
                try:
                    issue_date = datetime.strptime(raw_issue, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    await interaction.response.send_message(
                        "❌ Issue date must be `YYYY-MM-DD` (left of the `|`).", ephemeral=True
                    )
                    return

            if raw_due:
                try:
                    due_date = datetime.strptime(raw_due, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    await interaction.response.send_message(
                        "❌ Due date must be `YYYY-MM-DD` (right of the `|`).", ephemeral=True
                    )
                    return

        proof_url = (self.proof_url.value or "").strip() or None
        note      = (self.note.value or "").strip() or None

        await interaction.response.defer()

        loan_doc = await db.create_loan(
            guild_id      = interaction.guild.id,
            lender_id     = interaction.user.id,
            borrower_id   = self.borrower.id,
            principal     = amount,
            currency      = currency,
            interest_rate = interest_rate,
            interest_type = interest_type,
            due_date      = due_date,
            proof_url     = proof_url,
            note          = note,
            created_at    = issue_date,  # None = use current time; set if user provided issue date
        )

        embed = _loan_embed(loan_doc, interaction.guild)
        embed.set_author(name="✅ Loan Created (via modal)")
        await interaction.followup.send(embed=embed)


class LoanProofModal(discord.ui.Modal, title="Attach Payment Proof"):
    proof_url = discord.ui.TextInput(
        label="Proof URL (message link or image URL)",
        placeholder="https://discord.com/channels/...",
        required=True,
        max_length=500,
    )
    paid_date = discord.ui.TextInput(
        label="Date Paid (YYYY-MM-DD, optional)",
        placeholder="Leave blank to use today",
        required=False,
        max_length=10,
    )
    pay_note = discord.ui.TextInput(
        label="Note (optional)",
        placeholder="e.g. final payment",
        required=False,
        max_length=200,
    )

    def __init__(self, loan_id: str):
        super().__init__()
        self.loan_id = loan_id

    async def on_submit(self, interaction: discord.Interaction):
        url       = self.proof_url.value.strip()
        note      = (self.pay_note.value or "").strip() or None
        raw_date  = (self.paid_date.value or "").strip()
        paid_date = None

        if raw_date:
            try:
                datetime.strptime(raw_date, "%Y-%m-%d")  # validate
                paid_date = raw_date
            except ValueError:
                await interaction.response.send_message(
                    "❌ Paid date must be `YYYY-MM-DD`.", ephemeral=True
                )
                return

        await interaction.response.defer()

        loan_doc = await db.get_loan(self.loan_id)
        if not loan_doc:
            await interaction.followup.send(f"❌ Loan `{self.loan_id}` not found.", ephemeral=True)
            return

        # Check permission: lender, borrower, or mod
        is_involved = interaction.user.id in (loan_doc["lender_id"], loan_doc["borrower_id"])
        is_mod      = interaction.user.guild_permissions.manage_guild
        if not (is_involved or is_mod):
            await interaction.followup.send("❌ You're not involved in this loan.", ephemeral=True)
            return

        updated = await db.update_loan_proof_with_meta(self.loan_id, url, note=note, paid_date=paid_date)
        embed   = _loan_embed(updated, interaction.guild)
        embed.set_author(name="🔗 Proof Attached (via modal)")
        await interaction.followup.send(embed=embed)


# ── Pay modal ─────────────────────────────────────────────────────────────────

class LoanPayModal(discord.ui.Modal, title="Record Loan Payment"):
    amount = discord.ui.TextInput(
        label="Amount Paid",
        placeholder="e.g. 2500",
        required=True,
        max_length=20,
    )
    proof_url = discord.ui.TextInput(
        label="Proof URL (optional)",
        placeholder="https://discord.com/channels/...",
        required=False,
        max_length=500,
    )
    paid_date = discord.ui.TextInput(
        label="Date Paid (YYYY-MM-DD, optional)",
        placeholder="Leave blank to use today",
        required=False,
        max_length=10,
    )
    pay_note = discord.ui.TextInput(
        label="Note (optional)",
        placeholder="e.g. first instalment",
        required=False,
        max_length=200,
    )

    def __init__(self, loan_id: str):
        super().__init__()
        self.loan_id = loan_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            pay_amount = float(self.amount.value.replace(",", "").strip())
            if pay_amount <= 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("❌ Amount must be a positive number.", ephemeral=True)
            return

        url       = (self.proof_url.value or "").strip() or None
        note      = (self.pay_note.value  or "").strip() or None
        raw_date  = (self.paid_date.value or "").strip()
        paid_date = None

        if raw_date:
            try:
                datetime.strptime(raw_date, "%Y-%m-%d")
                paid_date = raw_date
            except ValueError:
                await interaction.response.send_message("❌ Paid date must be `YYYY-MM-DD`.", ephemeral=True)
                return

        await interaction.response.defer()

        loan_doc = await db.get_loan(self.loan_id)
        if not loan_doc:
            await interaction.followup.send(f"❌ Loan `{self.loan_id}` not found.", ephemeral=True)
            return

        is_involved = interaction.user.id in (loan_doc["lender_id"], loan_doc["borrower_id"])
        is_mod      = interaction.user.guild_permissions.manage_guild
        if not (is_involved or is_mod):
            await interaction.followup.send("❌ Only the lender or borrower can record payments.", ephemeral=True)
            return

        if loan_doc["status"] in ("paid", "cancelled"):
            await interaction.followup.send(f"❌ This loan is already **{loan_doc['status']}**.", ephemeral=True)
            return

        updated = await db.record_payment(self.loan_id, pay_amount, note=note, proof_url=url, paid_date=paid_date)
        embed   = _loan_embed(updated, interaction.guild)
        embed.set_author(name="💸 Payment Recorded (via modal)")
        await interaction.followup.send(embed=embed)


# ── Loan action view (buttons on loan info embed) ─────────────────────────────

class LoanActionView(discord.ui.View):
    """Shown on a!loan info — quick action buttons."""

    def __init__(self, loan: dict, invoker: discord.Member):
        super().__init__(timeout=120)
        self.loan    = loan
        self.invoker = invoker
        loan_id = loan["loan_id"]

        # Pay button
        pay_btn = discord.ui.Button(
            label="💸 Record Payment",
            style=discord.ButtonStyle.green,
            custom_id=f"loan_pay_{loan_id}",
            disabled=loan["status"] in ("paid", "cancelled"),
        )
        pay_btn.callback = self._pay_callback
        self.add_item(pay_btn)

        # Proof button
        proof_btn = discord.ui.Button(
            label="🔗 Attach Proof",
            style=discord.ButtonStyle.blurple,
            custom_id=f"loan_proof_{loan_id}",
        )
        proof_btn.callback = self._proof_callback
        self.add_item(proof_btn)

    async def _pay_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.invoker.id:
            await interaction.response.send_message("❌ Not your button.", ephemeral=True)
            return
        await interaction.response.send_modal(LoanPayModal(self.loan["loan_id"]))

    async def _proof_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.invoker.id:
            await interaction.response.send_message("❌ Not your button.", ephemeral=True)
            return
        await interaction.response.send_modal(LoanProofModal(self.loan["loan_id"]))


# ── Paginated list view ────────────────────────────────────────────────────────

class LoanListView(discord.ui.View):

    def __init__(self, loans: list[dict], guild: discord.Guild, title: str, invoker_id: int):
        super().__init__(timeout=300)
        self.loans       = loans
        self.guild       = guild
        self.title       = title
        self.invoker_id  = invoker_id
        self.page        = 0
        self.total_pages = max(1, (len(loans) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def _build_embed(self) -> discord.Embed:
        start = self.page * PAGE_SIZE
        chunk = self.loans[start : start + PAGE_SIZE]
        lines = [_loan_row(l, self.guild) for l in chunk] or ["*No loans found.*"]
        embed = discord.Embed(
            title       = self.title,
            description = "\n".join(lines),
            color       = discord.Color.gold(),
        )
        embed.set_footer(text=f"Page {self.page + 1}/{self.total_pages}  •  {len(self.loans)} loans total")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ Not your menu.", ephemeral=True)
            return
        await interaction.response.defer()
        self.page -= 1
        self._update_buttons()
        await interaction.message.edit(embed=self._build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ Not your menu.", ephemeral=True)
            return
        await interaction.response.defer()
        self.page += 1
        self._update_buttons()
        await interaction.message.edit(embed=self._build_embed(), view=self)


# ── Confirm view for destructive actions ──────────────────────────────────────

class ConfirmView(discord.ui.View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=30)
        self.invoker_id = invoker_id
        self.confirmed  = False

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ Not your button.", ephemeral=True)
            return
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ Not your button.", ephemeral=True)
            return
        self.stop()
        await interaction.response.defer()


# ── Cog ────────────────────────────────────────────────────────────────────────

class LoansCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Group ──────────────────────────────────────────────────────────────────

    @commands.group(name="loan", aliases=["loans"], invoke_without_command=True)
    async def loan(self, ctx: commands.Context):
        """Loan tracker. Use `a!loan help` for all subcommands."""
        embed = discord.Embed(
            title="🏦 Loan Commands",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Creating Loans",
            value=(
                "`a!loan give @user` — opens a **modal form** (recommended)\n"
                "`a!loan give <user_id>` — same, using a raw Discord ID\n"
                "`a!loan give @user <amount> [pc|pokecoins] [--rate N] [--type flat|compound] [--due YYYY-MM-DD] [--note text]` — classic one-liner"
            ),
            inline=False,
        )
        embed.add_field(
            name="Managing Loans",
            value=(
                "`a!loan pay <ID>` — opens a **modal** (amount + proof + date + note)\n"
                "`a!loan pay <ID> <amount> [--note text]` — classic quick payment\n"
                "`a!loan proof <ID>` — attach proof via **modal**\n"
                "`a!loan cancel <ID>` — cancel a loan\n"
                "`a!loan info <ID>` — full details + action buttons"
            ),
            inline=False,
        )
        embed.add_field(
            name="Viewing Loans",
            value=(
                "`a!loan list [lent|borrowed|active|all]`\n"
                "`a!loan server [active|paid|all]` *(mod only)*\n"
                "`a!loan summary [@user]`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Owner Only",
            value=(
                "`a!loan reset all` — wipe ALL loans in server\n"
                "`a!loan reset @user` — wipe one user's loans"
            ),
            inline=False,
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── a!loan give — modal button OR classic inline args ─────────────────────
    #
    #   a!loan give @User              → sends a button that opens the full modal
    #   a!loan give <user_id>          → same, resolves the ID to a member first
    #   a!loan give @User 5000 pc ...  → classic inline args, no modal needed
    #
    # The modal path is the recommended default; classic args still work for
    # power-users who want a one-liner.

    @loan.command(name="give")
    async def loan_give(self, ctx: commands.Context, borrower_arg: str, *, args: str = ""):
        """
        Issue a loan.
        • a!loan give @User / <id>          → opens a modal form (button)
        • a!loan give @User <amount> [opts] → classic inline mode
        """
        # ── Resolve borrower (mention OR raw ID) ──────────────────────────────
        borrower: discord.Member | None = None

        # Try discord.py's converter first (handles <@id> mentions)
        try:
            converter = commands.MemberConverter()
            borrower  = await converter.convert(ctx, borrower_arg)
        except commands.BadArgument:
            pass

        # Fall back to raw integer ID
        if borrower is None:
            raw = re.sub(r"[<@!>]", "", borrower_arg)
            if raw.isdigit():
                borrower = ctx.guild.get_member(int(raw))
                if borrower is None:
                    try:
                        borrower = await ctx.guild.fetch_member(int(raw))
                    except discord.NotFound:
                        pass

        if borrower is None:
            await ctx.reply(
                f"❌ Could not find a member matching `{borrower_arg}`. "
                "Use a mention or a valid user ID.",
                mention_author=False,
            )
            return

        if borrower.bot:
            await ctx.reply("❌ You can't loan to a bot.", mention_author=False)
            return
        if borrower == ctx.author:
            await ctx.reply("❌ You can't loan to yourself.", mention_author=False)
            return

        # ── No inline args → open modal via button ────────────────────────────
        if not args.strip():
            modal = LoanGiveModal(borrower)
            view  = _ModalTriggerView(ctx.author.id, modal, label="📝 Open Loan Form")
            await ctx.reply(
                f"Click the button below to fill in the loan form for "
                f"**{borrower.display_name}**:",
                view=view,
                mention_author=False,
            )
            return

        # ── Inline args → classic path ────────────────────────────────────────
        try:
            parsed = _parse_give_args(args.strip())
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

    # ── a!loan mgive (modal) — kept as alias for backwards compat ─────────────

    @loan.command(name="mgive")
    async def loan_mgive(self, ctx: commands.Context, borrower: discord.Member):
        """Open the loan modal form for @user (alias for `a!loan give @user`)."""
        if borrower.bot:
            await ctx.reply("❌ You can't loan to a bot.", mention_author=False)
            return
        if borrower == ctx.author:
            await ctx.reply("❌ You can't loan to yourself.", mention_author=False)
            return
        modal = LoanGiveModal(borrower)
        view  = _ModalTriggerView(ctx.author.id, modal, label="📝 Open Loan Form")
        await ctx.reply(
            f"Click the button below to fill in the loan form for **{borrower.display_name}**:",
            view=view,
            mention_author=False,
        )

    # ── a!loan pay — modal button OR classic inline ───────────────────────────
    #
    #   a!loan pay <loan_id>              → sends a button that opens the pay modal
    #   a!loan pay <loan_id> <amount>     → classic inline, records immediately
    #
    # The modal includes amount, proof URL, paid date, and note fields.
    # Classic mode only sets amount + optional --note for quick one-liners.

    @loan.command(name="pay")
    async def loan_pay(self, ctx: commands.Context, loan_id: str, amount: str = "", *, args: str = ""):
        """
        Record a repayment.
        • a!loan pay <ID>              → opens a modal (amount + proof + date + note)
        • a!loan pay <ID> <amount>     → classic inline mode
        """
        loan_id  = loan_id.upper()
        loan_doc = await db.get_loan(loan_id)
        if not loan_doc:
            await ctx.reply(f"❌ Loan `{loan_id}` not found.", mention_author=False)
            return

        is_involved = ctx.author.id in (loan_doc["lender_id"], loan_doc["borrower_id"])
        is_mod      = ctx.author.guild_permissions.manage_guild
        if not (is_involved or is_mod):
            await ctx.reply("❌ Only the lender or borrower can record payments.", mention_author=False)
            return

        if loan_doc["status"] in ("paid", "cancelled"):
            await ctx.reply(f"❌ This loan is already **{loan_doc['status']}**.", mention_author=False)
            return

        # ── No amount → open modal via button ─────────────────────────────────
        if not amount.strip():
            modal = LoanPayModal(loan_id)
            view  = _ModalTriggerView(ctx.author.id, modal, label="💸 Record Payment")
            await ctx.reply(
                f"Click the button below to record a payment for **{loan_id}**:",
                view=view,
                mention_author=False,
            )
            return

        # ── Amount provided → classic inline path ─────────────────────────────
        try:
            pay_amount = float(amount.replace(",", ""))
            if pay_amount <= 0:
                raise ValueError()
        except ValueError:
            await ctx.reply(f"❌ `{amount}` is not a valid amount.", mention_author=False)
            return

        note = None
        m = re.search(r"--note\s+(.+)", args)
        if m:
            note = m.group(1).strip().strip("\"'")

        updated = await db.record_payment(loan_id, pay_amount, note=note)
        embed   = _loan_embed(updated, ctx.guild)
        embed.set_author(name="💸 Payment Recorded")
        await ctx.reply(embed=embed, mention_author=False)

    # ── a!loan mpay (modal) — kept as alias for backwards compat ─────────────

    @loan.command(name="mpay")
    async def loan_mpay(self, ctx: commands.Context, loan_id: str):
        """Open the payment modal for a loan (alias for `a!loan pay <id>`)."""
        loan_id  = loan_id.upper()
        loan_doc = await db.get_loan(loan_id)
        if not loan_doc:
            await ctx.reply(f"❌ Loan `{loan_id}` not found.", mention_author=False)
            return
        if loan_doc["status"] in ("paid", "cancelled"):
            await ctx.reply(f"❌ This loan is already **{loan_doc['status']}**.", mention_author=False)
            return
        is_involved = ctx.author.id in (loan_doc["lender_id"], loan_doc["borrower_id"])
        is_mod      = ctx.author.guild_permissions.manage_guild
        if not (is_involved or is_mod):
            await ctx.reply("❌ Only the lender or borrower can record payments.", mention_author=False)
            return
        modal = LoanPayModal(loan_id)
        view  = _ModalTriggerView(ctx.author.id, modal, label="💸 Record Payment")
        await ctx.reply(
            f"Click the button below to record a payment for **{loan_id}**:",
            view=view,
            mention_author=False,
        )

    # ── a!loan cancel ─────────────────────────────────────────────────────────

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

    # ── a!loan info ───────────────────────────────────────────────────────────

    @loan.command(name="info")
    async def loan_info(self, ctx: commands.Context, loan_id: str):
        """Show full details and payment history for a loan."""
        loan_doc = await db.get_loan(loan_id.upper())
        if not loan_doc:
            await ctx.reply(f"❌ Loan `{loan_id}` not found.", mention_author=False)
            return

        involved = ctx.author.id in (loan_doc["lender_id"], loan_doc["borrower_id"])
        is_mod   = ctx.author.guild_permissions.manage_guild
        if not (involved or is_mod):
            await ctx.reply("❌ You're not involved in this loan.", mention_author=False)
            return

        view = LoanActionView(loan_doc, ctx.author)
        await ctx.reply(embed=_loan_embed(loan_doc, ctx.guild), view=view, mention_author=False)

    # ── a!loan proof (modal) ──────────────────────────────────────────────────

    @loan.command(name="proof")
    async def loan_proof(self, ctx: commands.Context, loan_id: str):
        """Attach proof to a loan via modal (URL + paid date + note)."""
        loan_id  = loan_id.upper()
        loan_doc = await db.get_loan(loan_id)
        if not loan_doc:
            await ctx.reply(f"❌ Loan `{loan_id}` not found.", mention_author=False)
            return

        is_involved = ctx.author.id in (loan_doc["lender_id"], loan_doc["borrower_id"])
        is_mod      = ctx.author.guild_permissions.manage_guild
        if not (is_involved or is_mod):
            await ctx.reply("❌ You're not involved in this loan.", mention_author=False)
            return

        modal = LoanProofModal(loan_id)
        view  = _ModalTriggerView(ctx.author.id, modal, label="🔗 Attach Proof")
        await ctx.reply(f"Click below to attach proof for **{loan_id}**:", view=view, mention_author=False)

    # ── a!loan list ───────────────────────────────────────────────────────────

    @loan.command(name="list")
    async def loan_list(self, ctx: commands.Context, mode: str = "active"):
        """
        Show your loans.
          a!loan list           — active + partial (default)
          a!loan list lent      — only loans you gave
          a!loan list borrowed  — only loans you received
          a!loan list all       — every loan regardless of status
        """
        mode     = mode.lower()
        user_id  = ctx.author.id
        guild_id = ctx.guild.id

        # BUG FIX: active mode must include BOTH "active" and "partial" statuses.
        # The old code passed status="active" which excluded partial loans entirely.
        if mode == "all":
            status_filter = None
        else:
            # We'll filter in-memory after fetching so we can match multiple statuses.
            status_filter = None  # fetch everything, filter below

        if mode in ("lent", "active", "all"):
            lent = await db.get_loans_as_lender(guild_id, user_id, status=None)
        else:
            lent = []

        if mode in ("borrowed", "active", "all"):
            borrowed = await db.get_loans_as_borrower(guild_id, user_id, status=None)
        else:
            borrowed = []

        # Merge + deduplicate
        seen     = set()
        combined = []
        for l in lent + borrowed:
            lid = l["loan_id"]
            if lid not in seen:
                seen.add(lid)
                combined.append(l)

        # Filter by mode
        if mode in ("active", "lent", "borrowed"):
            # Show active AND partial (the real "outstanding" loans)
            combined = [l for l in combined if l["status"] in ACTIVE_STATUSES]
        # "all" → no filter

        combined.sort(key=lambda x: x["created_at"], reverse=True)

        if not combined:
            await ctx.reply("📭 No loans found.", mention_author=False)
            return

        title = f"📋 Your Loans — {mode.capitalize()}"
        view  = LoanListView(combined, ctx.guild, title, ctx.author.id)
        await ctx.reply(embed=view._build_embed(), view=view, mention_author=False)

    # ── a!loan server ─────────────────────────────────────────────────────────

    @loan.command(name="server")
    @commands.has_permissions(manage_guild=True)
    async def loan_server(self, ctx: commands.Context, mode: str = "active"):
        """(Mod only) View all server loans. Modes: active | paid | all"""
        mode = mode.lower()

        loans = await db.get_all_guild_loans(ctx.guild.id, status=None)

        # Filter
        if mode == "active":
            loans = [l for l in loans if l["status"] in ACTIVE_STATUSES]
        elif mode == "paid":
            loans = [l for l in loans if l["status"] == "paid"]
        # "all" → no filter

        if not loans:
            await ctx.reply("📭 No loans found.", mention_author=False)
            return

        title = f"🏦 Server Loans — {mode.capitalize()}"
        view  = LoanListView(loans, ctx.guild, title, ctx.author.id)
        await ctx.reply(embed=view._build_embed(), view=view, mention_author=False)

    # ── a!loan summary ────────────────────────────────────────────────────────

    @loan.command(name="summary")
    async def loan_summary(self, ctx: commands.Context, member: discord.Member = None):
        """Show loan activity summary. a!loan summary or a!loan summary @User"""
        target = member or ctx.author

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

    # ── a!loan reset (owner only) ─────────────────────────────────────────────

    @loan.command(name="reset")
    async def loan_reset(self, ctx: commands.Context, target: str = None, member: discord.Member = None):
        """
        (Bot owner only) Reset loan data.
          a!loan reset all       — wipe ALL loans for this server
          a!loan reset @user     — wipe one user's loans
        """
        if not await self.bot.is_owner(ctx.author):
            await ctx.reply("❌ Only the bot owner can reset loans.", mention_author=False)
            return

        # Determine what we're resetting
        reset_all    = False
        reset_member = None

        if target is None and member is None:
            await ctx.reply(
                "Usage:\n`a!loan reset all` — wipe all server loans\n`a!loan reset @user` — wipe one user's loans",
                mention_author=False,
            )
            return

        if target and target.lower() == "all":
            reset_all = True
        elif member:
            reset_member = member
        else:
            # Maybe target is a mention and member wasn't parsed
            # Try to interpret target as a user mention/id
            try:
                uid = int(re.sub(r"[<@!>]", "", target))
                reset_member = ctx.guild.get_member(uid)
                if not reset_member:
                    await ctx.reply(f"❌ Could not find that user.", mention_author=False)
                    return
            except ValueError:
                await ctx.reply(
                    "❌ Invalid target. Use `all` or mention a user.",
                    mention_author=False,
                )
                return

        # Confirm
        if reset_all:
            confirm_msg = f"⚠️ This will **permanently delete ALL loans** in **{ctx.guild.name}**. Are you sure?"
        else:
            confirm_msg = f"⚠️ This will **permanently delete all loans** for **{reset_member.display_name}**. Are you sure?"

        view = ConfirmView(ctx.author.id)
        msg  = await ctx.reply(confirm_msg, view=view, mention_author=False)
        await view.wait()

        if not view.confirmed:
            await msg.edit(content="❌ Reset cancelled.", view=None)
            return

        if reset_all:
            deleted = await db.reset_all_loans(ctx.guild.id)
            await msg.edit(
                content=f"✅ Deleted **{deleted}** loan(s) from the server.",
                view=None,
            )
        else:
            deleted = await db.reset_user_loans(ctx.guild.id, reset_member.id)
            await msg.edit(
                content=f"✅ Deleted **{deleted}** loan(s) for **{reset_member.display_name}**.",
                view=None,
            )

    # ── Error handlers ────────────────────────────────────────────────────────

    @loan_server.error
    async def loan_server_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("❌ You need the **Manage Server** permission to use this.", mention_author=False)

    @loan_reset.error
    async def loan_reset_error(self, ctx: commands.Context, error):
        # Handle the case where @user is passed as the `target` positional arg
        # but discord.py tries to convert member and fails
        if isinstance(error, commands.BadArgument):
            await ctx.reply(f"❌ {error}", mention_author=False)


# ── Helper: trigger a modal from a button (prefix command workaround) ──────────

class _ModalTriggerView(discord.ui.View):
    """A button that opens a modal. Required because modals can only be
    sent in response to an interaction, not a plain message (prefix command).

    timeout=None keeps the button alive indefinitely (until bot restart).
    Do NOT call self.stop() after send_modal — it races with Discord's ack
    and causes 'Interaction Failed' for the user.
    """

    def __init__(self, invoker_id: int, modal: discord.ui.Modal, label: str = "Open Form"):
        super().__init__(timeout=None)
        self.invoker_id = invoker_id
        self.modal      = modal

        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.blurple)
        btn.callback = self._open_modal
        self.add_item(btn)

    async def _open_modal(self, interaction: discord.Interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ Not your button.", ephemeral=True)
            return
        await interaction.response.send_modal(self.modal)


async def setup(bot: commands.Bot):
    await bot.add_cog(LoansCog(bot))
