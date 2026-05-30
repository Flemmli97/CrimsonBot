import asyncio
import re
from datetime import timedelta, timezone
from typing import Callable

import discord
from discord import Optional, app_commands, datetime
from discord.ext import commands

from utils.bot import EMBED_COLOR, Bot

SnowflakeTime = discord.abc.Snowflake | datetime

URL_REGEX = re.compile(r"https://\S+")


async def _purge(
        channel: discord.TextChannel | discord.channel.VocalGuildChannel | discord.Thread,
        amount: Optional[int] = 100,
        check: Callable[[discord.Message], bool] = None,
        after: Optional[SnowflakeTime] = None,
        oldest_first: Optional[bool] = None,
        exclude: Optional[int] = None,
) -> list[discord.Message]:
    limit = 1000
    if not check:
        check = lambda _: True
        limit = amount + 1 if amount else None
    if exclude:
        curr = check
        check = lambda msg: curr(msg) and msg.id != exclude

    deleted: list[discord.Message] = []
    to_delete: list[discord.Message] = []
    minimum_time = datetime.now(timezone.utc) - timedelta(days=13, hours=23)

    async def delete_batch(to_delete: list[discord.Message], deleted: list[discord.Message]):
        if len(to_delete) == 0:
            return
        await channel.delete_messages(to_delete)
        deleted += to_delete
        to_delete.clear()

    iterator = channel.history(limit=limit, after=after, oldest_first=oldest_first)
    async for message in iterator:
        if not check(message):
            continue
        # Can only bulk delete messages not older than 14 days
        if message.created_at <= minimum_time:
            await message.delete()
            deleted.append(message)
            continue

        to_delete.append(message)
        # Can only bulk delete max of 100 msg
        if len(to_delete) == 100:
            await delete_batch(to_delete, deleted)
            await asyncio.sleep(1)
        if amount and len(to_delete) >= amount:
            break

    await delete_batch(to_delete, deleted)
    return deleted


class PurgeCommand(commands.Cog):

    def __init__(self, bot: Bot):
        self.bot = bot
        self.logger = self.bot.logger.getChild("Moderation")

    group = app_commands.Group(name="purge", description="Purges messages",
                               default_permissions=discord.Permissions(administrator=True))

    @group.command(name="amount", description="Purges x recent messages")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    async def purge_amount(self, interaction: discord.Interaction, amount: int):
        await self._purge(interaction=interaction, amount=amount)

    @group.command(name="after", description="Purge x messages after the given one")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    async def purge_after(self, interaction: discord.Interaction, after: int, amount: Optional[int]):
        await self._purge(interaction=interaction, after=discord.Object(after), amount=amount, oldest_first=True)

    @group.command(name="bots", description="Purges x recent bot messages")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    async def purge_bots(self, interaction: discord.Interaction, amount: int):
        await self._purge(interaction=interaction, check=lambda msg: msg.author.bot, amount=amount)

    @group.command(name="regex", description="Purges x recent messages based on a regex")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    async def purge_regex(self, interaction: discord.Interaction, amount: int, regex: str):
        await self._purge(interaction=interaction, check=lambda msg: re.search(regex, msg.content), amount=amount)

    @group.command(name="links", description="Purges x recent messages with links")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    async def purge_links(self, interaction: discord.Interaction, amount: int):
        await self._purge(interaction=interaction, check=lambda msg: URL_REGEX.match(msg.content), amount=amount)

    @group.command(name="user", description="Purges x recent messages from the given user")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    async def purge_user(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        await self._purge(interaction=interaction, check=lambda msg: msg.author.id == user.id, amount=amount)

    async def _purge(
            self,
            interaction: discord.Interaction,
            amount: Optional[int] = 100,
            check: Callable[[discord.Message], bool] = None,
            after: Optional[SnowflakeTime] = None,
            oldest_first: Optional[bool] = None,
    ):
        await interaction.response.defer()
        msg = await _purge(interaction.channel, amount, check, after, oldest_first,
                           exclude=(await interaction.original_response()).id)
        msgs = len(msg)
        embed = discord.Embed(description=f"Purged {msgs} messages" if msgs != 1 else "Purged 1 message",
                              color=EMBED_COLOR)
        await interaction.edit_original_response(embed=embed)
        await self.bot.send_embed_mod_log(
            interaction.guild,
            f"{interaction.user.mention} purged the channel {interaction.channel.mention}\n\nMessages purged: {msgs}\n`/purge {interaction.command.name}`",
            interaction.user,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(PurgeCommand(bot))
