"""Compact local status. No Saby calls or state changes."""
from app.services.saby_maintenance import check


def compact(data):
    scheduler=data.get('automatic_updates',{}).get('maintenance:saby:scheduler:v1',{})
    bonus=data.get('bonuses',{})
    return {'saby_requests_made':0,'sales':data.get('sales'),
            'latest_sync':data.get('latest_sync'),'summary':data.get('summary'),
            'bonuses':{k:bonus.get(k) for k in ('saved_ok','new_sales','retry_errors','unsupported_saved','last_success_at')},
            'automatic_jobs':{name:{k:job.get(k) for k in ('status','last_success_at','next_run_at','error_code')}
                              for name,job in scheduler.items() if isinstance(job,dict)}}


async def status():
    return compact(await check())


def render(data):
    sales=data.get('sales') or {}
    sync=data.get('latest_sync') or {}
    summary=data.get('summary') or {}
    bonus=data.get('bonuses') or {}
    labels={'OK':'готово','ERROR':'ошибка','RUNNING':'отмечен выполняющимся',
            'PARTIAL':'частично','PENDING':'ожидает','REVIEW':'нужна проверка'}
    lines=['Загрузка данных',f"Продаж: {sales.get('count','н/д')} · по {sales.get('date_to','н/д')}",
           f"Последняя синхронизация: {labels.get(sync.get('status'),sync.get('status','нет'))}",
           f"Завершена: {sync.get('finished_at') or 'нет отметки'}",
           f"Сверка закрытых дней: {labels.get(summary.get('closed_day_reconcile'),summary.get('closed_day_reconcile','н/д'))}",
           f"Бонусы: сохранено {bonus.get('saved_ok','н/д')} · новых {bonus.get('new_sales','н/д')} · ошибок {bonus.get('retry_errors','н/д')}"]
    names={'sales':'Продажи','reconcile':'Сверка','bonuses_new':'Новые бонусы','bonuses_errors':'Повторы бонусов','catalog':'Каталог','stores':'Магазины','prices':'Прайсы'}
    for name,job in data.get('automatic_jobs',{}).items():
        lines.append(f"{names.get(name,name)}: {labels.get(job.get('status'),job.get('status') or 'нет отметки')}; следующий запуск {job.get('next_run_at') or 'не назначен'}")
    if summary.get('bonus_session_present') or summary.get('maintenance_session_present'):
        lines.append('Сеанс загрузки занят. Продвижение проверяйте по времени и счётчикам.')
    return '\n'.join(lines)
