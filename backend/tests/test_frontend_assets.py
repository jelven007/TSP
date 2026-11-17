"""Content-addressed frontend files may be reused without caching entry HTML."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.frontend_assets import FrontendAssets


def test_cache_only_successful_fingerprinted_files(tmp_path):
    (tmp_path / "page-Ab3_dE-f.js").write_text("export const value = 1")
    (tmp_path / "inter-latin-400-normal-Ab3_dE-f.woff2").write_bytes(b"font")
    (tmp_path / "unversioned.js").write_text("unversioned")
    (tmp_path / "index.html").write_text("<html></html>")
    app = FastAPI()
    app.mount("/assets", FrontendAssets(directory=tmp_path))
    with TestClient(app) as client:
        script = client.get("/assets/page-Ab3_dE-f.js")
        assert script.status_code == 200
        assert script.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert script.text == "export const value = 1"
        revalidated = client.get("/assets/page-Ab3_dE-f.js", headers={"If-None-Match": script.headers["etag"]})
        assert revalidated.status_code == 304
        assert revalidated.headers["cache-control"] == script.headers["cache-control"]
        assert client.get("/assets/inter-latin-400-normal-Ab3_dE-f.woff2").headers["cache-control"] == script.headers["cache-control"]
        for path, status in (
            ("unversioned.js", 200), ("index.html", 200),
            ("missing-Ab3_dE-f.js", 404), ("../../private-Ab3_dE-f.js", 404),
        ):
            response = client.get("/assets/" + path)
            assert response.status_code == status
            assert "immutable" not in response.headers.get("cache-control", "")
