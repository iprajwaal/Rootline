from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://rootline:rootline@localhost:5432/rootline"
    raw_dir: Path = Path("data/raw")
    catalog_path: Path = Path("corpus/catalog.yaml")
    google_api_key: str = ""
    llm_small_model: str = "gemini-3.5-flash-lite"
    llm_cache_dir: Path = Path("cache/llm")
