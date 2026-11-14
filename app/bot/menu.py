"""Owner-bound Telegram button navigation with durable short-lived form state."""
import json
import logging
from datetime import datetime,timedelta,timezone,date
from uuid import uuid4
from aiogram import Router,F
from aiogram.filters import Command,BaseFilter
from aiogram.types import InlineKeyboardMarkup,InlineKeyboardButton,ReplyKeyboardMarkup,KeyboardButton,BufferedInputFile
from app.config import get_settings
from app.db.database import pool
from app.agent.conversations import key as conversation_key,serial
from app.services import period_reports,personal_followups as followups
from app.bot import menu_reports as reports

router=Router(name='button_menu')
logger=logging.getLogger(__name__)
SECTIONS={'network':'📊 Обзор сети','stores':'🏪 Магазины','sellers':'👥 Продавцы','products':'📦 Товары',
          'basket':'🛒 Покупки','clients':'💳 Клиенты и бонусы','tasks':'📋 Задачи','notifications':'🔔 Уведомления','data':'⚙️ Данные'}
ITEMS={'network':['sales','comparison','drivers','morning'],'stores':['card','sales','daynight'],
       'sellers':['averages','sellers','quantities'],'products':['products','categories','quantities','modifiers'],
       'basket':['averages','basket','returns'],'clients':['clients','loyalty','customers'],
       'data':['status','quality']}
SHIFTS={'':'Все смены','DAY':'Дневная смена','NIGHT':'Ночная смена'}
TASK_STATES={'PENDING':'Запланировано','PROCESSING':'Проверяется','WAITING':'Ожидание данных',
             'PAUSED':'Пауза','CANCELLED':'Отменено','DONE':'Завершено','SENDING':'Отправляется','DELIVERY_UNKNOWN':'Доставка не подтверждена'}


def allowed(user): return bool(user and user.id in get_settings().allowed_user_ids)
def actor(user,message):
    return {'user_id':user.id,'private':message.chat.type=='private','message_id':message.message_id}
def session(user,message): return 'assistant:menu:v1:'+conversation_key(message.chat.id,user.id,getattr(message,'message_thread_id',None)).split(':')[-1]


async def load(key):
    async with pool().acquire() as conn: value=await conn.fetchval('SELECT value FROM app_meta WHERE key=$1',key)
    if not value: return {}
    value=json.loads(value) if isinstance(value,str) else value
    if datetime.now(timezone.utc)-datetime.fromisoformat(value['updated_at'])>timedelta(days=1): return {}
    return value


async def save(key,state):
    state['updated_at']=datetime.now(timezone.utc).isoformat()
    async with pool().acquire() as conn:
        await conn.execute('''INSERT INTO app_meta(key,value,updated_at) VALUES($1,$2::jsonb,now())
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''',key,json.dumps(state,ensure_ascii=False))


def keyboard(state,buttons,nav=True):
    buttons=list(buttons)
    if nav: buttons += [('⬅️ Назад','back',''),('🏠 Главное меню','home','')]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label,callback_data=f"ui:{state['nonce']}:{action}:{value}")] for label,action,value in buttons])


async def show(message,key,state,text,buttons,nav=True):
    state['nonce']=uuid4().hex[:10]
    state['actions']=[[a,v] for _,a,v in buttons]+([['back',''],['home','']] if nav else [])
    await save(key,state)
    await message.answer(text,reply_markup=keyboard(state,buttons,nav))


async def home(message,key,state):
    state.clear();state['page']='home'
    await show(message,key,state,'Причал AI · выберите раздел. Можно также написать вопрос своими словами.',
               [(label,'section',name) for name,label in SECTIONS.items()],False)


async def section(message,key,s,user):
    s.pop('input',None);s['page']='section'
    name=s.get('section','network')
    if name=='notifications': return await settings(message,key,s,user)
    if name=='tasks': return await task_list(message,key,s,user)
    await show(message,key,s,SECTIONS[name],[(reports.TITLES.get(r,{'morning':'🌅 Утренний обзор','status':'Статус загрузки'}.get(r,r)),'report',r) for r in ITEMS[name]])


