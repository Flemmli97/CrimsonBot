from dataclasses import dataclass
from logging import Logger
from textwrap import dedent
from typing import Awaitable, Callable, Generic, Type

import aiosqlite

from utils.sqlutils import S, COG, SQLSchema, encode_sql_obj, decode_sql_obj

GUILD_ID = "guild_id"
DEFAULT_VERSION = 1


@dataclass
class SchemaConfig:
    version: int


class GlobalDatabase(Generic[S]):
    def __init__(self, logger: Logger, connection: aiosqlite.Connection, table: str, cls: Type[S]):
        self._connection = connection
        self._connection.row_factory = aiosqlite.Row
        self._logger = logger
        self._cls = cls
        self.table = table

    async def upsert(self, obj: S) -> bool:
        async with self._connection.cursor() as cursor:
            query = []
            params = []
            for key, value in encode_sql_obj(obj).items():
                query.append(key)
                params.append(value)
            await cursor.execute(
                f"REPLACE INTO {self.table} ({', '.join(query)}) values ({', '.join(['?' for _ in query])})", params)
            await self._connection.commit()
            return cursor.rowcount > 0

    async def remove(self, **kwargs) -> bool:
        if len(kwargs) == 0:
            self._logger.info("No arguments passed, skipping remove")
            return False
        async with self._connection.cursor() as cursor:
            query = ""
            params = []
            for key, value in kwargs.items():
                query += f"{key}=?" if not query else f" AND {key}=?"
                params.append(value)
            await cursor.execute(f"DELETE FROM {self.table} WHERE {query}", params)
            await self._connection.commit()
            return cursor.rowcount > 0

    async def get(self, **kwargs) -> S | None:
        if len(kwargs) == 0:
            self._logger.info("No arguments passed!")
            return None
        async with self._connection.cursor() as cursor:
            query = ""
            params = []
            for key, value in kwargs.items():
                query += f"{key}=?" if not query else f" AND {key}=?"
                params.append(value)
            await cursor.execute(f"SELECT * FROM {self.table} WHERE {query}", params)
            result = await cursor.fetchone()
            if result:
                return decode_sql_obj(dict(result), self._cls)
            return None

    async def get_all(self) -> list[S]:
        async with self._connection.cursor() as cursor:
            await cursor.execute(f"SELECT * FROM {self.table}")
            fetched = await cursor.fetchall()
            results = []
            for result in fetched:
                result_v = dict(result)
                results.append(decode_sql_obj(result_v, self._cls))
            return results

    async def execute(self, runner: Callable[[str, aiosqlite.Cursor], Awaitable[bool]]):
        async with self._connection.cursor() as cursor:
            if await runner(self.table, cursor):
                await self._connection.commit()


class CogDatabase(Generic[S]):
    def __init__(self, logger: Logger, connection: aiosqlite.Connection, table: str, cls: Type[S]):
        self._connection = connection
        self._connection.row_factory = aiosqlite.Row
        self._logger = logger
        self._cls = cls
        self.table = table

    async def upsert(self, guild: int, obj: S) -> bool:
        async with self._connection.cursor() as cursor:
            query = [GUILD_ID]
            params = [guild]
            for key, value in encode_sql_obj(obj).items():
                query.append(key)
                params.append(value)
            await cursor.execute(
                f"REPLACE INTO {self.table} ({', '.join(query)}) values ({', '.join(['?' for _ in query])})", params)
            await self._connection.commit()
            return cursor.rowcount > 0

    async def remove(self, guild: int, forced=False, **kwargs) -> bool:
        if len(kwargs) == 0 and not forced:
            self._logger.info("No arguments passed, skipping remove")
            return False
        async with self._connection.cursor() as cursor:
            query = f"{GUILD_ID}=?"
            params = [guild]
            for key, value in kwargs.items():
                query += f" AND {key}=?"
                params.append(value)
            await cursor.execute(f"DELETE FROM {self.table} WHERE {query}", params)
            await self._connection.commit()
            return cursor.rowcount > 0

    async def clear_guild_data(self, guild: int) -> bool:
        return await self.remove(guild=guild, forced=True)

    async def get(self, guild: int, **kwargs) -> S | None:
        async with self._connection.cursor() as cursor:
            query = f"{GUILD_ID}=?"
            params = [guild]
            for key, value in kwargs.items():
                query += f" AND {key}=?"
                params.append(value)
            await cursor.execute(f"SELECT * FROM {self.table} WHERE {query}", params)
            result = await cursor.fetchone()
            if result:
                return decode_sql_obj(dict(result), self._cls)
            return None

    async def get_all_of(self) -> list[tuple[int, S]]:
        async with self._connection.cursor() as cursor:
            await cursor.execute(f"SELECT * FROM {self.table}")
            fetched = await cursor.fetchall()
            results = []
            for result in fetched:
                result_v = dict(result)
                results.append((int(result_v[GUILD_ID]), decode_sql_obj(result_v, self._cls)))
            return results

    async def get_all(self, guild: int, **kwargs) -> list[S]:
        async with self._connection.cursor() as cursor:
            query = f"{GUILD_ID}=?"
            params = [guild]
            for key, value in kwargs.items():
                query += f" AND {key}=?"
                params.append(value)
            await cursor.execute(f"SELECT * FROM {self.table} WHERE {query}", params)
            fetched = await cursor.fetchall()
            results = []
            for result in fetched:
                results.append(decode_sql_obj(dict(result), self._cls))
            return results


