
<img width="1536" height="1024" alt="ChatGPT Image 6 de fev  de 2026, 17_55_53" src="https://github.com/user-attachments/assets/0deecabf-a84b-4238-8483-fe355f3d0076" />


# Telegram Finance Bot (PT-BR) — Groq + Supabase + Koyeb (Totalmente Gratuito)

Bot de Telegram para registrar **gastos e ganhos** a partir de mensagens em portugues, gerar relatorios, graficos e acompanhar seu saldo. Inclui envio automatico de resumo as 23:00 (horario de Sao Paulo) e um endpoint de health check para manter o servico vivo em hosting free.

---

## Sumario

- [Visao geral](#visão-geral)
- [Funcionalidades](#funcionalidades)
- [Arquitetura e fluxo](#arquitetura-e-fluxo)
- [Stack](#stack)
- [Estrutura do projeto](#estrutura-do-projeto)
- [Banco de dados (Supabase/Postgres)](#banco-de-dados-supabasepostgres)
- [Variaveis de ambiente](#variáveis-de-ambiente)
- [Como rodar localmente](#como-rodar-localmente)
- [Testes](#testes)
- [Deploy no Koyeb](#deploy-no-koyeb)
- [Uptime / evitar sleep do Free](#uptime--evitar-sleep-do-free)
- [Comandos do bot](#comandos-do-bot)
- [Relatorio automatico as 23:00](#relatório-automático-às-2300)
- [Seguranca e protecoes](#segurança-e-proteções)
- [Troubleshooting](#troubleshooting)

---

## Visao geral

Este projeto implementa um bot de Telegram que interpreta mensagens como "gastei 50 no Uber" ou "recebi 3000 de salario" e transforma isso em registros estruturados no banco (Postgres/Supabase). A IA detecta automaticamente se e um **gasto** ou **ganho**.

Alem disso, oferece:

- Listagem dos ultimos gastos e ganhos
- Relatorio por periodo (hoje e semana)
- Saldo mensal (ganhos - gastos)
- Grafico de gastos diarios (30 dias)
- Grafico comparativo gastos x ganhos (8 semanas)
- Envio automatico diario as 23:00 com resumo e grafico

O parsing do texto e feito via Groq (modelo Llama), retornando **apenas JSON**, o que facilita validacao e persistencia.

---

## Funcionalidades

- **Registro automatico de gastos e ganhos** a partir de mensagens em PT-BR
- **Deteccao por IA** — diferencia automaticamente gasto de ganho
- **Categorias**: alimentacao, transporte, saude, lazer, casa, salario, freelance, investimento, outros
- `/gastos` — lista ultimos 10 gastos
- `/ganhos` — lista ultimos 10 ganhos
- `/relatorio` — resumo de hoje + semana
- `/saldo` — saldo do mes (ganhos - gastos)
- `/grafico` — grafico de gastos diarios (ultimos 30 dias)
- `/balanco` — grafico gastos x ganhos por semana (8 semanas)
- **Agendamento** — envio automatico as **23:00 (America/Sao_Paulo)**
- **Health server** (`/healthz`) para hospedar como Web Service (Koyeb)
- **Rate limiting** — protecao contra spam (configuravel)
- **Allowlist** — restringe quem pode usar o bot (opcional)
- **Validacao de entrada** — limite de tamanho e valores
- **Testes unitarios** — 25 testes cobrindo funcoes puras

---

## Arquitetura e fluxo

### 1) Mensagem do usuario → extracao por IA (Groq)
1. Usuario envia mensagem no Telegram (ex.: "gastei 50 no uber" ou "recebi 3000 de salario")
2. O bot chama a Groq com um *system prompt* que exige retorno **JSON puro**
3. A IA classifica como `expense` ou `income` automaticamente
4. O retorno e validado (amount positivo, max R$1M, texto max 500 chars)
5. Se valido → salva no banco

### 2) Persistencia no banco (Supabase/Postgres)
- A tabela `public.expenses` armazena:
  - user_id, chat_id, raw_text
  - amount, currency, category, description, confidence
  - **type** (`expense` ou `income`)
  - created_at (timestamp)

### 3) Relatorios e graficos
- `/relatorio` — agrega por dia e semana (por usuario)
- `/saldo` — saldo mensal (ganhos - gastos)
- `/grafico` — grafico de gastos diarios com estilo minimalista
- `/balanco` — grafico de barras comparativo gastos x ganhos

### 4) Agendamento (23:00)
- Um job diario roda as 23:00 (fuso SP) e envia:
  - Relatorio (hoje + semana)
  - Grafico (30 dias)

### 5) Deploy e healthcheck
- No Koyeb, o servico roda como Web Service e expoe `/healthz`
- UptimeRobot pode pingar `/healthz` para evitar sleep em plano free

<img width="2816" height="1536" alt="Gemini_Generated_Image_nkkrcdnkkrcdnkkr" src="https://github.com/user-attachments/assets/8e4aa545-c22e-4947-80d3-5e1e4514553e" />

---

## Stack

- **Python 3.11**
- **python-telegram-bot** (polling)
- **Groq API** (Llama 3.3-70B, chat completions em modo JSON)
- **SQLAlchemy + psycopg v3** (Postgres)
- **Supabase Postgres** (ou qualquer Postgres com SSL)
- **Matplotlib + NumPy** (graficos)
- **Koyeb** (deploy)
- **UptimeRobot** (monitor/ping)

---

## Estrutura do projeto

```
bot.py              — handlers, graficos, logica principal
db.py               — acesso ao Postgres (insert, listagem, agregacoes, saldo)
utils.py            — funcoes puras (format_brl, format_reply, ranges, emojis)
tests/test_bot.py   — 25 testes unitarios
.env.example        — template de variaveis de ambiente
requirements.txt    — dependencias
.python-version     — fixa Python 3.11
Procfile            — define o processo web para o buildpack
```

---

## Banco de dados (Supabase/Postgres)

A tabela esperada e `public.expenses`:

| Coluna | Tipo | Descricao |
|---|---|---|
| `id` | serial | chave primaria |
| `created_at` | timestamp | data de criacao |
| `user_id` | text | ID do usuario no Telegram |
| `chat_id` | text | ID do chat (para envio automatico) |
| `raw_text` | text | mensagem original |
| `amount` | numeric | valor |
| `currency` | text | moeda (sempre "BRL") |
| `category` | text | categoria |
| `description` | text | descricao curta |
| `confidence` | float | confianca da IA |
| `type` | text | `expense` ou `income` (default: `expense`) |

Para adicionar a coluna `type` (necessario para ganhos):

```sql
ALTER TABLE public.expenses ADD COLUMN type TEXT NOT NULL DEFAULT 'expense';
```

---

## Variaveis de ambiente

Crie um `.env` local (nao commitar) ou veja `.env.example`:

```env
# Obrigatorias
TELEGRAM_BOT_TOKEN=123456:ABC-DEF
GROQ_API_KEY=gsk_xxx
DATABASE_URL=postgresql://user:pass@host:5432/postgres?sslmode=require

# Opcionais
PORT=8080
ALLOWED_USERS=123456,789012
RATE_LIMIT_MSGS=5
RATE_LIMIT_WINDOW=60
```

| Variavel | Obrigatoria | Descricao |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Sim | Token do BotFather |
| `GROQ_API_KEY` | Sim | Chave da API Groq |
| `DATABASE_URL` | Sim | URL do PostgreSQL |
| `PORT` | Nao | Porta do health server (default: 8080) |
| `ALLOWED_USERS` | Nao | IDs autorizados separados por virgula. Se vazio, qualquer um usa |
| `RATE_LIMIT_MSGS` | Nao | Max mensagens por janela (default: 5) |
| `RATE_LIMIT_WINDOW` | Nao | Janela em segundos (default: 60) |

---

## Como rodar localmente

### 1) Criar e ativar venv

Windows (PowerShell):
```bash
python -m venv .venv
.venv\Scripts\activate
```

Linux/macOS:
```bash
python -m venv .venv
source .venv/bin/activate
```

### 2) Instalar dependencias

```bash
pip install -r requirements.txt
```

### 3) Configurar .env

```bash
cp .env.example .env
# Editar .env com seus tokens
```

### 4) Rodar o bot

```bash
python bot.py
```

---

## Testes

```bash
pip install pytest
pytest tests/ -v
```

Os testes cobrem funcoes puras (sem dependencias externas):
- Formatacao BRL (valores inteiros, decimais, milhares, invalidos)
- Respostas de gasto e ganho (emojis, categorias, HTML)
- Calculo de ranges de data (dia, semana)
- Mapa de categorias e emojis

---

## Deploy no Koyeb

### 1) Preparar o repositorio

Garanta que existem no repo:
* `requirements.txt`
* `Procfile` (ex.: `web: python bot.py`)
* `.python-version` (conteudo: `3.11`)

### 2) Criar o servico

* Koyeb → Create service → GitHub repo → Buildpack
* Defina as **Environment variables**:
  * `TELEGRAM_BOT_TOKEN`
  * `GROQ_API_KEY`
  * `DATABASE_URL`
  * `ALLOWED_USERS` (opcional)

### 3) Porta e healthcheck

* O bot inicia um `HTTPServer` e responde em:
  * `GET /healthz` → `ok`
  * `GET /` → `ok`

No Koyeb, mantenha o Health Check apontando para a porta configurada.

### 4) Verificacao

No painel do Koyeb:
* Deployment: **Healthy**
* Logs: "Bot rodando via polling..."
* Acesse: `https://SEU_APP.koyeb.app/healthz` → deve retornar `ok`

---

## Uptime / evitar sleep do Free

Se o plano free escala para zero em inatividade, o bot pode "dormir" e perder o horario do job das 23:00.

Solucao pratica:
* Use o UptimeRobot para monitorar e pingar `/healthz` periodicamente
* Monitor tipo HTTP(s), intervalo de 5 minutos

---

## Comandos do bot

| Comando | Descricao |
|---|---|
| `/start` | Instrucoes e lista de comandos |
| `/gastos` | Ultimos 10 gastos |
| `/ganhos` | Ultimos 10 ganhos |
| `/relatorio` | Resumo de hoje + semana |
| `/saldo` | Saldo do mes (ganhos - gastos) |
| `/grafico` | Grafico de gastos diarios (30 dias) |
| `/balanco` | Grafico gastos x ganhos (8 semanas) |

Alem dos comandos, basta enviar mensagens naturais:
* `gastei 50 no uber` → registra como gasto
* `almocei 35 reais` → registra como gasto
* `recebi 3000 de salario` → registra como ganho
* `ganhei 500 de freelance` → registra como ganho

---

## Relatorio automatico as 23:00

O job diario utiliza o fuso `America/Sao_Paulo`. Ele:

* Lista usuarios com gastos registrados
* Recupera `chat_id` armazenado
* Envia relatorio (hoje + semana) + grafico (30 dias)

Pre-requisito:
* `chat_id` precisa estar preenchido para cada usuario

---

## Seguranca e protecoes

* **Secrets** — nao commite `.env`, nao exponha tokens
* **SSL** — conexao com Postgres usa `sslmode=require`
* **Rate limiting** — max 5 mensagens por 60 segundos por usuario (configuravel)
* **Allowlist** — restringe quem pode usar o bot via `ALLOWED_USERS`
* **Validacao** — texto max 500 chars, amount positivo, max R$1M
* **SQL injection** — queries parametrizadas via SQLAlchemy

---

## Troubleshooting

### 1) Erro 409 Conflict (getUpdates)

**Causa:** dois processos rodando o bot ao mesmo tempo via polling (ex.: local + Koyeb).
**Solucao:** mantenha **apenas uma instancia** ativa.

### 2) Healthcheck acusando 501 Not Implemented (UptimeRobot)

**Causa:** o monitor faz `HEAD /healthz` e o servidor nao implementa HEAD.
**Solucao:** o endpoint ja responde a HEAD e GET.

### 3) Job das 23:00 nao executou

**Causas comuns:**
* Instancia dormiu (scale to zero) — manter vivo com UptimeRobot
* `chat_id` nao esta preenchido

### 4) Grafico "achatado" por outlier

O grafico usa escala `symlog` automaticamente quando detecta picos, garantindo boa visualizacao.
