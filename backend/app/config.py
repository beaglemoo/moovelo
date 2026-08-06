from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    valhalla_url: str = "http://valhalla:8002"
    cors_origins: str = ""
    static_dir: str = "/app/static"


settings = Settings()
