"""Bounded, deterministic book-list parsing; no model or network access."""
from __future__ import annotations

import csv
import io
import json
from pathlib import PurePath

from .store import ClubError, checked_text


MAX_BOOKS = 100
MAX_BYTES = 64 * 1024
_FIELDS = {"title": "title", "название": "title", "author": "author", "автор": "author",
           "materials": "materials", "материалы": "materials"}


def book_identity(book):
    return tuple(" ".join(book[key].split()).casefold() for key in ("title", "author"))


def _checked_record(record, label):
    if not isinstance(record, dict) or not {"title", "author"} <= record.keys():
        raise ClubError(f"{label}: нужны поля title (название) и author (автор).")
    if set(record) - {"title", "author", "materials"}:
        raise ClubError(f"{label}: допустимы только title, author и materials.")
    result = {}
    for key, title, limit, required in (("title", "Название", 180, True),
                                        ("author", "Автор", 180, True),
                                        ("materials", "Материалы", 4000, False)):
        value = record.get(key, "")
        if not isinstance(value, str):
            raise ClubError(f"{label}, {title}: нужен текст.")
        try:
            result[key] = checked_text(value, title, limit, required)
        except ClubError as exc:
            raise ClubError(f"{label}, {exc}") from exc
    return result


def validate_books(books):
    if not isinstance(books, (list, tuple)) or not 1 <= len(books) <= MAX_BOOKS:
        raise ClubError(f"В списке должно быть от 1 до {MAX_BOOKS} книг.")
    return [_checked_record(book, f"Книга {index}") for index, book in enumerate(books, 1)]


def _csv_reader(text, delimiter):
    return csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)


def _csv_columns(header):
    columns = [_FIELDS.get(value.strip().casefold()) for value in header]
    if (None in columns or len(set(columns)) != len(columns)
            or not {"title", "author"} <= set(columns)):
        return None
    return columns


def _csv_dialect(text):
    for delimiter in (",", ";", "\t"):
        try:
            header = next(_csv_reader(text, delimiter))
        except (StopIteration, csv.Error):
            continue
        columns = _csv_columns(header)
        if columns:
            return delimiter, columns
    return None


def _parse_csv(text, dialect):
    if dialect is None:
        raise ClubError("CSV: первая строка должна содержать title,author[,materials] "
                        "или название,автор[,материалы]; разделитель — запятая, точка с запятой или табуляция.")
    delimiter, columns = dialect
    reader = _csv_reader(text, delimiter)
    next(reader)
    books = []
    try:
        for row in reader:
            if not row or all(not value.strip() for value in row):
                continue
            if len(row) != len(columns):
                raise ClubError(f"Строка {reader.line_num}: число столбцов не совпадает с заголовком CSV.")
            books.append(_checked_record(dict(zip(columns, row)), f"Строка {reader.line_num}"))
            if len(books) > MAX_BOOKS:
                raise ClubError(f"За один раз можно добавить не более {MAX_BOOKS} книг.")
    except csv.Error as exc:
        raise ClubError(f"Строка {reader.line_num}: некорректные кавычки или структура CSV.") from exc
    return validate_books(books)


def parse_book_list(text, filename=None):
    """Read UTF-8 TXT (title | author [| materials]), JSON objects or headed CSV."""
    if isinstance(text, bytes):
        if len(text) > MAX_BYTES:
            raise ClubError("Файл списка книг должен быть не больше 64 КиБ.")
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ClubError("Сохраните список книг в кодировке UTF-8.") from exc
    elif not isinstance(text, str):
        raise ClubError("Нужен текстовый список книг.")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ClubError("Сохраните список книг в кодировке UTF-8.") from exc
    if size > MAX_BYTES:
        raise ClubError("Файл списка книг должен быть не больше 64 КиБ.")
    text = text.lstrip("\ufeff")
    suffix = PurePath(filename or "").suffix.casefold()
    if suffix and suffix not in {".txt", ".md", ".csv", ".json", ".tsv"}:
        raise ClubError("Загрузите список в формате TXT, CSV, TSV или JSON.")
    if suffix == ".json" or (not suffix and text.lstrip().startswith(("[", "{"))):
        try:
            books = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ClubError(f"Строка {exc.lineno}: некорректный JSON.") from exc
        return validate_books(books)
    dialect = _csv_dialect(text)
    if suffix in {".csv", ".tsv"} or (not suffix and dialect):
        return _parse_csv(text, dialect)
    books = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) not in (2, 3):
            raise ClubError(f"Строка {number}: используйте «Название | Автор» "
                            "или «Название | Автор | Материалы».")
        books.append(_checked_record(dict(zip(("title", "author", "materials"), parts)), f"Строка {number}"))
        if len(books) > MAX_BOOKS:
            raise ClubError(f"За один раз можно добавить не более {MAX_BOOKS} книг.")
    return validate_books(books)
