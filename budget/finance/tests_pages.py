from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.urls import reverse

from finance.models import Account, Category, Transaction

MSK = ZoneInfo('Europe/Moscow')


def _at(day: int) -> datetime:
    return datetime(2026, 10, day, 12, 0, tzinfo=MSK)


class ReportPagesTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(
            name='Т-Банк', type=Account.Type.CARD, balance=Decimal('70.00'),
        )
        self.income_category = Category.objects.create(
            name='Пополнение', operation_type='income',
        )
        self.expense_category = Category.objects.create(
            name='Продукты, еда', operation_type='expense',
        )

        Transaction.objects.create(
            account=self.account, operation_type='income',
            category=self.income_category, amount=Decimal('100.00'),
            occurred_at=_at(1), description='Пополнение через СБП',
            source='tbank',
        )
        Transaction.objects.create(
            account=self.account, operation_type='expense',
            category=self.expense_category, amount=Decimal('30.00'),
            occurred_at=_at(2), description='Оплата в MAGNIT',
            source='tbank',
        )

    def test_income_page_shows_only_income(self):
        response = self.client.get(reverse('page_incomes'), {'year': 2026})

        self.assertEqual(response.status_code, 200)
        labels = [item['label'] for item in response.context['datasets']]
        self.assertIn('Пополнение', labels)
        self.assertNotIn('Продукты, еда', labels)
        self.assertContains(response, 'Доходы по категориям')

    def test_expense_page_shows_only_expenses(self):
        response = self.client.get(reverse('page_categories'), {'year': 2026})

        labels = [item['label'] for item in response.context['datasets']]
        self.assertIn('Продукты, еда', labels)
        self.assertNotIn('Пополнение', labels)

    def test_monthly_page_shows_both_directions(self):
        response = self.client.get(reverse('page_monthly'), {'month': '2026-10'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['income_total'], Decimal('100.00'))
        self.assertEqual(response.context['expense_total'], Decimal('30.00'))
        self.assertEqual(response.context['balance_delta'], Decimal('70.00'))
        self.assertContains(response, 'Пополнение через СБП')
        self.assertContains(response, 'Оплата в MAGNIT')


class BalancePageTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(
            name='Т-Банк', type=Account.Type.CARD, balance=Decimal('70.00'),
        )
        Transaction.objects.create(
            account=self.account, operation_type='income',
            amount=Decimal('100.00'), occurred_at=_at(1),
            description='Пополнение', source='tbank',
        )
        Transaction.objects.create(
            account=self.account, operation_type='expense',
            amount=Decimal('30.00'), occurred_at=_at(2),
            description='Оплата', source='tbank',
        )

    def test_balance_series_is_cumulative_and_ends_at_current_balance(self):
        response = self.client.get(reverse('page_balance'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['labels'], ['2026-10-01', '2026-10-02'])
        self.assertEqual(response.context['datasets'][0]['data'], [100.0, 70.0])
        self.assertEqual(response.context['summary'][0]['balance'], Decimal('70.00'))

    def test_unknown_operations_are_included_in_balance(self):
        Transaction.objects.create(
            account=self.account, operation_type='income',
            amount=Decimal('1000.00'), occurred_at=_at(3),
            description='Перевод себе', source='tbank', is_unknown=True,
        )
        Account.objects.filter(pk=self.account.pk).update(balance=Decimal('1070.00'))

        response = self.client.get(reverse('page_balance'))

        self.assertEqual(response.context['datasets'][0]['data'][-1], 1070.0)

    def test_account_filter(self):
        other = Account.objects.create(
            name='Наличные', type=Account.Type.CASH, balance=Decimal('500.00'),
        )

        all_accounts = self.client.get(reverse('page_balance'))
        self.assertEqual(len(all_accounts.context['datasets']), 3)  # 2 счёта + «Все счета»
        self.assertEqual(all_accounts.context['total_balance'], Decimal('570.00'))

        one = self.client.get(reverse('page_balance'), {'account': other.id})
        self.assertFalse(one.context['has_data'])
        self.assertEqual(len(one.context['summary']), 1)
        self.assertEqual(one.context['datasets'][0]['label'], 'Наличные')

    def test_no_transactions_shows_empty_state(self):
        empty = Account.objects.create(name='Пустой', type=Account.Type.CASH)
        response = self.client.get(reverse('page_balance'), {'account': empty.id})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Операций для построения графика нет')
