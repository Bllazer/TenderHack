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
        # GPU used when present; CPU path stays the fallback and keeps the original
        # thread cap. Encoding is the only heavy step, so device choice lives here.
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.batch_size = 64 if self.device.type == "cuda" else 16
        if self.device.type == "cpu":
            torch.set_num_threads(2)
        self.tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
        self.model = AutoModel.from_pretrained(directory, local_files_only=True, trust_remote_code=False).eval().to(self.device)
        self.lock = Lock()

    def encode(self, texts: list[str], prefix: str) -> np.ndarray:
        import torch
        results = []
        with self.lock, torch.inference_mode():
            for offset in range(0, len(texts), self.batch_size):
                batch = self.tokenizer([prefix + text for text in texts[offset:offset + self.batch_size]],
                                       max_length=512, truncation=True, padding=True, return_tensors="pt")
                batch = {name: tensor.to(self.device) for name, tensor in batch.items()}
                output = self.model(**batch).last_hidden_state
                mask = batch["attention_mask"].unsqueeze(-1)
                embeddings = (output * mask).sum(1) / mask.sum(1).clamp(min=1)
                embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1)
                results.append(embeddings.cpu().numpy().astype(np.float32))
        return np.concatenate(results) if results else np.empty((0, 384), dtype=np.float32)


class CharNgramIndex:
    """Character n-gram TF-IDF over category names: a typo fallback for BM25.

    BM25 works on lemmas and misses misspellings entirely; dense vectors often do
    find the right family, but a single-channel leader loses inside RRF to documents
    that appear in two channels. This channel adds an independent ranking without
    training and without rewriting the user's text.

    Design notes, both learned from measured failures:
      * grams are built per word, never across a word boundary — joining
        "шуруповирт аккумуляторный" into one string produced junk grams;
      * scores are cosine-normalised, otherwise a long query accumulates weight from
        unrelated words and short names (like "Шуруповерт") lose despite matching
        almost completely.
    """

    N = 3

    def __init__(self, names: list[str], min_df: int = 1):
        postings: dict[str, list[tuple[int, int]]] = {}
        document_frequency: Counter = Counter()
        for index, name in enumerate(names):
            grams = Counter(self.grams(name))
            for gram, frequency in grams.items():
                postings.setdefault(gram, []).append((index, frequency))
                document_frequency[gram] += 1
        # Grams seen in a single document cannot discriminate anything.
        self.postings = {gram: rows for gram, rows in postings.items()
                         if document_frequency[gram] >= min_df}
        total = max(len(names), 1)
        self.idf = {gram: math.log(1 + (total - document_frequency[gram] + .5)
                                   / (document_frequency[gram] + .5))
                    for gram in self.postings}
        self.size = len(names)
        self.grams_total = sum(len(rows) for rows in self.postings.values())

        # Norms let us score by cosine instead of a raw sum.
        self.norms = np.zeros(self.size)
        for gram, rows in self.postings.items():
            idf = self.idf[gram]
            for index, frequency in rows:
                weight = idf * self._saturate(frequency)
                self.norms[index] += weight * weight
        np.sqrt(self.norms, out=self.norms)

    @staticmethod
    def _saturate(frequency: int) -> float:
        """Saturated term frequency: long names must not dominate."""
        return frequency / (frequency + 1.5)

    def grams(self, text: str) -> list[str]:
        result: list[str] = []
        for word in canon(text).split():
            if len(word) < self.N:
                if word:
                    result.append(word)
                continue
            result.extend(word[index:index + self.N] for index in range(len(word) - self.N + 1))
        return result

    def scores(self, text: str) -> np.ndarray:
        counts = Counter(self.grams(text))
        if not counts:
            return np.zeros(self.size)
        query: list[tuple[str, float]] = []
        norm = 0.0
        for gram, frequency in counts.items():
            idf = self.idf.get(gram)
            if idf is None:
                continue
            weight = idf * self._saturate(frequency)
            query.append((gram, weight))
            norm += weight * weight
        norm = math.sqrt(norm)
        if not norm:
            return np.zeros(self.size)
        result = np.zeros(self.size)
        for gram, weight in query:
            idf = self.idf[gram]
            for index, frequency in self.postings[gram]:
                result[index] += weight * idf * self._saturate(frequency)
        result /= norm * np.maximum(self.norms, 1e-9)
        return result


class Search:
    def __init__(self, data: Snapshot, encoder, analyzer: Analyzer | None = None,
                 use_char: bool = True):
        self.data, self.encoder = data, encoder
        self.analyzer = analyzer or Analyzer()
        self.documents = tuple(Counter(row) for row in data.tokens)
        self.lengths = np.array([sum(row.values()) for row in self.documents], dtype=np.float64)
        self.average = max(float(self.lengths.mean()) if len(self.lengths) else 0, 1)
        self.df = Counter(token for row in self.documents for token in row)
        self.char = (CharNgramIndex([category.name for category in data.catalog.categories])
                     if use_char and data.catalog.categories else None)

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
        char = self.char.scores(text) if self.char else None
        ranks: dict[int, float] = {}
        sources: dict[int, list[str]] = {}
        order: dict[int, list[tuple[str, int]]] = {}
        channels = (("BM25", lexical), ("E5", dense), ("CHAR", char))
        for label, scores in channels:
            if scores is None:
                continue
            indices = np.argsort(-scores, kind="stable")[:50]
            for rank, index in enumerate(indices, 1):
                # Channels that can legitimately return zeros must not award ranks
                # for unrelated documents.
                if label in ("BM25", "CHAR") and scores[index] <= 0:
                    continue
                key = int(index)
                ranks[key] = ranks.get(key, 0) + 1 / (60 + rank)
                sources.setdefault(key, []).append(label)
                order.setdefault(key, []).append((label, rank))
        categories = self.data.catalog.categories

        def quality(index: int) -> tuple:
            """Tie-break for equal RRF sums.

            RRF sums collide constantly: every candidate that is rank 1 in exactly
            one channel scores 1/61, and ordering those by category id is arbitrary.
            Agreement between channels is the real signal, so it comes first, then
            the sum of the ranks inside each channel.
            """
            evidence = order.get(index, [])
            return (-len(evidence), sum(rank for _, rank in evidence), categories[index].id)

        candidates = []
        for rank, index in enumerate(sorted(ranks, key=lambda index: (-ranks[index], quality(index)))[:5], 1):
            evidence = order.get(index, [])
            candidates.append(CategoryCandidate(
                id=categories[index].id, name=categories[index].name, rank=rank,
                rank_score=ranks[index], reasons=sources[index]))
        return candidates
