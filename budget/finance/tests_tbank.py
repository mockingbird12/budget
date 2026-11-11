import json
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase

from finance.importers import TbankImporter, parse_amount, parse_occurred_at
from finance.models import Account, Category, CategoryKeyword, Transaction

MSK = ZoneInfo('Europe/Moscow')


def _row(description, amount, date='01.10.2026', card='6677'):
    return {
        'Дата и время операции': date,
        'Дата списания': date,
        'Сумма в валюте операции': amount,
        'Сумма операции в валюте карты': amount,
        'Описание операции': description,
        'Номер карты': card,
    }


class ParseAmountTests(SimpleTestCase):
    def test_plain(self):
        self.assertEqual(parse_amount('-220.00 ₽'), (Decimal('-220.00'), 'RUB'))

    def test_thousands_with_spaces(self):
        self.assertEqual(parse_amount('+1 234 567.89 ₽'), (Decimal('1234567.89'), 'RUB'))

    def test_nbsp_and_comma(self):
        self.assertEqual(parse_amount('-1\u00a0234,50 ₽'), (Decimal('-1234.50'), 'RUB'))

    def test_other_currency(self):
        self.assertEqual(parse_amount('10.00 $'), (Decimal('10.00'), 'USD'))


class ParseDateTests(SimpleTestCase):
    def test_date_only_becomes_noon_moscow(self):
        value = parse_occurred_at('21.10.2026')
        self.assertEqual(value, datetime(2026, 10, 21, 12, 0, tzinfo=MSK))

    def test_date_and_time(self):
        value = parse_occurred_at('21.10.2026 08:15')
        self.assertEqual(value, datetime(2026, 10, 21, 8, 15, tzinfo=MSK))


class TbankImportTests(TestCase):
    def setUp(self):
        other = Category.objects.create(name='Прочее', operation_type='expense')
        groceries = Category.objects.create(name='Продукты, еда', operation_type='expense')
        cashback = Category.objects.create(name='Кэшбэк', operation_type='income')
        topup = Category.objects.create(name='Пополнение', operation_type='income')

        CategoryKeyword.objects.create(category=groceries, keyword='pyaterochka')
        CategoryKeyword.objects.create(category=other, keyword='внешний перевод по номеру')
        CategoryKeyword.objects.create(category=cashback, keyword='кэшбэк')
        CategoryKeyword.objects.create(category=topup, keyword='пополнение')

        payload = [
            _row('Оплата в PYATEROCHKA 123 MOSCOW RUS', '-100.00 ₽'),
            _row('Оплата в PYATEROCHKA 123 MOSCOW RUS', '-100.00 ₽'),
            _row('Перевод себе', '+500.00 ₽'),
            _row('Внутренний перевод на договор 123', '-300.00 ₽'),
            _row('Внешний перевод по номеру телефона +79990000000', '-200.00 ₽'),
            _row('Кэшбэк за покупки', '+10.00 ₽'),
            _row('Пополнение через СБП', '+1 000.00 ₽'),
        ]
        handle = tempfile.NamedTemporaryFile(
            'w', suffix='.json', delete=False, encoding='utf-8',
        )
        json.dump(payload, handle, ensure_ascii=False)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        self.path = handle.name

    def _importer(self, **kwargs):
        return TbankImporter(account_name='Т-Банк', **kwargs)

    def test_dry_run_writes_nothing(self):
        report = self._importer(dry_run=True).run(self.path)

        self.assertEqual(report.imported, 7)
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertFalse(Account.objects.filter(name='Т-Банк').exists())
        self.assertFalse(report.account_exists)

    def test_commit_creates_operations_and_balance(self):
        report = self._importer(dry_run=False).run(self.path)

        self.assertEqual(report.imported, 7)
        self.assertEqual(Transaction.objects.count(), 7)
        account = Account.objects.get(name='Т-Банк')
        # -100 -100 +500 -300 -200 +10 +1000
        self.assertEqual(account.balance, Decimal('810.00'))
        self.assertTrue(report.account_created)

    def test_duplicates_get_distinct_external_ids(self):
        self._importer(dry_run=False).run(self.path)

        groceries = Transaction.objects.filter(description__startswith='Оплата в PYATEROCHKA')
        self.assertEqual(groceries.count(), 2)
        self.assertEqual(len(set(groceries.values_list('external_id', flat=True))), 2)

    def test_own_transfers_without_comment_are_unknown(self):
        self._importer(dry_run=False).run(self.path)

        self.assertTrue(Transaction.objects.get(description='Перевод себе').is_unknown)
        self.assertTrue(Transaction.objects.get(description='Внутренний перевод на договор 123').is_unknown)

    def test_external_phone_transfer_is_expense(self):
        self._importer(dry_run=False).run(self.path)

        tx = Transaction.objects.get(description__startswith='Внешний перевод по номеру')
        self.assertEqual(tx.operation_type, 'expense')
        self.assertFalse(tx.is_unknown)
        self.assertEqual(tx.category.name, 'Прочее')

    def test_merchant_is_categorized(self):
        self._importer(dry_run=False).run(self.path)

        tx = Transaction.objects.filter(description__startswith='Оплата в PYATEROCHKA').first()
        self.assertEqual(tx.category.name, 'Продукты, еда')
        self.assertFalse(tx.is_unknown)

    def test_repeated_import_is_idempotent(self):
        self._importer(dry_run=False).run(self.path)
        report = self._importer(dry_run=False).run(self.path)

        self.assertEqual(report.skipped_existing, 7)
        self.assertEqual(report.imported, 0)
        self.assertEqual(Transaction.objects.count(), 7)
        self.assertEqual(Account.objects.get(name='Т-Банк').balance, Decimal('810.00'))

    def test_uncategorized_expense_has_no_category(self):
        payload = [_row('Оплата в НЕИЗВЕСТНЫЙ МАГАЗИН', '-50.00 ₽')]
        handle = tempfile.NamedTemporaryFile(
            'w', suffix='.json', delete=False, encoding='utf-8',
        )
        json.dump(payload, handle, ensure_ascii=False)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)

        self._importer(dry_run=False).run(handle.name)

        tx = Transaction.objects.get(description='Оплата в НЕИЗВЕСТНЫЙ МАГАЗИН')
        self.assertIsNone(tx.category)
        self.assertFalse(tx.is_unknown)


