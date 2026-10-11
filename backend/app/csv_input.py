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
    "характеристики", "характеристика", "значение", "свойства", "категория", "код", "ктру", "№", "n",
    "description", "name", "text", "product", "title", "category", "code", "attributes",
    "characteristics", "characteristic", "value", "parameter", "params",
}
#: «Наименование товара», «Описание», «Код КТРУ», «Свойства:» — но не «Лопата; штыковая»
_HEADER_PATTERN = re.compile(
    r"^(наименование|описание|название|товар|строка|текст|продукт|характеристик\w*|свойств\w*|"
    r"значени\w*|категори\w*|код|ктру|№|n|description|name|text|product|title|category|code|"
    r"attributes|characteristic\w*|value|param\w*)"
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


class CsvEncodingError(ValueError):
    """Кодировку файла определить нельзя: это ошибка ввода (422), а не сбой сервера."""

    code = "csv_encoding"


def decode_bytes(data: bytes) -> tuple:
    """Байты файла -> (текст, имя кодировки).

    Повреждённая кодировка — это ошибка ввода, а не «примерно прочитанный текст»: раньше
    файл молча превращался в кракозябры и уходил в подбор. Теперь поднимается
    `CsvEncodingError`, и HTTP-слой отвечает 422 с понятной причиной.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError as error:
            raise CsvEncodingError("UTF-16 с меткой не читается: %s" % error) from error
    for candidate in ("utf-8", "cp1251"):
        try:
            decoded = data.decode(candidate)
        except UnicodeDecodeError:
            continue
        if "\ufffd" in decoded:
            continue
        return decoded, candidate
    raise CsvEncodingError("кодировка не определяется: сохраните файл в UTF-8 или cp1251")


def decode_base64(value: str) -> bytes:
    """Строка base64 из запроса -> байты файла."""
    try:
        return base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError) as error:
        raise ValueError("csv_base64 не разбирается: %s" % error) from error


def _sniff_delimiter(text: str) -> tuple:
    """Разделитель по СТРУКТУРЕ разобранных строк -> (разделитель, замечание или None).

    Кандидат проверяется настоящим разбором CSV: так учитываются кавычки и переносы внутри
    ячеек. Годится тот разделитель, при котором большинство строк даёт одинаковое число
    колонок (не меньше двух). Одинокая запятая в числе («мощность 0,65 кВт») колонок не
    создаёт: разделитель между цифрами не считается вовсе, поэтому «0,65» не превращается
    в «0; 65». Если ни один разделитель не даёт устойчивой таблицы — файл одноколоночный.
    """
    candidates = {";": text, "\t": text, ",": re.sub(r"(?<=\d),(?=\d)", "", text)}
    scored: list[tuple[float, int, str]] = []
    for delimiter, sample in candidates.items():
        counts = [len(cells) for cells in
                  csv.reader(io.StringIO(sample, newline=""), delimiter=delimiter)
                  if any((cell or "").strip() for cell in cells)]
        multi = [count for count in counts if count > 1]
        if not multi:
            continue
        common = max(set(multi), key=multi.count)
        # Согласованность считаем по строкам, где колонок больше одной: одноколоночный
        # заголовок («описание») не должен мешать распознать таблицу с данными.
        consistent = multi.count(common) / max(len(multi), 1)
        if consistent >= 0.6:
            scored.append((consistent, common, delimiter))
    if not scored:
        return "", "разделитель не найден: файл разобран как одноколоночный"
    scored.sort(reverse=True)
    delimiter = scored[0][2]
    ambiguous = len(scored) > 1 and scored[1][0] == scored[0][0] and scored[1][1] == scored[0][1]
    note = ("разделитель определён неуверенно: подходит и «%s», и «%s»; передайте csv_delimiter явно"
            % (scored[0][2], scored[1][2])) if ambiguous else None
    return delimiter, note


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


def _pair_names(cells: list) -> list:
    """Имена характеристик из строки заголовка: первая колонка — товар, дальше свойства.

    Связь колонок выдаётся только если ВСЕ подписи (кроме первой) годятся в имя свойства:
    короткие (до 60 символов и не больше четырёх слов) и не служебные вроде «Наименование»
    или «Значение». Иначе имена колонок неизвестны, и строка склеивается как раньше: иначе
    «Товар; Характеристика; Значение» подписывало бы значение чужим именем.
    """
    names = []
    for cell in cells[1:]:
        text = re.sub(r"\s+", " ", (cell or "")).strip().strip(":;-–—")
        words = text.split()
        usable = 0 < len(text) <= 60 and 0 < len(words) <= 4 and \
            not (text.lower() in _HEADER_HINTS or _HEADER_PATTERN.match(text.lower()))
        if not usable:
            return []          # подписи колонок неизвестны: связь не выдаём вовсе
        names.append(text)
    return names


def _join_cells(cells: list, pairs: list | None = None) -> str:
    """Ячейки одной строки -> одна строка ввода, как её вводит пользователь.

    Если у файла была строка заголовка с подписями, связь колонок сохраняется:
    «Дрель; Мощность: 650 Вт; Напряжение: 220 В». Без заголовка ячейки просто
    склеиваются, и дальше работает обычный разбор пар.
    """
    parts = []
    for index, cell in enumerate(cells):
        text = re.sub(r"\s+", " ", (cell or "").replace("\u00a0", " ")).strip()
        if not text:
            continue
        name = pairs[index - 1] if pairs and 0 < index <= len(pairs) else ""
        parts.append(f"{name}: {text}" if name else text)
    return "; ".join(parts)


def split_rows(data: bytes, *, max_rows: int = MAX_ROWS, skip_header: bool = True,
               delimiter: str | None = None) -> CsvRows:
    """Байты CSV -> строки ввода. Одна строка файла = одно описание товара.

    `delimiter` — явный выбор разделителя пользователем: когда формат неоднозначен, лучше
    спросить, чем угадать и испортить значения.
    """
    text, encoding = decode_bytes(data)
    result = CsvRows(encoding=encoding)
    if delimiter:
        result.delimiter = delimiter
    else:
        result.delimiter, note = _sniff_delimiter(text)
        if note:
            result.notes.append(note)
    if not result.delimiter:
        # Одноколоночный файл: строки берутся целиком, разделитель не применяется вовсе —
        # так десятичная запятая и любые другие символы внутри описания сохраняются как есть.
        # Разбор идёт через csv.reader с недостижимым разделителем: кавычки и переносы строк
        # внутри ячеек соблюдаются так же, как в таблице.
        first = True
        single = list(csv.reader(io.StringIO(text, newline=""), delimiter="\x1f"))
        for line_number, cells in enumerate(single, 1):
            raw = cells[0] if cells else ""
            if not raw.strip() or not re.search(r"[\wА-Яа-яЁё]", raw):
                continue                 # пустые строки и строки из одних разделителей
            if first and skip_header and _is_header([raw]):
                result.header_skipped = True
                first = False
                continue
            first = False
            description = re.sub(r"\s+", " ", raw.replace("\u00a0", " ")).strip()
            if len(description) > MAX_DESCRIPTION:
                result.skipped.append({"line": line_number,
                                       "reason": f"описание длиннее {MAX_DESCRIPTION} символов"})
                continue
            if len(result.rows) >= max_rows:
                result.truncated = True
                result.skipped.append({"line": line_number, "reason": "превышен лимит строк"})
                continue
            result.rows.append({"line": line_number, "description": description})
        if not result.rows and not result.skipped:
            result.notes.append("в файле не найдено ни одной строки с товаром")
        return result

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=result.delimiter)
    pairs: list | None = None
    first = True
    for cells in reader:
        line_number = reader.line_num
        if not any((cell or "").strip() for cell in cells):
            continue
        if first and skip_header and _is_header(cells):
            result.header_skipped = True
            first = False
            pairs = _pair_names(cells) if len(cells) > 1 else None
            continue
        first = False
        description = _join_cells(cells, pairs)
        if not description:
            result.skipped.append({"line": line_number, "reason": "пустая строка"})
            continue
        if len(description) > MAX_DESCRIPTION:
            # Молча обрезать нельзя: урезанное описание описывает уже другой товар.
            result.skipped.append({"line": line_number,
                                   "reason": f"описание длиннее {MAX_DESCRIPTION} символов"})
            continue
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