async def settings(message,key,s,user):
    s.pop('input',None)
    prefs=await followups.configure(actor(user,message))
    s['page']='settings'
    label=lambda enabled:'✅ Включено' if enabled else '⏸ Выключено'
    buttons=[(f"Все уведомления: {label(prefs['enabled'])}",'pref','enabled,'+('off' if prefs['enabled'] else 'on')),
             (f"Утренний обзор: {label(prefs['morning'])}",'pref','morning,'+('off' if prefs['morning'] else 'on')),
             (f"Результаты задач: {label(prefs['tasks_enabled'])}",'pref','tasks_enabled,'+('off' if prefs['tasks_enabled'] else 'on')),
             (f"🕙 Время обзора: {prefs['time']}",'time','')]
    await show(message,key,s,'Личные уведомления · Екатеринбург\nОбщий выключатель сохраняет настройки и задачи.',buttons)


async def task_list(message,key,s,user):
    s.pop('input',None)
    rows=await followups.tasks(actor(user,message))
    s['page']='tasks';s['task_ids']=[r['id'] for r in rows]
    await show(message,key,s,'Мои задачи · активные и последние завершённые',
               [('➕ Новая задача','newtask','')]+[(f"{r['store']} · {TASK_STATES.get(r['status'],r['status'])}",'task',r['id']) for r in rows])


async def task_card(message,key,s,user,task_id):
    rows=await followups.tasks(actor(user,message));t=next((r for r in rows if r['id']==task_id),None)
    if not t: raise ValueError('Задача не найдена в вашем списке.')
    s['page']='task';s['task_id']=t['id']
    text=f"{t['store']} · {SHIFTS[t['shift']]}\n{followups.METRICS[t['metric']]}\n{TASK_STATES[t['status']]}\nСрок: {followups.local(datetime.fromisoformat(t['due_at'])):%d.%m.%Y %H:%M}\nБаза: {t['baseline_from']} → {t['baseline_to']}\nПроверка: {t['date_from']} → {t['date_to']}"
    if not t.get('baseline'): text+='\nБаза ожидает загрузки и сверки.'
    result=t.get('result') or {}
    if result.get('conclusion'): text+=f"\n{result['conclusion']} · {followups.fmt(result['baseline'])} → {followups.fmt(result['current'])}"
    elif result.get('status')=='INSUFFICIENT_DATA': text+='\nИтог не рассчитан: недостаточно данных.'
    buttons=[]
    if t['status'] in ('PENDING','WAITING','PROCESSING'): buttons.append(('⏸ Пауза','taskact','pause'))
    if t['status']=='PAUSED': buttons.append(('▶️ Возобновить','taskact','resume'))
    if t['status'] in ('PENDING','WAITING','PROCESSING','PAUSED'): buttons.append(('❌ Отменить','cancelask',''))
    await show(message,key,s,text,buttons)


async def scopes(message,key,s,page=0):
    s.pop('input',None)
    stores=(await period_reports.stores())['stores'];page=max(0,page)
    s['page']='scope';s['store_names']={r['point_id']:r['name'] for r in stores}
    buttons=[]
    if not s.get('task_draft') and s.get('report')!='card': buttons.append(('Вся сеть без РЦ','point','network'))
    buttons += [(r['name'],'point',r['point_id']) for r in stores[page*8:page*8+8]]
    if page: buttons.append(('◀️ Предыдущие магазины','storespage',str(page-1)))
    if (page+1)*8<len(stores): buttons.append(('▶️ Следующие магазины','storespage',str(page+1)))
    await show(message,key,s,'Выберите магазин',buttons)


