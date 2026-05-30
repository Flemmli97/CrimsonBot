import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Optional

import discord
import requests
from discord import app_commands
from discord.ext import commands

from utils.bot import EMBED_COLOR, Bot
from utils.config import get_single_or_list
from utils.database import CogDatabase
from utils.sqlutils import Schema, SQLSchema

MOJANG_API = "https://api.mojang.com/users/profiles/minecraft/"


@dataclass
class LinkedUserData(Schema):
    user_id: int
    patreon_tier: int
    minecraft_uuid: str
    minecraft_username: str
    data: None | dict

    def update_tier(self, tier: int):
        return LinkedUserData(self.user_id, tier, self.minecraft_uuid, self.minecraft_username, self.data)

    def gist_formatted(self):
        out = {
            "uuid": str(uuid.UUID(self.minecraft_uuid)),
            "name": self.minecraft_username,
            "tier": self.patreon_tier,
            "defaultEffect": self.data.get("default_effect", "none") if self.data else "none",
        }
        return out

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema="""
                        user_id int(25) NOT NULL,
                        patreon_tier int(4) NOT NULL,
                        minecraft_uuid varchar(40) NOT NULL,
                        minecraft_username varchar(20) NOT NULL,
                        data json,
                    """, keys=["user_id"])
        return schema


@dataclass
class GuildRoleConfig(Schema):
    role: int
    tier: int

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema="""
                        role int(25) NOT NULL,
                        tier int(4) NOT NULL,
                    """, keys=["role"])
        return schema


