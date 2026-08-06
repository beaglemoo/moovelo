from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    valhalla_url: str = "http://valhalla:8002"
    cors_origins: str = ""
    static_dir: str = "/app/static"
    # Optional self-hosted CyclOSM tile URL template ({z}/{x}/{y} placeholders).
    # Empty means the public CyclOSM servers are used.
    tile_url_cyclosm: str = ""
    database_url: str = "postgresql+asyncpg://bikegps:bikegps@postgres:5432/bikegps"
    signups_enabled: bool = False
    cookie_secure: bool = False
    session_ttl_days: int = 30


settings = Settings()
