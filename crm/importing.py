"""Bounded, preview-first catalogue import. Recipes are deliberately outside its schema."""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from defusedxml import ElementTree
from django.db import IntegrityError, transaction
from django.utils import timezone
from openpyxl import load_workbook

from .models import AuditEvent, CatalogHistory, ImportBatch, Product, StockMovement

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10_000
MAX_COLUMNS = 64
FIELDS = ("sku", "name", "price", "stock")
ALIASES = {
    "sku": ("sku", "артикул", "код", "ид", "id", "кодтовара"),
    "name": ("name", "название", "наименование", "товар", "наименованиетовара"),
    "price": ("price", "цена", "ценазаединицу", "розничнаяцена", "стоимость"),
    "stock": ("stock", "остаток", "остатки", "количество", "количествонаскладе"),
}


def _normalized(value):
    return re.sub(r"[\W_]+", "", str(value).casefold())


def _text(value):
    return "" if value is None else str(value).strip()


def _header(values):
    headers = [_text(value) for value in values]
    while headers and not headers[-1]:
        headers.pop()
    if not headers or len(headers) > MAX_COLUMNS:
        raise ValueError("Нужна строка заголовков (не более 64 столбцов).")
    if any(not value for value in headers):
        raise ValueError("У каждого столбца должно быть название.")
    if len(set(headers)) != len(headers):
        raise ValueError("Заголовки столбцов не должны повторяться.")
    return headers


def _tabular_rows(iterator):
    try:
        columns = _header(next(iterator))
    except StopIteration:
        raise ValueError("Файл пуст.")
    rows = []
    for values in iterator:
        values = list(values)
        if not any(_text(value) for value in values):
            continue
        if len(values) > len(columns) and any(_text(value) for value in values[len(columns):]):
            raise ValueError(f"В строке {len(rows) + 2} больше значений, чем заголовков.")
        if len(rows) >= MAX_ROWS:
            raise ValueError("Допустимо не более 10 000 строк.")
        rows.append({column: _text(values[index]) if index < len(values) else ""
                     for index, column in enumerate(columns)})
    if not rows:
        raise ValueError("После заголовков нет данных.")
    return columns, rows


def _read_csv(data):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1251")
        except UnicodeDecodeError as error:
            raise ValueError("Используйте CSV в UTF-8 или Windows-1251.") from error
    if "\x00" in text:
        raise ValueError("CSV содержит недопустимые нулевые байты.")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
        # A single-column SKU catalogue is still a valid CSV preview.
    try:
        return _tabular_rows(iter(csv.reader(io.StringIO(text), dialect)))
    except csv.Error as error:
        raise ValueError("Не удалось прочитать CSV. Проверьте разделитель и кавычки.") from error


