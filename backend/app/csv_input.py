"""Разбор CSV: каждая строка файла становится обычной строкой ввода.

Формат файла заранее неизвестен, поэтому разбор намеренно терпимый:

* кодировка — UTF-8 с BOM или без, UTF-16 с BOM, иначе cp1251 (так Excel на
  Windows сохраняет русские CSV);
* разделитель — ``;``, ``,`` или табуляция, определяется автоматически;
* необязательная строка заголовка (``Наименование``, ``Описание`` и т. п.)
  пропускается, а не превращается в товар;
* кавычки, разделители и переводы строк внутри кавычек обрабатываются штатным
  :mod:`csv`, поэтому «Дрель; 650 Вт» в кавычках остаётся одной строкой;
* ячейки одной строки склеиваются через ``"; "`` — дальше работает обычный
  разбор текста, который понимает пары «Свойство: значение».

Модуль зависит только от стандартной библиотеки.
"""
from __future__ import annotations

import base64
import binascii
import csv
import io
import re
from dataclasses import dataclass, field
from typing import Optional

#: столько же, сколько допускает AnalysisCreate.description
MAX_DESCRIPTION = 12000
#: защита от файла на сотни тысяч строк в одном запросе
MAX_ROWS = 200

#: подписи заголовков, которые не являются товаром
_HEADER_HINTS = {
    "наименование", "описание", "название", "товар", "строка", "текст", "продукт",
    "характеристики", "свойства", "категория", "код", "ктру", "№", "n",
    "description", "name", "text", "product", "title", "category", "code", "attributes",
}
#: «Наименование товара», «Описание», «Код КТРУ», «Свойства:» — но не «Лопата; штыковая»
_HEADER_PATTERN = re.compile(
    r"^(наименование|описание|название|товар|строка|текст|продукт|характеристик\w*|свойств\w*|"
    r"категори\w*|код|ктру|№|n|description|name|text|product|title|category|code|attributes)"
    r"(\s+(товара|товар|продукта|продукт|строки|характеристик\w*|свойств\w*|ктру))?$")
_DELIMITERS = (";", ",", "\t")


@dataclass
class CsvRows:
    """Результат разбора файла: готовые строки ввода и сведения о разборе."""

    rows: list = field(default_factory=list)          # [{"line": int, "description": str}]
    encoding: str = "utf-8"
    delimiter: str = ";"
    header_skipped: bool = False
    skipped: list = field(default_factory=list)       # [{"line": int, "reason": str}]
    notes: list = field(default_factory=list)         # предупреждения о разборе
    truncated: bool = False                           # строк было больше MAX_ROWS

    def to_dict(self) -> dict:
        return {"rows": self.rows, "encoding": self.encoding, "delimiter": self.delimiter,
                "header_skipped": self.header_skipped, "skipped": self.skipped, "notes": self.notes,
                "truncated": self.truncated, "rows_total": len(self.rows) + len(self.skipped)}


def decode_bytes(data: bytes) -> tuple:
    """Байты файла -> (текст, имя кодировки). Никогда не падает."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1251"), "cp1251"
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace"), "utf-8/lossy"


def decode_base64(value: str) -> bytes:
    """Строка base64 из запроса -> байты файла."""
    try:
        return base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError) as error:
        raise ValueError("csv_base64 не разбирается: %s" % error) from error


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(_DELIMITERS)).delimiter
    except csv.Error:
        counts = {delimiter: sample.count(delimiter) for delimiter in _DELIMITERS}
        best = max(counts, key=lambda key: counts[key])
        return best if counts[best] else ";"


def _is_header(cells: list) -> bool:
    """Строка похожа на заголовок, а не на товар.

    Сравнение точное или по шаблону «Наименование товара»: обычная товарная строка
    вида «Лопата; штыковая» заголовком не считается, иначе первая позиция файла
    молча пропадала бы.
    """
    filled = [re.sub(r"\s+", " ", (cell or "")).strip().strip(":;-–—").lower()
              for cell in cells if (cell or "").strip()]
    if not filled:
        return False
    return any(cell in _HEADER_HINTS or _HEADER_PATTERN.match(cell) for cell in filled)


def _join_cells(cells: list) -> str:
    """Ячейки одной строки -> одна строка ввода, как её вводит пользователь."""
    parts = []
    for cell in cells:
        text = re.sub(r"\s+", " ", (cell or "").replace("\u00a0", " ")).strip()
        if text:
            parts.append(text)
    return "; ".join(parts)


def split_rows(data: bytes, *, max_rows: int = MAX_ROWS, skip_header: bool = True) -> CsvRows:
    """Байты CSV -> строки ввода. Одна строка файла = одно описание товара."""
    text, encoding = decode_bytes(data)
    result = CsvRows(encoding=encoding)
    sample = "\n".join(line for line in text.splitlines()[:20] if line.strip())
    result.delimiter = _sniff_delimiter(sample) if sample else ";"

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=result.delimiter)
    first = True
    for cells in reader:
        line_number = reader.line_num
        if not any((cell or "").strip() for cell in cells):
            continue
        if first and skip_header and _is_header(cells):
            result.header_skipped = True
            first = False
            continue
        first = False
        description = _join_cells(cells)
        if not description:
            result.skipped.append({"line": line_number, "reason": "пустая строка"})
            continue
        if len(description) > MAX_DESCRIPTION:
            description = description[:MAX_DESCRIPTION]
            result.notes.append("строка %d обрезана до %d символов" % (line_number, MAX_DESCRIPTION))
        if len(result.rows) >= max_rows:
            result.truncated = True
            result.skipped.append({"line": line_number, "reason": "превышен лимит строк"})
            continue
        result.rows.append({"line": line_number, "description": description})

    if not result.rows and not result.skipped:
        result.notes.append("в файле не найдено ни одной строки с товаром")
    return result


def sample_csv() -> str:
    """Демонстрационный CSV со «сложными» товарами — для проверок и curl."""
    return (
        "описание\n"
        "Халат детский; Пол: для мальчика\n"
        "\"Дрель ручная электрическая, от сети, мощность не менее 650 Вт\"\n"
        "Пальто мужское 48 размера; Материал: шерсть\n"
        "Шпажки для шашлыка; Материал: Дерево; Упаковка: картонная\n"
    )
