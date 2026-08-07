from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # psycopg, not asyncpg: this is a one-shot batch process, so it shares
    # nothing with the backend's async stack.
    database_url: str = "postgresql://bikegps:bikegps@postgres:5432/bikegps"
    # Where Valhalla leaves the extract it downloaded. Mounted read-only.
    osm_dir: str = "/custom_files"
    # Scratch space for the filtered extract. Must be writable, and must
    # not be osm_dir, which is read-only.
    work_dir: str = "/tmp"
    osmium_binary: str = "osmium"


settings = Settings()
