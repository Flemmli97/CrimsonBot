from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.moderation.moderation import Moderation
from utils.bot import EMBED_COLOR, Bot
from utils.database import CogDatabase
from utils.sqlutils import Schema, SQLSchema


@dataclass
class HoneypotConfig(Schema):
    channel: None | int
    ignored_roles: list[int]
    ignored_users: list[int]

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema="""
                        channel int(25),
                        ignored_roles json,
                        ignored_users json,
                    """, keys=[])
        return schema

    def empty(self) -> bool:
        return not self.channel and len(self.ignored_roles) == 0 and len(self.ignored_users) == 0


class Honeypot(commands.Cog):

    def __init__(self, bot: Bot):
        self.bot = bot
        self.logger = self.bot.logger.getChild("Honeypot")
        self.data: CogDatabase[HoneypotConfig] | None = None

    group = app_commands.Group(name="honeypot", description="Manage honeypot configs",
                               default_permissions=discord.Permissions(administrator=True))

    user_group = app_commands.Group(name="user", description="User configuration",
                                    default_permissions=discord.Permissions(administrator=True), parent=group)

    role_group = app_commands.Group(name="role", description="Role configuration",
                                    default_permissions=discord.Permissions(administrator=True), parent=group)

    async def load_db(self) -> None:
        self.data = await self.bot.database.get_for(self, "honeypot", HoneypotConfig)

    async def cog_load(self):
        await self.load_db()

    async def get_config(self, guild: discord.Guild):
        return await self.data.get(guild.id)

    @group.command(name="channel", description="Sets the honeypot channel")
    @app_commands.describe(
        channel="The channel for the honeypot",
    )
    async def set_honeypot_channel(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel):
        self.logger.info(f"{interaction.guild.name}: Setting honeypot channel to {channel.name}")
        current = await self.get_config(interaction.guild)
        if current:
            current.channel = channel.id
        else:
            current = HoneypotConfig(channel.id, [], [])
        res = await self.data.upsert(interaction.guild.id, current)
        msg = f"Set honeypot channel to {channel.mention}" if res else f"Could not set honeypot channel"
        await interaction.response.send_message(msg, allowed_mentions=False)

    @user_group.command(name="ignore", description="Ignores messages from given user")
    @app_commands.describe(
        user="The user whose messages will be ignored",
    )
    async def ignore_user(self, interaction: discord.Interaction, user: discord.Member):
        self.logger.info(f"{interaction.guild.name}: Adding user to ignore for honeypots {user.name}")
        current = await self.get_config(interaction.guild)
        if current and user.id not in current.ignored_users:
            current.ignored_users.append(user.id)
        else:
            current = HoneypotConfig(None, [user.id], [])
        res = await self.data.upsert(interaction.guild.id, current)
        msg = f"Ignoring messages from {user.mention} for honeypot triggers" if res else f"Could not add user {user.mention} to ignore for honeypot triggers"
        await interaction.response.send_message(msg, allowed_mentions=False)

    @user_group.command(name="unignore", description="Unignores messages from given user again")
    @app_commands.describe(
        user="The user to remove",
    )
    async def unignore_usesr(self, interaction: discord.Interaction, user: discord.Member):
        self.logger.info(f"{interaction.guild.name}: Removing the user {user.name} to ignore for honeypots ignore list")
        current = await self.get_config(interaction.guild)
        if current and user.id in current.ignored_users:
            current.ignored_users = [c for c in current.ignored_users if c != user.id]
            if current.empty():
                res = await self.data.remove(interaction.guild.id)
            else:
                res = await self.data.upsert(interaction.guild.id, current)
        msg = f"Not ignoring messages from {user.mention} for honeypot triggers anymore" if res else f"Could not remove user {user.mention} to ignore for honeypot triggers"
        await interaction.response.send_message(msg, allowed_mentions=False)

    @role_group.command(name="ignore", description="Ignores messages from given roles")
    @app_commands.describe(
        role="The role to to ignore",
    )
    async def ignore_user(self, interaction: discord.Interaction, role: discord.Role):
        self.logger.info(f"{interaction.guild.name}: Adding role to ignore for honeypots {role.name}")
        current = await self.get_config(interaction.guild)
        if current and role.id not in current.ignored_roles:
            current.ignored_roles.append(role.id)
        else:
            current = HoneypotConfig(None, [role.id], [])
        res = await self.data.upsert(interaction.guild.id, current)
        msg = f"Ignoring messages from {role.mention} for honeypot triggers" if res else f"Could not add role {role.mention} to ignore for honeypot triggers"
        await interaction.response.send_message(msg, allowed_mentions=False)

    @role_group.command(name="unignore", description="Unignores messages from given roles again")
    @app_commands.describe(
        role="The role to remove",
    )
    async def unignore_usesr(self, interaction: discord.Interaction, role: discord.Role):
        self.logger.info(f"{interaction.guild.name}: Removing the role {role.name} to ignore for honeypots ignore list")
        current = await self.get_config(interaction.guild)
        if current and role.id in current.ignored_roles:
            current.ignored_roles = [c for c in current.ignored_roles if c != role.id]
            if current.empty():
                res = await self.data.remove(interaction.guild.id)
            else:
                res = await self.data.upsert(interaction.guild.id, current)
        msg = f"Not ignoring messages from {role.mention} for honeypot triggers anymore" if res else f"Could not remove role {role.mention} to ignore for honeypot triggers"
        await interaction.response.send_message(msg, allowed_mentions=False)

    @group.command(name="get", description="Get Configs")
    async def get_linked_roles(self, interaction: discord.Interaction):
        config = await self.get_config(interaction.guild)
        self.logger.info(f"{interaction.guild.name}: Fetched honeypot configs {config}")
        if config:
            channel = discord.utils.get(interaction.guild.channels, id=int(config.channel)) if config.channel else None
            users = []
            for user_id in config.ignored_users:
                user = discord.utils.get(interaction.guild.members, id=user_id)
                name = user.mention if user else f"Unknown ({user_id})"
                users.append(name)
            roles = []
            for role_id in config.ignored_roles:
                role = discord.utils.get(interaction.guild.roles, id=role_id)
                name = role.mention if role else f"Unknown ({role_id})"
                roles.append(name)
            embed = discord.Embed(
                title=f"Honeypot setup in {channel.mention}" if channel else "Honeypot channel not set",
                description=f"> Ignored Users: {', '.join(users)}  \n  \n> Ignored Roles: {', '.join(roles)}",
                color=EMBED_COLOR
            )
            await interaction.response.send_message("Current honeypot configs on this server", embed=embed,
                                                    allowed_mentions=False)
        else:
            await interaction.response.send_message(f"No honeypot configs for this server configured")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        config = await self.get_config(message.guild)
        if not config or not config.channel or message.channel.id != config.channel:
            return
        if message.author.bot or message.author.guild_permissions.administrator:
            return
        if message.author.id in config.ignored_users:
            return
        for role_config in config.ignored_roles:
            role = discord.utils.get(message.author.guild.roles, id=role_config)
            has_role = role in message.author.roles
            if has_role:
                return
        self.logger.info(f"HONEYPOT - {message.author.name} in {message.guild.name}: {message.content}")
        mod: Moderation = self.bot.get_cog("Moderation")
        await mod.temp_ban(message.guild, message.author, datetime.now(timezone.utc) + timedelta(minutes=15),
                           reason="Talking in the honeypot", delete_seconds=60)


async def setup(bot: Bot) -> None:
    await bot.add_cog(Honeypot(bot))
