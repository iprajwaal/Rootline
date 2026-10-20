from functools import cache
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from pydantic import BaseModel

from rootline.config import Settings
from rootline.ingest.redaction import redact


def tracing_callbacks(settings: Settings) -> list[BaseCallbackHandler]:
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return []
    handler = langfuse_handler(
        settings.langfuse_public_key, settings.langfuse_secret_key, settings.langfuse_base_url
    )
    return [handler]


class SharedHandler(CallbackHandler):
    # The agent server deep-copies each graph's config per run, and a Langfuse client cannot
    # be copied. When the copy fails the server drops the checkpointer and chat memory with
    # it, so a copy hands back the same handler.
    def __deepcopy__(self, memo: dict[int, Any]) -> "SharedHandler":
        return self


@cache
def langfuse_handler(public_key: str, secret_key: str, base_url: str) -> CallbackHandler:
    # One handler per process. The graph and the LLM calls inside its nodes then hold the
    # same object, and LangChain adds it to a run only once.
    Langfuse(public_key=public_key, secret_key=secret_key, base_url=base_url, mask=mask)
    return SharedHandler(public_key=public_key)


def mask(*, data: Any, **kwargs: Any) -> Any:
    # Langfuse records the graph input before the guard node runs, so a pasted secret is
    # redacted here too before it leaves the process.
    if isinstance(data, str):
        return redact(data).text
    if isinstance(data, BaseModel):
        return mask(data=data.model_dump(mode="json"))
    if isinstance(data, dict):
        return {key: mask(data=value) for key, value in data.items()}
    if isinstance(data, list | tuple):
        return [mask(data=value) for value in data]
    return data
