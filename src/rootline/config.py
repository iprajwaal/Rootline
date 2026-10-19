from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://rootline:rootline@localhost:5432/rootline"
    raw_dir: Path = Path("data/raw")
    catalog_path: Path = Path("corpus/catalog.yaml")
    embed_model: str = "BAAI/bge-small-en-v1.5"
    model_cache_dir: Path = Path("data/models")
    google_api_key: str = ""
    llm_small_model: str = "gemini-3.5-flash-lite"
    llm_cache_dir: Path = Path("cache/llm")
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 2
    # Paid-tier USD per million tokens for llm_small_model. Output includes thinking tokens.
    llm_input_usd_per_mtok: float = 0.30
    llm_output_usd_per_mtok: float = 2.50
    # Tracing is off until both keys are set.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
