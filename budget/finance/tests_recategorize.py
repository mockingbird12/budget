from io import StringIO
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from finance.models import Account, Category, CategoryKeyword, Transaction


class RecategorizeTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(name='Т-Банк', type=Account.Type.CARD)
        self.groceries = Category.objects.create(
            name='Продукты, еда', operation_type='expense',
        )
        self.other = Category.objects.create(name='Прочее', operation_type='expense')
        CategoryKeyword.objects.create(
            category=self.groceries, keyword='pyaterochka',
        )

    def _run(self, *args):
        stdout = StringIO()
        call_command('recategorize', *args, stdout=stdout)
        return stdout.getvalue()

    def _tx(self, description, **kwargs):
        defaults = {
            'account': self.account,
            'operation_type': 'expense',
            'amount': Decimal('100.00'),
            'occurred_at': timezone.now(),
            'source': 'tbank',
        }
        defaults.update(kwargs)
        return Transaction.objects.create(description=description, **defaults)

    def test_preview_changes_nothing(self):
        tx = self._tx('Оплата в PYATEROCHKA 123')

        output = self._run()

        tx.refresh_from_db()
        self.assertIsNone(tx.category)
        self.assertIn('Будет изменено: 1', output)

    def test_commit_fills_empty_category(self):
        tx = self._tx('Оплата в PYATEROCHKA 123')

        self._run('--commit')

        tx.refresh_from_db()
        self.assertEqual(tx.category, self.groceries)
        self.assertFalse(tx.is_unknown)

    def test_existing_category_is_not_overwritten_by_default(self):
        tx = self._tx('Оплата в PYATEROCHKA 123', category=self.other)

        self._run('--commit')

        tx.refresh_from_db()
        self.assertEqual(tx.category, self.other)

    def test_overwrite_updates_existing_category(self):
        tx = self._tx('Оплата в PYATEROCHKA 123', category=self.other)

        self._run('--overwrite', '--commit')

        tx.refresh_from_db()
        self.assertEqual(tx.category, self.groceries)

    def test_overwrite_clears_category_without_match(self):
        tx = self._tx('Оплата в НЕИЗВЕСТНЫЙ МАГАЗИН', category=self.other)

        self._run('--overwrite', '--commit')

        tx.refresh_from_db()
        self.assertIsNone(tx.category)

    def test_unknown_operation_becomes_known_when_matched(self):
        tx = self._tx('Оплата в PYATEROCHKA 123', is_unknown=True)

        self._run('--commit')

        tx.refresh_from_db()
        self.assertEqual(tx.category, self.groceries)
        self.assertFalse(tx.is_unknown)

    def test_unknown_operation_without_match_stays_unknown(self):
        tx = self._tx('Перевод себе', is_unknown=True, operation_type='income')

        self._run('--commit')

        tx.refresh_from_db()
        self.assertIsNone(tx.category)
        self.assertTrue(tx.is_unknown)

    def test_source_filter(self):
        tbank = self._tx('Оплата в PYATEROCHKA 123', source='tbank')
        manual = self._tx('Оплата в PYATEROCHKA 456', source='manual')

        self._run('--source', 'tbank', '--commit')

        tbank.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(tbank.category, self.groceries)
        self.assertIsNone(manual.category)

    def test_account_filter(self):
        other_account = Account.objects.create(name='Наличные', type=Account.Type.CASH)
        tbank = self._tx('Оплата в PYATEROCHKA 123')
        cash = self._tx('Оплата в PYATEROCHKA 456', account=other_account)

        self._run('--account', 'Наличные', '--commit')

        tbank.refresh_from_db()
        cash.refresh_from_db()
        self.assertIsNone(tbank.category)
        self.assertEqual(cash.category, self.groceries)

    def test_keyword_of_other_direction_is_ignored(self):
        cashback = Category.objects.create(name='Кэшбэк', operation_type='income')
        CategoryKeyword.objects.create(category=cashback, keyword='кофе')
        tx = self._tx('Оплата в КОФЕ')

        self._run('--commit')

        tx.refresh_from_db()
        self.assertIsNone(tx.category)