async def after_scope(message,key,s):
    s['shift']='';s['query']='';s['offset']=0;s['compare']=False
    if s.get('task_draft') or s['report'] in ('averages','quantities','modifiers','basket'):
        s['page']='shift'
        return await show(message,key,s,'Выберите смену',[(label,'shift',v or 'ALL') for v,label in SHIFTS.items()])
    if s['report']=='clients':
        s['page']='segment'
        return await show(message,key,s,'Клиенты по давности последней покупки',[(v+' дней','segment',v) for v in ('0-29','30-59','60-89','90+')]+[('Все клиенты','segment','all')])
    return await periods(message,key,s)


async def after_shift(message,key,s):
    if s.get('task_draft'):
        s['page']='metric'
        return await show(message,key,s,'Что проверить?',[(label,'metric',v) for v,label in followups.METRICS.items()])
    if s['report'] in ('quantities','modifiers'):
        s['input']='query';s['page']='query'
        return await show(message,key,s,'Напишите название товара или модификатора для поиска.',[('Все товары / модификаторы','queryall','')])
    return await periods(message,key,s)


async def periods(message,key,s):
    s.pop('input',None);s['page']='period'
    buttons=[('Последний закрытый день','period','day'),('7 закрытых дней','period','week')]
    if s['report']!='quality': buttons += [('Последний полный месяц','period','month'),('С начала месяца','period','mtd')]
    buttons.append(('📅 Свой период','custom','dates'))
    await show(message,key,s,'Выберите период · бизнес-день 08:00 → 08:00',buttons)


async def result(message,key,s,full=False):
    data=await reports.calculate(s)
    title=f"{reports.TITLES[s['report']]} · {s.get('store_name','Сеть без РЦ')}\n{s['start']} → {s['end']}"
    if s.get('shift'): title+=' · '+SHIFTS[s['shift']]
    if s.get('query'): title+='\nПоиск: '+s['query']
    if s['report']=='clients': title+=f"\nДавность: {s.get('min_days',0)}–{s.get('max_days') if s.get('max_days') is not None else '∞'} дней"
    text=reports.compact(data,title)
    if full:
        if 'metadata' in data: text=title+'\n'+reports.a.render(data)
        else: text=title+'\n'+data.get('report_text',text)
    s['page']='result';s.pop('input',None)
    buttons=[('📅 Другой период','again',''),('🔎 Подробнее','full','')]
    if s['report'] in reports.COMPARE_KINDS and not s.get('compare'): buttons.append(('↔️ Сравнить с предыдущим периодом','compare',''))
    if s['report'] in reports.CSV_KINDS: buttons.append(('📥 Скачать CSV','csv',''))
    if data.get('next_offset') is not None: buttons.append(('▶️ Следующая страница','next',str(data['next_offset'])))
    if s.get('offset',0): buttons.append(('⏮ Первая страница','next','0'))
    # Full text for 50-row services is available via Подробнее; compact is a preview.
    if len(data.get('rows',[]))>10 and not full: text+='\nПоказаны первые 10 строк страницы. Полная страница — «Подробнее».'
    from app.agent import conversations
    conversation=key.replace('assistant:menu:v1:','assistant:conversation:v1:')
    async with conversations.serial(conversation):
        memory=await conversations.load(conversation)
        scope={'point_id':s.get('point') or 'network','ambiguous':False,'last_report':reports.memory_report(s)}
        await conversations.save(conversation,memory['messages']+[
            {'role':'user','content':'Выбран отчёт кнопками: '+title},
            {'role':'assistant','content':text}],scope)
    await show(message,key,s,text,buttons)


async def draft_confirmation(message,key,s):
    s.pop('input',None);s['page']='taskconfirm'
    await show(message,key,s,f"Создать поручение?\n{s['store_name']} · {SHIFTS[s['shift']]}\n{followups.METRICS[s['metric']]}\nЧерез {s['days']} дней · критерий: положительный рост.\nБаза — предыдущие {s['days']} закрытых дней, проверка — следующие {s['days']} дней.",
               [('✅ Создать задачу','createtask','')])


