"""Определение категории операции по словам из таблицы ``CategoryKeyword``.

Логика вынесена из импортёра Т-Банка, чтобы её могли использовать и импорт, и
команда переразметки уже загруженных операций.
"""

from __future__ import annotations

from decimal import Decimal

from .models import Category, CategoryKeyword

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


class KeywordMatcher:
    """Подбирает категорию по активным ключевым словам из БД."""

    def __init__(self) -> None:
        self._keywords_by_type: dict[str, list[CategoryKeyword]] | None = None

    def warmup(self) -> None:
        """Загрузить слова заранее (необязательно)."""
        self._ensure_keywords()

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

    def match(self, description: str, operation_type: str) -> Category | None:
        """Найти категорию по описанию для операций указанного типа."""
        self._ensure_keywords()
        text = description.lower()
        for keyword in self._keywords_by_type.get(operation_type, ()):
            if keyword.keyword in text:
                return keyword.category
        return None

    def classify(self, description: str, amount: Decimal) -> tuple[str, Category | None, bool]:
        """Вернуть (тип операции, категория, is_unknown) для суммы со знаком."""
        text = description.strip().lower()

        if text.startswith(EXTERNAL_PHONE_MARKER):
            return 'expense', self.match(text, 'expense'), False

        if any(marker in text for marker in OWN_TRANSFER_MARKERS):
            direction = 'expense' if amount < 0 else 'income'
            category = self.match(text, direction)
            return direction, category, category is None

        if text.startswith('оплата') or text.startswith('aia'):
            return 'expense', self.match(text, 'expense'), False

        if text.startswith(EXTERNAL_BANK_MARKER):
            return 'expense', self.match(text, 'expense'), False

        if text.startswith(INCOME_PREFIXES):
            return 'income', self.match(text, 'income'), False

        direction = 'expense' if amount < 0 else 'income'
        return direction, self.match(text, direction), False
