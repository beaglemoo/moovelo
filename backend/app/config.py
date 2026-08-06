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
    # External base URL of the app, used to build the OIDC redirect URI.
    app_url: str = "http://localhost:5173"
    # Optional OIDC single sign-on. All three must be set to enable it.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_provider_name: str = "SSO"

    # Set false to hide email+password login entirely (SSO-only). Ignored
    # unless OIDC is configured, so misconfiguration cannot lock everyone out.
    password_auth_enabled: bool = True

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def password_login_active(self) -> bool:
        return self.password_auth_enabled or not self.oidc_enabled

    # Wahoo Cloud API (sandbox or production app credentials).
    wahoo_client_id: str = ""
    wahoo_client_secret: str = ""

    @property
    def wahoo_enabled(self) -> bool:
        return bool(self.wahoo_client_id and self.wahoo_client_secret)


settings = Settings()