async def action(message,key,s,user,op,value):
    if op=='home': return await home(message,key,s)
    if op=='back':
        if s.get('page') in ('task','cancelconfirm'): return await task_list(message,key,s,user)
        if s.get('page') in ('section','settings','tasks'): return await home(message,key,s)
        if s.get('task_draft'): s.pop('task_draft',None);return await task_list(message,key,s,user)
        return await section(message,key,s,user)
    if op=='section':
        s.clear();s['section']=value
        return await section(message,key,s,user)
    if op=='pref':
        setting,val=value.split(',');await followups.configure(actor(user,message),setting,val)
        return await settings(message,key,s,user)
    if op=='time':
        s['input']='time';s['page']='time'
        return await show(message,key,s,'Напишите время обзора: ЧЧ:ММ, например 10:00. Часовой пояс — Екатеринбург.',[])
    if op=='task': return await task_card(message,key,s,user,value)
    if op=='taskact':
        await followups.tasks(actor(user,message),value,s['task_id'])
        return await task_card(message,key,s,user,s['task_id'])
    if op=='cancelask':
        s['page']='cancelconfirm'
        return await show(message,key,s,'Отменить выбранное поручение?', [('Да, отменить','taskact','cancel')])
    if op=='newtask':
        followups.require_actor(actor(user,message));s['task_draft']=True;s['section']='tasks'
        return await scopes(message,key,s)
    if op=='report':
        s.pop('task_draft',None);s.pop('input',None)
        s['min_days']=0;s['max_days']=None
        if value=='status':
            from app.services.load_status import status,render
            return await show(message,key,s,render(await status()),[('🔄 Обновить','report','status')])
        if value=='morning':
            from app.services.assistant_briefs import build
            data=await build('morning');s['page']='result'
            return await show(message,key,s,reports.compact(data,'Утренний обзор'),[('🔄 Обновить','report','morning')])
        s['report']=value
        if s['section']=='network':
            s['point']='';s['store_name']='Сеть без РЦ'
            return await after_scope(message,key,s)
        return await scopes(message,key,s)
    if op=='storespage': return await scopes(message,key,s,int(value))
    if op=='point':
        if value!='network': await period_reports.point_filter(value)
        s['point']='' if value=='network' else value
        s['store_name']='Сеть без РЦ' if value=='network' else s['store_names'][value]
        return await after_scope(message,key,s)
    if op=='shift':
        s['shift']='' if value=='ALL' else value
        return await after_shift(message,key,s)
    if op=='segment':
        s['min_days']=0 if value=='all' else int(value.split('-')[0].rstrip('+'))
        s['max_days']=None if value in ('all','90+') else int(value.split('-')[1])
        return await periods(message,key,s)
    if op=='queryall': s['query']='';return await periods(message,key,s)
    if op=='metric':
        s['metric']=value;s['page']='days'
        return await show(message,key,s,'Через сколько дней проверить?',[(f'Через {n} дней','days',str(n)) for n in (7,14,30)]+[('Другой срок','custom','days')])
    if op=='days': s['days']=int(value);return await draft_confirmation(message,key,s)
    if op=='createtask':
        task=await followups.create_task(actor(user,message),s['point'],s['shift'],s['metric'],s['days'])
        s.pop('task_draft',None)
        if not task['delivery_enabled']: await message.answer('Задача сохранена, но отправка выключена. Включите личные уведомления и результаты задач в разделе «Уведомления».')
        return await task_card(message,key,s,user,task['id'])
    if op=='custom':
        s['input']=value;s['page']='input'
        return await show(message,key,s,'Напишите две даты: 2026-10-01 2026-10-31' if value=='dates' else 'Напишите срок: целое число от 1 до 90 дней.',[])
    if op=='period':
        s['start'],s['end']=reports.period(value);s['offset']=0
        return await result(message,key,s)
    if op=='again': s['offset']=0;s['compare']=False;return await periods(message,key,s)
    if op=='compare': s['compare']=True;s['offset']=0;return await result(message,key,s)
    if op=='next': s['offset']=int(value);return await result(message,key,s,full=True)
    if op=='full': return await result(message,key,s,full=True)
    if op=='csv':
        content,name,count=await reports.export(s)
        await message.answer_document(BufferedInputFile(content,filename=name),caption=f'Полная выборка: {count} строк.')
        # Renew buttons without recomputing the report.
        return await show(message,key,s,'CSV отправлен с выбранными фильтрами.',[('Вернуться к отчёту','full','')])
    raise ValueError('Неизвестная кнопка. Откройте главное меню.')


