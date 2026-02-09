"""
Testes unitarios para funcoes puras do bot.
Roda com: pytest tests/ -v
"""
import pytz
from datetime import datetime, timedelta

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils import (
    format_brl,
    format_reply,
    day_range_local,
    week_range_local,
    CATEGORY_EMOJI,
)


TZ = pytz.timezone("America/Sao_Paulo")


# ─── format_brl ───────────────────────────────────────────────

class TestFormatBrl:
    def test_valor_inteiro(self):
        assert format_brl(50) == "R$ 50,00"

    def test_valor_decimal(self):
        assert format_brl(35.5) == "R$ 35,50"

    def test_valor_grande_com_separador_milhar(self):
        assert format_brl(1500) == "R$ 1.500,00"

    def test_valor_muito_grande(self):
        assert format_brl(1000000) == "R$ 1.000.000,00"

    def test_valor_zero(self):
        assert format_brl(0) == "R$ 0,00"

    def test_valor_centavos(self):
        assert format_brl(0.99) == "R$ 0,99"

    def test_valor_string_numerica(self):
        assert format_brl("123.45") == "R$ 123,45"

    def test_valor_invalido_retorna_string(self):
        assert format_brl("abc") == "R$ abc"

    def test_valor_negativo(self):
        result = format_brl(-50)
        assert "50" in result


# ─── format_reply ─────────────────────────────────────────────

class TestFormatReply:
    def test_gasto_valido(self):
        obj = {"amount": 50, "category": "transporte", "description": "uber", "confidence": 0.9, "type": "expense"}
        result = format_reply(obj)
        assert "Gasto registrado" in result
        assert "R$ 50,00" in result
        assert "transporte" in result
        assert "🔴" in result

    def test_ganho_valido(self):
        obj = {"amount": 3000, "category": "salario", "description": "salario mensal", "confidence": 0.95, "type": "income"}
        result = format_reply(obj)
        assert "Ganho registrado" in result
        assert "R$ 3.000,00" in result
        assert "🟢" in result

    def test_amount_null(self):
        obj = {"amount": None, "category": "outros", "confidence": 0.1}
        result = format_reply(obj)
        assert "Não entendi" in result

    def test_descricao_vazia_usa_padrao_gasto(self):
        obj = {"amount": 10, "category": "outros", "description": "", "confidence": 0.5, "type": "expense"}
        result = format_reply(obj)
        assert "Gasto" in result

    def test_descricao_vazia_usa_padrao_ganho(self):
        obj = {"amount": 10, "category": "outros", "description": "", "confidence": 0.5, "type": "income"}
        result = format_reply(obj)
        assert "Ganho" in result

    def test_categoria_com_emoji(self):
        obj = {"amount": 20, "category": "alimentacao", "description": "lanche", "confidence": 0.8, "type": "expense"}
        result = format_reply(obj)
        assert "🍔" in result

    def test_type_padrao_expense(self):
        obj = {"amount": 10, "category": "outros", "description": "teste"}
        result = format_reply(obj)
        assert "🔴" in result

    def test_html_tags_presentes(self):
        obj = {"amount": 100, "category": "casa", "description": "aluguel", "type": "expense"}
        result = format_reply(obj)
        assert "<b>" in result
        assert "<i>" in result


# ─── day_range_local ──────────────────────────────────────────

class TestDayRangeLocal:
    def test_retorna_inicio_e_fim_do_dia(self):
        d = datetime(2026, 2, 9, 15, 30, 0, tzinfo=TZ)
        start, end = day_range_local(d)
        assert start.hour == 0
        assert start.minute == 0
        assert end == start + timedelta(days=1)

    def test_inicio_do_dia(self):
        d = datetime(2026, 2, 9, 0, 0, 0, tzinfo=TZ)
        start, end = day_range_local(d)
        assert start == d
        assert end.day == 10

    def test_fim_do_dia(self):
        d = datetime(2026, 2, 9, 23, 59, 59, tzinfo=TZ)
        start, end = day_range_local(d)
        assert start.day == 9
        assert start.hour == 0


# ─── week_range_local ─────────────────────────────────────────

class TestWeekRangeLocal:
    def test_semana_comeca_na_segunda(self):
        d = datetime(2026, 2, 9, 15, 0, 0, tzinfo=TZ)
        start, end = week_range_local(d)
        assert start.weekday() == 0
        assert end == start + timedelta(days=7)

    def test_meio_da_semana(self):
        d = datetime(2026, 2, 11, 12, 0, 0, tzinfo=TZ)
        start, end = week_range_local(d)
        assert start.weekday() == 0
        assert start.day == 9

    def test_domingo(self):
        d = datetime(2026, 2, 15, 20, 0, 0, tzinfo=TZ)
        start, end = week_range_local(d)
        assert start.weekday() == 0
        assert start.day == 9


# ─── CATEGORY_EMOJI ───────────────────────────────────────────

class TestCategoryEmoji:
    def test_todas_categorias_tem_emoji(self):
        categorias = ["alimentacao", "transporte", "saude", "lazer", "casa",
                       "salario", "freelance", "investimento", "outros"]
        for cat in categorias:
            assert cat in CATEGORY_EMOJI, f"Categoria '{cat}' sem emoji"

    def test_emoji_nao_vazio(self):
        for cat, emoji in CATEGORY_EMOJI.items():
            assert len(emoji) > 0, f"Emoji vazio para '{cat}'"
