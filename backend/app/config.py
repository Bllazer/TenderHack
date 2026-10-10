from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    catalog_dir: Path = Path("resources/catalog")
    model_dir: Path = Path("resources/model")
    clean_supply_dir: Path | None = Path("../ktru_clean")
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    max_parallel_analyses: int = 2
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
