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
    amount: float,
    currency: str,
    category: str,
    description: str,
    confidence: float,
    entry_type: str = "expense",
):
    """
    Salva uma despesa ou ganho. entry_type: 'expense' ou 'income'.
    """
    q = text("""
        insert into public.expenses
            (user_id, chat_id, raw_text, amount, currency, category, description, confidence, type)
        values
            (:user_id, :chat_id, :raw_text, :amount, :currency, :category, :description, :confidence, :type)
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
            "type": entry_type,
        }).first()
        return row[0] if row else None


def get_chat_id_for_user(user_id: str) -> int | None:
    """
    Pega o último chat_id conhecido do usuário.
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


def list_last_entries(user_id: str, entry_type: str = "expense", limit: int = 10):
    """
    Lista ultimas entradas por tipo ('expense' ou 'income').
    """
    q = text("""
        select created_at, amount, currency, category, description
        from public.expenses
        where user_id = :user_id
          and coalesce(type, 'expense') = :type
        order by created_at desc
        limit :limit;
    """)
    with engine.begin() as conn:
        return conn.execute(q, {"user_id": user_id, "type": entry_type, "limit": limit}).fetchall()


# Alias para retrocompatibilidade
def list_last_expenses(user_id: str, limit: int = 10):
    return list_last_entries(user_id, entry_type="expense", limit=limit)


def totals_by_category(user_id: str, start_dt: datetime, end_dt: datetime, entry_type: str = "expense"):
    q = text("""
        select category, coalesce(sum(amount), 0) as total, count(*) as n
        from public.expenses
        where user_id = :user_id
          and created_at >= :start_dt
          and created_at < :end_dt
          and amount is not null
          and coalesce(type, 'expense') = :type
        group by category
        order by total desc;
    """)
    with engine.begin() as conn:
        return conn.execute(q, {
            "user_id": user_id, "start_dt": start_dt, "end_dt": end_dt, "type": entry_type,
        }).fetchall()


def totals_overall(user_id: str, start_dt: datetime, end_dt: datetime, entry_type: str = "expense"):
    q = text("""
        select coalesce(sum(amount), 0) as total, count(*) as n
        from public.expenses
        where user_id = :user_id
          and created_at >= :start_dt
          and created_at < :end_dt
          and amount is not null
          and coalesce(type, 'expense') = :type;
    """)
    with engine.begin() as conn:
        row = conn.execute(q, {
            "user_id": user_id, "start_dt": start_dt, "end_dt": end_dt, "type": entry_type,
        }).first()
        if not row:
            return 0, 0
        return row[0], row[1]


def daily_totals_last_n_days(user_id: str, days: int, start_dt: datetime, end_dt: datetime, entry_type: str = "expense"):
    """
    Retorna totais por dia no intervalo [start_dt, end_dt).
    """
    q = text("""
        select date_trunc('day', created_at) as day, coalesce(sum(amount), 0) as total
        from public.expenses
        where user_id = :user_id
          and created_at >= :start_dt
          and created_at < :end_dt
          and amount is not null
          and coalesce(type, 'expense') = :type
        group by 1
        order by 1 asc
        limit :days;
    """)
    with engine.begin() as conn:
        return conn.execute(q, {
            "user_id": user_id,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "days": days,
            "type": entry_type,
        }).fetchall()


def monthly_balance(user_id: str, start_dt: datetime, end_dt: datetime):
    """
    Retorna (total_gastos, n_gastos, total_ganhos, n_ganhos) no periodo.
    """
    q = text("""
        select
            coalesce(sum(case when coalesce(type, 'expense') = 'expense' then amount end), 0) as total_expense,
            count(case when coalesce(type, 'expense') = 'expense' then 1 end) as n_expense,
            coalesce(sum(case when type = 'income' then amount end), 0) as total_income,
            count(case when type = 'income' then 1 end) as n_income
        from public.expenses
        where user_id = :user_id
          and created_at >= :start_dt
          and created_at < :end_dt
          and amount is not null;
    """)
    with engine.begin() as conn:
        row = conn.execute(q, {"user_id": user_id, "start_dt": start_dt, "end_dt": end_dt}).first()
        if not row:
            return 0, 0, 0, 0
        return row[0], row[1], row[2], row[3]


def weekly_balance_last_n_weeks(user_id: str, weeks: int, start_dt: datetime, end_dt: datetime):
    """
    Retorna gastos e ganhos agrupados por semana para o grafico de balanco.
    """
    q = text("""
        select
            date_trunc('week', created_at) as week,
            coalesce(sum(case when coalesce(type, 'expense') = 'expense' then amount end), 0) as expenses,
            coalesce(sum(case when type = 'income' then amount end), 0) as income
        from public.expenses
        where user_id = :user_id
          and created_at >= :start_dt
          and created_at < :end_dt
          and amount is not null
        group by 1
        order by 1 asc
        limit :weeks;
    """)
    with engine.begin() as conn:
        return conn.execute(q, {
            "user_id": user_id,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "weeks": weeks,
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
