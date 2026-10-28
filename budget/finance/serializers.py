# finance/serializers.py
from rest_framework import serializers
from .models import Account, Category, Transaction


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = ['id', 'name', 'type', 'currency', 'balance', 'is_active']
        read_only_fields = ['balance']  # баланс меняется только через транзакции


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'operation_type', 'parent']


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = [
            'id', 'account', 'operation_type', 'category',
            'amount', 'currency', 'occurred_at', 'description',
            'transfer_account', 'external_id', 'source',
        ]

    def validate(self, attrs):
        op = attrs.get('operation_type')
        if op == 'transfer' and not attrs.get('transfer_account'):
            raise serializers.ValidationError('Для перевода нужен transfer_account')
        if op != 'transfer' and attrs.get('transfer_account'):
            raise serializers.ValidationError('transfer_account только для переводов')
        if op == 'transfer' and attrs.get('category'):
            raise serializers.ValidationError('У перевода не должно быть категории')
        return attrs