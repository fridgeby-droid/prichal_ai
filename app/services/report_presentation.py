"""Display labels only: enum values and calculations are unchanged."""
import re


def shift_label(value):
    return {'DAY':'Дневная смена','NIGHT':'Ночная смена'}.get(value,value)


def clean_message(text):
    # Preserve executable examples and technical enum values inside code fences.
    parts=re.split(r'(```[\s\S]*?```|`[^`\n]+`)',text)
    for i in range(0,len(parts),2):
        parts[i]=re.sub(r'\bDAY\b','Дневная смена',parts[i])
        parts[i]=re.sub(r'\bNIGHT\b','Ночная смена',parts[i])
        parts[i]=re.sub(r'\n[ \t]*\n(?=[ \t]*\|)', '\n',parts[i])
        parts[i]=re.sub(r'\n{3,}','\n\n',parts[i])
    return ''.join(parts).strip()
