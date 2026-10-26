"""Only application-owned codes may reach CLI output; never raw exception text."""
KNOWN_CODES = {
    'BONUS_LOADER_ALREADY_RUNNING', 'BONUS_LOAD_INCOMPLETE',
    'BACKFILL_NOT_COMPLETED', 'INCOMPLETE_PRICES', 'INCOMPLETE_PRICE',
    'DISABLE_AUTO_SYNC_FOR_MAINTENANCE', 'MAINTENANCE_ALREADY_RUNNING',
    'UNFINISHED_MAINTENANCE_USE_RESUME', 'NO_MAINTENANCE_TO_RESUME',
    'EMPTY_DB_SPECIFY_START_AND_END', 'ONLY_CLOSED_BUSINESS_DAYS',
}


def error_details(exc):
    code = str(exc) if type(exc) is ValueError and str(exc) in KNOWN_CODES else 'UNCLASSIFIED_ERROR'
    result = {'error_type': type(exc).__name__, 'error_code': code}
    if code == 'BONUS_LOADER_ALREADY_RUNNING':
        result['next'] = ('Другой сеанс PostgreSQL удерживает блокировку загрузчика бонусов. '
                          'Проверьте существующие запуски. Не сбрасывайте прогресс и не снимайте блокировку принудительно.')
    return result
