"""Команда импорта выписки Т-Банка.

Примеры::

    # предпросмотр (ничего не пишет в БД)
    python manage.py import_tbank output.json

    # импорт с созданием счёта
    python manage.py import_tbank output.json --commit
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from finance.importers import TbankImporter, TbankImportError


def _money(value: Decimal) -> str:
    return f'{value:,.2f}'.replace(',', ' ').replace('.', ',') + ' ₽'


def _top_rows(rows: dict[str, list], limit: int) -> list[tuple[str, int, Decimal]]:
    prepared = [(name, count, total) for name, (count, total) in rows.items()]
    prepared.sort(key=lambda item: (-item[2], item[0]))
    return prepared[:limit]


class Command(BaseCommand):
    help = 'Импортирует выписку Т-Банка из JSON (по умолчанию — предпросмотр)'

    def add_arguments(self, parser):
        parser.add_argument('json_file', help='Путь к JSON, созданному tbank-cash-flow-pdf-parser')
        parser.add_argument('--account', default='Т-Банк', help='Название счёта (по умолчанию: Т-Банк)')
        parser.add_argument('--source', default='tbank', help='Значение поля source (по умолчанию: tbank)')
        parser.add_argument(
            '--commit', action='store_true',
            help='Записать изменения в БД (без флага — только предпросмотр)',
        )
        parser.add_argument('--top', type=int, default=25, help='Сколько строк показывать в отчётах (по умолчанию: 25)')

    def handle(self, *args, **options):
        path = Path(options['json_file']).expanduser()
        if not path.is_file():
            raise CommandError(f'Файл не найден: {path}')

        dry_run = not options['commit']
        try:
            importer = TbankImporter(
                account_name=options['account'],
                source=options['source'],
                dry_run=dry_run,
            )
            report = importer.run(path)
        except TbankImportError as exc:
            raise CommandError(str(exc)) from exc

        mode = 'ПРЕДПРОСМОТР (в БД ничего не записано)' if dry_run else 'ИМПОРТ'
        self.stdout.write(self.style.MIGRATE_HEADING(f'=== Т-Банк: {mode} ==='))
        self.stdout.write(f'Файл: {path}')
        if dry_run:
            account_state = 'существует' if report.account_exists else 'будет создан'
        else:
            account_state = 'создан' if report.account_created else 'существует'
        self.stdout.write(f'Счёт: «{options["account"]}» — {account_state}')
        self.stdout.write(f'Строк в файле: {report.total_rows}')
        self.stdout.write(f'Уже импортировано (пропущено): {report.skipped_existing}')
        self.stdout.write(f'К импорту: {report.to_import}')
        if not dry_run:
            self.stdout.write(f'Записано: {report.imported}')

        self.stdout.write('')
        self.stdout.write('По типам:')
        labels = {'expense': 'расходы', 'income': 'доходы', 'transfer': 'переводы'}
        for op_type in ('expense', 'income', 'transfer'):
            count = report.counts.get(op_type, 0) - report.unknown_counts.get(op_type, 0)
            amount = report.amounts.get(op_type, 0) - report.unknown_amounts.get(op_type, 0)
            if count:
                self.stdout.write(f'  {labels[op_type]}: {count} на {_money(amount)}')
        if report.unknown:
            unknown_total = sum((total for _, total in report.unknown.values()), Decimal('0'))
            unknown_count = sum(count for count, _ in report.unknown.values())
            self.stdout.write(
                f'  неизвестные: {unknown_count} на {_money(unknown_total)} '
                f'(в балансе учитываются, в аналитике — нет)'
            )

        if report.by_category:
            self.stdout.write('')
            self.stdout.write('По категориям:')
            for (op_type, name), (count, total) in sorted(
                report.by_category.items(), key=lambda item: (item[0][0], -item[1][1]),
            ):
                prefix = '+' if op_type == 'income' else '−'
                self.stdout.write(f'  {name}: {count} на {prefix}{_money(total)}')

        if report.uncategorized:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                f'Не разобрано по категориям ({len(report.uncategorized)} описаний):'
            ))
            for name, count, total in _top_rows(report.uncategorized, options['top']):
                self.stdout.write(f'  {count:>3} × {name} — {_money(total)}')

        if report.unknown:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                f'Неизвестные операции ({len(report.unknown)} описаний):'
            ))
            for name, count, total in _top_rows(report.unknown, options['top']):
                self.stdout.write(f'  {count:>3} × {name} — {_money(total)}')

        self.stdout.write('')
        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                'Это был предпросмотр. Для записи запустите ту же команду с --commit'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('Импорт завершён.'))
