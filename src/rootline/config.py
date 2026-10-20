from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://rootline:rootline@localhost:5432/rootline"
    raw_dir: Path = Path("data/raw")
    catalog_path: Path = Path("corpus/catalog.yaml")
    # Labelled documents the classifier learns from.
    corpus_dir: Path = Path("corpus")
    google_api_key: str = ""
    # A Vertex AI API key calls the same Gemini models through Vertex instead of the
    # Gemini API. The cache does not care which one answered.
    llm_vertexai: bool = False
    llm_small_model: str = "gemini-3.5-flash-lite"
    llm_cache_dir: Path = Path("cache/llm")
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 2
    # Paid-tier USD per million tokens for llm_small_model. Output includes thinking tokens.
    llm_input_usd_per_mtok: float = 0.30
    llm_output_usd_per_mtok: float = 2.50
    # Vertex embeddings, cached on disk like the LLM answers. The vector size must match the
    # chunks.embedding column.
    embed_model: str = "text-embedding-005"
    # Empty means the LLM key is used.
    embed_api_key: str = ""
    embed_dims: int = 768
    embed_cache_dir: Path = Path("cache/embeddings")
    # Long enough for Vertex's slow first call on a new connection.
    embed_timeout_s: float = 30.0
    # Retries back off on rate limits; after the last one a search falls back to keywords.
    embed_query_attempts: int = 3
    # USD per million input characters for embed_model.
    embed_usd_per_mchar: float = 0.025
    # Tracing is off until both keys are set.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
    # Where chat answers link to, as seen from the user's browser.
    public_api_url: str = "http://localhost:8000"
    # Older documents are still shown, with a note to check they still hold.
    stale_after_days: int = 365
    log_level: str = "info"
    # The sandbox approved SOP steps run against, and the agent server alerts open threads on.
    shoplite_url: str = "http://localhost:8080"
    # shoplite: the simulated sandbox. k8s: the shop on the kind cluster, through kubectl.
    shop_mode: Literal["shoplite", "k8s"] = "shoplite"
    kube_health_url: str = "http://localhost:30080/health"
    # The reading and the fixing account's contexts. Empty uses the kubeconfig's current one.
    kube_context: str = ""
    kube_fix_context: str = ""
    agent_url: str = "http://localhost:2024"
    action_timeout_s: float = 30.0
    # Browser origins allowed to call the API, such as the chat UI.
    ui_origins: list[str] = ["http://localhost:3000"]
