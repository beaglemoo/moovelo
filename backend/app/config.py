from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    valhalla_url: str = "http://valhalla:8002"
    cors_origins: str = ""
    static_dir: str = "/app/static"
    # Optional self-hosted CyclOSM tile URL template ({z}/{x}/{y} placeholders).
    # Empty means the public CyclOSM servers are used.
    tile_url_cyclosm: str = ""


settings = Settings()
