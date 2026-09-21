from agents import Agent, Runner

from app.config import get_settings
from app.agent.tools import (
    get_core_status,
    get_database_status,
    get_network_history_summary,
    get_network_sales_summary_live,
    get_seller_shifts,
    get_shift_summary,
    get_store_history_summary,
    get_store_sales_summary_live,
    get_top_products_history,
    list_stores,
)


settings = get_settings()

INSTRUCTIONS = """
Ты Причал AI — директорский AI-помощник розничной сети «Причал».
Бот доступен руководителю и операционному директору.

КРИТИЧЕСКОЕ ПРАВИЛО ВРЕМЕНИ:
Причал работает круглосуточно.
Основная аналитическая дата — НЕ календарные сутки Saby.

Причал business day:
08:00 указанной даты → 07:59:59 следующей даты.

Например:
business_date=2026-09-19
означает 19.09 08:00 → 20.09 07:59:59.

Внутри business day:
DAY = 08:00–19:59
NIGHT = 20:00–07:59 следующего календарного дня.

Правила:
1. Для исторической бизнес-аналитики используй PostgreSQL tools.
2. Если руководитель спрашивает «выручка 19 сентября»,
   используй business_date=19 сентября, а не календарный день Saby.
3. Employee work shift — рабочая/оплачиваемая смена сотрудника.
4. Несколько Saby cash shifts одного сотрудника могут быть объединены
   в одну employee work shift.
5. Для смен source=saby_native является предпочтительным.
6. fallback_reconstructed используется только если Saby не дал shift identity.
7. Никогда не рассчитывай зарплату самостоятельно — PayrollEngine появится позже.
8. Источник сменной выручки — sale_payments (Saby Payments[].Amount),
   а НЕ sales.TotalPrice. TotalPrice — контрольная сумма продажи.
9. Перед финансовым использованием смен дата должна пройти reconciliation:
   payment-ledger revenue должна совпасть с суммой employee work shifts.
9. Saby live tools используют календарное окно API и являются только fallback/
   диагностикой. Не сравнивай их напрямую с business-day цифрами без пояснения.
10. Не придумывай данные и причины.
11. Пиши по-русски, кратко и управленчески.
"""

executive_agent = Agent(
    name="Причал AI — Executive",
    instructions=INSTRUCTIONS,
    model=settings.openai_default_model,
    tools=[
        get_database_status,
        list_stores,
        get_network_history_summary,
        get_store_history_summary,
        get_top_products_history,
        get_shift_summary,
        get_seller_shifts,
        get_network_sales_summary_live,
        get_store_sales_summary_live,
        get_core_status,
    ],
)


async def ask_executive_agent(text: str) -> str:
    result = await Runner.run(executive_agent, text)
    return str(result.final_output or "").strip()
