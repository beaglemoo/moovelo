"""Resolve the assistant's endpoint configuration.

Two sources, and the precedence is per-field rather than all-or-nothing:
a value set in the admin page wins, and anything left unset falls back to
its environment variable. That means an operator can change just the model
without having to restate the endpoint, and a fully declarative install
that never opens the admin page keeps working untouched.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
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


async def select_llm_settings(db: AsyncSession) -> LLMSettings | None:
    """The stored singleton, or None if the `llm_settings` table is absent -
    the state of an install downgraded past migration 0011.

    The SELECT runs inside a SAVEPOINT so a missing table - which aborts the
    statement at the Postgres level - is contained: only the savepoint rolls
    back, and the caller's surrounding transaction stays intact and
    committable. The earlier fix rolled the whole transaction back instead,
    which silently discarded a caller's uncommitted work (share_route assigns
    route.share_token two lines before it asks for a summary) and then blew up
    on the next ORM attribute access with MissingGreenlet. A savepoint touches
    nothing but the failed read.
    """
    try:
        async with db.begin_nested():
            return (await db.execute(select(LLMSettings))).scalar_one_or_none()
    except ProgrammingError:
        return None


async def resolve_llm_config(db: AsyncSession) -> LLMConfig:
    """The effective configuration: the stored row over the environment.

    Falls back to env-only config if `llm_settings` is absent (an install
    downgraded past migration 0011). Without this the unhandled
    UndefinedTableError is not just an /api/admin/llm 500: resolve_llm_config
    is also called from the unauthenticated GET /api/config on every page
    load, so the whole app 500s for every visitor, and 0011's promised
    recovery (set the LLM_* env vars) never runs.
    """
    env = config_from_env()
    row = await select_llm_settings(db)
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
