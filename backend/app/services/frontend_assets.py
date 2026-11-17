"""Static frontend files, separate from entry HTML and authenticated API reads."""
import re

from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

_FINGERPRINTED_ASSET = re.compile(
    r"^[^/]+-[A-Za-z0-9_-]{8}\.(?:js|css|woff2?|ttf|otf|svg|png|jpe?g|webp|avif|ico)$"
)


class FrontendAssets(StaticFiles):
    """Reuse successful Vite hash assets; HTML, missing files and API stay separate."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in {200, 206, 304} and _FINGERPRINTED_ASSET.fullmatch(path):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
