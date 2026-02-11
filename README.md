
<img width="1536" height="1024" alt="ChatGPT Image 6 de fev  de 2026, 17_55_53" src="https://github.com/user-attachments/assets/0deecabf-a84b-4238-8483-fe355f3d0076" />


# Telegram Expense Bot (PT-BR) — Groq + Supabase + Koyeb (Totalmente Gratuíto)

Bot de Telegram para registrar despesas a partir de mensagens em português, gerar relatórios (dia/semana) e enviar gráficos de evolução diária. Inclui envio automático de resumo às 23:00 (horário de São Paulo) e um endpoint de health check para manter o serviço vivo em hosting free.

---

## Sumário

- [Visão geral](#visão-geral)
- [Funcionalidades](#funcionalidades)
- [Arquitetura e fluxo](#arquitetura-e-fluxo)
- [Stack](#stack)
- [Estrutura do projeto](#estrutura-do-projeto)
- [Banco de dados (Supabase/Postgres)](#banco-de-dados-supabasepostgres)
- [Variáveis de ambiente](#variáveis-de-ambiente)
- [Como rodar localmente](#como-rodar-localmente)
- [Deploy no Koyeb](#deploy-no-koyeb)
- [Uptime / evitar sleep do Free](#uptime--evitar-sleep-do-free)
- [Comandos do bot](#comandos-do-bot)
- [Relatório automático às 23:00](#relatório-automático-às-2300)
- [Troubleshooting](#troubleshooting)
- [Segurança](#segurança)
- [Licença](#licença)

---

## 🚀 Atualizações Recentes (11/02/2026)

- ✅ **Botões de Confirmação:** O bot agora solicita confirmação (Confirmar/Cancelar) antes de salvar registros no banco de dados.
- ✅ **Comando /remover:** Permite apagar o último registro feito (gasto ou ganho) de forma rápida.

---

## Visão geral

Este projeto implementa um bot de Telegram que interpreta mensagens do tipo “gastei 50 no Uber” e transforma isso em um registro estruturado no banco (Postgres/Supabase). Além disso, oferece:

- listagem dos últimos gastos
- relatório por período (hoje e semana)
- gráfico diário (linhas, em PT-BR, com rótulos)
- envio automático diário às 23:00 com resumo e gráfico

O parsing do texto é feito via Groq (modelo Llama), retornando **apenas JSON**, o que facilita validação e persistência.

---

## Funcionalidades

- ✅ **Registro automático de despesas** a partir de mensagens em PT-BR
- ✅ **Categorias padrão** (alimentacao, transporte, saude, lazer, casa, outros)
- ✅ **/gastos**: lista últimos 10 gastos
- ✅ **/relatorio**: resumo de hoje + semana
- ✅ **/grafico**: gráfico de gastos diários (últimos 30 dias)
- ✅ **Agendamento**: envio automático às **23:00 (America/Sao_Paulo)**
- ✅ **Health server** (`/healthz`) para hospedar como Web Service (Koyeb)

---

## Arquitetura e fluxo

### 1) Mensagem do usuário → extração por IA (Groq)
1. Usuário envia mensagem no Telegram (ex.: “Pedi um berenice de 21 reais”)
2. O bot chama a Groq com um *system prompt* que exige retorno **JSON puro**
3. O retorno é parseado (JSON) e validado:
   - se `amount` existe → salva no banco
   - se `amount` é `null` → responde pedindo exemplo melhor

### 2) Persistência no banco (Supabase/Postgres)
- A tabela `public.expenses` armazena:
  - user_id (Telegram user id)
  - chat_id (para envio automático)
  - valores/categoria/descrição/confiança
  - timestamps (`created_at`)

### 3) Relatórios e gráficos
- `/relatorio`: agrega por dia e semana (por usuário)
- `/grafico`: consulta totais diários e renderiza PNG com Matplotlib

### 4) Agendamento (23:00)
- Um job diário roda às 23:00 (fuso SP) e envia:
  - relatório (hoje + semana)
  - gráfico (30 dias)

### 5) Deploy e healthcheck
- No Koyeb, o serviço roda como Web Service e expõe `/healthz`
- UptimeRobot pode pingar `/healthz` para evitar sleep em plano free

<img width="2816" height="1536" alt="Gemini_Generated_Image_nkkrcdnkkrcdnkkr" src="https://github.com/user-attachments/assets/8e4aa545-c22e-4947-80d3-5e1e4514553e" />


---

## Stack

- **Python 3.11**
- **python-telegram-bot** (polling)
- **Groq API** (chat completions em modo JSON)
- **SQLAlchemy + psycopg v3** (Postgres)
- **Supabase Postgres** (ou qualquer Postgres com SSL)
- **Matplotlib** (gráfico)
- **Koyeb** (deploy)
- **UptimeRobot** (monitor/ping)

---

## Estrutura do projeto

Arquivos principais:

- `bot.py` — bot do Telegram (handlers, relatórios, gráficos, job 23h, health server)
- `db.py` — acesso ao Postgres (insert, listagem, agregações)
- `requirements.txt` — dependências
- `.python-version` — fixa Python 3.11 (evita build diferente no Koyeb)
- `Procfile` — define o processo `web` para o buildpack (ex.: `python bot.py`)

---

## Banco de dados (Supabase/Postgres)

A tabela esperada é `public.expenses`. Campos típicos:

- `id` (serial / identity)
- `created_at` (timestamp)
- `user_id` (text)
- `chat_id` (text ou bigint; recomendado armazenar o chat para envio automático)
- `raw_text` (text)
- `amount` (numeric)
- `currency` (text)
- `category` (text)
- `description` (text)
- `confidence` (float)

> Observação: se você começou sem `chat_id`, adicione a coluna e passe a preenchê-la no insert.
> Isso evita depender de “user_id = chat_id” (não é sempre verdadeiro).

---

## Variáveis de ambiente

Crie um `.env` local (não commitar):

- `TELEGRAM_BOT_TOKEN` — token do BotFather
- `GROQ_API_KEY` — chave da Groq
- `DATABASE_URL` — URL do Postgres (Supabase)
- `PORT` — (opcional) porta do health server; em hosting costuma vir pronto

Exemplo de `.env`:

```env
TELEGRAM_BOT_TOKEN=123:abc
GROQ_API_KEY=gsk_xxx
DATABASE_URL=postgresql://user:pass@host:5432/postgres?sslmode=require
PORT=8080
````

---

## Como rodar localmente

### 1) Criar e ativar venv

Windows (Git Bash):

```bash
python -m venv .venv
source .venv/Scripts/activate
```

Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
```

### 2) Instalar dependências

```bash
pip install -r requirements.txt
```

### 3) Rodar o bot

```bash
python bot.py
```

---

## Deploy no Koyeb

### 1) Preparar o repositório

Garanta que existem no repo:

* `requirements.txt`
* `Procfile` (ex.: `web: python bot.py`)
* `.python-version` (conteúdo: `3.11`)

### 2) Criar o serviço

* Koyeb → Create service → GitHub repo → Buildpack
* Defina as **Environment variables**:

  * `TELEGRAM_BOT_TOKEN`
  * `GROQ_API_KEY`
  * `DATABASE_URL`

### 3) Porta e healthcheck

* O bot inicia um `HTTPServer` e responde em:

  * `GET /healthz` → `ok`
  * `GET /` → `ok`

No Koyeb, mantenha o Health Check apontando para a porta configurada (normalmente `8000` no Koyeb).

### 4) Verificação

No painel do Koyeb:

* Deployment: **Healthy**
* Logs: “Bot rodando via polling...”
* Acesse:

  * `https://SEU_APP.koyeb.app/healthz` → deve retornar `ok`

---

## Uptime / evitar sleep do Free

Se o plano free escala para zero em inatividade, o bot pode “dormir” e perder o horário do job das 23:00.

Solução prática:

* Use o UptimeRobot para monitorar e pingar `/healthz` periodicamente.

Sugestão:

* Monitor tipo HTTP(s)
* URL: `https://SEU_APP.koyeb.app/healthz`
* Intervalo: 5 minutos (ou o menor disponível no plano)

> Importante: o UptimeRobot usa HEAD em alguns casos. O endpoint `/healthz` deve responder corretamente a HEAD também (ou o monitor acusa 501/405).

---

## Comandos do bot

* `/start` — instruções rápidas
* `/gastos` — últimos 10 gastos
* `/relatorio` — resumo de hoje + semana
* `/grafico` — gráfico de gastos diários (últimos 30 dias)

---

## Relatório automático às 23:00

O job diário utiliza o fuso `America/Sao_Paulo`. Ele:

* lista usuários com gastos registrados
* recupera `chat_id` armazenado
* envia relatório + gráfico

Pré-requisito:

* `chat_id` precisa estar preenchido para cada usuário (ou não há como enviar pro chat).

---

## Troubleshooting

### 1) Erro 409 Conflict (getUpdates)

**Causa:** dois processos rodando o bot ao mesmo tempo via polling (ex.: local + Koyeb).
**Solução:** mantenha **apenas uma instância** ativa. Pare o local quando o Koyeb estiver rodando.

### 2) Healthcheck acusando 501 Not Implemented (UptimeRobot)

**Causa comum:** o monitor faz `HEAD /healthz` e o servidor não implementa HEAD.
**Solução:** implementar `do_HEAD` no handler do health server e retornar 200.

### 3) Job das 23:00 não executou

**Causas comuns:**

* instância dormiu (scale to zero)
* `chat_id` não está preenchido
  **Soluções:**
* manter vivo com UptimeRobot
* garantir `chat_id` salvo no insert / update

### 4) Gráfico “achatado” por outlier

Se existe um dia com gasto muito alto, o resto pode ficar imperceptível.
O gráfico pode usar estratégias como:

* ajuste de eixo (zoom no “miolo”)
* escala `symlog` (boa para valores com grande variação)

---

## Segurança

* Não commite `.env`
* Não exponha `DATABASE_URL`, `GROQ_API_KEY` e `TELEGRAM_BOT_TOKEN`
* Em Postgres, use SSL (`sslmode=require`) quando necessário (Supabase)


