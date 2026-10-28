# finance/views.py
from rest_framework import viewsets
from django_filters.rest_framework import DjangoFilterBackend
from .models import Account, Category, Transaction
from .serializers import AccountSerializer, CategorySerializer, TransactionSerializer
from .services import apply_transaction


class AccountViewSet(viewsets.ModelViewSet):
    queryset = Account.objects.all()
    serializer_class = AccountSerializer


class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    filterset_fields = ['operation_type', 'parent']


class TransactionViewSet(viewsets.ModelViewSet):
    queryset = Transaction.objects.select_related('account', 'category')
    serializer_class = TransactionSerializer
    filterset_fields = ['account', 'operation_type', 'category', 'source']
    ordering_fields = ['occurred_at', 'amount']

    def perform_create(self, serializer):
        apply_transaction(serializer.save())
