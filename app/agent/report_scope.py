"""Scope shared by tool tasks within one request, never between users."""
from contextvars import ContextVar

request_scope=ContextVar('report_scope',default=None)


def remember_store(result,query):
    state=request_scope.get()
    rows=result.get('stores',[])
    if state is not None and query.strip():
        state['point_id']=str(rows[0]['point_id']) if len(rows)==1 else None
        state['ambiguous']=len(rows)!=1


def resolve_point(value):
    if value=='network': return ''
    state=request_scope.get()
    if value: return value
    if state and state.get('ambiguous'):
        raise ValueError('Уточните магазин: справочник не дал единственного совпадения.')
    if state and state.get('point_id'): return state['point_id']
    raise ValueError('Укажите point_id из list_stores или явно network для всей сети. Пустой фильтр не разрешён.')


def validate_scope(result,point):
    if str(result.get('point_id') or '')!=str(point or ''):
        raise ValueError('REPORT_SCOPE_MISMATCH: область результата отличается от запрошенной.')
    result['scope_contract']={'kind':'store' if point else 'network','point_id':point or None,
                              'note':'Все суммы этого результата относятся только к этой области. Не переименовывать сеть в магазин.'}
    return result
