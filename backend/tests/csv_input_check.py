"""Проверка разбора CSV без запуска API: python tests/csv_input_check.py

Стандартная библиотека, внешние зависимости не нужны.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.csv_input import MAX_DESCRIPTION, decode_bytes, sample_csv, split_rows  # noqa: E402

CASES = []


def case(title):
    def wrapper(function):
        CASES.append((title, function))
        return function
    return wrapper


def check(condition, message):
    if not condition:
        raise AssertionError(message)


@case("обычный UTF-8 с заголовком и разделителем ';'")
def _utf8_header():
    rows = split_rows(sample_csv().encode("utf-8"))
    check(rows.header_skipped, "заголовок не пропущен")
    check(rows.delimiter == ";", "разделитель %r" % rows.delimiter)
    check(len(rows.rows) == 4, "строк %d, ожидалось 4" % len(rows.rows))
    check(rows.rows[0]["description"].startswith("Халат детский"), rows.rows[0]["description"])
    check(rows.rows[1]["description"] == "Дрель ручная электрическая, от сети, мощность не менее 650 Вт",
          "кавычки разобраны неверно: %r" % rows.rows[1]["description"])


@case("BOM и cp1251 из Excel")
def _encodings():
    text = "Наименование\nКиянка деревянная\n"
    bom = split_rows(b"\xef\xbb\xbf" + text.encode("utf-8"))
    check(bom.encoding == "utf-8-sig", bom.encoding)
    check(bom.rows[0]["description"] == "Киянка деревянная", bom.rows[0]["description"])
    cp = split_rows(text.encode("cp1251"))
    check(cp.encoding == "cp1251", cp.encoding)
    check(cp.rows[0]["description"] == "Киянка деревянная", "cp1251 прочитан как %r" % cp.rows[0]["description"])
    check(split_rows("Киянка\n".encode("utf-16")).rows[0]["description"] == "Киянка", "utf-16 не разобран")


@case("запятая как разделитель и кавычки с запятой внутри")
def _commas_quotes():
    data = ('description,characteristics\n'
            '"Дрель ручная электрическая, от сети",мощность 650 Вт\n'
            'Киянка,"дерево, круглая"\n').encode("utf-8")
    rows = split_rows(data)
    check(rows.delimiter == ",", "разделитель %r" % rows.delimiter)
    check(len(rows.rows) == 2, "строк %d" % len(rows.rows))
    check(rows.rows[0]["description"] == "Дрель ручная электрическая, от сети; мощность 650 Вт",
          rows.rows[0]["description"])
    check(rows.rows[1]["description"] == "Киянка; дерево, круглая", rows.rows[1]["description"])


@case("несколько колонок склеиваются, лишние пробелы убираются")
def _multi_column():
    rows = split_rows("Товар;Характеристика;Значение\nЛопата;  Материал ;  сталь\n".encode("utf-8"))
    check(len(rows.rows) == 1, "строк %d" % len(rows.rows))
    check(rows.rows[0]["description"] == "Лопата; Материал; сталь", rows.rows[0]["description"])


@case("первая строка — товар, а не заголовок")
def _no_header():
    rows = split_rows("Лопата; штыковая\nЛопата; совковая\n".encode("utf-8"))
    check(not rows.header_skipped, "товарная строка принята за заголовок")
    check(len(rows.rows) == 2, "строк %d, ожидалось 2" % len(rows.rows))


@case("перевод строки внутри кавычек — одна строка файла")
def _embedded_newline():
    rows = split_rows('Наименование\n"Дрель\nручная"\n'.encode("utf-8"))
    check(len(rows.rows) == 1, "строк %d" % len(rows.rows))
    check(rows.rows[0]["description"] == "Дрель ручная", repr(rows.rows[0]["description"]))


@case("пустые строки и хвостовой разделитель пропускаются")
def _empty_lines():
    rows = split_rows("Наименование\n\nКиянка\n;\n\nЛопата\n".encode("utf-8"))
    check([row["description"] for row in rows.rows] == ["Киянка", "Лопата"],
          [row["description"] for row in rows.rows])


@case("длинная строка отклоняется с понятной причиной")
def _long_line():
    # Политика изменена по требованию ревью: молчаливая обрезка превращала описание в
    # другой товар. Строка отклоняется целиком и попадает в skipped с причиной.
    rows = split_rows(("Киянка " + "х" * (MAX_DESCRIPTION + 500) + "\n").encode("utf-8"))
    check(rows.rows == [], rows.rows[:1])
    check(any("длиннее" in item["reason"] for item in rows.skipped), rows.skipped)


@case("лимит строк соблюдается, лишние попадают в skipped")
def _row_limit():
    data = "".join("Товар %d\n" % index for index in range(1, 8)).encode("utf-8")
    rows = split_rows(data, max_rows=3)
    check(len(rows.rows) == 3, "строк %d" % len(rows.rows))
    check(rows.truncated, "флаг truncated не выставлен")
    check(len(rows.skipped) == 4, "в skipped %d" % len(rows.skipped))


@case("одиночная строка без заголовка")
def _single_line():
    rows = split_rows("Халат детский для мальчика\n".encode("cp1251"))
    check(len(rows.rows) == 1 and rows.rows[0]["description"] == "Халат детский для мальчика",
          rows.rows)


def main():
    failures = 0
    for title, function in CASES:
        try:
            function()
            print("OK   %s" % title)
        except AssertionError as error:
            failures += 1
            print("FAIL %s -> %s" % (title, error))
    print("\nвсего %d, провалено %d" % (len(CASES), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
