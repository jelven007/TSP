"""Select the Cell API's small dependency closure from uv's hash-pinned export."""

import re
import sys
from pathlib import Path

NAMES = {
    "fastapi", "uvicorn", "pydantic", "pydantic-core", "pydantic-settings", "python-dotenv",
    "starlette", "typing-extensions", "typing-inspection", "annotated-types", "annotated-doc",
    "anyio", "idna", "sniffio", "click", "h11", "psycopg", "psycopg-binary", "psycopg-pool",
    "httpx", "httpcore", "certifi",
}
text = Path(sys.argv[1]).read_text(encoding="utf-8")
blocks = re.split(r"(?m)^(?=[a-zA-Z0-9_.-]+==)", text)
selected = [block for block in blocks if block.split("==")[0].strip() in NAMES]
found = {block.split("==")[0].strip() for block in selected}
if not {"fastapi", "psycopg", "psycopg-binary", "psycopg-pool", "uvicorn"} <= found:
    raise SystemExit("Missing Cell requirements in lock export")
Path(sys.argv[2]).write_text("\n".join(selected), encoding="utf-8")
