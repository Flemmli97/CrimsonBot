import json
from dataclasses import dataclass
from typing import Awaitable, Protocol, TypeVar, Generic, Callable, Any, Type, Union, get_args, get_origin

from aiosqlite import Cursor
from discord.ext import commands

T = TypeVar("T")


@dataclass
class SQLCodec(Generic[T]):
    encoder: Callable[[T], str]
    decoder: Callable[[str], T]


JSON_CODEC = SQLCodec(lambda x: json.dumps(x) if x else None, lambda x: json.loads(x) if x else None)


@dataclass
class SQLSchema:
    schema: str
    keys: list[str]

    def __post_init__(self):
        self.schema = self.schema.rstrip().rstrip(",")


class Schema(Protocol):
    @classmethod
    def to_sql_schema(cls) -> SQLSchema: ...

    @classmethod
    def version(cls) -> int | None:
        """The current version of this schema. 1 if undefined
        """
        ...

    @classmethod
    async def table_update(cls, table: str, updater: Callable[[str], Awaitable[Any]]) -> dict[int, Callable[
        [], Awaitable[Any]]] | None:
        """A Handler for updating the tables schema
        Parameters
        ------------
        table: :class:`str`
        The name of the table

        updater: Callable[[str], Awaitable[Any]]
        Runs a sql query

        Returns
        --------
        :class:`dict[int, Callable[[], Awaitable[Any]]]`
            A mapping of version - update
        """
        ...


S = TypeVar("S", bound=Schema)
COG = TypeVar("COG", bound=Union[commands.Cog, str])


def encode_sql_obj(obj: S) -> dict[str, Any]:
    result = {}
    for key, field in obj.__annotations__.items():
        codec = JSON_CODEC if _is_json_supported_type__(field)[0] else None
        if hasattr(field, "__metadata__"):
            for metadata in field.__metadata__:
                if isinstance(metadata, SQLCodec):
                    codec = metadata
                    break
        value = getattr(obj, key)
        result[key] = codec.encoder(value) if codec else value
    return result


def decode_sql_obj(sql_result: dict[str, Any], clss: Type[S]) -> S:
    result = {}
    for key, value in sql_result.items():
        if key not in clss.__annotations__:
            continue
        field = clss.__annotations__[key]
        factory, none = _is_json_supported_type__(field)
        codec = JSON_CODEC if factory else None
        if hasattr(field, "__metadata__"):
            for metadata in field.__metadata__:
                if isinstance(metadata, SQLCodec):
                    codec = metadata
                    break
        result[key] = codec.decoder(value) if codec else value
        if codec == JSON_CODEC and not none and not result[key]:
            result[key] = factory()
    return clss(**result)


def _is_json_supported_type__(field) -> tuple[Any, bool]:
    factory = None
    orig = get_origin(field)
    if field is list or field is dict:
        factory = field
    if orig is list or orig is dict:
        factory = orig
    allow_none = False
    for arg in get_args(field):
        if not factory:
            if arg is list or arg is dict:
                factory = arg
        if arg is type(None):  # For union type that allows NONE values
            allow_none = True
    return factory, allow_none
