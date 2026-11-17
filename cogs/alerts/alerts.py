import asyncio
import traceback
from collections import defaultdict
from datetime import datetime, UTC, timedelta
from typing import Callable, Optional

import discord
from discord import AllowedMentions, ChannelType
from discord import app_commands
from discord.ext import commands

from cogs.alerts.twitch import TwitchListener, TWITCH_BASE_URL
from cogs.alerts.utils import MediaInfo, ServerAlertSettings
from cogs.alerts.youtube import YoutubeListener, YOUTUBE_BASE_URL
from utils.bot import EMBED_COLOR, Bot
from utils.database import CogDatabase

TWITCH_ICON = "https://raw.githubusercontent.com/Flemmli97/CrimsonBot/refs/heads/main/assets/icons/twitch.py"
YOUTUBE_ICON = "https://raw.githubusercontent.com/Flemmli97/CrimsonBot/refs/heads/main/assets/icons/youtube.py"


class PendingStreams:

    def __init__(self):
        self.youtube: dict[int, dict[str, tuple[datetime, MediaInfo]]] = defaultdict(dict)
        self.twitch: dict[int, dict[str, tuple[datetime, MediaInfo]]] = defaultdict(dict)


class StreamAlerts(commands.Cog):

    def __init__(self, bot: Bot):
        self.bot = bot
        self.logger = self.bot.logger.getChild("StreamAlerts")
        self.config: dict = self.bot.get_config_for("alerts")
        self.data: CogDatabase[ServerAlertSettings] | None = None
        self.twitch_listener = TwitchListener(self.logger, self.config["twitch_client_id"],
                                              self.config["twitch_client_secret"])
        self.youtube_listener = YoutubeListener(self.logger, self.config["youtube_api_key"])
        self.listener = None
        self.pending_entries = PendingStreams()

    group = app_commands.Group(name="alerts", description="Manage alerts for twitch & youtube",
                               default_permissions=discord.Permissions(administrator=True))

    async def load_db(self) -> None:
        self.data = await self.bot.database.get_for(self, "media_alert_settings", ServerAlertSettings)

    async def cog_load(self):
        await self.load_db()
        all_settings = await self.data.get_all_of()
        await self.twitch_listener.setup(all_settings)
        await self.youtube_listener.setup(all_settings, self.bot.database)
        self.setup_tasks()

    async def get_data(self, guild: int, name: str):
        return await self.data.get(guild, name=name)

    async def add_listener(self, guild: int, settings: ServerAlertSettings):
        if settings.twitch_channel:
            await self.twitch_listener.listen(guild, settings.twitch_channel, settings.name)
        if settings.youtube_channel:
            await self.youtube_listener.listen(guild, settings)

    def remove_listener(self, guild: int, settings: ServerAlertSettings):
        if settings.twitch_channel:
            self.twitch_listener.unlisten(guild, settings.twitch_channel, settings.name)
        if settings.youtube_channel:
            self.youtube_listener.unlisten(guild, settings.youtube_channel, settings.name)

    def is_enabled(self):
        return self.twitch_listener.valid() or self.youtube_listener.valid()

    @group.command(name="subscribe", description="Listen to updates for a given channel")
    @app_commands.describe(
        name="Name of the listener. Must be unique!",
    )
    async def listen(self, interaction: discord.Interaction, name: str, ping: Optional[discord.Role]):
        current = await self.get_data(guild=interaction.guild.id, name=name)
        if current:
            await interaction.response.send_message(
                f"Setting with {name} already exists!",
                ephemeral=True)
            return
        await interaction.response.send_modal(AlertsModal(cog=self, name=name, ping=ping))

    @group.command(name="list", description="Lists all current alerts")
    @app_commands.describe(
        page="The page to show. Each page has a 10 entry limit",
    )
    async def list_custom_commands(self, interaction: discord.Interaction, page: Optional[int]):
        self.logger.info(f"{interaction.guild.name}: Listing alerts")
        page = page or 0
        current = await self.data.get_all(interaction.guild.id)
        if len(current) == 0:
            embed = discord.Embed(
                title=f"Server has no alerts setup",
                color=EMBED_COLOR
            )
            await interaction.response.send_message(embed=embed)
            return
        paginated = current[page:page + 10]
        desc = ""
        for entry in paginated:
            desc += f"> {await entry.display(self.twitch_listener.get_user_profile_else, self.youtube_listener.get_user_profile_else, self.bot)}\n"
        embed = discord.Embed(
            title=f"Alert list:",
            description=desc,
            color=EMBED_COLOR
        )
        await interaction.response.send_message(embed=embed, allowed_mentions=AllowedMentions.none())

    @group.command(name="unsubscribe", description="Stop listening to the given channel")
    @app_commands.describe(
        name="Name of the listener to remove",
    )
    async def unlisten(self, interaction: discord.Interaction, name: str):
        self.logger.info(f"{interaction.guild.name}: Stop listening to for {name}")
        current = await self.data.get(guild=interaction.guild.id, name=name)
        if not current:
            await interaction.response.send_message(
                f"Listener named {name} does not exist!", ephemeral=True)
            return
        await self.data.remove(guild=interaction.guild.id, name=name)
        await interaction.response.send_message(
            f"Removed alert listener {name}.", ephemeral=True)
        self.remove_listener(interaction.guild.id, current)

    @unlisten.autocomplete("name")
    async def cmd_autocomplete(self, interaction: discord.Interaction, current: str):
        alerts = await self.data.get_all(interaction.guild.id)
        return [app_commands.Choice(name=alert.name, value=alert.name) for alert in alerts if
                alert.name.startswith(current)][:25]

    async def run_repeated(self, runner, valid: Callable[[], bool] | None, interval: int, session: bool = False):
        await self.bot.wait_until_ready()
        try:
            while True:
                if valid and not valid():
                    break
                await runner()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
            raise

    def setup_tasks(self):
        interval = 10
        asyncio.create_task(self.run_repeated(self.handle_twitch_poll, self.twitch_listener.valid, interval))
        asyncio.create_task(self.run_repeated(self.handle_youtube_poll, self.youtube_listener.valid, interval))
        asyncio.create_task(self.run_repeated(self.handle_dual_streams, None, interval))

    async def handle_twitch_poll(self):
        twitch_alerts = await self.twitch_listener.fetch_streams()
        for guild_id, twitch in twitch_alerts.items():
            for entry, stream in twitch.items():
                setting = await self.get_data(guild=guild_id, name=entry)
                if not setting:
                    continue
                # Same alerts gets merged later down
                if setting.youtube_channel:
                    self.pending_entries.twitch[guild_id][entry] = (datetime.now(UTC), stream)
                    continue
                channel_send = self.bot.get_channel(setting.alert_channel)
                if not channel_send:
                    continue
                embed = await self.createEmbed(stream)
                await channel_send.send(content=formatAlert(setting=setting, twitch=stream, youtube=None),
                                        embed=embed)

    async def handle_youtube_poll(self):
        youtube_alerts = await self.youtube_listener.fetch_videos()
        for guild_id, youtube in youtube_alerts.items():
            for entry, videos in youtube.items():
                setting = await self.get_data(guild=guild_id, name=entry)
                if not setting:
                    continue
                # Same alerts gets merged later down
                if setting.twitch_channel:
                    # Streams should only allow one entry
                    stream = [info for info in videos if info["video_type"] == "live"][0]
                    self.pending_entries.youtube[guild_id][entry] = (datetime.now(UTC), stream)
                    continue
                channel_send = self.bot.get_channel(setting.alert_channel)
                if not channel_send:
                    continue
                for video in videos:
                    embed = await self.createEmbed(video)
                    await channel_send.send(content=formatAlert(setting=setting, twitch=None, youtube=video),
                                            embed=embed)

    async def handle_dual_streams(self):
        expiration = datetime.now(UTC) - timedelta(hours=6)
        handled: dict[int, set[str]] = defaultdict(set)
        for guild_id, entries in self.pending_entries.twitch.items():
            to_delete: list[tuple[bool, str]] = []
            for entry, (tracked, stream) in entries.items():
                # No corresponding stream found yet
                if entry not in self.pending_entries.youtube[guild_id]:
                    if tracked < expiration:
                        to_delete.append((False, entry))
                    continue
                setting = await self.get_data(guild=guild_id, name=entry)
                if not setting:
                    continue
                channel_send = self.bot.get_channel(setting.alert_channel)
                if not channel_send:
                    continue
                _, youtube = self.pending_entries.youtube[guild_id][entry]
                embeds = [await self.createEmbed(stream), await self.createEmbed(youtube)]
                await channel_send.send(content=formatAlert(setting=setting, twitch=stream, youtube=youtube),
                                        embeds=embeds)
                to_delete.append((True, entry))
                handled[guild_id].add(entry)
            for both, entry in to_delete:
                del self.pending_entries.twitch[guild_id][entry]
                if both:
                    del self.pending_entries.youtube[guild_id][entry]
        # Only unhandled entries are left here anyway so just clean up outdated entries
        for guild_id, entries in self.pending_entries.youtube.items():
            to_delete: list[str] = []
            for entry, (tracked, stream) in entries.items():
                if tracked < expiration:
                    to_delete.append(entry)
            for entry in to_delete:
                del self.pending_entries.youtube[guild_id][entry]

    async def createEmbed(self, info: MediaInfo):
        desc = f"### [{info["title"]}]({info["url"]})"
        embed = discord.Embed(description=desc, color=EMBED_COLOR)
        if info["source"] == "youtube":
            profile = await self.youtube_listener.get_user_profile_else(info["user"])
        else:
            profile = await self.twitch_listener.get_user_profile_else(info["user"])
        embed.set_author(name=info["user_name"], url=info["channel_url"],
                         icon_url=profile["thumbnail"] if profile else None)
        embed.set_footer(text="Twitch" if info["source"] == "twitch" else "Youtube",
                         icon_url=TWITCH_ICON if info["source"] == "twitch" else YOUTUBE_ICON)
        if "game" in info:
            embed.add_field(name="Playing", value=f"{info["game"]}")
        embed.set_image(url=info["thumbnail"])
        return embed


