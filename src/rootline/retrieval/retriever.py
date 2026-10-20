import psycopg
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document as LangChainDocument
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from rootline.ingest.embedding import Embedder
from rootline.retrieval.search import Lane, LaneResult, Searcher


class LaneRetriever(BaseRetriever):
    # One list per retriever, so a chain never gets RCAs and SOPs mixed together.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    database_url: str
    embedder: Embedder
    services: dict[str, list[str]]
    lane: Lane

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[LangChainDocument]:
        with psycopg.connect(self.database_url) as conn:
            response = Searcher(conn, self.embedder, self.services).search(query)
        result: LaneResult = getattr(response, self.lane)
        return [
            LangChainDocument(
                # An ID or title lookup has no matched passage, only the document.
                page_content=hit.snippet or hit.title,
                metadata={
                    "doc_id": hit.doc_id,
                    "doc_type": hit.doc_type,
                    "title": hit.title,
                    "version": hit.version,
                    "status": hit.status,
                    "score": hit.score,
                    "evidence": hit.evidence,
                },
            )
            for hit in result.hits
        ]
