from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.catalog import Catalog, snapshot
from app.search import Analyzer, Search


class FixtureEncoder:
    """Deterministic test-only vectors; never part of the working resource loader."""
    def encode(self, texts, prefix):
        result = np.zeros((len(texts), 384), dtype=np.float32)
        for index, text in enumerate(texts):
            target = 1 if "стол" in text.casefold() else 2 if "куртк" in text.casefold() else 0
            result[index, target] = 1
        return result


@pytest.fixture
def catalog_data():
    return Catalog.model_validate_json((Path(__file__).parent / "fixtures/verified.json").read_text(encoding="utf-8"))


def runtime(catalog):
    analyzer = Analyzer()
    vectors = FixtureEncoder().encode([category.search_text for category in catalog.categories], "passage: ")
    vectors.setflags(write=False)
    data = snapshot(catalog, vectors=vectors, tokens=tuple(analyzer.tokens(category.search_text) for category in catalog.categories))
    return data, Search(data, FixtureEncoder(), analyzer)


@pytest.fixture
def engine(catalog_data):
    return runtime(catalog_data)


@pytest.fixture
def client(monkeypatch, engine):
    import app.main as main
    monkeypatch.setattr(main, "resources", lambda: engine)
    with TestClient(main.app) as api:
        yield api
