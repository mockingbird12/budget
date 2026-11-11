"""Импорт выписки Т-Банка (JSON от tbank-cash-flow-pdf-parser).

Ключевые принципы:
* импорт идемпотентен — повторный запуск не создаёт дублей;
* ``external_id`` детерминирован и учитывает порядковое вхождение, поэтому
  одинаковые операции из выписки (например, две поездки на метро в один день)
  не склеиваются;
* категория определяется по словам из таблицы ``CategoryKeyword`` (редактируется
  в админке), а не по файлу с правилами;
* переводы между своими счетами без категорийного комментария помечаются как
  ``is_unknown``: они влияют на баланс, но исключаются из отчётов;
* по умолчанию команда работает в режиме предпросмотра и ничего не пишет в БД.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from django.db import transaction as db_transaction

from finance.models import Account, Category, CategoryKeyword, Transaction
from finance.services import apply_transaction

MSK = ZoneInfo('Europe/Moscow')

CURRENCY_SYMBOLS = {
    '₽': 'RUB',
    'руб': 'RUB',
    'руб.': 'RUB',
    'rub': 'RUB',
    '$': 'USD',
    '€': 'EUR',
    '₸': 'KZT',
}

# Переводы между своими счетами: без категорийного комментария считаем
# операцию неизвестной.
OWN_TRANSFER_MARKERS = (
    'перевод себе',
    'внутренний перевод',
    'перевод с договора',
    'внесение наличных',
    'снятие наличных',
    'вывод средств',
)
EXTERNAL_PHONE_MARKER = 'внешний перевод по номеру'
EXTERNAL_BANK_MARKER = 'внешний банковский перевод'
INCOME_PREFIXES = ('кэшбэк', 'cashback', 'пополнение', 'возврат покупки', 'выплата за')

AMOUNT_RE = re.compile(r'^([+-]?)\s*([\d\s\u00a0]+(?:[.,]\d+)?)\s*(.*)$')
DATE_FORMATS = ('%d.%m.%Y %H:%M', '%d.%m.%Y')


class TbankImportError(Exception):
    """Ошибка разбора или импорта выписки."""


@dataclass
class Row:
    occurred_at: datetime
    amount: Decimal
    currency: str
    description: str
    card: str
    operation_type: str
    category: Category | None
    is_unknown: bool
    external_id: str


@dataclass
class ImportReport:
    total_rows: int = 0
    to_import: int = 0
    imported: int = 0
    skipped_existing: int = 0
    account_exists: bool = False
    account_created: bool = False
    counts: Counter = field(default_factory=Counter)
    amounts: Counter = field(default_factory=Counter)
    unknown_counts: Counter = field(default_factory=Counter)
    unknown_amounts: Counter = field(default_factory=Counter)
    by_category: dict[tuple[str, str], list[Any]] = field(default_factory=dict)
    uncategorized: dict[str, list[Any]] = field(default_factory=dict)
    unknown: dict[str, list[Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.imported or self.account_created)


def parse_amount(raw: str) -> tuple[Decimal, str]:
    """Разобрать сумму вида ``-2 660.00 ₽`` в (Decimal, код валюты)."""
    if not isinstance(raw, str) or not raw.strip():
        raise TbankImportError(f'Пустая сумма: {raw!r}')

    text = raw.replace('\u00a0', ' ').strip()
    match = AMOUNT_RE.match(text)
    if not match:
        raise TbankImportError(f'Не удалось разобрать сумму: {raw!r}')

    sign = -1 if match.group(1) == '-' else 1
    number = match.group(2).replace(' ', '').replace(',', '.')
    try:
        value = Decimal(number) * sign
    except InvalidOperation as exc:
        raise TbankImportError(f'Некорректное число: {raw!r}') from exc

    symbol = match.group(3).strip()
    if not symbol:
        currency = 'RUB'
    elif symbol in CURRENCY_SYMBOLS:
        currency = CURRENCY_SYMBOLS[symbol]
    elif re.fullmatch(r'[A-Za-z]{3}', symbol):
        currency = symbol.upper()
    else:
        currency = 'RUB'
    return value, currency


def parse_occurred_at(raw: str) -> datetime:
    """Разобрать дату операции. Если времени нет — ставим полдень по Москве.

    Полдень выбран специально: он не «переезжает» в соседний день/месяц при
    группировке в UTC, даже если настройка TIME_ZONE остаётся UTC.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise TbankImportError(f'Пустая дата операции: {raw!r}')

    text = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == '%d.%m.%Y':
            parsed = parsed.replace(hour=12, minute=0)
        return parsed.replace(tzinfo=MSK)
    raise TbankImportError(f'Не удалось разобрать дату операции: {raw!r}')


