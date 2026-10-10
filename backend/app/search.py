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

ANALYZER_VERSION = "nfc-pymorphy3-v2"
MODEL_ID = "intfloat/multilingual-e5-small"


def canon(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().replace("ё", "е").split())


#: ЙЦУКЕН по физическим клавишам QWERTY: неверная раскладка — частая ошибка ввода.
#: Применяется только к тексту БЕЗ кириллицы: смешанный текст — не ошибка раскладки.
LAYOUT_LATIN_TO_CYRILLIC = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~",
    "йцукенгшщзхъфывапролджэячсмитьбю.ёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё",
)

#: Латиница, неотличимая на вид от кириллицы. Применяется ТОЛЬКО внутри токена,
#: где уже есть кириллица: так «дрeль» исправляется, а латинские названия, коды и
#: идентификаторы остаются нетронутыми.
HOMOGLYPH_LATIN_TO_CYRILLIC = str.maketrans({
    "a": "а", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м", "o": "о", "p": "р",
    "t": "т", "x": "х", "y": "у", "b": "в", "A": "А", "C": "С", "E": "Е", "H": "Н",
    "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У", "B": "В",
})

#: Токен с цифрой или разделителем — код, ГОСТ, артикул или идентификатор: не трогаем.
PROTECTED_TOKEN = re.compile(r"[\d._/@#№-]")


def _latin_only(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text)) and not re.search(r"[А-Яа-яЁё]", text)


def query_variants(text: str) -> tuple[str, ...]:
    """Исходный запрос и безопасные поисковые варианты: раскладка и смешение алфавитов.

    Исходный текст всегда первый и никогда не подменяется: варианты добавляются к
    нему, а не вместо него. Пользовательские характеристики и факты здесь не
    переписываются — это только поисковый запрос.

    Раскладка: если в тексте нет кириллицы, но есть латиница, добавляется вариант
    «как если бы набирали в русской раскладке». Идентификаторы, коды и токены с
    цифрами пропускаются.

    Смешение алфавитов: внутри токена, где уже есть кириллица, латинские
    двойники заменяются на кириллические. Латинские названия целиком не трогаются.
    """
    variants: list[str] = [text]
    if not text.strip():
        return tuple(variants)

    if _latin_only(text):
        mapped = []
        for token in text.split():
            if PROTECTED_TOKEN.search(token):
                mapped.append(token)
                continue
            converted = token.translate(LAYOUT_LATIN_TO_CYRILLIC)
            mapped.append(converted if re.search(r"[А-Яа-яЁё]", converted) else token)
        candidate = " ".join(mapped)
        if candidate != text and re.search(r"[А-Яа-яЁё]", candidate):
            variants.append(candidate)

    mapped = []
    changed = False
    for token in text.split():
        if PROTECTED_TOKEN.search(token):
            mapped.append(token)
            continue
        if re.search(r"[А-Яа-яЁё]", token) and re.search(r"[A-Za-z]", token):
            fixed = token.translate(HOMOGLYPH_LATIN_TO_CYRILLIC)
            mapped.append(fixed)
            changed = changed or fixed != token
        else:
            mapped.append(token)
    if changed:
        candidate = " ".join(mapped)
        if candidate not in variants:
            variants.append(candidate)

    return tuple(variants)


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
        # min_df=1, а не 2: на малом каталоге (или в узкой области) у названия, не
        # похожего на остальные, все n-граммы встречаются ровно один раз, и порог 2
        # удалял их все — документ становился невидимым и получал нулевую оценку даже
        # на точном совпадении. Замер на независимом наборе: min_df=1 и min_df=2 с
        # возвратом грамм невидимому документу дают одинаковый результат (те же 4
        # промаха из 46), но min_df=1 проще и оставляет редкой грамме максимальный
        # idf, то есть работает как точное совпадение подстроки.
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


def _rank_order(ranks: dict[int, float], order: dict[int, list[tuple[str, int]]],
                categories) -> list[int]:
    """Порядок кандидатов: убывание RRF-score, и только потом разрешение равенств.

    Раньше сортировка шла по одному `quality` (число каналов, сумма рангов, id), а
    сам RRF-score в ключ не входил. Из-за этого кандидат с большей оценкой объединения
    мог стоять ниже кандидата с меньшей: в независимом прогоне score
    0.038733454862487124 оказался четвёртым, а 0.038099747474747474 — первым.
    Оценка объединения и есть результат; `quality` разрешает только точные равенства,
    потому что суммы RRF постоянно совпадают (1/61 у любого одиночного лидера канала).

    RRF-score — не вероятность и не подтверждение типа товара.
    """
    def key(index: int) -> tuple:
        evidence = order.get(index, [])
        return (-ranks[index], -len(evidence), sum(rank for _, rank in evidence),
                categories[index].id)

    return sorted(ranks, key=key)


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

    def channels_for(self, text: str) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]:
        """Оценки трёх каналов для запроса и его безопасных вариантов.

        Варианты (неверная раскладка, смешение алфавитов) объединяются по максимуму
        на документ: исходный запрос всегда участвует, поэтому вариант может только
        добавить попадание, но не ухудшить его. Исходный текст при этом не
        подменяется — см. `query_variants`.
        """
        variants = query_variants(text)
        lexical = [self.lexical(variant) for variant in variants]
        char = [self.char.scores(variant) for variant in variants] if self.char else None
        dense = None
        if self.encoder is not None:
            encoded = self.encoder.encode(list(variants), "query: ")
            dense = [self.data.vectors @ row for row in encoded]
        pick = lambda rows: np.maximum.reduce(rows) if rows else None  # noqa: E731
        return pick(lexical), pick(dense) if dense is not None else None, pick(char) if char else None

    def find(self, text: str) -> list[CategoryCandidate]:
        lexical, dense, char = self.channels_for(text)
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
        ordered = _rank_order(ranks, order, categories)
        candidates = []
        for rank, index in enumerate(ordered[:5], 1):
            evidence = order.get(index, [])
            candidates.append(CategoryCandidate(
                id=categories[index].id, name=categories[index].name, rank=rank,
                rank_score=ranks[index], reasons=sources[index]))
        return candidates