@router.message(Command('start','menu'))
@router.message(F.text=='☰ Меню')
async def open_menu(message):
    if not allowed(message.from_user): return await message.answer('Доступ не разрешён.')
    key=session(message.from_user,message)
    async with serial(key):
        await message.answer('Меню доступно кнопкой внизу. Свободные вопросы тоже работают.',
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='☰ Меню')]],resize_keyboard=True))
        await home(message,key,{})


def valid_callback(state,data):
    parts=data.split(':',3)
    return len(parts)==4 and parts[0]=='ui' and parts[1]==state.get('nonce') and parts[2:] in state.get('actions',[])


@router.callback_query(F.data.startswith('ui:'))
async def click(callback):
    if not allowed(callback.from_user) or not callback.message or not hasattr(callback.message,'answer'):
        return await callback.answer('Доступ не разрешён.',show_alert=True)
    key=session(callback.from_user,callback.message)
    async with serial(key):
        s=await load(key)
        if not valid_callback(s,callback.data): return await callback.answer('Это меню устарело или открыто другим пользователем. Нажмите «☰ Меню».',show_alert=True)
        await callback.answer()
        _,_,op,value=callback.data.split(':',3)
        # Consume the old keyboard before executing a potentially slow operation.
        s['nonce']=uuid4().hex[:10];await save(key,s)
        try: await action(callback.message,key,s,callback.from_user,op,value)
        except ValueError as exc:
            await show(callback.message,key,s,str(exc),[])
        except Exception:
            logger.exception('Button operation failed')
            await show(callback.message,key,s,'Не удалось выполнить действие. Проверьте состояние через «Данные» или повторите позже.',[])


class MenuInput(BaseFilter):
    async def __call__(self,message):
        if not allowed(message.from_user) or not message.text or message.text.startswith('/') or message.text=='☰ Меню': return False
        return bool((await load(session(message.from_user,message))).get('input'))


@router.message(MenuInput())
async def input_text(message):
    key=session(message.from_user,message)
    async with serial(key):
        s=await load(key);field=s.get('input');text=message.text.strip()
        try:
            if field=='time':
                await followups.configure(actor(message.from_user,message),'time',text);s.pop('input',None)
                return await settings(message,key,s,message.from_user)
            if field=='days':
                if not text.isdecimal() or not 1<=int(text)<=90: raise ValueError('Введите число дней от 1 до 90.')
                s['days']=int(text);return await draft_confirmation(message,key,s)
            if field=='query':
                if not text or len(text)>200: raise ValueError('Название должно содержать от 1 до 200 символов.')
                s['query']=text;return await periods(message,key,s)
            if field=='dates':
                parts=text.split()
                if len(parts)!=2: raise ValueError('Формат: 2026-10-01 2026-10-31')
                period_reports.period(*parts)
                s['start'],s['end']=parts;s['offset']=0;s.pop('input',None)
                return await result(message,key,s)
            await home(message,key,s)
        except ValueError as exc: await show(message,key,s,str(exc),[])
        except Exception:
            logger.exception('Menu input failed');await show(message,key,s,'Не удалось выполнить действие. Вернитесь назад и повторите.',[])
