import threading

import httpx

from rootline.agent.answer import answer_asker
from rootline.agent.drafts import Drafter, draft_asker
from rootline.agent.executor import Executor, ShopLite
from rootline.agent.graph import Graph, Writers, build_graph
from rootline.agent.knowledge import Knowledge
from rootline.agent.present import Presenter
from rootline.agent.troubleshoot import agent_asker, conclusion_asker
from rootline.agent.understand import Understander, intent_asker
from rootline.catalog import load_catalog
from rootline.config import Settings
from rootline.ingest.factory import build_embedder
from rootline.llm.ledger import PostgresLedger
from rootline.llm.structured import build_llm
from rootline.llm.tracing import tracing_callbacks


def default_graph(settings: Settings) -> Graph:
    catalog = load_catalog(settings.catalog_path)
    ledger = PostgresLedger(settings.database_url).record
    llm = build_llm(settings, ledger)
    embedder = build_embedder(settings, ledger)
    threading.Thread(target=embedder.warm, daemon=True).start()
    return build_graph(
        Understander(catalog, intent_asker(llm)),
        Knowledge(settings.database_url, embedder, catalog),
        Presenter(settings.public_api_url, settings.stale_after_days),
        Writers(answer_asker(llm), agent_asker(llm), conclusion_asker(llm), draft_asker(llm)),
        Executor(
            settings.database_url,
            ShopLite(
                httpx.Client(base_url=settings.shoplite_url, timeout=settings.action_timeout_s)
            ),
            frozenset(catalog.actions),
            frozenset(catalog.checks),
        ),
        Drafter(settings.database_url, catalog),
        tracing_callbacks(settings),
    )
