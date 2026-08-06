"""Regenerate the golden export files after an intentional format change.

Run from backend/: uv run python -m tests.golden.regen
"""

from pathlib import Path

from tests.conftest import make_snapshot
from tests.test_exports import build_outputs


def main() -> None:
    golden = Path(__file__).parent
    gpx, fit = build_outputs(make_snapshot())
    (golden / "route.gpx").write_bytes(gpx)
    (golden / "route.fit").write_bytes(fit)
    print(f"wrote {len(gpx)} GPX bytes, {len(fit)} FIT bytes")


if __name__ == "__main__":
    main()
