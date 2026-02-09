import os
import json
import asyncio
import logging
from collections import defaultdict
from io import BytesIO
from datetime import datetime, timedelta
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
import httpx
import matplotlib.pyplot as plt
import numpy as np

from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CommandHandler, ContextTypes, filters
)
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest

# Carrega .env ANTES de importar db
load_dotenv()

from db import (
    insert_expense,
    list_last_expenses,
    list_last_entries,
    totals_by_category,
    totals_overall,
    daily_totals_last_n_days,
    monthly_balance,
    weekly_balance_last_n_weeks,
    list_users_with_expenses,
    get_chat_id_for_user,
)
from utils import (
    TZ,
    CATEGORY_EMOJI,
    now_local,
    day_range_local,
    week_range_local,
    format_brl,
    format_reply,
)


logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Faltou TELEGRAM_BOT_TOKEN no .env")
if not GROQ_API_KEY:
    raise RuntimeError("Faltou GROQ_API_KEY no .env")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "llama-3.3-70b-versatile"

# ── Rate limiting ──────────────────────────────────────────────
RATE_LIMIT_MSGS = int(os.getenv("RATE_LIMIT_MSGS", "5"))   # msgs por janela
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))  # janela em segundos
_user_timestamps: dict[int, list[float]] = defaultdict(list)

def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    timestamps = _user_timestamps[user_id]
    _user_timestamps[user_id] = [t for t in timestamps if t > window_start]
    if len(_user_timestamps[user_id]) >= RATE_LIMIT_MSGS:
        return True
    _user_timestamps[user_id].append(now)
    return False

# ── Allowlist de usuarios ─────────────────────────────────────
_allowed_env = os.getenv("ALLOWED_USERS", "").strip()
ALLOWED_USERS: set[int] | None = (
    {int(uid.strip()) for uid in _allowed_env.split(",") if uid.strip()}
    if _allowed_env else None  # None = qualquer um pode usar
)

def is_allowed(user_id: int) -> bool:
    if ALLOWED_USERS is None:
        return True
    return user_id in ALLOWED_USERS

# ── Validacao de entrada ──────────────────────────────────────
MAX_TEXT_LENGTH = 500
MAX_AMOUNT = 1_000_000  # R$ 1 milhão

SYSTEM_PROMPT = """
Você é um extrator de despesas e ganhos financeiros em português do Brasil.
Dada uma mensagem, devolva APENAS um JSON válido (sem texto extra) com:
{
  "type": "expense" | "income",
  "amount": number | null,
  "currency": "BRL",
  "category": "alimentacao"|"transporte"|"saude"|"lazer"|"casa"|"salario"|"freelance"|"investimento"|"outros",
  "description": string,
  "confidence": number
}
Regras:
- Se a mensagem indicar dinheiro RECEBIDO (salário, pagamento, venda, freelance, transferência recebida, investimento), type="income".
- Se a mensagem indicar dinheiro GASTO (compra, pagamento de conta, despesa), type="expense".
- Se não houver valor claro, amount=null, category="outros" e confidence baixa.
- description deve ser curta (2 a 6 palavras).
- currency sempre "BRL".
"""


