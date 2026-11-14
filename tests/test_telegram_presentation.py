import asyncio
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiogram.methods import SendMessage,EditMessageText,SendDocument
from app.bot.presentation import telegram_chunks,TelegramPresentation


class Check(HTMLParser):
    def __init__(self): super().__init__();self.stack=[];self.text=''
    def handle_starttag(self,tag,attrs):
        assert tag in ('b','code') and not attrs
        self.stack.append(tag)
    def handle_endtag(self,tag): assert self.stack.pop()==tag
    def handle_data(self,value): self.text+=value


def test_headings_bold_lists_and_safe_data():
    text='## Батумская 5\n\n**Выручка** — 100 ₽\n\n- DAY\n\n- NIGHT\n<bad>&'
    chunks=telegram_chunks(text)
    assert len(chunks)==1 and '##' not in chunks[0] and '**' not in chunks[0]
    assert '<b>Батумская 5</b>' in chunks[0]
    assert '• Дневная смена' in chunks[0] and '• Ночная смена' in chunks[0]
    parser=Check();parser.feed(chunks[0]);assert not parser.stack
    assert '<bad>&' in parser.text


def test_table_becomes_all_product_blocks_without_lost_values():
    text='| № | Товар | Сумма | Количество |\n\n|---|---|---:|---:|\n\n| 1 | Эль | 100 ₽ | 2 л |\n\n| 2 | Пакет | 5 ₽ | 1 шт |'
    rendered='\n'.join(telegram_chunks(text))
    assert '<b>1. Эль</b>' in rendered and '<b>2. Пакет</b>' in rendered
    assert 'Сумма: 100 ₽ · Количество: 2 л' in rendered
    assert '|' not in rendered


def test_long_text_splits_with_balanced_html_and_safe_unicode():
    text=('**Товар** — 123 ₽ & <x> 😀\n'*1000)
    chunks=telegram_chunks(text)
    assert len(chunks)>1
    total=''
    for chunk in chunks:
        parser=Check();parser.feed(chunk)
        assert not parser.stack and len(parser.text.encode('utf-16-le'))//2<=4096
        total+=parser.text
    assert total.count('123 ₽')==1000


def test_code_preserves_commands():
    rendered='\n'.join(telegram_chunks('```sh\npython -m scripts.saby_data check\nshift=DAY\n```'))
    assert '<code>shift=DAY</code>' in rendered
    assert 'python -m scripts.saby_data check' in rendered


def test_send_and_edit_share_formatting_and_split():
    for method in (SendMessage(chat_id=1,text='**Тест**\n'*1500),
                   EditMessageText(chat_id=1,message_id=5,text='**Тест**\n'*1500)):
        first=SimpleNamespace(message_thread_id=7)
        send=AsyncMock(return_value=first)
        assert asyncio.run(TelegramPresentation()(send,None,method)) is first
        assert send.await_count>1
        calls=[c.args[1] for c in send.await_args_list]
        assert all(c.parse_mode=='HTML' for c in calls)
        assert all(isinstance(c,SendMessage) for c in calls[1:])
        if isinstance(method,EditMessageText): assert calls[1].message_thread_id==7


def test_explicit_html_and_non_text_methods_not_reformatted():
    send=AsyncMock()
    method=SendMessage(chat_id=1,text='<b>Готово</b>',parse_mode='HTML')
    asyncio.run(TelegramPresentation()(send,None,method))
    assert send.await_args.args[1] is method
