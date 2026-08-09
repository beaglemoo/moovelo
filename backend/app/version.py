import tomllib
from pathlib import Path


def _read_version() -> str:
    # pyproject.toml ships in the image next to app/ (Dockerfile), so this
    # resolves identically in dev and prod. The project is not pip-installed
    # (uv sync --no-install-project), so importlib.metadata cannot be used.
    path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with path.open("rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "unknown"


APP_VERSION = _read_version()
