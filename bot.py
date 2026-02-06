import os
import json
import asyncio
import logging
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
    list_users_with_expenses
)  # noqa: E402

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
    start = end - timedelta(days=days)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)

    rows = daily_totals_last_n_days(user_id, days=days + 2, start_dt=start, end_dt=end)

    # mapa dia -> total
    totals_by_day = {r[0].date(): float(r[1] or 0) for r in rows}

    # série completa (para linha contínua)
    x_all = []
    y_all = []
    cur = start.date()
    while cur <= end.date():
        x_all.append(cur)
        y_all.append(totals_by_day.get(cur, 0.0))
        cur = cur + timedelta(days=1)

    # apenas dias com gasto (para marcadores e rótulos)
    x_pts = [d for d in x_all if totals_by_day.get(d, 0.0) > 0]
    y_pts = [totals_by_day[d] for d in x_pts]

    def brl(v: float) -> str:
        s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {s}"

    # ---- gráfico ----
    fig, ax = plt.subplots(figsize=(10, 4))

    # Linha completa (inclui zeros)
    ax.plot(x_all, y_all, marker=None)

    # Marcadores só nos dias com gasto
    if x_pts:
        ax.plot(x_pts, y_pts, linestyle="None", marker="o")

        # rótulos nos pontos
        for xd, yd in zip(x_pts, y_pts):
            ax.annotate(
                brl(yd),
                (xd, yd),
                textcoords="offset points",
                xytext=(0, 8),
                ha="center",
                fontsize=9,
            )

    ax.set_title(f"Gastos por dia (últimos {days} dias)")
    ax.set_xlabel("Dia (dd/mm)")
    ax.set_ylabel("Valor (R$)")
    ax.grid(True, axis="y", linestyle="--", linewidth=0.7, alpha=0.6)

    # X em PT-BR: ticks semanais (ou ~6-8 marcas)
    # Se days=30 => a cada 5 dias fica bom
    step = 5 if days >= 25 else 2
    tick_dates = x_all[::step]
    ax.set_xticks(tick_dates)
    ax.set_xticklabels([d.strftime("%d/%m") for d in tick_dates], rotation=0)

    # ------ Melhor visualização com pico ------
    # Se existe 1 pico muito alto, o resto some.
    # Estratégia: limitar o topo do eixo para mostrar o "miolo"
    # e ainda não cortar o pico: colocamos um teto baseado em percentil.
    USE_LOG = False  # se quiser, troca pra True

    if USE_LOG:
        ax.set_yscale("log")
        # evita log(0): soma 1 centavo pra zeros
        # (se quiser, eu te mando a versão log bem certinha)
    else:
        positives = [v for v in y_all if v > 0]
        if positives:
            positives_sorted = sorted(positives)
            p90 = positives_sorted[int(0.90 * (len(positives_sorted) - 1))]
            ymax = max(p90 * 1.8, max(positives) * 0.6)
            # se não tiver outlier, ymax vira o max normal
            if ymax < max(positives) * 0.95:
                # tem outlier forte; usa o zoom (miolo)
                ax.set_ylim(0, ymax)

                # avisa no gráfico que existe pico maior
                ax.text(
                    0.99, 0.95,
                    "Obs.: eixo Y ajustado (há pico acima do limite)",
                    transform=ax.transAxes,
                    ha="right",
                    va="top",
                    fontsize=9,
                )
            else:
                ax.set_ylim(0, max(positives) * 1.15)
        else:
            ax.set_ylim(0, 1)

    fig.tight_layout()

    bio = BytesIO()
    fig.savefig(bio, format="png", dpi=180)
    plt.close(fig)
    return bio.getvalue()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    user_id = str(update.effective_user.id)
    text = build_report_text(user_id)
    await safe_send_markdown(context, update.effective_chat.id, text)

async def grafico(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    png = build_daily_chart_png(user_id, days=30)
    await safe_send_photo(context, update.effective_chat.id, png, caption="📈 Gastos por dia (30 dias)")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text_in = update.message.text or ""
    if not text_in.strip():
        return

    try:
        obj = await extract_expense(text_in)
        reply = format_reply(obj)

        if obj.get("amount") is not None:
            user_id = str(update.effective_user.id)
            expense_id = insert_expense(
                user_id=user_id,
                raw_text=text_in,
                amount=obj.get("amount"),
                currency=obj.get("currency") or "BRL",
                category=obj.get("category") or "outros",
                description=obj.get("description") or "",
                confidence=float(obj.get("confidence") or 0),
            )
            reply = reply + f"\nID: `{expense_id}`"

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
    (Para cada user_id que já tenha gasto registrado)
    """
    user_ids = list_users_with_expenses()
    for uid in user_ids:
        try:
            chat_id = int(uid)  # aqui estamos assumindo user_id = telegram user id
            text = build_report_text(uid)
            await safe_send_markdown(context, chat_id, "🕚 *Relatório automático (23:00)*\n" + text)

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

    app.add_error_handler(on_error)

    # Agenda job diário às 23:00 no fuso de SP
    app.job_queue.run_daily(
        scheduled_23h,
        time=datetime.now(TZ).replace(hour=23, minute=0, second=0, microsecond=0).time(),
        name="relatorio_23h",
    )

    return app
def start_health_server():
    port = int(os.getenv("PORT", "8080"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/healthz"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
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
    start_health_server()git add .
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
