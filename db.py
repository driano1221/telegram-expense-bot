import os
from datetime import datetime
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Faltou DATABASE_URL no .env")

# Força psycopg v3
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

# Supabase exige SSL
if "sslmode=" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = DATABASE_URL + f"{sep}sslmode=require"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def insert_expense(
    *,
    user_id: str,
    chat_id: str | None = None,
    raw_text: str,
    amount,
    currency: str,
    category: str,
    description: str,
    confidence: float,
):
    """
    Salva uma despesa. Se chat_id vier preenchido, guarda também para relatórios automáticos.
    """
    q = text("""
        insert into public.expenses (user_id, chat_id, raw_text, amount, currency, category, description, confidence)
        values (:user_id, :chat_id, :raw_text, :amount, :currency, :category, :description, :confidence)
        returning id;
    """)
    with engine.begin() as conn:
        row = conn.execute(q, {
            "user_id": user_id,
            "chat_id": chat_id,
            "raw_text": raw_text,
            "amount": amount,
            "currency": currency,
            "category": category,
            "description": description,
            "confidence": confidence,
        }).first()
        return row[0] if row else None


def get_chat_id_for_user(user_id: str) -> int | None:
    """
    Pega o último chat_id conhecido do usuário (pode ser chat privado ou grupo).
    """
    q = text("""
        select chat_id
        from public.expenses
        where user_id = :user_id
          and chat_id is not null
          and chat_id <> ''
        order by created_at desc
        limit 1;
    """)
    with engine.begin() as conn:
        row = conn.execute(q, {"user_id": user_id}).first()

    if not row or not row[0]:
        return None

    try:
        return int(row[0])
    except Exception:
        return None


def list_last_expenses(user_id: str, limit: int = 10):
    q = text("""
        select created_at, amount, currency, category, description
        from public.expenses
        where user_id = :user_id
        order by created_at desc
        limit :limit;
    """)
    with engine.begin() as conn:
        return conn.execute(q, {"user_id": user_id, "limit": limit}).fetchall()


def totals_by_category(user_id: str, start_dt: datetime, end_dt: datetime):
    q = text("""
        select category, coalesce(sum(amount), 0) as total, count(*) as n
        from public.expenses
        where user_id = :user_id
          and created_at >= :start_dt
          and created_at < :end_dt
          and amount is not null
        group by category
        order by total desc;
    """)
    with engine.begin() as conn:
        return conn.execute(q, {"user_id": user_id, "start_dt": start_dt, "end_dt": end_dt}).fetchall()


def totals_overall(user_id: str, start_dt: datetime, end_dt: datetime):
    q = text("""
        select coalesce(sum(amount), 0) as total, count(*) as n
        from public.expenses
        where user_id = :user_id
          and created_at >= :start_dt
          and created_at < :end_dt
          and amount is not null;
    """)
    with engine.begin() as conn:
        row = conn.execute(q, {"user_id": user_id, "start_dt": start_dt, "end_dt": end_dt}).first()
        if not row:
            return 0, 0
        return row[0], row[1]


def daily_totals_last_n_days(user_id: str, days: int, start_dt: datetime, end_dt: datetime):
    """
    Retorna totais por dia no intervalo [start_dt, end_dt), limitado a 'days' como segurança.
    """
    q = text("""
        select date_trunc('day', created_at) as day, coalesce(sum(amount), 0) as total
        from public.expenses
        where user_id = :user_id
          and created_at >= :start_dt
          and created_at < :end_dt
          and amount is not null
        group by 1
        order by 1 asc
        limit :days;
    """)
    with engine.begin() as conn:
        return conn.execute(q, {
            "user_id": user_id,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "days": days
        }).fetchall()


def list_users_with_expenses(only_with_chat_id: bool = True):
    """
    Lista usuários distintos. Se only_with_chat_id=True, só retorna usuários
    que já têm chat_id salvo (necessário para envio automático).
    """
    if only_with_chat_id:
        q = text("""
            select distinct user_id
            from public.expenses
            where chat_id is not null and chat_id <> '';
        """)
    else:
        q = text("select distinct user_id from public.expenses;")

    with engine.begin() as conn:
        return [r[0] for r in conn.execute(q).fetchall()]
