import typing
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import discord
import requests
import typesense
import typesense.configuration
import typesense.types.document as tst
from bs4 import BeautifulSoup, Tag
from discord import app_commands, Interaction
from discord.ext import commands
from typesense.configuration import NodeConfigDict

from utils.bot import EMBED_COLOR, Bot
from utils.database import CogDatabase
from utils.sqlutils import SQLSchema, Schema


class ProjectData(typing.TypedDict):
    label: str
    versions: list[str]


@dataclass
class WikiDefaultScopes(Schema):
    channel: int
    mod: str

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema="""
                        channel int(25),
                        command varchar(20),
                    """, keys=['channel'])
        return schema


class Wiki(commands.Cog):

    def __init__(self, bot: Bot):
        self.project_data = None
        self.default_tags = None
        self.bot = bot
        self.logger = self.bot.logger.getChild("wiki")
        self.config = self.bot.get_config_for("wiki")
        search_config = self.config["search"]
        self.client = typesense.AsyncClient({
            "api_key": search_config["api_key"],
            "nodes": [NodeConfigDict(
                host=search_config["host"],
                port=search_config["port"],
                path=search_config["path"],
                protocol=search_config["protocol"])
            ],
            "connection_timeout_seconds": 2,
        })
        self.collection = search_config["collection"]
        self.wikiScopes: CogDatabase[WikiDefaultScopes] | None = None

    scopes = app_commands.Group(name="wikiscopes", description="Manage default mods for wiki searches",
                                default_permissions=discord.Permissions(administrator=True))

    async def fetch_projects(self):
        search_params: tst.SearchParameters = {
            "q": "*",
            "query_by": "hierarchy.lvl0",
            "facet_by": "docusaurus_tag",
            "per_page": 0
        }
        res = await self.client.collections[self.collection].documents.search(search_params)
        mods = []
        for facet_schema in res["facet_counts"]:
            if facet_schema["field_name"] == "docusaurus_tag":
                for facet in facet_schema["counts"]:
                    mods.append(facet["value"])
                break
        labelled: dict[str, ProjectData] = {}
        for mod in mods:
            search_params: tst.SearchParameters = {
                "q": "*",
                "query_by": "hierarchy.lvl0",
                "facet_by": "hierarchy.lvl0",
                "filter_by": f"docusaurus_tag:={mod}",
                "per_page": 0,
            }
            res = await self.client.collections[self.collection].documents.search(search_params)
            label = res["facet_counts"][0]["counts"][0]["value"]
            parts = mod.split("-")
            id = parts[1]
            version = "-".join(parts[2::])
            if id in labelled:
                labelled[id]["versions"].append(version)
            else:
                labelled[id] = {"label": label, "versions": [version]}
        defaults = ["default"]
        defaults += [mod for mod in mods if "current" in mod]
        return [labelled, str(defaults).replace("'", "")]

    async def load_db(self) -> None:
        self.wikiScopes = await self.bot.database.get_for(self, "channel_scopes", WikiDefaultScopes)

    async def cog_load(self):
        projects = await self.fetch_projects()
        self.project_data = projects[0]
        self.default_tags = projects[1]
        # Add computed choices to the command
        mod_choices = [app_commands.Choice(name=data["label"], value=id) for [id, data] in self.project_data.items()]
        app_commands.choices(mod=mod_choices)(self.wiki)
        app_commands.choices(mod=mod_choices)(self.scopesSet)
        await self.load_db()

    @scopes.command(name="set", description="Set the default mod to search for in a channel")
    @app_commands.describe(
        channel="The channel",
        mod="The mod to search for",
    )
    async def scopesSet(self, interaction: Interaction, channel: discord.TextChannel, mod: str):
        self.logger.info(f"{interaction.guild.name}: Setting default mod for wiki searches in {channel.name} to {mod}")
        current = await self.wikiScopes.get(interaction.guild.id, channel=channel.id)
        if current:
            current.mod = mod
        else:
            current = WikiDefaultScopes(channel.id, mod)
        await self.wikiScopes.upsert(interaction.guild.id, current)
        await interaction.response.send_message(f"Set default mod for wiki searches in {channel.name} to {mod}",
                                                ephemeral=True)

    @scopes.command(name="remove", description="Remove the default mod for a channel")
    @app_commands.describe(
        channel="The channel",
    )
    async def scopesRemove(self, interaction: Interaction, channel: discord.TextChannel):
        self.logger.info(f"{interaction.guild.name}: Removing default mod for wiki searches in {channel.name}")
        res = await self.wikiScopes.remove(interaction.guild.id, channel=channel.id)
        await interaction.response.send_message(
            f"Removed default mod for wiki searches in {channel.name}" if res else f"Could not remove anything. Probably nothing was configured.",
            ephemeral=True)

    @scopes.command(name="get", description="Get current server configs")
    async def scopesGet(self, interaction: Interaction):
        self.logger.info(f"{interaction.guild.name}: Getting default mod configs for wiki searches")
        scopes = await self.wikiScopes.get_all(interaction.guild.id)
        if len(scopes) == 0:
            await interaction.response.send_message(f"No default mods set for searches.")
            return
        desc = ""
        for scope in scopes:
            desc += f"<#{scope.channel}> - `{scope.mod}`\n"
        embed = discord.Embed(
            title=f"Default mod to search in channels",
            description=desc,
            color=EMBED_COLOR
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Search something on the wiki")
    @app_commands.describe(
        mod="The mod to search for",
        # version="The documentation version to use",
        query="The search query"
    )
    async def wiki(self, interaction: Interaction, mod: Optional[str], query: str):
        """
        Searches the wiki with the given query
        If top level already returns result we only return them. Otherwise try for next level anchors before searching all contents
        """
        current = await self.wikiScopes.get(interaction.guild, channel=interaction.channel.id)
        if not mod and current:
            mod = current.mod
        tag = self.parse_mod_data(mod)
        await interaction.response.defer()
        first = await self.run_search(query=query, tag=tag, type="lvl1")
        if await self.handle_result(interaction, query, mod, first):
            return
        second = await self.run_search(query=query, tag=tag, type="lvl2")
        if await self.handle_result(interaction, query, mod, second):
            return
        content = await self.run_search(query=query, tag=tag, type="content")
        if not await self.handle_result(interaction, query, mod, content, True):
            await interaction.edit_original_response(content=f"Nothing found for {query}!")

    @staticmethod
    def parse_mod_data(mod: Optional[str]):
        if mod:
            version = "current"
            return f"docs-{mod}-{version}"
        return None

    async def run_search(self, query: str, tag: Optional[str], type: Optional[str]):
        filters = [f"docusaurus_tag:={tag if tag else self.default_tags}", f"type:={type}" if type else ""]
        filter_by = " && ".join([f for f in filters if f])
        search_params: tst.SearchParameters = {"q": query, "query_by": "*", "filter_by": f"{filter_by}",
                                               "min_len_1typo": 5, "min_len_2typo": 9}
        if type != "content":
            search_params["num_typos"] = 1
        res = await self.client.collections[self.collection].documents.search(search_params)
        hits = res["hits"]
        urls = {}
        for doc in hits:
            url = doc["document"]["url"]
            if url not in urls:
                highlight = doc["highlight"]["content"]["snippet"] if "content" in doc["highlight"] else ""
                urls[url] = Wiki.parse_typesense_highlight(highlight)
        return urls

    async def handle_result(self, interaction: Interaction, query: str, mod: Optional[str], urls: dict[str, str],
                            use_highlight: bool = False):
        amount = len(urls)
        if amount == 0:
            return False
        if amount == 1:
            url = next(iter(urls))
            msg = f"<{url}>"
            meta = Wiki.fetch_url_meta(url)
            embed = None
            if meta:
                embed = discord.Embed(
                    title=meta.get("title", url),
                    description=urls[url] if use_highlight and urls[url] else meta.get("description", ""), url=url,
                    color=EMBED_COLOR
                )
            await interaction.edit_original_response(content=msg, embed=embed)
            return True
        msg = f"Found multiple matching pages: "
        description = ""
        count = 0
        for url in urls:
            if count != 0:
                description += "  \n"
            description += f"{url}  \n"
            url_desc = urls[url] if use_highlight and urls[url] else Wiki.fetch_url_meta(url).get("description", "")
            url_desc = url_desc.split("\n")
            for desc in url_desc:
                description += f"> {desc}  \n"
            count += 1
            if count == 5:
                break
        embed = discord.Embed(title=f'Search result for "{query}"', description=description, color=EMBED_COLOR)
        if amount > 5:
            search_url = f"{self.config['url']}/search?q={query}"
            if mod:
                search_url += f"&p={mod}"
            embed.add_field(name="For more results see", value=search_url)
        await interaction.edit_original_response(content=msg, embed=embed)
        return True

    @staticmethod
    def parse_typesense_highlight(snippet: str) -> str:
        soup = BeautifulSoup(snippet, "html.parser")
        for mark in soup.find_all("mark"):
            mark.string = f"**{mark.get_text()}**"
        return soup.get_text()

    @staticmethod
    def fetch_url_meta(url: str):
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code != 200:
            return {}
        anchor = urlparse(url).fragment
        soup = BeautifulSoup(resp.text, "html.parser")
        meta = {}
        title = soup.find("meta", property="og:title")
        meta["title"] = title["content"] if title else None
        desc = soup.find("meta", property="og:description")
        # Anchor elements description should be the next immediately following the anchor
        if anchor:
            anchor_element = soup.find(
                lambda tag: tag.name in ["h1", "h2", "h3", "h4", "h5", "h6"] and tag.get("id") == anchor)
            if anchor_element:
                next = anchor_element.next_sibling
                while next:
                    if next and next.name == "p":
                        desc = next
                        break
                    next = next.next_sibling
        meta["description"] = desc["content"] if desc and desc.has_attr("content") else Wiki.fetch_element_text(
            desc) if desc else None
        img = soup.find("meta", property="og:image")
        meta["image"] = img["content"] if img else None
        return meta

    @staticmethod
    def fetch_element_text(element: Tag) -> str:
        for br in element.find_all("br"):
            br.replace_with("\n")
        # Limit to 3 lines to not clutter it
        txt = "\n".join([t for t in element.get_text().split("\n")[:3] if t.strip()])
        if len(txt) <= 150:
            return txt
        return txt[:150].rsplit(" ", 1)[0] + "..."


async def setup(bot: Bot) -> None:
    try:
        await bot.add_cog(Wiki(bot))
    except typesense.configuration.ConfigError as e:
        bot.logger.info(f"Invalid wiki config. Cog will not be loaded. {e}")
