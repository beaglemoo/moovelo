import mimetypes

import app.main  # noqa: F401  # importing runs mimetypes.add_type at module load


def test_webmanifest_served_as_manifest_json() -> None:
    # Starlette's StaticFiles picks the Content-Type from mimetypes, which does
    # not know .webmanifest on every Python build. Some browsers refuse to
    # install a PWA whose manifest arrives as text/plain, so app.main registers
    # the type at import time; this pins that it took effect.
    guessed, _ = mimetypes.guess_type("manifest.webmanifest")
    assert guessed == "application/manifest+json"
