from functools import cache

from langchain_core.callbacks import BaseCallbackHandler
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

from rootline.config import Settings


def tracing_callbacks(settings: Settings) -> list[BaseCallbackHandler]:
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return []
    handler = langfuse_handler(
        settings.langfuse_public_key, settings.langfuse_secret_key, settings.langfuse_base_url
    )
    return [handler]


@cache
def langfuse_handler(public_key: str, secret_key: str, base_url: str) -> CallbackHandler:
    # One handler per process. The graph and the LLM calls inside its nodes then hold the
    # same object, and LangChain adds it to a run only once.
    Langfuse(public_key=public_key, secret_key=secret_key, base_url=base_url)
    return CallbackHandler(public_key=public_key)