def _read_xlsx(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 1000 or sum(info.file_size for info in infos) > 50 * 1024 * 1024:
                raise ValueError("Распакованный Excel превышает допустимый размер.")
            if any(info.flag_bits & 1 for info in infos):
                raise ValueError("Зашифрованный Excel не поддерживается.")
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            sheet = workbook.active
            if sheet.max_column and sheet.max_column > MAX_COLUMNS:
                raise ValueError("Допустимо не более 64 столбцов.")
            if sheet.max_row and sheet.max_row > MAX_ROWS + 1:
                raise ValueError("Допустимо не более 10 000 строк.")
            return _tabular_rows(iter(sheet.iter_rows(values_only=True)))
        finally:
            workbook.close()
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("Не удалось прочитать XLSX. Старый формат XLS не поддерживается.") from error


def _tag(element):
    return element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""


def _direct(element, names):
    for child in element:
        if _tag(child) in names:
            return _text(child.text)
    return ""


def _read_xml(data):
    try:
        root = ElementTree.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except Exception as error:
        raise ValueError("Некорректный или небезопасный XML; DTD и внешние сущности запрещены.") from error
    entries = {}
    seen_kinds = {}
    for element in root.iter():
        if _tag(element) not in {"Товар", "Предложение", "product", "item", "offer"}:
            continue
        external_id = _direct(element, {"Ид", "id", "sku", "Артикул"})
        sku = _direct(element, {"Артикул", "sku"})
        identity = external_id or sku
        if not identity:
            identity = f"__missing_{len(entries)}"
        row = {
            "Артикул": sku or external_id,
            "Наименование": _direct(element, {"Наименование", "name"}),
        }
        for child in element.iter():
            tag = _tag(child)
            if tag in {"ЦенаЗаЕдиницу", "price", "Цена"} and not len(child) and "Цена" not in row:
                row["Цена"] = _text(child.text)
            if tag in {"Количество", "stock"} and "Количество" not in row:
                row["Количество"] = _text(child.text)
        warehouses = [child for child in element if _tag(child) == "Склад"]
        if warehouses and "Количество" not in row:
            try:
                row["Количество"] = str(sum(Decimal(child.attrib.get("КоличествоНаСкладе", "0"))
                                           for child in warehouses))
            except InvalidOperation:
                row["Количество"] = "некорректное количество"
        previous = entries.get(identity)
        kinds = seen_kinds.setdefault(identity, set())
        if previous:
            # Catalogue and offers legitimately share an ID; two records of the
            # same kind are duplicates and must reach validation as duplicates.
            if _tag(element) in kinds:
                entries[f"__duplicate_{len(entries)}"] = (_tag(element), row)
            else:
                merged = previous[1]
                for key, value in row.items():
                    if value and (key != "Артикул" or not merged.get("Артикул")):
                        merged[key] = value
        else:
            entries[identity] = (_tag(element), row)
        kinds.add(_tag(element))
        if len(entries) > MAX_ROWS:
            raise ValueError("Допустимо не более 10 000 товаров.")
    if not entries:
        raise ValueError("В XML не найдены Товар или Предложение (CommerceML).")
    rows = [item[1] for item in entries.values()]
    columns = ["Артикул", "Наименование"]
    columns.extend(key for key in ("Цена", "Количество") if any(key in row for row in rows))
    return columns, rows


def _mapping(columns, provided=None):
    result = {}
    for field, aliases in ALIASES.items():
        for alias in aliases:
            matching = next((column for column in columns if _normalized(column) == alias), None)
            if matching is not None:
                result[field] = matching
                break
    if provided is not None:
        if isinstance(provided, str):
            try:
                provided = json.loads(provided)
            except (TypeError, ValueError) as error:
                raise ValueError("Сопоставление полей должно быть JSON-объектом.") from error
        if not isinstance(provided, dict) or set(provided) - set(FIELDS):
            raise ValueError("Сопоставление содержит неизвестные поля.")
        for field, column in provided.items():
            if column in (None, ""):
                result.pop(field, None)
            elif not isinstance(column, str) or column not in columns:
                raise ValueError(f"Не найден столбец для поля {field}.")
            else:
                result[field] = column
    return result


def _number(value, field):
    value = _text(value).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        number = Decimal(value)
        model_field = Product._meta.get_field(field)
        if not number.is_finite() or number < 0:
            raise ValueError
        quantum = Decimal(1).scaleb(-model_field.decimal_places)
        if number != number.quantize(quantum) or number >= Decimal(10) ** (model_field.max_digits - model_field.decimal_places):
            raise ValueError
        return number
    except (InvalidOperation, ValueError):
        raise ValueError("Нужно неотрицательное число допустимой точности.")


def _validate(rows, mapping):
    errors, cleaned, seen = [], [], set()
    if not mapping.get("sku"):
        return [], [{"row": 1, "field": "sku", "message": "Выберите столбец артикула."}]
    skus = [_text(row.get(mapping["sku"])) for row in rows]
    existing = set()
    for start in range(0, len(skus), 500):
        existing.update(Product.objects.filter(sku__in=skus[start:start + 500]).values_list("sku", flat=True))
    for index, row in enumerate(rows, 2):
        item = {field: _text(row.get(column)) for field, column in mapping.items()}
        sku = item.get("sku", "")
        if not sku:
            errors.append({"row": index, "field": "sku", "message": "Артикул обязателен."})
        elif sku in seen:
            errors.append({"row": index, "field": "sku", "message": f"Артикул {sku} повторяется в файле."})
        seen.add(sku)
        for field in ("sku", "name"):
            max_length = Product._meta.get_field(field).max_length
            if len(item.get(field, "")) > max_length:
                errors.append({"row": index, "field": field, "message": f"Не более {max_length} символов."})
        if not item.get("name"):
            if sku not in existing:
                errors.append({"row": index, "field": "name", "message": "Для нового товара нужно наименование."})
            item.pop("name", None)
        for field in ("price", "stock"):
            if field not in item or item[field] == "":
                item.pop(field, None)
                continue
            try:
                item[field] = _number(item[field], field)
            except ValueError as error:
                errors.append({"row": index, "field": field, "message": str(error)})
        cleaned.append(item)
    return cleaned, errors


def preview_import(upload, user, mapping=None):
    """Persist a bounded preview, including validation errors; never write products."""
    if getattr(upload, "size", 0) > MAX_BYTES:
        raise ValueError("Размер файла не должен превышать 5 МБ.")
    data = upload.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Размер файла не должен превышать 5 МБ.")
    filename = Path(upload.name).name[:200]
    extension = Path(filename).suffix.lower()
    readers = {".csv": _read_csv, ".xlsx": _read_xlsx, ".xml": _read_xml}
    if extension not in readers:
        raise ValueError("Поддерживаются CSV, XLSX и XML/CommerceML.")
    columns, rows = readers[extension](data)
    mapped = _mapping(columns, mapping)
    _, errors = _validate(rows, mapped)
    with transaction.atomic():
        batch = ImportBatch.objects.create(uploaded_by=user, filename=filename, rows=rows,
                                           columns=columns, mapping=mapped, errors=errors)
        AuditEvent.objects.create(actor=user, action="import_preview", entity="ImportBatch",
                                  entity_id=str(batch.pk), details={"filename": filename, "rows": len(rows), "errors": len(errors)})
    return batch


def apply_import(batch, user, mapping=None):
    """Apply once, all-or-nothing, updating only catalogue-owned fields."""
    try:
        with transaction.atomic():
            batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)
            if batch.status != "preview":
                raise ValueError("Этот импорт уже применён.")
            mapped = _mapping(batch.columns, mapping if mapping is not None else batch.mapping)
            cleaned, errors = _validate(batch.rows, mapped)
            if errors:
                first = errors[0]
                raise ValueError(f"Строка {first['row']}: {first['message']} Всего ошибок: {len(errors)}.")
            created = updated = 0
            for item in cleaned:
                product = Product.objects.select_for_update().filter(sku=item["sku"]).first()
                if product is None:
                    product = Product(sku=item["sku"], name=item["name"], price=Decimal("0"), stock=Decimal("0"))
                    old_price = old_stock = Decimal("0")
                    created += 1
                else:
                    old_price, old_stock = product.price, product.stock
                    updated += 1
                for field in ("name", "price", "stock"):
                    if field in item:
                        setattr(product, field, item[field])
                product.save()
                CatalogHistory.objects.create(product=product, batch=batch, old_price=old_price,
                                              new_price=product.price, old_stock=old_stock, new_stock=product.stock)
                delta = product.stock - old_stock
                if delta:
                    StockMovement.objects.create(product=product, quantity=delta, actor=user,
                                                 reason=f"Корректировка остатка по импорту №{batch.pk}")
            batch.status, batch.applied_at = "applied", timezone.now()
            batch.mapping, batch.errors = mapped, []
            batch.save(update_fields=["status", "applied_at", "mapping", "errors"])
            counts = {"created": created, "updated": updated, "total": len(cleaned)}
            AuditEvent.objects.create(actor=user, action="import_apply", entity="ImportBatch",
                                      entity_id=str(batch.pk), details=counts)
            return counts
    except IntegrityError as error:
        raise ValueError("Каталог изменился параллельно. Создайте новый предпросмотр и повторите импорт.") from error
