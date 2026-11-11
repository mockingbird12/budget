# finance/admin.py
from django.contrib import admin
from .models import Account, Category, CategoryKeyword, Transaction
from .services import apply_transaction


class CategoryKeywordInline(admin.TabularInline):
    model = CategoryKeyword
    extra = 0
    fields = ('keyword', 'priority', 'is_active')


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'type', 'currency', 'balance', 'is_active')
    list_filter = ('type', 'is_active')


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'operation_type', 'parent')
    list_filter = ('operation_type',)
    search_fields = ('name',)
    inlines = (CategoryKeywordInline,)


@admin.register(CategoryKeyword)
class CategoryKeywordAdmin(admin.ModelAdmin):
    list_display = ('keyword', 'category', 'priority', 'is_active')
    list_filter = ('is_active', 'category__operation_type')
    search_fields = ('keyword',)
    autocomplete_fields = ('category',)
    ordering = ('-priority', 'keyword')


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('occurred_at', 'account', 'operation_type', 'amount', 'category', 'source', 'is_unknown')
    list_filter = ('operation_type', 'account', 'source', 'is_unknown')
    search_fields = ('description', 'external_id')
    date_hierarchy = 'occurred_at'

    def save_model(self, request, obj, form, change):
        if not change:  # только при создании
            apply_transaction(obj)
        else:
            # редактирование транзакции — отдельная история, позже
            super().save_model(request, obj, form, change)