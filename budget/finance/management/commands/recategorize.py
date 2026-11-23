"""Переразметка существующих операций по ключевым словам из БД.

По умолчанию команда трогает только операции без категории и помеченные как
``is_unknown``: уже разобранные вручную операции остаются как есть. Полный
пересчёт (включая очистку категорий, для которых больше нет слова) — с флагом
``--overwrite``.

Примеры::

    # предпросмотр
    python manage.py recategorize

    # записать найденные категории
    python manage.py recategorize --commit

    # пересчитать всё, в том числе уже разобранные операции
    python manage.py recategorize --overwrite --commit
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction
from django.db.models import Q

from finance.categorization import KeywordMatcher
from finance.models import Transaction

NO_CATEGORY = 'Без категории'
UNKNOWN = 'Неизвестные'


def _money(value: Decimal) -> str:
    return f'{value:,.2f}'.replace(',', ' ').replace('.', ',') + ' ₽'


@dataclass
class Totals:
    count: int = 0
    amount: Decimal = Decimal('0')

    def add(self, amount: Decimal) -> None:
        self.count += 1
        self.amount += amount


@dataclass
class Report:
    considered: int = 0
    unchanged: int = 0
    filled: int = 0
    resolved_unknown: int = 0
    changes: dict[tuple[str, str], Totals] = field(default_factory=dict)
    examples: dict[tuple[str, str, str], Totals] = field(default_factory=dict)

    @property
    def changed(self) -> int:
        return sum(item.count for item in self.changes.values())

    @property
    def changed_amount(self) -> Decimal:
        return sum((item.amount for item in self.changes.values()), Decimal('0'))


def _label(category, is_unknown: bool) -> str:
    if is_unknown:
        return UNKNOWN
    return category.name if category is not None else NO_CATEGORY


class Command(BaseCommand):
    help = 'Переразмечает операции по словам из таблицы категорий (по умолчанию — предпросмотр)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--account', default=None,
            help='Только операции указанного счёта (название)',
        )
        parser.add_argument(
            '--source', default=None,
            help='Только операции с указанным source (например, tbank)',
        )
        parser.add_argument(
            '--overwrite', action='store_true',
            help='Пересчитать все операции, включая уже разобранные (иначе — только без категории и неизвестные)',
        )
        parser.add_argument(
            '--commit', action='store_true',
            help='Записать изменения в БД (без флага — только предпросмотр)',
        )
        parser.add_argument(
            '--top', type=int, default=20,
            help='Сколько изменившихся описаний показывать (по умолчанию: 20)',
        )

    def handle(self, *args, **options):
        queryset = (
            Transaction.objects
            .select_related('account', 'category')
            .order_by('occurred_at', 'id')
        )
        if options['account']:
            queryset = queryset.filter(account__name__iexact=options['account'])
        if options['source']:
            queryset = queryset.filter(source=options['source'])
        if not options['overwrite']:
            queryset = queryset.filter(
                Q(category__isnull=True) | Q(is_unknown=True)
            )

        dry_run = not options['commit']
        matcher = KeywordMatcher()
        report = Report(considered=queryset.count())
        to_update: list[Transaction] = []

        for tx in queryset.iterator():
            new_category = matcher.match(tx.description, tx.operation_type)
            new_unknown = tx.is_unknown if new_category is None else False

            if new_category == tx.category and new_unknown == tx.is_unknown:
                report.unchanged += 1
                continue

            old_label = _label(tx.category, tx.is_unknown)
            new_label = _label(new_category, new_unknown)

            report.changes.setdefault((old_label, new_label), Totals()).add(tx.amount)
            report.examples.setdefault(
                (tx.description or '—', old_label, new_label), Totals(),
            ).add(tx.amount)

            if tx.category is None and new_category is not None:
                report.filled += 1
            if tx.is_unknown and not new_unknown:
                report.resolved_unknown += 1

            tx.category = new_category
            tx.is_unknown = new_unknown
            to_update.append(tx)

        if not dry_run and to_update:
            with db_transaction.atomic():
                Transaction.objects.bulk_update(
                    to_update, ['category', 'is_unknown'], batch_size=500,
                )

        self._print_report(report, options, dry_run)
        return None

    def _print_report(self, report: Report, options: dict, dry_run: bool) -> None:
        mode = 'ПРЕДПРОСМОТР (в БД ничего не записано)' if dry_run else 'ПЕРЕРАЗМЕТКА'
        self.stdout.write(self.style.MIGRATE_HEADING(f'=== {mode} ==='))
        filters = []
        if options['account']:
            filters.append(f'счёт «{options["account"]}»')
        if options['source']:
            filters.append(f'source={options["source"]}')
        if options['overwrite']:
            filters.append('режим: полный пересчёт')
        self.stdout.write(f'Фильтры: {", ".join(filters) if filters else "все операции"}')
        self.stdout.write(f'Операций в выборке: {report.considered}')

        if not report.changes:
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('Изменений нет.'))
            return

        self.stdout.write(
            f'Будет изменено: {report.changed} на {_money(report.changed_amount)}'
        )
        self.stdout.write(f'Без изменений: {report.unchanged}')
        if report.filled:
            self.stdout.write(f'  заполнено категорий: {report.filled}')
        if report.resolved_unknown:
            self.stdout.write(f'  неизвестных стало известно: {report.resolved_unknown}')

        self.stdout.write('')
        self.stdout.write('Изменения по категориям:')
        for (old_label, new_label), totals in sorted(
            report.changes.items(), key=lambda item: (-item[1].amount, item[0]),
        ):
            self.stdout.write(
                f'  {old_label} → {new_label}: '
                f'{totals.count} на {_money(totals.amount)}'
            )

        limit = options['top']
        if limit > 0:
            self.stdout.write('')
            self.stdout.write(f'Примеры (до {limit} описаний):')
            ordered = sorted(
                report.examples.items(), key=lambda item: (-item[1].amount, item[0]),
            )
            for (description, old_label, new_label), totals in ordered[:limit]:
                self.stdout.write(
                    f'  {totals.count:>3} × {description} '
                    f'[{old_label} → {new_label}] — {_money(totals.amount)}'
                )

        self.stdout.write('')
        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                'Это был предпросмотр. Для записи запустите ту же команду с --commit'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Обновлено операций: {report.changed}'
            ))
