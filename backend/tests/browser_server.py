"""Isolated browser-test API; synthetic resources are never a production option."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.catalog import Catalog
import app.main as main
from conftest import runtime
import uvicorn

catalog = Catalog.model_validate_json((Path(__file__).parent / "fixtures/verified.json").read_text(encoding="utf-8"))
engine = runtime(catalog)
main.resources = lambda: engine

if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=18001, workers=1)