class Wrapper:

    def __init__(self, lookup: dict, value: str):
        self.value = value
        self.dict = lookup

    def __getattr__(self, name):
        return self.dict.get(f"{self.value}.{name}", f"{self.value}.{name}")

    def __str__(self):
        return "{" + self.value + "}"


class SafeDict(dict):

    def __missing__(self, key):
        return Wrapper(self, key)


def formatAlert(setting: ServerAlertSettings, twitch: MediaInfo | None = None, youtube: MediaInfo | None = None):
    args = {
        "user": twitch["user_name"] if twitch else youtube["user_name"],
        "title": twitch["title"] if twitch else youtube["title"],
        "twitch.user": twitch["user_name"] if twitch else "",
        "twitch.title": twitch["title"] if twitch else "",
        "youtube.user": youtube["user_name"] if youtube else "",
        "youtube.title": youtube["title"] if youtube else "",
    }
    ping = f'<@&{setting.alert_role}> ' if setting.alert_role else ''
    msg = setting.message.format_map(SafeDict(args))
    return f"{ping}{msg}"


class AlertsModal(discord.ui.Modal):

    def __init__(self, cog: StreamAlerts, name: str, ping: Optional[discord.Role]):
        super().__init__(title="Setup stream alerts")
        self.cog = cog
        self.name = name
        self.ping = ping

    message = discord.ui.TextInput(
        label="Message to send. Supports placeholders:",
        style=discord.TextStyle.paragraph,
        placeholder="Placeholders can be prefixed with platform e.g. {youtube.user}.\n{user}\n{title}",
    )

    channel = discord.ui.Label(
        text="The alert channel post in",
        component=discord.ui.ChannelSelect(
            placeholder="Which channel to post in",
            channel_types=[ChannelType.text],
            required=True
        )
    )

    twitch_channel = discord.ui.TextInput(
        label="Twitch",
        style=discord.TextStyle.short,
        placeholder="The twitch channel. Link or channel ID",
        required=False
    )

    youtube_channel = discord.ui.TextInput(
        label="Youtube",
        style=discord.TextStyle.short,
        placeholder="The youtube channel. Link or channel ID",
        required=False
    )

    listen_to = discord.ui.Label(
        text="Video types",
        description="What types of upload to listen to. Does nothing if twitch channel is provided",
        component=discord.ui.CheckboxGroup(
            min_values=1,
            options=[discord.components.CheckboxGroupOption(
                label="Videos",
                value="videos",
                default=True
            ), discord.components.CheckboxGroupOption(
                label="Live",
                value="live",
                default=True
            ), discord.components.CheckboxGroupOption(
                label="Shorts",
                value="shorts",
                default=True
            )],
            required=False
        )
    )

    async def on_submit(self, interaction: discord.Interaction):
        youtube = self.youtube_channel.value
        youtube_types = []
        if youtube:
            youtube = youtube.replace(f"{YOUTUBE_BASE_URL}/", "")
            input = youtube
            youtube = await self.cog.youtube_listener.get_and_verify_youtube_channel_id(youtube)
            if not youtube:
                await interaction.response.send_message(
                    f"Youtube channel `{input}` does not exist!",
                    ephemeral=True)
                return
            youtube_types = self.listen_to.component.values
            if not youtube_types:
                await interaction.response.send_message(
                    f"No video types selected for youtube listener!",
                    ephemeral=True)
                return
        twitch = self.twitch_channel.value
        if twitch:
            twitch = twitch.replace(f"{TWITCH_BASE_URL}/", "")
            pfp = await self.cog.twitch_listener.fetch_user_profile(twitch)
            if not pfp:
                await interaction.response.send_message(
                    f"Twitch channel `{twitch}` does not exist!",
                    ephemeral=True)
                return
            if youtube:
                youtube_types = ["live"]
        if not youtube and not twitch:
            await interaction.response.send_message(
                f"Neither Platform has been set!",
                ephemeral=True)
            return
        setting = ServerAlertSettings(name=self.name,
                                      message=self.message.value,
                                      alert_role=self.ping.id if self.ping else None,
                                      twitch_channel=twitch,
                                      youtube_channel=youtube,
                                      youtube_video_types=youtube_types,
                                      alert_channel=self.channel.component.values[0].id)
        success = await self.cog.data.upsert(interaction.guild.id, setting)
        if not success:
            await interaction.response.send_message(
                f"Could setup setting {self.name}!",
                ephemeral=True)
            return
        self.cog.logger.info(f"{interaction.guild.name}: Start listening for setting {setting}")
        await self.cog.add_listener(interaction.guild.id, setting)
        await interaction.response.send_message(
            f"Created alert setting `{self.name}`", ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(StreamAlerts(bot))
