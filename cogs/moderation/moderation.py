import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import discord
from discord import Object, app_commands
from discord.ext import commands

from utils.bot import Bot
from utils.database import CogDatabase
from utils.sqlutils import SQLCodec, Schema, SQLSchema


@dataclass
class TempBanEntry(Schema):
    user: int
    banned_till: Annotated[datetime, SQLCodec[datetime](lambda d: d.isoformat(), lambda s: datetime.fromisoformat(s))]

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema="""
                        user int(25) NOT NULL,
                        banned_till varchar(32) NOT NULL,
                    """, keys=["user"])
        return schema


class Moderation(commands.Cog):

    def __init__(self, bot: Bot):
        self.bot = bot
        self.logger = self.bot.logger.getChild("Moderation")
        self.temp_ban_handler = asyncio.create_task(self.temp_ban_update())
        self.temp_bans: CogDatabase[TempBanEntry] | None = None

    async def load_db(self) -> None:
        self.temp_bans = await self.bot.database.get_for(self, "temp_bans", TempBanEntry)

    async def cog_load(self):
        await self.load_db()

    @app_commands.command(name="softban", description="Softbans (ban and unbans) user to delete all recent messages")
    @app_commands.default_permissions(discord.Permissions(ban_members=True))
    @app_commands.describe(
        user="The user to ban",
        delete_days="How far back messages will be deleted. Default is 3 days",
    )
    async def softban(self, interaction: discord.Interaction, user: discord.Member, delete_days: Optional[int]):
        if user.bot or user.guild_permissions.administrator:
            await interaction.response.send_message(f"Cannot ban this user", ephemeral=True)
            return
        self.logger.info(f"{interaction.guild.name}: Softbanning {user.name}")
        delete_days = delete_days or 3
        await user.ban(delete_message_days=delete_days, reason="Softban")
        await user.unban(reason="Softban")
        await interaction.response.send_message(f"Softbanned {user.id} for channel", ephemeral=True)
        await self.bot.send_embed_mod_log(interaction.guild, f"{interaction.user.mention} softbanned {user.mention}",
                                          interaction.user)

    @app_commands.command(name="slowmode", description="Sets slowmode for chat")
    @app_commands.default_permissions(discord.Permissions(manage_channels=True))
    @app_commands.describe(
        channel="The channel in which to enable slowmode. Defaults to the current channel",
        time="Duration for slowmode in seconds. If 0 removes slowmode. Defaults to 0",
    )
    async def slowmode(
            self,
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.Thread],
            time: Optional[int]
    ):
        channel = channel or interaction.channel
        time = max(0, time or 0)
        if channel.slowmode_delay == time:
            await interaction.response.send_message(f"Channel already has this slowmode state", ephemeral=True)
            return
        self.logger.info(f"{interaction.guild.name}: Setting slowmode for channel {channel.name} to {time}s")
        await channel.edit(slowmode_delay=time)
        await interaction.response.send_message(
            f"Setting slowmode in channel {channel.mention} to {time}s" if time > 0 else f"Removing slowmode for channel {channel.mention}",
            ephemeral=True
        )
        await self.bot.send_embed_mod_log(
            interaction.guild,
            f"{interaction.user.mention} enabled slowmode ({time}s) in channel {channel.mention}"
            if time != 0
            else f"{interaction.user.mention} removed slowmode in channel {channel.mention}",
            interaction.user,
        )

    @app_commands.command(name="tempban", description="Bans a user for the given duration")
    @app_commands.default_permissions(discord.Permissions(ban_members=True))
    @app_commands.describe(
        user="User to ban",
        duration_minutes="Duration in minutes to ban for",
        duration_hours="Duration in hours to ban for",
        duration_days="Duration in days to ban for",
        delete_days="How far back messages will be deleted. Default is 3 days",
        reason="The reason for the ban. This shows up in the audit logs",
    )
    async def temp_ban_cmd(
            self,
            interaction: discord.Interaction,
            user: discord.Member,
            duration_minutes: Optional[int],
            duration_hours: Optional[int],
            duration_days: Optional[int],
            delete_days: Optional[int],
            reason: Optional[str],
    ):
        if user.bot or user.guild_permissions.administrator:
            await interaction.response.send_message(f"Cannot ban this user", ephemeral=True)
            return
        if not duration_minutes and not duration_hours and not duration_days:
            await interaction.response.send_message(
                f"Invalid ban time duration. One of minute/hours/days must be specified!", ephemeral=True)
            return
        end_time = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes or 0, hours=duration_hours or 0,
                                                          days=duration_days or 0)
        entry = await self.temp_ban(interaction.guild, user, end_time, reason, delete_days=delete_days or 3)
        if entry:
            await interaction.response.send_message(
                f"User is already temporary banned till <t:{round(entry.banned_till.timestamp())}:s>", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Banned user {user.mention} till <t:{round(end_time.timestamp())}:s>", ephemeral=True)

    async def temp_ban(
            self, guild: discord.Guild, user: discord.Member, end_time: datetime, reason: Optional[str],
            delete_days: int | None = None, delete_seconds: int | None = None
    ):
        current = await self.temp_bans.get(guild.id, user=user.id)
        if current and current.banned_till > end_time:
            return current
        self.logger.info(f"{guild.name}: Temporary banning {user.name}{f' {reason}' if reason else ''} till {end_time}")
        self.bot._temp_banning.append((guild.id, user.id))
        reason_str = f": **{reason}**" if reason else ""
        await user.send(f"You got banned from {guild.name} till <t:{round(end_time.timestamp())}:s>{reason_str}")
        await user.ban(delete_message_days=delete_days, delete_message_seconds=delete_seconds, reason=reason)
        entry = TempBanEntry(user=user.id, banned_till=end_time)
        await self.temp_bans.upsert(guild.id, entry)
        await self.bot.send_embed_mod_log(guild,
                                          f"{user.mention} banned {user.mention} till <t:{round(end_time.timestamp())}:s>{reason_str}",
                                          user)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        entry = await self.temp_bans.get(guild.id, user=user.id)
        if entry:
            self.logger.info(f"{guild.name}: Lifting temp ban for {user.name}")
            await self.temp_bans.remove(guild.id, user=user.id)

    async def temp_ban_update(self) -> None:
        while True:
            if self.temp_bans:
                try:
                    entries = await self.temp_bans.get_all_of()
                    now = datetime.now(timezone.utc)
                    for guild, entry in entries:
                        if now >= entry.banned_till:
                            await self.temp_bans.remove(guild=guild, user=entry.user)
                            await self.bot.get_guild(guild).unban(user=Object(entry.user), reason="Temp Ban expired")
                except Exception:
                    self.logger.exception("Something went wrong in updating temp bans")
            await asyncio.sleep(10)


async def setup(bot: Bot) -> None:
    await bot.add_cog(Moderation(bot))
