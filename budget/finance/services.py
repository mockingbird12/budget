# finance/services.py
from decimal import Decimal
from django.db import transaction as db_transaction
from .models import Account, Transaction


def _signed_delta(op_type: str, amount: Decimal) -> Decimal:
    if op_type == 'income':
        return amount
    if op_type == 'expense':
        return -amount
    return Decimal('0')  # перевод меняет два счёта отдельно


@db_transaction.atomic
def apply_transaction(tx: Transaction) -> Transaction:
    """Создаёт транзакцию и обновляет балансы счетов."""
    tx.save()
    delta = _signed_delta(tx.operation_type, tx.amount)

    Account.objects.filter(pk=tx.account_id).update(
        balance=models.F('balance') + delta
    )
    if tx.operation_type == 'transfer' and tx.transfer_account_id:
        Account.objects.filter(pk=tx.transfer_account_id).update(
            balance=models.F('balance') + tx.amount
        )
    return tx