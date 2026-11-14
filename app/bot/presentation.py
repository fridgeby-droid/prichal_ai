"""Common Telegram HTML renderer. Source text is never trusted HTML."""
import html
import re
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import SendMessage,EditMessageText
from app.services.report_presentation import clean_message


def inline(text):
    # A deliberately small Markdown subset; escape all data and attribute text.
    pattern=r'(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__)'
    parts=re.split(pattern,text)
    out=[]
    for part in parts:
        if part.startswith('`') and part.endswith('`') and len(part)>2:
            out.append('<code>'+html.escape(part[1:-1])+'</code>')
        elif ((part.startswith('**') and part.endswith('**')) or
              (part.startswith('__') and part.endswith('__'))) and len(part)>4:
            out.append('<b>'+html.escape(part[2:-2])+'</b>')
        else: out.append(html.escape(part))
    return ''.join(out)


def cells(line):
    return [s.strip() for s in line.strip().strip('|').split('|')]


def telegram_chunks(text,limit=3500):
    lines=clean_message(text).splitlines()
    blocks=[]; table=[]; code=False
    def flush_table():
        if not table: return
        header=cells(table[0])
        for line in table[1:]:
            values=cells(line)
            if all(re.fullmatch(r':?-{2,}:?',v) for v in values): continue
            if len(values)!=len(header):
                blocks.append(line);continue
            numbered=header[0] in ('№','#') and len(header)>1
            title=(values[0]+'. '+values[1]) if numbered else values[0]
            start=2 if numbered else 1
            blocks.append('**'+title.replace('**','')+'**')
            blocks.append(' · '.join(f'{header[i]}: {values[i]}' for i in range(start,len(header))))
            blocks.append('')
        if len(table)==1: blocks.append(table[0])
        table.clear()
    for line in lines:
        if line.strip().startswith('```'):
            flush_table(); code=not code;continue
        if code:
            # Render each code line independently so splitting remains valid.
            blocks.append(('code',line));continue
        if line.strip().startswith('|') and line.count('|')>=2:
            table.append(line);continue
        flush_table()
        if re.match(r'^\s*[-*_]{3,}\s*$',line): continue
        line=re.sub(r'^#{1,6}\s+(.+)$',r'**\1**',line)
        line=re.sub(r'^\s*[-*]\s+', '• ',line)
        blocks.append(line)
    flush_table()
    result=[]; current=''
    for block in blocks:
        is_code=isinstance(block,tuple)
        raw=block[1] if is_code else block
        # A bounded source segment keeps escaped HTML and UTF-16 length safe,
        # even with emoji or text consisting entirely of ampersands.
        segments=[raw[i:i+500] for i in range(0,len(raw),500)] or ['']
        for segment in segments:
            rendered='<code>'+html.escape(segment)+'</code>' if is_code else inline(segment)
            if not rendered and (not current or current.endswith('\n\n')): continue
            added=('\n' if current else '')+rendered
            if len(current)+len(added)>limit:
                result.append(current.strip());current=rendered
            else: current+=added
    if current.strip(): result.append(current.strip())
    return result or ['…']


class TelegramPresentation(BaseRequestMiddleware):
    async def __call__(self,make_request,bot,method):
        if not isinstance(method,(SendMessage,EditMessageText)) or method.entities:
            return await make_request(bot,method)
        # Explicit preformatted messages keep their own formatting contract.
        if isinstance(method.parse_mode,str):
            return await make_request(bot,method)
        if isinstance(method,EditMessageText) and method.chat_id is None:
            return await make_request(bot,method)
        chunks=telegram_chunks(method.text)
        first=await make_request(bot,method.model_copy(update={'text':chunks[0],'parse_mode':'HTML'}))
        for chunk in chunks[1:]:
            if isinstance(method,SendMessage):
                extra=method.model_copy(update={'text':chunk,'parse_mode':'HTML','reply_markup':None})
            else:
                extra=SendMessage(chat_id=method.chat_id,text=chunk,parse_mode='HTML',
                    business_connection_id=method.business_connection_id,
                    message_thread_id=getattr(first,'message_thread_id',None))
            await make_request(bot,extra)
        return first
