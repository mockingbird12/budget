# finance/models.py
from django.db import models


class OperationType(models.TextChoices):
    INCOME = 'income', 'Доход'
    EXPENSE = 'expense', 'Расход'
    TRANSFER = 'transfer', 'Перевод'


class Account(models.Model):
    class Type(models.TextChoices):
        CARD = 'card', 'Карта'
        CASH = 'cash', 'Наличные'
        DEPOSIT = 'deposit', 'Вклад'
        BROKERAGE = 'brokerage', 'Брокерский'

    name = models.CharField(max_length=100)
    type = models.CharField(max_length=20, choices=Type.choices)
    currency = models.CharField(max_length=3, default='RUB')
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.name} ({self.balance} {self.currency})'


class Category(models.Model):
    name = models.CharField(max_length=100)
    operation_type = models.CharField(max_length=10, choices=OperationType.choices)
    parent = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.CASCADE, related_name='children',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['name', 'parent'], name='uniq_category_name_parent'),
        ]

    def __str__(self):
        return self.name


class Transaction(models.Model):
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='transactions')
    operation_type = models.CharField(max_length=10, choices=OperationType.choices)
    category = models.ForeignKey(
        Category, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='transactions',
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)  # всегда > 0
    currency = models.CharField(max_length=3, default='RUB')
    occurred_at = models.DateTimeField()
    description = models.TextField(blank=True)

    # только для переводов
    transfer_account = models.ForeignKey(
        Account, null=True, blank=True,
        on_delete=models.PROTECT, related_name='incoming_transfers',
    )

    # защита от дублей при импорте парсером
    external_id = models.CharField(max_length=128, blank=True, db_index=True)
    source = models.CharField(max_length=50, blank=True)  # 'tbank', 'manual', ...

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-occurred_at']
        indexes = [
            models.Index(fields=['occurred_at']),
            models.Index(fields=['account', 'occurred_at']),
            models.Index(fields=['category', 'occurred_at']),
        ]
        constraints = [
            models.CheckConstraint(check=models.Q(amount__gt=0), name='amount_positive'),
        ]

    def __str__(self):
        return f'{self.occurred_at:%Y-%m-%d} {self.operation_type} {self.amount}'