class BotDatabase:
    def __init__(self, logger: Logger, connection: aiosqlite.Connection):
        self._connection = connection
        self._logger = logger
        self.version = False

    async def close(self):
        await self._connection.close()

    async def _fetch_schema_db(self):
        op = dedent(f"""
        CREATE TABLE IF NOT EXISTS sql_table_schema (
            version int(40) NOT NULL,
            database_id varchar(25) NOT NULL PRIMARY KEY
        );""").lstrip()
        await self._connection.executescript(op)
        await self._connection.commit()

    async def _current_schema_version(self, table: str) -> int:
        if not self.version:
            await self._fetch_schema_db()
            self.version = True
        async with self._connection.cursor() as cursor:
            await cursor.execute(f"SELECT version FROM sql_table_schema WHERE database_id=?", (table,))
            result = await cursor.fetchone()
            if result:
                return result[0]
            return DEFAULT_VERSION

    async def _update_current_schema_version(self, table: str, version: int):
        if not self.version:
            await self._fetch_schema_db()
            self.version = True
        async with self._connection.cursor() as cursor:
            await cursor.execute(
                f"REPLACE INTO sql_table_schema (database_id, version) values (?, ?)", (table, version))
            await self._connection.commit()

    async def _register_table(self, identifier: COG, table: str, schema: Type[S], global_db: bool = False):
        sql: SQLSchema = schema.to_sql_schema()
        primary = ["guild_id"] if not global_db else []
        primary.extend(sql.keys)
        primary = ", ".join(primary)
        if len(primary) == 0:
            raise ValueError("No primary key defined for table!")
        table = f"{identifier if type(identifier) is str else identifier.qualified_name}_{table}"
        guild_row = f"{GUILD_ID} int(25) NOT NULL," if not global_db else ""
        op = dedent(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            {sql.schema},
            {guild_row}
            CONSTRAINT unique_id PRIMARY KEY ({primary})
        );""").lstrip()
        await self._connection.executescript(op)
        await self._connection.commit()

        current_version = await self._current_schema_version(table=table)

        ran_script = False

        async def run(script: str):
            nonlocal ran_script
            ran_script = True
            return await self._connection.executescript(script)

        updates = await schema.table_update(table, run)
        if updates:
            versions = sorted(updates.keys())
            for version in versions:
                if current_version >= version:
                    break
                self._logger.info(f"Migrating database schema for {table} to {version}")
                await updates[version]()
            if ran_script:
                await self._connection.commit()
        await self._update_current_schema_version(table=table, version=schema.version() or DEFAULT_VERSION)
        return table

    async def get_for(self, identifier: COG, table: str, schema: Type[S]) -> CogDatabase[S]:
        table = await self._register_table(identifier, table, schema)
        return CogDatabase[S](self._logger, self._connection, table, schema)

    async def get_global(self, table: str, schema: Type[S]) -> GlobalDatabase[S]:
        table = await self._register_table("Global", table, schema, global_db=True)
        return GlobalDatabase[S](self._logger, self._connection, table, schema)
