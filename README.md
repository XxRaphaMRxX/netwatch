# 📡 NetWatch — Monitoramento de Rede "Power BI Style"

Dashboard de monitoramento de internet self-hosted com análise histórica, heatmaps e detecção de quedas.

## 🚀 Início Rápido

### Pré-requisitos
- Docker ≥ 20.10
- Docker Compose ≥ 2.x

### Subir tudo com um comando

```bash
docker compose up -d --build
```

Aguarde ~30 segundos para o banco inicializar e o primeiro teste rodar.

### Acessar o dashboard

| Serviço   | URL                          |
|-----------|------------------------------|
| Dashboard | http://localhost:3001        |
| API REST  | http://localhost:8000/docs   |

---

## 📐 Arquitetura

```
┌─────────────────────────────────────────────┐
│                 Docker Network               │
│                                             │
│  ┌──────────┐   SQL    ┌──────────────┐     │
│  │ collector│ ──────►  │  PostgreSQL  │     │
│  │(speedtest│          │  (porta 5432)│     │
│  │  15 min) │          └──────┬───────┘     │
│  └──────────┘                 │             │
│                               │ queries     │
│  ┌──────────┐   REST   ┌──────▼───────┐     │
│  │ Nginx    │ ◄─────── │  FastAPI     │     │
│  │ Frontend │          │  (porta 8000)│     │
│  │(porta 3001)         └─────────────┘     │
│  └──────────┘                               │
└─────────────────────────────────────────────┘
```

## ⚙️ Configuração

### Intervalo de testes (padrão: 15 min)

Edite `docker-compose.yml`:
```yaml
environment:
  TEST_INTERVAL_MINUTES: 10  # altere aqui
```

### Variáveis disponíveis

| Variável              | Padrão   | Descrição                     |
|-----------------------|----------|-------------------------------|
| TEST_INTERVAL_MINUTES | 15       | Intervalo entre testes (min)  |
| DB_HOST               | db       | Host do PostgreSQL            |
| DB_NAME               | netwatch | Nome do banco                 |
| DB_USER               | netwatch | Usuário                       |
| DB_PASSWORD           | netwatch_secret | Senha (altere em prod!) |

---

## 📊 Métricas Coletadas

- **Download** (Mbps) — velocidade de download
- **Upload** (Mbps) — velocidade de upload  
- **Ping** (ms) — latência de ida
- **Jitter** (ms) — variação da latência
- **Packet Loss** (%) — perda de pacotes
- **Status** — ok / error / timeout

## 🔐 Autenticação

- O dashboard agora exige login para acessar os endpoints `/api/*`.
- Sessão baseada em cookie `HttpOnly`.
- Usuário inicial criado automaticamente no primeiro startup da API:
  - `DEFAULT_ADMIN_USERNAME` (padrão: `admin`)
  - `DEFAULT_ADMIN_PASSWORD` (padrão: `admin123`)

⚠️ **Troque imediatamente em produção** usando variáveis de ambiente seguras.

## 🧾 Relatório PDF

- Botão **Gerar PDF** disponível na dashboard.
- O PDF inclui:
  - KPIs do período
  - Resumo diário
  - Resumo horário (últimas 48h do período)
  - Quedas e instabilidades
  - Último teste do período
- O período segue o seletor ativo da dashboard (7d/30d/60d/90d).

Endpoint direto:

```bash
GET /api/report/pdf?days=30
```

Também aceita intervalo customizado:

```bash
GET /api/report/pdf?start_date=2026-04-01&end_date=2026-04-13
```

## 🕒 Fuso Horário (Brasília)

- Serviços com `TZ=America/Sao_Paulo`.
- PostgreSQL com `PGTZ=America/Sao_Paulo`.
- Views agregadas usam conversão explícita com `AT TIME ZONE 'America/Sao_Paulo'` para manter heatmap e agregações coerentes.

## 🛡️ Deploy sem perda de dados (servidor principal)

Antes de atualizar:

```bash
# 1) Backup lógico
docker compose exec -T db pg_dump -U netwatch -d netwatch > backup_netwatch.sql

# 2) Snapshot dos volumes (opcional, recomendado)
docker volume ls | grep postgres_data
```

Deploy recomendado:

```bash
docker compose up -d --build
```

Notas importantes:

- Não use `docker compose down -v` no servidor principal, pois remove o volume do banco.
- A API aplica migrações idempotentes no startup (sem apagar `speed_tests`).

## 📈 Visualizações do Dashboard

| Painel | O que mostra |
|--------|-------------|
| KPI Cards | Médias do período, uptime, falhas |
| Timeline | Download/Upload ao longo do tempo |
| Ping & Jitter | Latência e estabilidade |
| Download Diário | Barras coloridas por qualidade |
| Heatmap | Velocidade por hora × dia da semana |
| Quedas | Tabela de outages com duração |
| Último Teste | Resultado mais recente em tempo real |

## 🔧 Comandos Úteis

```bash
# Ver logs do coletor em tempo real
docker compose logs -f collector

# Ver logs da API
docker compose logs -f api

# Parar tudo
docker compose down

# Parar e apagar dados (IRREVERSÍVEL)
docker compose down -v

# Rebuild após mudanças
docker compose up -d --build
```

## 🗄️ Acesso direto ao banco

```bash
docker compose exec db psql -U netwatch -d netwatch

# Consultas úteis:
SELECT * FROM speed_tests ORDER BY tested_at DESC LIMIT 10;
SELECT * FROM daily_stats ORDER BY day DESC LIMIT 7;
SELECT * FROM heatmap_stats ORDER BY dow, hour_of_day;
```

## 🔒 Segurança (produção)

1. Altere a senha em `docker-compose.yml` → `POSTGRES_PASSWORD`
2. Coloque um reverse proxy (Caddy/Traefik) na frente para HTTPS
3. Adicione autenticação básica ao nginx se exposto na internet

---

## 🐛 Troubleshooting

**Dashboard mostra "—" em tudo?**
→ Aguarde o primeiro teste completar (~2 min após subir)
→ Verifique: `docker compose logs collector`

**Erro de conexão com banco?**
→ Execute: `docker compose restart collector api`

**speedtest-cli retorna erro?**
→ Confirme acesso à internet do container:
  `docker compose exec collector curl -I https://www.speedtest.net`