class TbankImporter:
    def __init__(
        self,
        *,
        account_name: str = 'Т-Банк',
        source: str = 'tbank',
        dry_run: bool = True,
    ) -> None:
        if not account_name.strip():
            raise TbankImportError('Не задано название счёта')
        self.account_name = account_name.strip()
        self.source = source
        self.dry_run = dry_run
        self._keywords_by_type: dict[str, list[CategoryKeyword]] | None = None

    # --------------------------------------------------------------- категории
    def _ensure_keywords(self) -> None:
        if self._keywords_by_type is not None:
            return

        grouped: dict[str, list[CategoryKeyword]] = {}
        query = (
            CategoryKeyword.objects
            .filter(is_active=True)
            .select_related('category')
        )
        for keyword in query:
            grouped.setdefault(keyword.category.operation_type, []).append(keyword)

        # Приоритет: явный priority, затем более длинное слово, затем порядок в БД.
        for items in grouped.values():
            items.sort(key=lambda item: (-item.priority, -len(item.keyword), item.id))

        self._keywords_by_type = grouped

    def _match_category(self, description: str, operation_type: str) -> Category | None:
        self._ensure_keywords()
        text = description.lower()
        for keyword in self._keywords_by_type.get(operation_type, ()):
            if keyword.keyword in text:
                return keyword.category
        return None

    def classify(self, description: str, amount: Decimal) -> tuple[str, Category | None, bool]:
        """Вернуть (тип операции, категория, is_unknown)."""
        text = description.strip().lower()

        if text.startswith(EXTERNAL_PHONE_MARKER):
            return 'expense', self._match_category(text, 'expense'), False

        if any(marker in text for marker in OWN_TRANSFER_MARKERS):
            direction = 'expense' if amount < 0 else 'income'
            category = self._match_category(text, direction)
            return direction, category, category is None

        if text.startswith('оплата') or text.startswith('aia'):
            return 'expense', self._match_category(text, 'expense'), False

        if text.startswith(EXTERNAL_BANK_MARKER):
            return 'expense', self._match_category(text, 'expense'), False

        if text.startswith(INCOME_PREFIXES):
            return 'income', self._match_category(text, 'income'), False

        direction = 'expense' if amount < 0 else 'income'
        return direction, self._match_category(text, direction), False

    # ------------------------------------------------------------------- разбор
    def read_rows(self, path: str | Path) -> list[Row]:
        try:
            payload = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise TbankImportError(f'Не удалось прочитать файл {path}: {exc}') from exc

        if not isinstance(payload, list):
            raise TbankImportError('Ожидался JSON-массив операций')

        rows: list[Row] = []
        occurrences: Counter[str] = Counter()
        for index, item in enumerate(payload, start=1):
            if not isinstance(item, dict):
                raise TbankImportError(f'Строка {index}: ожидался объект')

            try:
                description = str(item.get('Описание операции', '')).strip()
                card = str(item.get('Номер карты', '') or '').strip()
                amount, currency = parse_amount(item['Сумма в валюте операции'])
                occurred_at = parse_occurred_at(item['Дата и время операции'])
            except KeyError as exc:
                raise TbankImportError(f'Строка {index}: нет поля {exc}') from exc
            except TbankImportError as exc:
                raise TbankImportError(f'Строка {index}: {exc}') from exc

            if amount == 0:
                continue

            operation_type, category, is_unknown = self.classify(description, amount)
            absolute = abs(amount)
            sign = '+' if amount > 0 else '-'
            canonical = '|'.join([
                occurred_at.date().isoformat(),
                sign,
                str(absolute),
                currency,
                description,
                card,
            ])
            occurrence = occurrences[canonical]
            occurrences[canonical] += 1
            digest = hashlib.sha1(canonical.encode('utf-8')).hexdigest()[:24]
            external_id = f'tbank-{digest}-{occurrence}'

            rows.append(Row(
                occurred_at=occurred_at,
                amount=absolute,
                currency=currency,
                description=description,
                card=card,
                operation_type=operation_type,
                category=category,
                is_unknown=is_unknown,
                external_id=external_id,
            ))
        return rows

    # ------------------------------------------------------------------ запись
    def _get_account(self, report: ImportReport) -> Account | None:
        account = Account.objects.filter(name=self.account_name).first()
        report.account_exists = account is not None
        if account is not None or self.dry_run:
            return account

        account = Account.objects.create(
            name=self.account_name,
            type=Account.Type.CARD,
            currency='RUB',
        )
        report.account_created = True
        return account

    def run(self, path: str | Path) -> ImportReport:
        self._ensure_keywords()
        rows = self.read_rows(path)
        report = ImportReport(total_rows=len(rows))

        existing_ids = set(
            Transaction.objects
            .filter(source=self.source, external_id__in=[r.external_id for r in rows])
            .values_list('external_id', flat=True)
        )
        report.skipped_existing = sum(1 for r in rows if r.external_id in existing_ids)

        with db_transaction.atomic():
            account = self._get_account(report)

            for row in rows:
                if row.external_id in existing_ids:
                    continue

                report.to_import += 1
                report.counts[row.operation_type] += 1
                report.amounts[row.operation_type] += row.amount
                if row.is_unknown:
                    report.unknown_counts[row.operation_type] += 1
                    report.unknown_amounts[row.operation_type] += row.amount

                if row.is_unknown:
                    bucket = report.unknown
                elif row.category is None and row.operation_type == 'expense':
                    bucket = report.uncategorized
                else:
                    bucket = None

                if bucket is not None:
                    entry = bucket.setdefault(row.description or '—', [0, Decimal('0')])
                    entry[0] += 1
                    entry[1] += row.amount

                if row.is_unknown:
                    category_label = 'Неизвестные'
                elif row.category is not None:
                    category_label = row.category.name
                else:
                    category_label = 'Без категории'
                cat_entry = report.by_category.setdefault(
                    (row.operation_type, category_label), [0, Decimal('0')],
                )
                cat_entry[0] += 1
                cat_entry[1] += row.amount

                if self.dry_run:
                    report.imported += 1
                    continue

                if account is None:
                    raise TbankImportError('Не удалось создать счёт для импорта')

                tx = Transaction(
                    account=account,
                    operation_type=row.operation_type,
                    category=row.category,
                    amount=row.amount,
                    currency=row.currency,
                    occurred_at=row.occurred_at,
                    description=row.description,
                    external_id=row.external_id,
                    source=self.source,
                    is_unknown=row.is_unknown,
                )
                apply_transaction(tx)
                report.imported += 1

        return report
