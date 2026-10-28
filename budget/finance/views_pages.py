# finance/views_pages.py
from collections import defaultdict
from datetime import datetime, date
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import render
from django.core.paginator import Paginator

from .models import Category, Transaction




def _month_range(start: date, end: date):
    """Список (year, month) от start до end включительно."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def page_categories(request):
    # фильтр по году, по умолчанию — текущий
    today = date.today()
    try:
        year = int(request.GET.get('year', today.year))
    except ValueError:
        year = today.year

    qs = (
        Transaction.objects
        .filter(operation_type='expense', occurred_at__year=year)
        .annotate(month=TruncMonth('occurred_at'))
        .values('month', 'category__name')
        .annotate(total=Sum('amount'))
        .order_by('month')
    )

    # months: ['2026-01', ...]
    months = [f'{y:04d}-{m:02d}' for y, m in _month_range(date(year, 1, 1), date(year, 12, 1))]
    month_labels = {m: m for m in months}  # можно заменить на 'янв 26' и т.п.

    # categories: {name: {month: amount}}
    categories = defaultdict(lambda: {m: Decimal('0') for m in months})
    for row in qs:
        mkey = row['month'].strftime('%Y-%m')
        name = row['category__name'] or 'Без категории'
        categories[name][mkey] = row['total']

    # Цвета для стеков (можно генерировать по хешу)
    palette = [
        '#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f',
        '#edc948', '#b07aa1', '#ff9da7', '#9c755f', '#bab0ac',
    ]
    datasets = []
    for i, (name, values) in enumerate(sorted(categories.items())):
        datasets.append({
            'label': name,
            'data': [float(values[m]) for m in months],
            'backgroundColor': palette[i % len(palette)],
        })

    # Таблица: строки — категории, колонки — месяцы + итог
    table_rows = []
    for name, values in sorted(categories.items(), key=lambda kv: -sum(kv[1].values())):
        row_total = sum(values.values())
        table_rows.append({
            'name': name,
            'cells': [values[m] for m in months],
            'total': row_total,
        })

    return render(request, 'finance/categories.html', {
        'year': year,
        'months': months,
        'month_labels': month_labels,
        'datasets': datasets,
        'table_rows': table_rows,
        'has_data': bool(datasets),
    })


def page_monthly(request):
    today = date.today()
    month_str = request.GET.get('month') or f'{today.year:04d}-{today.month:02d}'
    try:
        month_start = datetime.strptime(month_str, '%Y-%m').date().replace(day=1)
    except ValueError:
        month_start = today.replace(day=1)

    # границы месяца
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)

    qs = (
        Transaction.objects
        .filter(
            operation_type='expense',
            occurred_at__gte=month_start,
            occurred_at__lt=next_month,
        )
        .select_related('account', 'category')
        .order_by('-occurred_at')
    )

    total = qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')

    # Список месяцев для селектора: последние 24 месяца
    months_options = []
    y, m = today.year, today.month
    for _ in range(24):
        months_options.append(f'{y:04d}-{m:02d}')
        m -= 1
        if m == 0:
            m = 12
            y -= 1

    # пагинация
    paginator = Paginator(qs, 100)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'finance/monthly.html', {
        'month': month_start.strftime('%Y-%m'),
        'month_start': month_start,
        'next_month': next_month,
        'total': total,
        'transactions': page,
        'months_options': months_options,
        'has_data': qs.exists(),
    })