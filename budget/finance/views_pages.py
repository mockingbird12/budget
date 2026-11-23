# finance/views_pages.py
from collections import defaultdict
from datetime import datetime, date, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import Sum
from django.db.models.functions import TruncYear, TruncMonth
from django.shortcuts import render
from django.core.paginator import Paginator

from .models import Account, Transaction
from .services import signed_delta

MSK = ZoneInfo('Europe/Moscow')

PALETTE = [
    '#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f',
    '#edc948', '#b07aa1', '#ff9da7', '#9c755f', '#bab0ac',
]


def _month_range(start: date, end: date):
    """Список (year, month) от start до end включительно."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def _available_years(operation_type: str) -> list[int]:
    years = (
        Transaction.objects
        .filter(operation_type=operation_type, is_unknown=False)
        .annotate(year=TruncYear('occurred_at'))
        .values_list('year', flat=True)
        .distinct()
    )
    return sorted({d.year for d in years if d}, reverse=True)


def _category_report(request, operation_type: str, page_title: str, template: str):
    """Общая логика страниц «по категориям» для расходов и доходов."""
    years = _available_years(operation_type)
    default_year = years[0] if years else date.today().year
    try:
        year = int(request.GET.get('year', default_year))
    except ValueError:
        year = default_year

    qs = (
        Transaction.objects
        .filter(
            operation_type=operation_type,
            is_unknown=False,
            occurred_at__year=year,
        )
        .annotate(month=TruncMonth('occurred_at'))
        .values('month', 'category__name')
        .annotate(total=Sum('amount'))
        .order_by('month')
    )

    months = [f'{y:04d}-{m:02d}' for y, m in _month_range(date(year, 1, 1), date(year, 12, 1))]
    month_labels = {m: m for m in months}  # можно заменить на 'янв 26' и т.п.

    # categories: {name: {month: amount}}
    categories = defaultdict(lambda: {m: Decimal('0') for m in months})
    for row in qs:
        mkey = row['month'].strftime('%Y-%m')
        name = row['category__name'] or 'Без категории'
        categories[name][mkey] = row['total']

    datasets = []
    for i, (name, values) in enumerate(sorted(categories.items())):
        datasets.append({
            'label': name,
            'data': [float(values[m]) for m in months],
            'backgroundColor': PALETTE[i % len(PALETTE)],
        })

    table_rows = []
    for name, values in sorted(categories.items(), key=lambda kv: -sum(kv[1].values())):
        table_rows.append({
            'name': name,
            'cells': [values[m] for m in months],
            'total': sum(values.values()),
        })

    return render(request, template, {
        'page_title': page_title,
        'kind_label': 'расходов' if operation_type == 'expense' else 'доходов',
        'year': year,
        'years': sorted(set(years) | {year}, reverse=True),
        'months': months,
        'month_labels': month_labels,
        'datasets': datasets,
        'table_rows': table_rows,
        'has_data': bool(datasets),
    })


def page_categories(request):
    return _category_report(request, 'expense', 'Траты по категориям', 'finance/categories.html')


def page_incomes(request):
    return _category_report(request, 'income', 'Доходы по категориям', 'finance/categories.html')


def page_monthly(request):
    today = date.today()
    month_str = request.GET.get('month') or f'{today.year:04d}-{today.month:02d}'
    try:
        month_start = datetime.strptime(month_str, '%Y-%m').date().replace(day=1)
    except ValueError:
        month_start = today.replace(day=1)

    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)

    def in_month(operation_type):
        return (
            Transaction.objects
            .filter(
                operation_type=operation_type,
                is_unknown=False,
                occurred_at__gte=datetime.combine(month_start, time(12, 0), tzinfo=MSK),
                occurred_at__lt=datetime.combine(next_month, time(12, 0), tzinfo=MSK),
            )
            .select_related('account', 'category')
        )

    expense_qs = in_month('expense').order_by('-occurred_at')
    income_qs = in_month('income').order_by('-occurred_at')

    expense_total = expense_qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    income_total = income_qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')

    # Список месяцев для селектора: последние 24 месяца
    months_options = []
    y, m = today.year, today.month
    for _ in range(24):
        months_options.append(f'{y:04d}-{m:02d}')
        m -= 1
        if m == 0:
            m = 12
            y -= 1

    # Пагинация только у расходов — их обычно намного больше.
    paginator = Paginator(expense_qs, 100)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'finance/monthly.html', {
        'month': month_start.strftime('%Y-%m'),
        'month_start': month_start,
        'next_month': next_month,
        'expense_total': expense_total,
        'income_total': income_total,
        'balance_delta': income_total - expense_total,
        'income_transactions': income_qs[:100],
        'has_income': income_qs.exists(),
        'transactions': page,
        'months_options': months_options,
        'has_data': expense_qs.exists(),
    })


def page_balance(request):
    """График остатка денег на счетах с нарастающим итогом по дням."""
    accounts = list(Account.objects.order_by('name'))

    raw = request.GET.get('account', 'all')
    selected = int(raw) if raw.isdigit() and any(a.id == int(raw) for a in accounts) else 'all'
    scoped_accounts = accounts if selected == 'all' else [a for a in accounts if a.id == selected]

    transactions = Transaction.objects.select_related('account').order_by('occurred_at', 'id')
    if selected != 'all':
        transactions = transactions.filter(account_id=selected)

    daily = defaultdict(lambda: defaultdict(Decimal))
    deltas = {a.id: Decimal('0') for a in scoped_accounts}
    inflows = {a.id: Decimal('0') for a in scoped_accounts}
    outflows = {a.id: Decimal('0') for a in scoped_accounts}

    for tx in transactions.iterator():
        delta = signed_delta(tx.operation_type, tx.amount)
        deltas[tx.account_id] += delta
        if tx.operation_type == 'income':
            inflows[tx.account_id] += tx.amount
        elif tx.operation_type == 'expense':
            outflows[tx.account_id] += tx.amount
        day = tx.occurred_at.astimezone(MSK).date().isoformat()
        daily[day][tx.account_id] += delta

    # Начальный остаток выводим из текущего баланса минус все изменения за
    # период: так график всегда заканчивается на реальном балансе счёта, даже
    # если он задан вручную или часть операций правилась без пересчёта.
    summary = []
    for account in scoped_accounts:
        summary.append({
            'id': account.id,
            'name': account.name,
            'opening': account.balance - deltas[account.id],
            'inflow': inflows[account.id],
            'outflow': outflows[account.id],
            'delta': deltas[account.id],
            'balance': account.balance,
        })

    labels = sorted(daily)
    series = {a.id: [] for a in scoped_accounts}
    running = {item['id']: item['opening'] for item in summary}
    for label in labels:
        for account in scoped_accounts:
            running[account.id] += daily[label].get(account.id, Decimal('0'))
            series[account.id].append(float(running[account.id]))

    datasets = []
    for i, account in enumerate(scoped_accounts):
        datasets.append({
            'label': account.name,
            'data': series[account.id],
            'borderColor': PALETTE[i % len(PALETTE)],
            'backgroundColor': PALETTE[i % len(PALETTE)],
        })
    if selected == 'all' and len(scoped_accounts) > 1:
        datasets.append({
            'label': 'Все счета',
            'data': [
                sum(series[a.id][i] for a in scoped_accounts)
                for i in range(len(labels))
            ],
            'borderColor': '#333',
            'backgroundColor': '#333',
            'borderWidth': 2,
        })

    return render(request, 'finance/balance.html', {
        'accounts': accounts,
        'selected_account': str(selected),
        'summary': summary,
        'total_balance': sum((a.balance for a in scoped_accounts), Decimal('0')),
        'labels': labels,
        'datasets': datasets,
        'has_data': bool(labels),
    })
