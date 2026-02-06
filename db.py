import os
from datetime import datetime, timezone
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
    raw_text: str,
    amount,
    currency: str,
    category: str,
    description: str,
    confidence: float,
):
    q = text("""
        insert into public.expenses (user_id, raw_text, amount, currency, category, description, confidence)
        values (:user_id, :raw_text, :amount, :currency, :category, :description, :confidence)
        returning id;
    """)
    with engine.begin() as conn:
        row = conn.execute(q, {
            "user_id": user_id,
            "raw_text": raw_text,
            "amount": amount,
            "currency": currency,
            "category": category,
            "description": description,
            "confidence": confidence,
        }).first()
        return row[0] if row else None

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

def list_users_with_expenses():
    q = text("select distinct user_id from public.expenses;")
    with engine.begin() as conn:
        return [r[0] for r in conn.execute(q).fetchall()]
