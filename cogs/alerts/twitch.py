from datetime import datetime, timedelta
from logging import Logger

import aiohttp
import requests

from cogs.alerts.utils import Listener, MediaInfo, ProfileInfo, ServerAlertSettings

TWITCH_OAUTH_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_STREAM_API = "https://api.twitch.tv/helix/streams"
TWITCH_USERS_API = "https://api.twitch.tv/helix/users"
TWITCH_BASE_URL = "https://www.twitch.tv"


class Token:

    def __init__(self, token: str, expires_in: int):
        self.token = token
        self.expires_at = datetime.now() + timedelta(seconds=expires_in)

    def isvalid(self) -> bool:
        return self.expires_at + timedelta(seconds=60) < datetime.now()


class TwitchListener:

    def __init__(self, logger: Logger, client_id: str, client_secret: str):
        self.logger = logger
        self.client_id = client_id
        self.client_secret = client_secret
        self.listeners: dict[str, list[Listener]] = {}
        self.profiles: dict[str, ProfileInfo] = {}
        self.twitch_token: Token | None = None
        self.stream_cache: set[str] | None = None
        self.session = None

    async def setup(self, settings: list[tuple[int, ServerAlertSettings]]):
        self.session = aiohttp.ClientSession(headers=requests.utils.default_headers())
        self.listeners = {}
        for guild, setting in settings:
            if setting.twitch_channel:
                if setting.twitch_channel not in self.listeners:
                    self.listeners[setting.twitch_channel] = []
                self.listeners[setting.twitch_channel].append(Listener(setting.name, guild))
        self.profiles = await self._fetch_twitch_user_info([k for k in self.listeners.keys()])

    def valid(self) -> bool:
        if self.client_id or self.client_secret:
            return True
        return False

    async def listen(self, guild: int, channel: str, entry_name: str):
        if channel not in self.listeners:
            self.listeners[channel] = []
        pfp = await self.fetch_user_profile(channel)
        if not pfp:
            return
        self.listeners[channel].append(Listener(entry_name, guild))

    def unlisten(self, guild: int, channel: str, entry_name: str):
        if channel not in self.listeners:
            return
        self.listeners[channel] = [listener for listener in self.listeners[channel] if
                                   listener.name != entry_name or listener.guild_id != guild]
        if len(self.listeners[channel]) == 0:
            del self.listeners[channel]

    async def fetch_streams(self) -> dict[int, dict[str, MediaInfo]]:
        if not self.listeners:
            return {}
        token = await self._get_twitch_token()
        if not token:
            return {}

        async with self.session.get(
                TWITCH_STREAM_API,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Client-Id": self.client_id,
                },
                params=[
                    ("user_login", channel)
                    for channel in self.listeners.keys()
                ]
        ) as response:
            if response.status != 200:
                self.logger.error(f"Failed to fetch twitch streams {response.status} {response.text}")
                return {}
            data = await response.json()
            guilds_to_alert: dict[int, dict[str, MediaInfo]] = {}
            streamers = set()
            for stream in data.get("data", []):
                streamer = stream['user_login']
                streamers.add(streamer)
                # The first run dont send anything yet. Dont want e.g. reboots trigger alerts
                if self.stream_cache is None:
                    self.stream_cache = set()
                    continue
                if streamer in self.stream_cache:
                    continue
                info: MediaInfo = {
                    "source": "twitch",
                    "user": stream['user_login'],
                    "user_name": stream["user_name"],
                    "channel_url": f"{TWITCH_BASE_URL}/{stream['user_login']}",
                    "title": stream["title"],
                    "url": f"{TWITCH_BASE_URL}/{stream['user_login']}",
                    "thumbnail": stream["thumbnail_url"].replace("{width}", "1920").replace("{height}", "1080"),
                    "game": stream["game_name"],
                    "video_type": "live"
                }
                listeners = self.listeners.get(streamer, [])
                for listener in listeners:
                    if listener.guild_id not in guilds_to_alert:
                        guilds_to_alert[listener.guild_id] = {}
                    guilds_to_alert[listener.guild_id][listener.name] = info
            self.stream_cache = streamers
            return guilds_to_alert

    async def _get_twitch_token(self):
        if not self.twitch_token or not self.twitch_token.isvalid():
            async with self.session.post(TWITCH_OAUTH_URL, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            }) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                self.twitch_token = Token(data["access_token"], data["expires_in"])
        return self.twitch_token.token

    async def fetch_user_profile(self, channel: str):
        if channel not in self.profiles or not self.profiles[channel]["thumbnail"]:
            pfp = await self._fetch_twitch_user_info([channel])
            if pfp:
                self.profiles[channel] = pfp.get(channel)
        return self.profiles.get(channel)

    async def get_user_profile_else(self, channel: str) -> ProfileInfo:
        pfp = await self.fetch_user_profile(channel)
        if not pfp:
            return {
                "user_name": channel,
                "url": f"{TWITCH_BASE_URL}/{channel}",
                "thumbnail": "",
            }
        return pfp

    async def _fetch_twitch_user_info(self, channels: list[str]) -> dict[str, ProfileInfo]:
        if not channels:
            return {}
        token = await self._get_twitch_token()
        if not token:
            return {}
        async with self.session.get(
                TWITCH_USERS_API,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Client-Id": self.client_id,
                },
                params=[
                    ("login", channel)
                    for channel in channels
                ]
        ) as response:
            results: dict[str, ProfileInfo] = {}
            if response.status == 200:
                data = await response.json()
                items = data.get("data", [])
                for item in items:
                    user = item["login"]
                    results[user] = {
                        "user_name": item["display_name"],
                        "url": f"{TWITCH_BASE_URL}/{user}",
                        "thumbnail": item["profile_image_url"]
                    }
            return results
