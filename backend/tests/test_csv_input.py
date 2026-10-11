"""Группа 3: разбор CSV не меняет смысл исходных данных.

Синтетические проверки разбора (без каталога и без сети). Реальные проверки через HTTP —
в `tests/batch_check.py` и `tests/csv_input_check.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.csv_input import CsvEncodingError, decode_bytes, split_rows  # noqa: E402


def rows_of(text: str, **kwargs) -> list[str]:
    return [row["description"] for row in split_rows(text.encode("utf-8"), **kwargs).rows]


def test_decimal_comma_is_not_a_delimiter():
    """«мощность 0,65 кВт» — одна колонка: запятая в числе не разделяет поля.

    Раньше файл резался по этой запятой и значение превращалось в «0; 65 кВт».
    """
    parsed = split_rows("Описание\nДрель мощность 0,65 кВт\n".encode("utf-8"))
    assert parsed.delimiter == ""
    assert [row["description"] for row in parsed.rows] == ["Дрель мощность 0,65 кВт"]
    assert any("одноколоночный" in note for note in parsed.notes)


def test_header_columns_keep_their_names():
    """Заголовок связывает колонки: «Дрель; Мощность: 650 Вт; Напряжение: 220 В»."""
    text = "Наименование;Мощность;Напряжение\nДрель;650 Вт;220 В\n"
    assert rows_of(text) == ["Дрель; Мощность: 650 Вт; Напряжение: 220 В"]


def test_first_column_stays_the_product():
    """Первая колонка остаётся товаром, а не превращается в пару «Наименование: …»."""
    text = "Наименование;Мощность\nДрель;650 Вт\n"
    assert rows_of(text)[0].startswith("Дрель; ")


def test_single_column_without_header():
    """Одноколоночный файл без заголовка читается построчно и целиком."""
    assert rows_of("Дрель мощность 0,65 кВт\nЛопата штыковая\n") == ["Дрель мощность 0,65 кВт", "Лопата штыковая"]


def test_quotes_and_newlines_inside_cells():
    """Кавычки и перенос строки внутри ячейки не разрывают товар."""
    text = 'Наименование;Описание\nДрель;"мощность 0,65 кВт;\nот сети"\n'
    parsed = split_rows(text.encode("utf-8"))
    assert parsed.delimiter == ";"
    assert len(parsed.rows) == 1
    assert "0,65 кВт" in parsed.rows[0]["description"] and "от сети" in parsed.rows[0]["description"]


def test_delimiter_can_be_chosen_explicitly():
    """Неоднозначный формат можно разобрать явно выбранным разделителем."""
    text = "Наименование,Мощность\nДрель,650 Вт\n"
    parsed = split_rows(text.encode("utf-8"), delimiter=",")
    assert parsed.delimiter == ","
    assert parsed.rows[0]["description"] == "Дрель; Мощность: 650 Вт" or \
        parsed.rows[0]["description"] == "Дрель; 650 Вт"


@pytest.mark.parametrize("encoding", ["utf-8", "cp1251", "utf-16"])
def test_encodings_are_read_without_garbage(encoding):
    """UTF-8, cp1251 и UTF-16 читаются без кракозябр."""
    text = "Дрель мощность 0,65 кВт\n"
    parsed = split_rows(text.encode(encoding))
    assert parsed.rows and "Дрель" in parsed.rows[0]["description"]
    assert "\ufffd" not in parsed.rows[0]["description"]


def test_broken_encoding_raises_a_structured_error():
    """Повреждённая кодировка — ошибка ввода с кодом, а не «примерно прочитанный текст»."""
    with pytest.raises(CsvEncodingError) as error:
        decode_bytes(b"\xff\xfe\x00\xd8\x00\x00\xff\xff\xff\xff")
    assert error.value.code == "csv_encoding"


def test_long_row_is_rejected_with_a_reason_not_truncated():
    """Длинное описание не обрезается молча: строка отклоняется с понятной причиной."""
    text = "Описание\n" + ("Д" * 13000) + "\n"
    parsed = split_rows(text.encode("utf-8"))
    assert parsed.rows == []
    assert parsed.skipped and "длиннее" in parsed.skipped[0]["reason"]


def test_row_limit_is_kept_and_reported():
    """Лимит строк сохраняется, лишние строки помечаются причиной."""
    text = "Описание\n" + "\n".join(f"Товар {index} описание" for index in range(5)) + "\n"
    parsed = split_rows(text.encode("utf-8"), max_rows=3)
    assert len(parsed.rows) == 3 and parsed.truncated
    assert any("лимит строк" in item["reason"] for item in parsed.skipped)
