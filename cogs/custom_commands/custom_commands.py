import json
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import discord
from discord import app_commands, HTTPException
from discord.ext import commands

from utils.bot import EMBED_COLOR, Bot
from utils.database import CogDatabase
from utils.sqlutils import Schema, SQLSchema


@dataclass
class CustomCommandsConfig(Schema):
    prefix: str

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema="""
                        prefix varchar(10)
                    """, keys=[])
        return schema


@dataclass
class CustomCommandsEntry(Schema):
    command: str
    message: dict

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema="""
                        command varchar(20),
                        message json,
                    """, keys=["command"])
        return schema


class CustomCommands(commands.Cog):

    def __init__(self, bot: Bot):
        self.bot = bot
        self.logger = self.bot.logger.getChild("CustomCommands")
        self.config: CogDatabase[CustomCommandsConfig] | None = None
        self.data: CogDatabase[CustomCommandsEntry] | None = None

    group = app_commands.Group(name="custom", description="Manage custom commands",
                               default_permissions=discord.Permissions(administrator=True))

    async def load_db(self) -> None:
        self.config = await self.bot.database.get_for(self, "custom_commands_config", CustomCommandsConfig)
        self.data = await self.bot.database.get_for(self, "custom_commands", CustomCommandsEntry)

    async def cog_load(self):
        await self.load_db()

    async def get_config(self, guild: discord.Guild):
        return await self.config.get(guild.id)

    async def get_data(self, guild: discord.Guild, command: str):
        return await self.data.get(guild.id, command=command)

    @group.command(name="create", description="Create a new custom command")
    @app_commands.describe(
        command="The name of the new command",
        replace="Whether to replace an existing command or not",
    )
    async def create_custom_command(self, interaction: discord.Interaction, command: str, replace: Optional[bool]):
        self.logger.info(f"{interaction.guild.name}: Creating custom command {command}")
        current = await self.get_data(interaction.guild, command)
        if current and not replace:
            await interaction.response.send_message(
                f"Command {command} exists already. Use replace option to overwrite it", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        follow = await interaction.followup.send(
            f"Creating a custom command `{command}`. Send either text or a json file for setup", ephemeral=True,
            wait=True)
        res = await self.bot.wait_for(
            "message",
            check=lambda x: x.channel.id == interaction.channel.id and x.author.id == interaction.user.id,
            timeout=60,
        )
        if len(res.attachments) == 1:
            attachment = res.attachments[0]
            if not attachment.filename.endswith((".txt", ".json")):
                await interaction.edit_original_response(
                    content=f"Unsupported file. Only .json and .txt files are supported!")
                return
            output = await attachment.to_file()
            content: dict = json.loads(output.fp.read().decode("UTF-8"))
            content = {
                "content": content.get("content"),
                "embeds": content.get("embeds"),
            }
        else:
            content = {"content": res.content}
        try:
            await follow.delete()
            await res.delete()
        except HTTPException:
            pass
        entry = CustomCommandsEntry(command, content)
        self.logger.info(f"{interaction.guild.name}: Created custom command {entry}")
        await self.data.upsert(interaction.guild.id, entry)
        msg = self.message_from_json(entry.message)
        msg["content"] = f"> Setup custom command `{command}`\n=====\n{msg['content'] if msg['content'] else ''}"
        await interaction.edit_original_response(**msg)

    @group.command(name="remove", description="Remove a custom command")
    @app_commands.describe(
        command="The command to remove",
    )
    async def remove_custom_command(self, interaction: discord.Interaction, command: str):
        self.logger.info(f"{interaction.guild.name}: Removing custom command {command}")
        current = await self.get_data(interaction.guild, command)
        if not current:
            await interaction.response.send_message(f"No such command `{command}` exists!")
            return
        res = await self.data.remove(interaction.guild.id, command=command)
        await interaction.response.send_message(
            f"Removed custom command {command}" if res else f"Could not remove custom command {command}")

    @group.command(name="get", description="Get the json of a custom command")
    @app_commands.describe(
        command="The command to get",
    )
    async def get_custom_command(self, interaction: discord.Interaction, command: str):
        self.logger.info(f"{interaction.guild.name}: Get custom command {command}")
        current = await self.get_data(interaction.guild, command)
        if not current:
            await interaction.response.send_message(f"No such command `{command}` exists!")
            return
        msg = f"Custom command data of {command}:"
        await interaction.response.send_message(msg, file=discord.File(
            fp=BytesIO(json.dumps(current.message, indent=4).encode()),
            filename=f"{command}_command.json"
        ))

    @group.command(name="prefix", description="Set the prefix for custom commands")
    @app_commands.describe(
        prefix="Prefix to use for all commands",
    )
    async def command_prefix(self, interaction: discord.Interaction, prefix: str):
        self.logger.info(f"{interaction.guild.name}: Set custom command prefix to {prefix}")
        config = await self.get_config(interaction.guild)
        if not config:
            config = CustomCommandsConfig(prefix)
        else:
            config.prefix = prefix
        res = await self.config.upsert(interaction.guild.id, config)
        await interaction.response.send_message(f"Set custom command to {prefix}" if res else f"Could not set prefix")

    @app_commands.command(name="commands", description="Lists all custom commands")
    @app_commands.describe(
        page="The page to show. Each page has a 10 entry limit",
    )
    async def list_custom_commands(self, interaction: discord.Interaction, page: Optional[int]):
        self.logger.info(f"{interaction.guild.name}: List custom commands")
        page = page or 0
        current = await self.data.get_all(interaction.guild.id)
        if len(current) == 0:
            embed = discord.Embed(
                title=f"Server has no custom commands",
                color=EMBED_COLOR
            )
            await interaction.response.send_message(embed=embed)
            return
        paginated = current[page:page + 10]
        prefix = await self.get_command_prefix(interaction.guild)
        desc = ""
        for entry in paginated:
            desc += f"> {prefix}{entry.command}\n"
        embed = discord.Embed(
            title=f"Custom command list:",
            description=desc,
            color=EMBED_COLOR
        )
        await interaction.response.send_message(embed=embed)

    @remove_custom_command.autocomplete("command")
    @get_custom_command.autocomplete("command")
    async def cmd_autocomplete(self, interaction: discord.Interaction, current: str):
        commands = await self.data.get_all(interaction.guild.id)
        return [app_commands.Choice(name=cmd.command, value=cmd.command) for cmd in commands if
                cmd.command.startswith(current)][:25]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        prefix = await self.get_command_prefix(message.guild)
        if message.content.startswith(prefix):
            command = message.content[len(prefix):].strip()
            entry = await self.get_data(message.guild, command)
            if not entry:
                return
            await message.channel.send(**CustomCommands.message_from_json(entry.message))

    async def get_command_prefix(self, guild: discord.Guild):
        config = await self.get_config(guild)
        return config.prefix if config and config.prefix else "??"

    @staticmethod
    def message_from_json(message: dict):
        msg = {
            "content": message.get("content", ""),
            "embeds": [discord.Embed.from_dict(e) for e in message.get("embeds", [])]
        }
        return msg


async def setup(bot: Bot) -> None:
    await bot.add_cog(CustomCommands(bot))
