from dataclasses import dataclass
from datetime import timedelta

import discord
import requests
from discord import app_commands
from discord.ext import commands

from utils.bot import EMBED_COLOR, Bot
from utils.config import get_single_or_list
from utils.sqlutils import SQLSchema, Schema

# ====== Configs

MC_LOGS = "https://api.mclo.gs/1/log"


@dataclass
class PasteConfig(Schema):
    channels: list[int]
    channel_categories: list[int]

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema="""
                        channels json,
                        channel_categories json,
                    """, keys=[])
        return schema


class FilePaste(commands.Cog):

    def __init__(self, bot: Bot):
        self.bot = bot
        self.logger = self.bot.logger.getChild("FilePaste")
        self.config = self.bot.get_config_for("paste")

    group = app_commands.Group(name="paste", description="Manage paste Configs",
                               default_permissions=discord.Permissions(administrator=True))

    chat_group = app_commands.Group(name="chat", description="Paste Configs for channels",
                                    default_permissions=discord.Permissions(administrator=True), parent=group)

    category_group = app_commands.Group(name="category", description="Paste Configs for channel categories",
                                        default_permissions=discord.Permissions(administrator=True), parent=group)

    async def load_db(self) -> None:
        self.configs_db = await self.bot.database.get_for(self, "config", PasteConfig)

    async def cog_load(self):
        await self.load_db()

    @group.command(name="get", description="Get Paste Configs")
    async def get_paste_config(self, interaction: discord.Interaction):
        configs = await self.configs_db.get(interaction.guild.id)
        self.logger.info(f"{interaction.guild.name}: Fetched paste configs {configs}")
        if configs:
            channels = []
            for channel_config in configs.channels:
                channel = discord.utils.get(interaction.guild.channels, id=int(channel_config))
                name = channel.mention if channel else f"Unknown ({channel_config})"
                channels.append(name)
            categories = []
            for category_config in configs.channel_categories:
                channel = discord.utils.get(interaction.guild.categories, id=int(category_config))
                name = channel.mention if channel else f"Unknown ({category_config})"
                categories.append(name)
            embed = discord.Embed(
                title="Listening in",
                description=f"> Chats: {', '.join(channels)}  \n  \n> Chat Categories: {', '.join(categories)}",
                color=EMBED_COLOR
            )
            await interaction.response.send_message("Current paste configs on this server", embed=embed,
                                                    allowed_mentions=False)
        else:
            await interaction.response.send_message(f"No paste configs for this server configured. Using defaults")

    @chat_group.command(name="add", description="Add a chat to the whitelist")
    @app_commands.describe(
        channel="The chat to add",
    )
    async def add_channel(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel):
        self.logger.info(f"{interaction.guild.name}: Adding channel {channel.name} to paste whitelist")
        current = await self.get_config(interaction.guild)
        if current and channel.id not in current.channels:
            current.channels.append(channel.id)
        else:
            current = PasteConfig([channel.id], [])
        res = await self.configs_db.upsert(interaction.guild.id, current)
        msg = f"Added channel {channel.mention} to paste whitelist" if res else f"Could not add channel {channel.mention} to paste whitelist"
        await interaction.response.send_message(msg, allowed_mentions=False)

    @chat_group.command(name="remove", description="Remove a chat from the whitelist")
    @app_commands.describe(
        channel="The chat to remove",
    )
    async def remove_channel(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel):
        self.logger.info(f"{interaction.guild.name}: Removing channel {channel.name} from paste whitelist")
        current = await self.get_config(interaction.guild)
        if current and channel.id in current.channels:
            current.channels = [c for c in current.channels if c != channel.id]
            if len(current.channels) == 0 and len(current.channel_categories) == 0:
                res = await self.configs_db.remove(interaction.guild.id)
            else:
                res = await self.configs_db.upsert(interaction.guild.id, current)
        msg = f"Added channel {channel.mention} to paste whitelist" if res else f"Could not add channel {channel.mention} to paste whitelist"
        await interaction.response.send_message(msg, allowed_mentions=False)

    @category_group.command(name="add", description="Add a channel category to the whitelist")
    @app_commands.describe(
        category="The channel category to add",
    )
    async def add_category(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        self.logger.info(f"{interaction.guild.name}: Adding category {category.name} to paste whitelist")
        current = await self.get_config(interaction.guild)
        if current and category.id not in current.channel_categories:
            current.channel_categories.append(category.id)
        else:
            current = PasteConfig([], [category.id])
        res = await self.configs_db.upsert(interaction.guild.id, current)
        msg = f"Added category {category.mention} to paste whitelist" if res else f"Could not add category {category.mention} to paste whitelist"
        await interaction.response.send_message(msg, allowed_mentions=False)

    @category_group.command(name="remove", description="Remove a channel category from the whitelist")
    @app_commands.describe(
        category="The channel category to remove",
    )
    async def remove_category(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        self.logger.info(f"{interaction.guild.name}: Removing category {category.name} from paste whitelist")
        current = await self.get_config(interaction.guild)
        if current and category.id in current.channel_categories:
            current.channel_categories = [c for c in current.channel_categories if c != category.id]
            if len(current.channels) == 0 and len(current.channel_categories) == 0:
                res = await self.configs_db.remove(interaction.guild.id)
            else:
                res = await self.configs_db.upsert(interaction.guild.id, current)
        msg = f"Added category {category.mention} to paste whitelist" if res else f"Could not add category {category.mention} to paste whitelist"
        await interaction.response.send_message(msg, allowed_mentions=False)

    async def get_config(self, guild: discord.Guild):
        return await self.configs_db.get(guild.id)

    def send_paste(self, filename: str, content):
        """
        Sends the content to a paste site. Adjust for paste site api
        """
        data = {"text": content, "filename": filename, "expires": int(timedelta(days=30).total_seconds())}
        # Send content to paste site
        send = requests.post(self.config["paste_site_api"], json=data)
        if send.ok:
            result = send.json()
            path = result["path"].removeprefix("/")
            return filename, f"{self.config['paste_site']}/{path}"
        else:
            self.logger.error("Error sending to paste: %s", send.raise_for_status())
            return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user or message.author.bot:
            return
        if len(message.attachments) > 0:
            config = await self.get_config(message.guild)
            channels: list[str] = config.channels if config else []
            if not len(channels) != 0 and not str(message.channel.id) not in channels:
                return
            channel_category: list[str] = config.channel_categories if config else []
            if not len(channel_category) != 0 and str(message.channel.category.id) not in channel_category:
                return
            self.logger.info(
                f"{message.guild.name}: Attempting to process message (at {message.created_at}) with attachments: {message.attachments}")
            urls = []
            allowed: tuple[str] = tuple(get_single_or_list(self.config, "allowed_files"))
            for attachment in message.attachments:
                if not attachment.filename.endswith(allowed):
                    continue
                output = await attachment.to_file()
                attachment_content = output.fp.read().decode("UTF-8")

                # Send logfiles to mc-logs since they have better syntax highlighting
                if attachment.filename.endswith(".log"):
                    data = {"content": attachment_content}
                    send = requests.post(MC_LOGS, data=data)
                    if send.ok:
                        result = send.json()
                        if result["success"] == "True":
                            urls.append((attachment.filename, f"{result['url']}"))
                    else:
                        self.logger.error("Error sending to paste: %s", send.raise_for_status())
                else:
                    result = self.send_paste(attachment.filename, attachment_content)
                    if result:
                        urls.append(result)

            if len(urls) > 0:
                attachment_files = ", ".join([f"`{file}`" for (file, _) in urls])
                msg = f"Created paste version of {attachment_files} from {message.author.mention}!"
                view = discord.ui.View()
                for file, url in urls:
                    view.add_item(discord.ui.Button(url=url, label=f"View {file}"))
                await message.channel.send(msg, view=view)


async def setup(bot: Bot) -> None:
    await bot.add_cog(FilePaste(bot))
