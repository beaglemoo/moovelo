"""Resolve the assistant's endpoint configuration.

Two sources, and the precedence is per-field rather than all-or-nothing:
a value set in the admin page wins, and anything left unset falls back to
its environment variable. That means an operator can change just the model
without having to restate the endpoint, and a fully declarative install
that never opens the admin page keeps working untouched.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import LLMSettings

# OpenRouter routing modes. "balanced" is the gateway's own default and is
# expressed by sending no sort at all.
PROVIDER_SORTS = ("balanced", "price", "throughput", "latency")


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    providers: list[str] = field(default_factory=list)
    provider_sort: str = ""
    max_prompt_price: float | None = None

    @property
    def enabled(self) -> bool:
        # Deliberately not the API key: a local Ollama endpoint needs none.
        return bool(self.base_url and self.model)


def _split_providers(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [name.strip() for name in raw.split(",") if name.strip()]


def config_from_env() -> LLMConfig:
    return LLMConfig(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        providers=settings.llm_providers,
        provider_sort="",
        max_prompt_price=None,
    )


async def resolve_llm_config(db: AsyncSession) -> LLMConfig:
    """The effective configuration: the stored row over the environment."""
    row = (await db.execute(select(LLMSettings))).scalar_one_or_none()
    env = config_from_env()
    if row is None:
        return env
    # `or` is the right operator here rather than an `is not None` check:
    # an empty string in the database means the same as unset, and treating
    # it as an override would silently disable a working env-configured
    # install the first time someone saved the form with a blank field.
    return LLMConfig(
        base_url=row.base_url or env.base_url,
        model=row.model or env.model,
        api_key=row.api_key or env.api_key,
        providers=_split_providers(row.provider_order) or env.providers,
        provider_sort=row.provider_sort or "",
        max_prompt_price=row.max_prompt_price,
    )
