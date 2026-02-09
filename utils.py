"""
Funcoes utilitarias puras — sem dependencias externas pesadas.
Podem ser importadas em testes sem carregar telegram/db/etc.
"""
import pytz
from datetime import datetime, timedelta

TZ = pytz.timezone("America/Sao_Paulo")

CATEGORY_EMOJI: dict[str, str] = {
    "alimentacao": "🍔",
    "transporte": "🚗",
    "saude": "💊",
    "lazer": "🎮",
    "casa": "🏠",
    "salario": "💼",
    "freelance": "💻",
    "investimento": "📈",
    "outros": "📦",
}


def now_local() -> datetime:
    return datetime.now(TZ)


def day_range_local(d: datetime) -> tuple[datetime, datetime]:
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def week_range_local(d: datetime) -> tuple[datetime, datetime]:
    start = d.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=d.weekday())
    end = start + timedelta(days=7)
    return start, end


def format_brl(amount: float | int | str) -> str:
    try:
        amount_f = float(amount)
        return f"R$ {amount_f:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return f"R$ {amount}"


def format_reply(obj: dict) -> str:
    amount = obj.get("amount")
    category = obj.get("category", "outros")
    entry_type = obj.get("type", "expense")
    desc = (obj.get("description") or "").strip() or ("Gasto" if entry_type == "expense" else "Ganho")
    emoji = CATEGORY_EMOJI.get(category, "📦")

    if amount is None:
        return (
            "😅 <b>Não entendi</b>\n\n"
            "Tenta algo como:\n"
            "  <code>gastei 50 no uber</code>\n"
            "  <code>recebi 3000 de salario</code>"
        )

    if entry_type == "income":
        return (
            f"🟢 <b>Ganho registrado!</b>\n"
            f"\n"
            f"💰 Valor: <b>{format_brl(amount)}</b>\n"
            f"{emoji} Categoria: <b>{category}</b>\n"
            f"📝 Descrição: <i>{desc}</i>"
        )

    return (
        f"🔴 <b>Gasto registrado!</b>\n"
        f"\n"
        f"💰 Valor: <b>{format_brl(amount)}</b>\n"
        f"{emoji} Categoria: <b>{category}</b>\n"
        f"📝 Descrição: <i>{desc}</i>"
    )