class KeywordMatchingTests(TestCase):
    """Категория определяется словами из БД, а не файлом правил."""

    def setUp(self):
        self.common = Category.objects.create(name='Общее', operation_type='expense')
        self.special = Category.objects.create(name='Специальное', operation_type='expense')
        self.income = Category.objects.create(name='Доход', operation_type='income')

    def _importer(self):
        return TbankImporter(dry_run=True)

    def test_matches_case_insensitive_substring(self):
        CategoryKeyword.objects.create(category=self.common, keyword='ПЯТЕРОЧКА')

        category, is_unknown = self._classify('Оплата в Пятерочка 123', '-100.00 ₽')

        self.assertEqual(category, self.common)
        self.assertFalse(is_unknown)

    def test_longer_keyword_wins_at_equal_priority(self):
        CategoryKeyword.objects.create(category=self.common, keyword='кофе')
        CategoryKeyword.objects.create(category=self.special, keyword='кофе с собой')

        category, _ = self._classify('Оплата в КОФЕ С СОБОЙ', '-150.00 ₽')

        self.assertEqual(category, self.special)

    def test_priority_wins_over_keyword_length(self):
        CategoryKeyword.objects.create(category=self.special, keyword='кофе с собой')
        CategoryKeyword.objects.create(category=self.common, keyword='кофе', priority=10)

        category, _ = self._classify('Оплата в КОФЕ С СОБОЙ', '-150.00 ₽')

        self.assertEqual(category, self.common)

    def test_inactive_keyword_is_ignored(self):
        CategoryKeyword.objects.create(
            category=self.common, keyword='кофе', is_active=False,
        )

        category, _ = self._classify('Оплата в КОФЕ', '-150.00 ₽')

        self.assertIsNone(category)

    def test_keyword_from_other_operation_type_is_ignored(self):
        CategoryKeyword.objects.create(category=self.income, keyword='кофе')

        category, _ = self._classify('Оплата в КОФЕ', '-150.00 ₽')

        self.assertIsNone(category)

    def test_keyword_is_normalized_to_lowercase(self):
        keyword = CategoryKeyword.objects.create(
            category=self.common, keyword='  КОФЕ   С   СОБОЙ ',
        )

        self.assertEqual(keyword.keyword, 'кофе с собой')

    def _classify(self, description, amount):
        operation_type, category, is_unknown = self._importer().classify(
            description, Decimal(amount.replace(' ₽', '')),
        )
        self.assertEqual(operation_type, 'expense')
        return category, is_unknown