class MinecraftLinker(commands.Cog):

    def __init__(self, bot: Bot):
        self.bot = bot
        self.logger = self.bot.logger.getChild("MinecraftLinker")
        self.config = self.bot.get_config_for("minecraft_linker")
        self.data: CogDatabase[LinkedUserData] | None = None
        self.role_config: CogDatabase[GuildRoleConfig] | None = None

    group = app_commands.Group(name="account_link_roles", description="Account Link Roles Config",
                               default_permissions=discord.Permissions(administrator=True))

    async def load_db(self) -> None:
        self.data = await self.bot.database.get_for(self, "mclink_accounts", LinkedUserData)
        self.role_config = await self.bot.database.get_for(self, "role_config", GuildRoleConfig)

    async def cog_load(self):
        await self.load_db()

    @group.command(name="get", description="Get Account Role Config")
    async def get_linked_roles(self, interaction: discord.Interaction):
        roles = await self.role_config.get_all(interaction.guild.id)
        self.logger.info(f"{interaction.guild.name}: Fetched role configs {roles}")
        if roles:
            desc = ""
            for role_config in roles:
                role = discord.utils.get(interaction.guild.roles, id=int(role_config.role))
                name = role.mention if role else f"Unknown ({role_config.role})"
                desc += f"- {name} - {role_config.tier}  \n"
            embed = discord.Embed(
                title="Roles",
                description=desc,
                color=EMBED_COLOR
            )
            await interaction.response.send_message("Current role configs on this server", embed=embed,
                                                    allowed_mentions=False)
        else:
            await interaction.response.send_message(
                f"No roles configured. Use `/{MinecraftLinker.group.name} set` to configure some roles")

    @group.command(name="set", description="Set role tier for account linking")
    @app_commands.describe(
        role="The role",
        tier="The tier of the role",
    )
    async def set_link_roles(self, interaction: discord.Interaction, role: discord.Role, tier: int):
        self.logger.info(f"{interaction.guild.name}: Configuring role {role.name} with {tier}")
        res = await self.role_config.upsert(interaction.guild.id, GuildRoleConfig(role.id, tier))
        msg = f"Configured role {role.mention} for tier {tier}" if res else f"Could not configure for role {role.mention}"
        await interaction.response.send_message(msg, allowed_mentions=False)

    @group.command(name="remove", description="Remove a role tier from account linking")
    @app_commands.describe(
        role="The role",
    )
    async def remove_link_roles(self, interaction: discord.Interaction, role: discord.Role):
        self.logger.info(f"{interaction.guild.name}: Removing role config for {role.name}")
        res = await self.role_config.remove(interaction.guild.id, role=role.id)
        msg = f"Removed role {role} tier" if res else f"Could not remove role config {role.mention}"
        await interaction.response.send_message(msg, allowed_mentions=False)

    @app_commands.command(description="Links your minecraft account to your discord account")
    @app_commands.describe(
        user_name="Your minecraft username",
    )
    async def mclink(self, interaction: discord.Interaction, user_name: str):
        await self.mc_link_user(interaction, interaction.user, user_name)

    @app_commands.command(description="Admin version of account linking to link other people")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    @app_commands.describe(
        user="The target user to link",
        user_name="The minecraft username",
    )
    async def mcadminlink(self, interaction: discord.Interaction, user: discord.Member, user_name: str,
                          overwrite: Optional[bool]):
        if type(user) is int:
            member = interaction.guild.get_member(user)
            if not member:
                await interaction.response.send_message(f"User with id {user} not found!", ephemeral=True,
                                                        delete_after=15 if interaction else None)
                return
            user = member
        await self.mc_link_user(interaction, user, user_name, overwrite=overwrite)

    async def mc_link_user(self, interaction: discord.Interaction, user: discord.Member, user_name: str,
                           overwrite=False):
        tier = await self.get_contributor_tier(user)
        self.logger.info(f"{interaction.guild.name}: Attempting to link account {user_name} with tier {tier}")
        is_self = interaction.user.id == user.id
        if tier == 0:
            await interaction.response.send_message(
                "You do not have the required tiers" if is_self else "Target does not have the required tiers",
                ephemeral=True,
                delete_after=15 if interaction else None
            )
            return
        try:
            send = requests.get(f"{MOJANG_API}{user_name}?", timeout=10)
        except asyncio.TimeoutError:
            await interaction.response.send_message("Timeout asking mojang servers", ephemeral=True,
                                                    delete_after=15 if interaction else None)
            return
        if send.ok:
            sender = user.id
            self.logger.info(f"{interaction.guild.name}: Linking account {user_name} with id {sender} and tier {tier}")
            result = send.json()["id"]
            user_data = await self.fetch_linked_for(interaction.guild.id, sender)
            exist = await self.existing_link_for(interaction.guild.id, result)
            if exist and (not user_data or user_data.user_id != sender):
                if not overwrite:
                    await interaction.response.send_message(
                        "This minecraft account is already linked with an discord account",
                        ephemeral=True,
                        delete_after=15 if interaction else None
                    )
                    return
                await self.data.remove(interaction.guild.id, minecraft_uuid=result)
            if user_data:
                user_data = user_data.update_tier(tier)
            else:
                user_data = LinkedUserData(sender, tier, result, user_name, None)
            res = await self.update_linked_users(interaction.guild.id, user_data)
            if res:
                await self.sync_with_gist(interaction.guild)
                await interaction.response.send_message(
                    "Linked your username with your discord account" if is_self else "Linked the username with the given discord account",
                    ephemeral=True,
                    delete_after=15 if interaction else None
                )
            if self.bot.config["log_channel"] != "":
                await interaction.guild.get_channel(int(self.bot.config["log_channel"])).send(
                    f"{user.mention} linked their mc account")
        else:
            await interaction.response.send_message("No such minecraft account found", ephemeral=True,
                                                    delete_after=15 if interaction else None)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        user_data = await self.fetch_linked_for(before.guild.id, after.id)
        if not user_data:
            return
        tier = await self.get_contributor_tier(before)
        tier_after = await self.get_contributor_tier(after)
        if tier != tier_after:
            self.logger.info(f"{before.guild.name}: Tier changed for {after} to tier {tier_after}")
            res = False
            if tier_after == 0:
                await asyncio.sleep(15)  # Add a delay in case the role was removed to update it with a new role
                delayed_data = await self.fetch_linked_for(before.guild.id, after.id)
                if delayed_data and delayed_data.patreon_tier == tier:
                    res = await self.remove_linked_users(before.guild.id,
                                                         after.id)  # If role still remains same assume no other update occurred and remove the data
            else:
                user_data = user_data.update_tier(tier_after)
                res = await self.update_linked_users(after.guild.id, user_data)
            if res:
                await self.sync_with_gist(after.guild)

    async def update_linked_users(self, guild: int, link: LinkedUserData) -> bool:
        return await self.data.upsert(guild, link)

    async def remove_linked_users(self, guild: int, user: int) -> bool:
        return await self.data.remove(guild, user_id=user)

    async def fetch_linked_for(self, guild: int, user_id: int) -> LinkedUserData | None:
        return await self.data.get(guild, user_id=user_id)

    async def existing_link_for(self, guild: int, minecraft_uuid: str):
        return await self.data.get(guild, minecraft_uuid=minecraft_uuid)

    async def fetch_all_linked_users(self, guild: discord.Guild) -> list[LinkedUserData]:
        return await self.data.get_all(guild.id)

    async def sync_with_gist(self, guild: discord.Guild):
        if str(guild.name) != self.config["required_server"]:
            server_conf = self.config["required_server"]
            self.logger.info(
                f'Guild {guild.name} with id {guild.id} does not match required "{server_conf}". Not syncing to gist!')
            return
        token = self.config["github_token"]
        if not token:
            self.logger.info(f"Unable to sync to gist. No github token defined!")
            return
        file = self.config["gist_file"]
        gist_id = self.config["gist_id"]
        headers = {"Authorization": f"Bearer {self.config['github_token']}"}
        current = requests.get("https://api.github.com/gists/" + gist_id, headers=headers)
        if current.ok:
            content = current.json()["files"][file]["content"]  # Fetch current data
            content_json = json.loads(content)
            new_data = []
            users = await self.fetch_all_linked_users(guild)
            for user in users:  # Add the data from the bot
                new_data.append(user.gist_formatted())
            ignored = get_single_or_list(self.config, "ignored_users")
            for c in content_json:
                if c["uuid"] in ignored:
                    new_data.append(c)
            if new_data != content_json:  # Only update if changed
                out = json.dumps(new_data, default=vars, indent=2).replace("'", '"')  # Format the data
                r = requests.patch("https://api.github.com/gists/" + gist_id,
                                   data=json.dumps({"files": {file: {"content": out}}}, default=vars, indent=4),
                                   headers=headers)
                if not r.ok:
                    self.logger.error("Couldn't update remote data!")
        else:
            self.logger.error("Couldn't update remote data!")

    async def get_contributor_tier(self, user: discord.Member):
        tier = 0
        roles = await self.role_config.get_all(user.guild.id)
        if not roles:
            return tier
        for role_config in roles:
            role = discord.utils.get(user.guild.roles, id=int(role_config.role))
            has_role = role in user.roles
            if has_role:
                tier = max(tier, role_config.tier)
        return tier


async def setup(bot: Bot) -> None:
    await bot.add_cog(MinecraftLinker(bot))
