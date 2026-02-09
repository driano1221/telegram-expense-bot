import os
import json
import asyncio
import logging
from collections import defaultdict
from io import BytesIO
from datetime import datetime, timedelta
import pytz
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
import httpx
import matplotlib.pyplot as plt

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
    totals_by_category,
    totals_overall,
    daily_totals_last_n_days,
    list_users_with_expenses,
    get_chat_id_for_user,
)


logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TZ = pytz.timezone("America/Sao_Paulo")

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
Você é um extrator de despesas em português do Brasil.
Dada uma mensagem, devolva APENAS um JSON válido (sem texto extra) com:
{
  "amount": number | null,
  "currency": "BRL",
  "category": "alimentacao"|"transporte"|"saude"|"lazer"|"casa"|"outros",
  "description": string,
  "confidence": number
}
Regras:
- Se não houver gasto claro, amount=null, category="outros" e confidence baixa.
- description deve ser curta (2 a 6 palavras).
- currency sempre "BRL".
"""

def now_local() -> datetime:
    return datetime.now(TZ)

def day_range_local(d: datetime):
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end

def week_range_local(d: datetime):
    # Semana começando na segunda (0)
    start = d.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=d.weekday())
    end = start + timedelta(days=7)
    return start, end

def format_brl(amount) -> str:
    try:
        amount_f = float(amount)
        return f"R$ {amount_f:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return f"R$ {amount}"

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

def format_reply(obj: dict) -> str:
    amount = obj.get("amount")
    category = obj.get("category", "outros")
    desc = (obj.get("description") or "").strip() or "Gasto"
    conf = float(obj.get("confidence") or 0)

    if amount is None:
        return f"Não entendi como gasto 😅\nTenta: `gastei 50 no uber` (conf={conf:.2f})"

    return f"✅ {format_brl(amount)} em *{category}* — {desc}"

async def safe_send_markdown(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    for attempt, delay in enumerate([1, 2, 4], start=1):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
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
    lines.append(f"📅 *Hoje* ({d_start.strftime('%d/%m')}): {format_brl(day_total)} em {day_n} gasto(s)")
    if day_rows:
        for cat, total, n in day_rows[:8]:
            lines.append(f"  • {cat}: {format_brl(total)} ({n})")
    else:
        lines.append("  • (sem gastos hoje)")

    lines.append("")
    lines.append(f"🗓️ *Semana* (desde {w_start.strftime('%d/%m')}): {format_brl(week_total)} em {week_n} gasto(s)")
    if week_rows:
        for cat, total, n in week_rows[:8]:
            lines.append(f"  • {cat}: {format_brl(total)} ({n})")
    else:
        lines.append("  • (sem gastos na semana)")

    return "\n".join(lines)

def build_daily_chart_png(user_id: str, days: int = 30) -> bytes:
    end = now_local()
    start = (end - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

    rows = daily_totals_last_n_days(user_id, days=days + 5, start_dt=start, end_dt=end)

    # mapa dia -> total
    totals_by_day = {r[0].date(): float(r[1] or 0) for r in rows}

    # série completa (linha contínua)
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

    # --------- escolhe escala Y automaticamente ----------
    positives = sorted([v for v in y_all if v > 0])
    use_symlog = False
    linthresh = 10  # até R$10 fica "linear" (ajuste se quiser)
    if positives:
        # Se o maior for muito maior que a mediana, tem "pico"
        median = positives[len(positives) // 2]
        vmax = positives[-1]
        if median > 0 and (vmax / median) >= 8:
            use_symlog = True

    fig, ax = plt.subplots(figsize=(12, 4.5))

    # Linha
    ax.plot(x_all, y_all, linewidth=2)

    # Marcadores só onde tem gasto
    if x_pts:
        ax.plot(x_pts, y_pts, linestyle="None", marker="o", markersize=5)

    # Formatação BRL no eixo Y
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, pos: format_brl(x)))

    ax.set_title(f"Gastos por dia (últimos {days} dias)")
    ax.set_xlabel("Dia")
    ax.set_ylabel("Valor (R$)")
    ax.grid(True, axis="y", linestyle="--", linewidth=0.7, alpha=0.6)

    # Eixo X: datas legíveis (sem colar)
    locator = mdates.AutoDateLocator(minticks=5, maxticks=8)
    formatter = mdates.ConciseDateFormatter(locator)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    fig.autofmt_xdate(rotation=0)

    # Escala "symlog" se houver pico (melhor visualização)
    if use_symlog:
        ax.set_yscale("symlog", linthresh=linthresh)
        ax.text(
            0.99, 0.95,
            f"Escala ajustada (symlog) p/ mostrar picos",
            transform=ax.transAxes,
            ha="right", va="top",
            fontsize=9,
        )
    else:
        # sem pico: usa limite confortável
        if positives:
            ax.set_ylim(0, positives[-1] * 1.15)
        else:
            ax.set_ylim(0, 1)

    # Rótulos: só nos pontos mais relevantes (pra não poluir)
    # - mostra no máximo 6 rótulos
    # - prioriza valores maiores
    if x_pts:
        pairs = list(zip(x_pts, y_pts))
        pairs_sorted = sorted(pairs, key=lambda t: t[1], reverse=True)
        to_label = pairs_sorted[:6]

        for xd, yd in to_label:
            ax.annotate(
                format_brl(yd),
                (xd, yd),
                textcoords="offset points",
                xytext=(0, 10),
                ha="center",
                fontsize=9,
                bbox=dict(boxstyle="round,pad=0.25", fc="white", alpha=0.8),
            )

    fig.tight_layout()

    bio = BytesIO()
    fig.savefig(bio, format="png", dpi=180)
    plt.close(fig)
    return bio.getvalue()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    msg = (
        "Olá! Me manda uma frase tipo:\n"
        "- gastei 50 no uber\n"
        "- almocei 35 reais\n"
        "- comprei remédio 120\n\n"
        "Comandos:\n"
        "/gastos — últimos 10\n"
        "/relatorio — resumo hoje + semana\n"
        "/grafico — gráfico últimos 30 dias"
    )
    await safe_send_markdown(context, update.effective_chat.id, msg)

async def gastos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    user_id = str(update.effective_user.id)
    rows = list_last_expenses(user_id=user_id, limit=10)

    if not rows:
        await safe_send_markdown(context, update.effective_chat.id, "Ainda não tem gastos salvos.")
        return

    lines = []
    for created_at, amount, currency, category, description in rows:
        dt = str(created_at)[:19].replace("T", " ")
        amt = f"{amount}".replace(".", ",") if amount is not None else "—"
        lines.append(f"{dt} — R$ {amt} — {category} — {description}")

    await safe_send_markdown(context, update.effective_chat.id, "\n".join(lines))

async def relatorio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    user_id = str(update.effective_user.id)
    text = build_report_text(user_id)
    await safe_send_markdown(context, update.effective_chat.id, text)

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
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id

    text = build_report_text(user_id)
    await safe_send_markdown(context, chat_id, "🧪 *Teste do relatório (simulando 23:00)*\n" + text)

    png = build_daily_chart_png(user_id, days=30)
    await safe_send_photo(context, chat_id, png, caption="📈 Gráfico (30 dias) — teste")

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
        await safe_send_markdown(
            context, update.effective_chat.id,
            "⏳ Calma! Limite de mensagens atingido. Tente novamente em alguns segundos.",
        )
        return

    # ── Validacao de tamanho ──
    if len(text_in) > MAX_TEXT_LENGTH:
        await safe_send_markdown(
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

            insert_expense(
                user_id=user_id,
                chat_id=chat_id,
                raw_text=text_in,
                amount=obj["amount"],
                currency=obj.get("currency") or "BRL",
                category=obj.get("category") or "outros",
                description=obj.get("description") or "",
                confidence=float(obj.get("confidence") or 0),
            )

    except httpx.HTTPStatusError as e:
        reply = f"Erro na Groq (status {e.response.status_code}).\nTrecho: {e.response.text[:300]}"
    except Exception as e:
        reply = f"Deu erro: {type(e).__name__}: {e}"

    await safe_send_markdown(context, update.effective_chat.id, reply)

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
            await safe_send_markdown(
                context,
                chat_id,
                "🕚 *Relatório automático (23:00)*\n" + text
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
    app.add_handler(CommandHandler("relatorio", relatorio))
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
