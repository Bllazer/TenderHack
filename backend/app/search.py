"""Shared offline/query analyzer, BM25 and exact NumPy RRF retrieval."""
import math
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path
from threading import Lock

import numpy as np

from app.catalog import Snapshot
from app.schemas import CategoryCandidate

ANALYZER_VERSION = "nfc-pymorphy3-v1"
MODEL_ID = "intfloat/multilingual-e5-small"


def canon(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().replace("ё", "е").split())


class Analyzer:
    def __init__(self):
        import pymorphy3
        self.morph = pymorphy3.MorphAnalyzer()

    @lru_cache(maxsize=20000)
    def lemma(self, token: str) -> str:
        # Protect identifiers, Latin names, numbers and units from morphology.
        if re.fullmatch(r"[а-я]+", token):
            return self.morph.parse(token)[0].normal_form
        return token

    def tokens(self, text: str) -> tuple[str, ...]:
        return tuple(self.lemma(token) for token in re.findall(r"[\w²³]+(?:[-·/][\w²³]+)*", canon(text)))


class E5Encoder:
    def __init__(self, directory: Path, expected_revision: str):
        import json
        import torch
        from transformers import AutoModel, AutoTokenizer
        from app.catalog import digest
        metadata = json.loads((directory / "model.meta.json").read_text(encoding="utf-8"))
        if metadata["model_id"] != MODEL_ID or metadata["revision"] != expected_revision:
            raise ValueError("Model/index revision mismatch")
        if not {"model.safetensors", "config.json", "tokenizer.json"}.issubset(metadata["sha256"]):
            raise ValueError("Model metadata does not cover necessary weights/tokenizer")
        for name, checksum in metadata["sha256"].items():
            path = (directory / name).resolve()
            if not path.is_relative_to(directory.resolve()) or digest(path) != checksum:
                raise ValueError("Model checksum mismatch")
        torch.set_num_threads(2)
        self.tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
        self.model = AutoModel.from_pretrained(directory, local_files_only=True, trust_remote_code=False).eval()
        self.lock = Lock()

    def encode(self, texts: list[str], prefix: str) -> np.ndarray:
        import torch
        results = []
        with self.lock, torch.inference_mode():
            for offset in range(0, len(texts), 16):
                batch = self.tokenizer([prefix + text for text in texts[offset:offset + 16]],
                                       max_length=512, truncation=True, padding=True, return_tensors="pt")
                output = self.model(**batch).last_hidden_state
                mask = batch["attention_mask"].unsqueeze(-1)
                embeddings = (output * mask).sum(1) / mask.sum(1).clamp(min=1)
                embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1)
                results.append(embeddings.cpu().numpy().astype(np.float32))
        return np.concatenate(results) if results else np.empty((0, 384), dtype=np.float32)


class Search:
    def __init__(self, data: Snapshot, encoder, analyzer: Analyzer | None = None):
        self.data, self.encoder = data, encoder
        self.analyzer = analyzer or Analyzer()
        self.documents = tuple(Counter(row) for row in data.tokens)
        self.lengths = np.array([sum(row.values()) for row in self.documents], dtype=np.float64)
        self.average = max(float(self.lengths.mean()) if len(self.lengths) else 0, 1)
        self.df = Counter(token for row in self.documents for token in row)

    def lexical(self, text: str) -> np.ndarray:
        scores = np.zeros(len(self.documents))
        n = len(self.documents)
        for token in set(self.analyzer.tokens(text)):
            df = self.df[token]
            if not df:
                continue
            idf = math.log(1 + (n - df + .5) / (df + .5))
            freq = np.array([row[token] for row in self.documents])
            scores += idf * freq * 2.5 / (freq + 1.5 * (.25 + .75 * self.lengths / self.average))
        return scores

    def find(self, text: str) -> list[CategoryCandidate]:
        lexical = self.lexical(text)
        dense = self.data.vectors @ self.encoder.encode([text], "query: ")[0] if self.encoder else None
        ranks: dict[int, float] = {}
        sources: dict[int, list[str]] = {}
        for label, scores in (("BM25", lexical), ("E5", dense)):
            if scores is None:
                continue
            indices = np.argsort(-scores, kind="stable")[:50]
            for rank, index in enumerate(indices, 1):
                if label == "BM25" and scores[index] <= 0:
                    continue
                key = int(index)
                ranks[key] = ranks.get(key, 0) + 1 / (60 + rank)
                sources.setdefault(key, []).append(label)
        categories = self.data.catalog.categories
        return [CategoryCandidate(id=categories[index].id, name=categories[index].name, rank=rank,
                                  rank_score=ranks[index], reasons=sources[index])
                for rank, index in enumerate(sorted(ranks, key=lambda i: (-ranks[i], categories[i].id))[:5], 1)]