async def extract_expense(text: str) -> dict:
    payload = {
        "model": MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.strip()},
            {"role": "user", "content": text.strip()},
        ],
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(GROQ_URL, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    content = data["choices"][0]["message"]["content"]
    return json.loads(content)

async def safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    for attempt, delay in enumerate([1, 2, 4], start=1):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            return
        except (NetworkError, TimedOut) as e:
            logger.warning("Falha ao enviar msg (tentativa %s): %s", attempt, e)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.exception("Erro inesperado ao enviar msg: %s", e)
            return

async def safe_send_photo(context: ContextTypes.DEFAULT_TYPE, chat_id: int, data: bytes, caption: str = "") -> None:
    for attempt, delay in enumerate([1, 2, 4], start=1):
        try:
            bio = BytesIO(data)
            bio.name = "grafico.png"
            await context.bot.send_photo(chat_id=chat_id, photo=bio, caption=caption)
            return
        except (NetworkError, TimedOut) as e:
            logger.warning("Falha ao enviar foto (tentativa %s): %s", attempt, e)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.exception("Erro inesperado ao enviar foto: %s", e)
            return

def build_report_text(user_id: str) -> str:
    d0 = now_local()
    d_start, d_end = day_range_local(d0)
    w_start, w_end = week_range_local(d0)

    day_total, day_n = totals_overall(user_id, d_start, d_end)
    week_total, week_n = totals_overall(user_id, w_start, w_end)

    day_rows = totals_by_category(user_id, d_start, d_end)
    week_rows = totals_by_category(user_id, w_start, w_end)

    lines = []

    # ── Hoje ──
    lines.append(f"📅 <b>Hoje</b> ({d_start.strftime('%d/%m')})")
    lines.append(f"    💰 Total: <b>{format_brl(day_total)}</b>  •  {day_n} gasto(s)")
    lines.append("")
    if day_rows:
        for cat, total, n in day_rows[:8]:
            emoji = CATEGORY_EMOJI.get(cat, "📦")
            lines.append(f"    {emoji} {cat}: <code>{format_brl(total)}</code> ({n})")
    else:
        lines.append("    <i>Nenhum gasto hoje</i>")

    lines.append("")
    lines.append("─────────────────────")
    lines.append("")

    # ── Semana ──
    lines.append(f"🗓 <b>Semana</b> (desde {w_start.strftime('%d/%m')})")
    lines.append(f"    💰 Total: <b>{format_brl(week_total)}</b>  •  {week_n} gasto(s)")
    lines.append("")
    if week_rows:
        for cat, total, n in week_rows[:8]:
            emoji = CATEGORY_EMOJI.get(cat, "📦")
            lines.append(f"    {emoji} {cat}: <code>{format_brl(total)}</code> ({n})")
    else:
        lines.append("    <i>Nenhum gasto na semana</i>")

    return "\n".join(lines)

def build_daily_chart_png(user_id: str, days: int = 30) -> bytes:
    end = now_local()
    start = (end - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

    rows = daily_totals_last_n_days(user_id, days=days + 5, start_dt=start, end_dt=end)

    # mapa dia -> total
    totals_by_day = {r[0].date(): float(r[1] or 0) for r in rows}

    # serie completa (linha continua)
    x_all = []
    y_all = []
    cur = start.date()
    while cur <= end.date():
        x_all.append(cur)
        y_all.append(totals_by_day.get(cur, 0.0))
        cur = cur + timedelta(days=1)

    # pontos com gasto (para marcadores)
    x_pts = [d for d in x_all if totals_by_day.get(d, 0.0) > 0]
    y_pts = [totals_by_day[d] for d in x_pts]

    # ── Cores e estilo ──
    COLOR_LINE = "#2563EB"       # azul moderno
    COLOR_FILL = "#2563EB"
    COLOR_DOT = "#1D4ED8"
    COLOR_GRID = "#E5E7EB"
    COLOR_TEXT = "#374151"
    COLOR_LABEL_BG = "#F0F4FF"
    BG_COLOR = "#FAFBFC"

    fig, ax = plt.subplots(figsize=(12, 5))
    fig.set_facecolor(BG_COLOR)
    ax.set_facecolor(BG_COLOR)

    # ── Linha principal com area preenchida ──
    ax.plot(x_all, y_all, linewidth=2.5, color=COLOR_LINE, zorder=3)
    ax.fill_between(x_all, y_all, alpha=0.08, color=COLOR_FILL, zorder=2)

    # ── Marcadores elegantes so onde tem gasto ──
    if x_pts:
        ax.scatter(x_pts, y_pts, s=30, color=COLOR_DOT, zorder=4, edgecolors="white", linewidths=1.5)

    # ── Eixo Y: formato BRL ──
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, pos: format_brl(x)))

    # ── Grid sutil apenas horizontal ──
    ax.grid(True, axis="y", linestyle="-", linewidth=0.5, color=COLOR_GRID, alpha=0.8)
    ax.grid(False, axis="x")

    # ── Remover bordas (spines) exceto inferior ──
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color(COLOR_GRID)
    ax.spines["bottom"].set_linewidth(0.8)

    # ── Ticks limpos ──
    ax.tick_params(axis="both", which="both", length=0, labelcolor=COLOR_TEXT, labelsize=9)

    # ── Titulo minimalista ──
    ax.set_title(
        f"Gastos diarios — ultimos {days} dias",
        fontsize=14, fontweight="bold", color=COLOR_TEXT,
        pad=16, loc="left",
    )

    # ── Eixo X: mostrar mais datas ──
    locator = mdates.AutoDateLocator(minticks=6, maxticks=15)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    fig.autofmt_xdate(rotation=45, ha="right")

    # ── Escala Y ──
    positives = sorted([v for v in y_all if v > 0])
    if positives:
        median = positives[len(positives) // 2]
        vmax = positives[-1]
        if median > 0 and (vmax / median) >= 8:
            ax.set_yscale("symlog", linthresh=10)
        else:
            ax.set_ylim(0, vmax * 1.2)
    else:
        ax.set_ylim(0, 1)

    # ── Rotulos nos top 5 valores ──
    if x_pts:
        pairs = list(zip(x_pts, y_pts))
        pairs_sorted = sorted(pairs, key=lambda t: t[1], reverse=True)
        to_label = pairs_sorted[:5]

        for xd, yd in to_label:
            ax.annotate(
                format_brl(yd),
                (xd, yd),
                textcoords="offset points",
                xytext=(0, 12),
                ha="center",
                fontsize=8,
                fontweight="bold",
                color=COLOR_TEXT,
                bbox=dict(
                    boxstyle="round,pad=0.3",
                    fc=COLOR_LABEL_BG,
                    ec=COLOR_LINE,
                    linewidth=0.6,
                    alpha=0.9,
                ),
            )

    # ── Resumo no rodape ──
    total = sum(y_all)
    dias_com_gasto = len([v for v in y_all if v > 0])
    media = total / dias_com_gasto if dias_com_gasto > 0 else 0
    maior = max(y_all) if y_all else 0

    resumo = (
        f"Total: {format_brl(total)}"
        f"   |   Media/dia: {format_brl(media)}"
        f"   |   Maior gasto: {format_brl(maior)}"
    )
    fig.text(
        0.5, 0.01, resumo,
        ha="center", fontsize=9, color=COLOR_TEXT, alpha=0.7,
        style="italic",
    )

    fig.tight_layout()
    fig.subplots_adjust(bottom=0.13)

    bio = BytesIO()
    fig.savefig(bio, format="png", dpi=180, facecolor=BG_COLOR, edgecolor="none")
    plt.close(fig)
    return bio.getvalue()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    msg = (
        "👋 <b>Olá! Eu sou seu bot de finanças.</b>\n"
        "\n"
        "Me manda uma frase tipo:\n"
        "  <code>gastei 50 no uber</code>\n"
        "  <code>almocei 35 reais</code>\n"
        "  <code>recebi 3000 de salario</code>\n"
        "\n"
        "📋 <b>Comandos:</b>\n"
        "  /gastos — últimos 10 gastos\n"
        "  /ganhos — últimos 10 ganhos\n"
        "  /relatorio — resumo hoje + semana\n"
        "  /saldo — saldo do mês\n"
        "  /grafico — gráfico de gastos (30 dias)\n"
        "  /balanco — gráfico gastos x ganhos"
    )
    await safe_send(context, update.effective_chat.id, msg)

async def gastos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    user_id = str(update.effective_user.id)
    rows = list_last_expenses(user_id=user_id, limit=10)

    if not rows:
        await safe_send(context, update.effective_chat.id, "📭 <i>Nenhum gasto registrado ainda.</i>")
        return

    lines = ["📋 <b>Últimos gastos</b>\n"]
    for i, (created_at, amount, currency, category, description) in enumerate(rows, 1):
        dt = str(created_at)[:16].replace("T", " ")
        emoji = CATEGORY_EMOJI.get(category, "📦")
        lines.append(
            f"{i}. {emoji} <b>{format_brl(amount)}</b> — {category}\n"
            f"     <i>{description}</i>\n"
            f"     🕐 <code>{dt}</code>"
        )
        if i < len(rows):
            lines.append("")

    await safe_send(context, update.effective_chat.id, "\n".join(lines))

async def relatorio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    user_id = str(update.effective_user.id)
    text = build_report_text(user_id)
    await safe_send(context, update.effective_chat.id, text)

async def grafico(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    user_id = str(update.effective_user.id)
    png = build_daily_chart_png(user_id, days=30)
    await safe_send_photo(context, update.effective_chat.id, png, caption="📈 Gastos por dia (30 dias)")

async def teste23(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Dispara manualmente o mesmo relatório automático das 23:00 (pra teste).
    """
    if not is_allowed(update.effective_user.id):
        return
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id

    text = build_report_text(user_id)
    await safe_send(context, chat_id, "🧪 <b>Teste do relatório (simulando 23:00)</b>\n\n" + text)

    png = build_daily_chart_png(user_id, days=30)
    await safe_send_photo(context, chat_id, png, caption="📈 Gráfico (30 dias) — teste")

async def ganhos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    user_id = str(update.effective_user.id)
    rows = list_last_entries(user_id, entry_type="income", limit=10)

    if not rows:
        await safe_send(context, update.effective_chat.id, "📭 <i>Nenhum ganho registrado ainda.</i>")
        return

    lines = ["💚 <b>Últimos ganhos</b>\n"]
    for i, (created_at, amount, currency, category, description) in enumerate(rows, 1):
        dt = str(created_at)[:16].replace("T", " ")
        emoji = CATEGORY_EMOJI.get(category, "📦")
        lines.append(
            f"{i}. {emoji} <b>{format_brl(amount)}</b> — {category}\n"
            f"     <i>{description}</i>\n"
            f"     🕐 <code>{dt}</code>"
        )
        if i < len(rows):
            lines.append("")

    await safe_send(context, update.effective_chat.id, "\n".join(lines))


async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    user_id = str(update.effective_user.id)

    d0 = now_local()
    # Mes atual: dia 1 ate agora
    m_start = d0.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    m_end = d0 + timedelta(days=1)

    total_gastos, n_gastos, total_ganhos, n_ganhos = monthly_balance(user_id, m_start, m_end)
    saldo_val = float(total_ganhos) - float(total_gastos)

    if saldo_val >= 0:
        saldo_icon = "🟢"
        saldo_label = "positivo"
    else:
        saldo_icon = "🔴"
        saldo_label = "negativo"

    mes_nome = d0.strftime("%B/%Y").capitalize()

    text = (
        f"💰 <b>Saldo de {mes_nome}</b>\n"
        f"\n"
        f"🟢 Ganhos: <b>{format_brl(total_ganhos)}</b>  ({n_ganhos} registro(s))\n"
        f"🔴 Gastos: <b>{format_brl(total_gastos)}</b>  ({n_gastos} registro(s))\n"
        f"\n"
        f"─────────────────────\n"
        f"\n"
        f"{saldo_icon} Saldo: <b>{format_brl(abs(saldo_val))}</b> {saldo_label}"
    )
    await safe_send(context, update.effective_chat.id, text)


def build_balance_chart_png(user_id: str, weeks: int = 8) -> bytes:
    """Grafico de barras: gastos x ganhos por semana + linha de saldo."""
    end = now_local()
    start = (end - timedelta(weeks=weeks)).replace(hour=0, minute=0, second=0, microsecond=0)

    rows = weekly_balance_last_n_weeks(user_id, weeks=weeks + 2, start_dt=start, end_dt=end)

    week_labels = []
    expenses = []
    incomes = []
    for row in rows:
        week_labels.append(row[0].strftime("%d/%m"))
        expenses.append(float(row[1] or 0))
        incomes.append(float(row[2] or 0))

    # Cores
    COLOR_EXPENSE = "#EF4444"
    COLOR_INCOME = "#22C55E"
    COLOR_BALANCE = "#2563EB"
    COLOR_GRID = "#E5E7EB"
    COLOR_TEXT = "#374151"
    BG_COLOR = "#FAFBFC"

    fig, ax = plt.subplots(figsize=(12, 5))
    fig.set_facecolor(BG_COLOR)
    ax.set_facecolor(BG_COLOR)

    if not week_labels:
        ax.text(0.5, 0.5, "Sem dados ainda", ha="center", va="center",
                fontsize=14, color=COLOR_TEXT, transform=ax.transAxes)
        bio = BytesIO()
        fig.savefig(bio, format="png", dpi=180, facecolor=BG_COLOR)
        plt.close(fig)
        return bio.getvalue()

    x = np.arange(len(week_labels))
    width = 0.35

    # Barras
    bars_exp = ax.bar(x - width/2, expenses, width, label="Gastos", color=COLOR_EXPENSE, alpha=0.85, zorder=3)
    bars_inc = ax.bar(x + width/2, incomes, width, label="Ganhos", color=COLOR_INCOME, alpha=0.85, zorder=3)

    # Linha de saldo
    balances = [inc - exp for inc, exp in zip(incomes, expenses)]
    ax.plot(x, balances, color=COLOR_BALANCE, linewidth=2.5, marker="o", markersize=6,
            label="Saldo", zorder=4, markeredgecolor="white", markeredgewidth=1.5)

    # Rotulos nas barras
    for bar in bars_exp:
        h = bar.get_height()
        if h > 0:
            ax.text(bar.get_x() + bar.get_width()/2, h, format_brl(h),
                    ha="center", va="bottom", fontsize=7, color=COLOR_EXPENSE, fontweight="bold")

    for bar in bars_inc:
        h = bar.get_height()
        if h > 0:
            ax.text(bar.get_x() + bar.get_width()/2, h, format_brl(h),
                    ha="center", va="bottom", fontsize=7, color=COLOR_INCOME, fontweight="bold")

    # Rotulos de saldo
    for i, bal in enumerate(balances):
        offset = 12 if bal >= 0 else -12
        ax.annotate(
            format_brl(bal),
            (x[i], bal),
            textcoords="offset points", xytext=(0, offset),
            ha="center", fontsize=7, fontweight="bold", color=COLOR_BALANCE,
        )

    # Estilo
    ax.set_xticks(x)
    ax.set_xticklabels([f"Sem.\n{l}" for l in week_labels], fontsize=8)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, pos: format_brl(v)))
    ax.axhline(y=0, color=COLOR_GRID, linewidth=1, zorder=1)
    ax.grid(True, axis="y", linestyle="-", linewidth=0.5, color=COLOR_GRID, alpha=0.8)
    ax.grid(False, axis="x")

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color(COLOR_GRID)
    ax.tick_params(axis="both", which="both", length=0, labelcolor=COLOR_TEXT, labelsize=9)

    ax.set_title(
        f"Gastos x Ganhos — ultimas {weeks} semanas",
        fontsize=14, fontweight="bold", color=COLOR_TEXT, pad=16, loc="left",
    )

    ax.legend(loc="upper left", frameon=False, fontsize=10)

    # Resumo
    total_exp = sum(expenses)
    total_inc = sum(incomes)
    saldo_total = total_inc - total_exp
    resumo = f"Total gastos: {format_brl(total_exp)}   |   Total ganhos: {format_brl(total_inc)}   |   Saldo: {format_brl(saldo_total)}"
    fig.text(0.5, 0.01, resumo, ha="center", fontsize=9, color=COLOR_TEXT, alpha=0.7, style="italic")

    fig.tight_layout()
    fig.subplots_adjust(bottom=0.15)

    bio = BytesIO()
    fig.savefig(bio, format="png", dpi=180, facecolor=BG_COLOR, edgecolor="none")
    plt.close(fig)
    return bio.getvalue()


async def balanco(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    user_id = str(update.effective_user.id)
    png = build_balance_chart_png(user_id, weeks=8)
    await safe_send_photo(context, update.effective_chat.id, png, caption="📊 Gastos x Ganhos (8 semanas)")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text_in = update.message.text or ""
    if not text_in.strip():
        return

    uid = update.effective_user.id

    # ── Allowlist ──
    if not is_allowed(uid):
        return  # ignora silenciosamente

    # ── Rate limiting ──
    if is_rate_limited(uid):
        await safe_send(
            context, update.effective_chat.id,
            "⏳ Calma! Limite de mensagens atingido. Tente novamente em alguns segundos.",
        )
        return

    # ── Validacao de tamanho ──
    if len(text_in) > MAX_TEXT_LENGTH:
        await safe_send(
            context, update.effective_chat.id,
            f"Mensagem muito longa ({len(text_in)} chars). Máximo: {MAX_TEXT_LENGTH}.",
        )
        return

    try:
        obj = await extract_expense(text_in)

        # ── Validacao do amount retornado pela LLM ──
        amount = obj.get("amount")
        if amount is not None:
            try:
                amount = float(amount)
                if amount <= 0 or amount > MAX_AMOUNT:
                    obj["amount"] = None
                    obj["confidence"] = 0
                else:
                    obj["amount"] = amount
            except (ValueError, TypeError):
                obj["amount"] = None
                obj["confidence"] = 0

        reply = format_reply(obj)

        if obj.get("amount") is not None:
            user_id = str(uid)
            chat_id = str(update.effective_chat.id)

            entry_type = obj.get("type", "expense")
            if entry_type not in ("expense", "income"):
                entry_type = "expense"

            insert_expense(
                user_id=user_id,
                chat_id=chat_id,
                raw_text=text_in,
                amount=obj["amount"],
                currency=obj.get("currency") or "BRL",
                category=obj.get("category") or "outros",
                description=obj.get("description") or "",
                confidence=float(obj.get("confidence") or 0),
                entry_type=entry_type,
            )

    except httpx.HTTPStatusError as e:
        reply = f"Erro na Groq (status {e.response.status_code}).\nTrecho: {e.response.text[:300]}"
    except Exception as e:
        reply = f"Deu erro: {type(e).__name__}: {e}"

    await safe_send(context, update.effective_chat.id, reply)

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Erro no handler: %s", context.error)

async def scheduled_23h(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Envia automaticamente às 23:00 um resumo do dia + semana.
    (Para cada user_id que já tenha chat_id salvo)
    """
    user_ids = list_users_with_expenses(only_with_chat_id=True)

    for uid in user_ids:
        try:
            chat_id = get_chat_id_for_user(uid)
            if not chat_id:
                logger.warning("Usuário %s sem chat_id salvo. Pulando.", uid)
                continue

            text = build_report_text(uid)
            await safe_send(
                context,
                chat_id,
                "🕚 <b>Relatório automático (23:00)</b>\n\n" + text
            )

            # opcional: manda gráfico também
            png = build_daily_chart_png(uid, days=30)
            await safe_send_photo(context, chat_id, png, caption="📈 Gráfico (30 dias)")

        except Exception as e:
            logger.exception("Falha ao enviar relatório automático para %s: %s", uid, e)


def build_app() -> Application:
    request = HTTPXRequest(
        http_version="1.1",
        connect_timeout=20,
        read_timeout=20,
        write_timeout=20,
        pool_timeout=20,
    )
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gastos", gastos))
    app.add_handler(CommandHandler("ganhos", ganhos))
    app.add_handler(CommandHandler("relatorio", relatorio))
    app.add_handler(CommandHandler("saldo", saldo))
    app.add_handler(CommandHandler("balanco", balanco))
    app.add_handler(CommandHandler("grafico", grafico))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CommandHandler("teste23", teste23))

    app.add_error_handler(on_error)

    # Agenda job diário às 23:00 no fuso de SP
    from datetime import time as dt_time
    app.job_queue.run_daily(
        scheduled_23h,
        time=dt_time(hour=23, minute=0, second=0, tzinfo=TZ),
        name="relatorio_23h",
    )

    return app

def start_health_server():
    port = int(os.getenv("PORT", "8080"))

    class Handler(BaseHTTPRequestHandler):
        def _send_ok_headers(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()

        def do_HEAD(self):
            if self.path in ("/", "/healthz"):
                self._send_ok_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def do_GET(self):
            if self.path in ("/", "/healthz"):
                self._send_ok_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            return  # silencia log

    server = HTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"Health server em http://0.0.0.0:{port}/healthz")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    start_health_server()
    backoffs = [2, 5, 10, 20]
    i = 0

    while True:
        try:
            # Garante event loop no Python 3.11 (Windows)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            app = build_app()
            print("Bot rodando via polling... (CTRL+C para parar)")

            # Deixa o PTB gerenciar o loop e fechar corretamente ao sair
            app.run_polling(close_loop=True)
            return

        except NetworkError as e:
            wait = backoffs[min(i, len(backoffs) - 1)]
            i += 1
            logger.warning("Falha de rede ao iniciar. Tentando de novo em %ss: %s", wait, e)

            # NÃO usar asyncio.run aqui (pra não bagunçar o loop)
            time.sleep(wait)

        except Exception as e:
            logger.exception("Erro fatal ao iniciar: %s", e)
            raise


if __name__ == "__main__":
    main()
