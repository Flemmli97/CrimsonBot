from logging import Logger
from textwrap import dedent
from typing import Generic, Type

import aiosqlite

from utils.sqlutils import S, COG, SQLSchema, encode_sql_obj, decode_sql_obj

GUILD_ID = 'guild_id'


class CogDatabase(Generic[S]):
    def __init__(self, logger: Logger, connection: aiosqlite.Connection,
                 table: str, cls: Type[S]):
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
                f'REPLACE INTO {self.table} ({", ".join(query)}) values ({", ".join(["?" for _ in query])})', params)
            await self._connection.commit()
            return cursor.rowcount > 0

    async def remove(self, guild: int, forced=False, **kwargs) -> bool:
        if len(kwargs) == 0 and not forced:
            self._logger.info("No arguments passed, skipping remove")
            return False
        async with self._connection.cursor() as cursor:
            query = f'{GUILD_ID}=?'
            params = [guild]
            for key, value in kwargs.items():
                query += f' AND {key}=?'
                params.append(value)
            await cursor.execute(f'DELETE FROM {self.table} WHERE {query}', params)
            await self._connection.commit()
            return cursor.rowcount > 0

    async def clear_guild_data(self, guild: int) -> bool:
        return await self.remove(guild=guild, forced=True)

    async def get(self, guild: int, **kwargs) -> S | None:
        async with self._connection.cursor() as cursor:
            query = f'{GUILD_ID}=?'
            params = [guild]
            for key, value in kwargs.items():
                query += f' AND {key}=?'
                params.append(value)
            await cursor.execute(
                f'SELECT * FROM {self.table} WHERE {query}', params)
            result = await cursor.fetchone()
            if result:
                return decode_sql_obj(dict(result), self._cls)
            return None

    async def get_all_of(self) -> list[tuple[int, S]]:
        async with self._connection.cursor() as cursor:
            await cursor.execute(
                f'SELECT * FROM {self.table}')
            fetched = await cursor.fetchall()
            results = []
            for result in fetched:
                result_v = dict(result)
                results.append((int(result_v[GUILD_ID]), decode_sql_obj(result_v, self._cls)))
            return results

    async def get_all(self, guild: int, **kwargs) -> list[S]:
        async with self._connection.cursor() as cursor:
            query = f'{GUILD_ID}=?'
            params = [guild]
            for key, value in kwargs.items():
                query += f' AND {key}=?'
                params.append(value)
            await cursor.execute(
                f'SELECT * FROM {self.table} WHERE {query}', params)
            fetched = await cursor.fetchall()
            results = []
            for result in fetched:
                results.append(decode_sql_obj(dict(result), self._cls))
            return results


class BotDatabase:
    def __init__(self, logger: Logger, connection: aiosqlite.Connection):
        self._connection = connection
        self._logger = logger

    async def close(self):
        await self._connection.close()

    async def _register_table(self, identifier: COG, table: str, schema: Type[S]):
        sql: SQLSchema = schema.to_sql_schema()
        primary = ['guild_id']
        primary.extend(sql.keys)
        primary = ', '.join(primary)
        table = f'{identifier if type(identifier) is str else identifier.qualified_name}_{table}'
        op = dedent(f'''
        CREATE TABLE IF NOT EXISTS {table} (
            {sql.schema},
            {GUILD_ID} int(25) NOT NULL,
            CONSTRAINT unique_id PRIMARY KEY ({primary})
        );''').lstrip()
        await self._connection.executescript(op)
        await self._connection.commit()

        async def run(s: str):
            return await self._connection.executescript(s)

        updater = await schema.table_update(table, run)
        if updater:
            op = f'''
            ALTER TABLE {table}
            {updater}
            '''
            await self._connection.executescript(op)
            await self._connection.commit()
        return table

    async def get_for(self, identifier: COG, table: str, schema: Type[S]) -> CogDatabase[S]:
        table = await self._register_table(identifier, table, schema)
        return CogDatabase[S](self._logger, self._connection, table, schema)
