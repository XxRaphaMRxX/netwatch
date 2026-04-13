#!/usr/bin/env python3
"""
NetWatch — REST API  (FastAPI)
Exposes authenticated endpoints consumed by the dashboard frontend.
"""

import hashlib
import io
import os
import secrets
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import bcrypt
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


APP_TZ = os.getenv("APP_TIMEZONE", "America/Sao_Paulo")
try:
    LOCAL_TZ = ZoneInfo(APP_TZ)
except Exception:
    APP_TZ = "America/Sao_Paulo"
    LOCAL_TZ = ZoneInfo(APP_TZ)

SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "netwatch_session")
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))
SESSION_COOKIE_SECURE = parse_bool(os.getenv("SESSION_COOKIE_SECURE", "false"), False)
SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "lax")
SESSION_PEPPER = os.getenv("SESSION_SECRET", os.getenv("DB_PASSWORD", "change_me"))

DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123")

MAX_LOGIN_FAILS = int(os.getenv("MAX_LOGIN_FAILS", "5"))
LOGIN_WINDOW_MINUTES = int(os.getenv("LOGIN_WINDOW_MINUTES", "15"))
SCHEMA_VERSION = 1

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="NetWatch API", version="2.0.0")

cors_origins = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:3001,http://127.0.0.1:3001").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Requested-With"],
)

DB_DSN = (
    f"host={os.getenv('DB_HOST', 'db')} "
    f"port={os.getenv('DB_PORT', '5432')} "
    f"dbname={os.getenv('DB_NAME', 'netwatch')} "
    f"user={os.getenv('DB_USER', 'netwatch')} "
    f"password={os.getenv('DB_PASSWORD', 'netwatch_secret')}"
)


MIGRATION_SQL = f"""
CREATE TABLE IF NOT EXISTS users (
    id             BIGSERIAL PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    created_ip      TEXT,
    user_agent      TEXT
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_lookup
    ON user_sessions (token_hash, expires_at, revoked_at);

CREATE TABLE IF NOT EXISTS login_audit (
    id           BIGSERIAL PRIMARY KEY,
    username     TEXT NOT NULL,
    success      BOOLEAN NOT NULL,
    ip           TEXT,
    user_agent   TEXT,
    reason       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_login_audit_username_created
    ON login_audit (username, created_at DESC);

DROP MATERIALIZED VIEW IF EXISTS hourly_stats;
CREATE MATERIALIZED VIEW hourly_stats AS
SELECT
    date_trunc('hour', tested_at AT TIME ZONE '{APP_TZ}') AS hour,
    ROUND(AVG(download)::NUMERIC, 2)   AS avg_download,
    ROUND(AVG(upload)::NUMERIC, 2)     AS avg_upload,
    ROUND(AVG(ping)::NUMERIC, 2)       AS avg_ping,
    ROUND(AVG(jitter)::NUMERIC, 2)     AS avg_jitter,
    ROUND(MIN(download)::NUMERIC, 2)   AS min_download,
    ROUND(MAX(download)::NUMERIC, 2)   AS max_download,
    COUNT(*)                           AS total_tests,
    COUNT(*) FILTER (WHERE status = 'error' OR status = 'timeout') AS failed_tests
FROM speed_tests
WHERE tested_at >= NOW() - INTERVAL '90 days'
GROUP BY date_trunc('hour', tested_at AT TIME ZONE '{APP_TZ}')
ORDER BY hour DESC;
CREATE UNIQUE INDEX IF NOT EXISTS idx_hourly_stats_hour ON hourly_stats (hour);

DROP MATERIALIZED VIEW IF EXISTS daily_stats;
CREATE MATERIALIZED VIEW daily_stats AS
SELECT
    date_trunc('day', tested_at AT TIME ZONE '{APP_TZ}') AS day,
    ROUND(AVG(download)::NUMERIC, 2)   AS avg_download,
    ROUND(AVG(upload)::NUMERIC, 2)     AS avg_upload,
    ROUND(AVG(ping)::NUMERIC, 2)       AS avg_ping,
    ROUND(AVG(jitter)::NUMERIC, 2)     AS avg_jitter,
    ROUND(MIN(download)::NUMERIC, 2)   AS min_download,
    ROUND(MAX(download)::NUMERIC, 2)   AS max_download,
    ROUND(MIN(ping)::NUMERIC, 2)       AS best_ping,
    ROUND(MAX(ping)::NUMERIC, 2)       AS worst_ping,
    COUNT(*)                           AS total_tests,
    COUNT(*) FILTER (WHERE status = 'error' OR status = 'timeout') AS failed_tests,
    ROUND(
      (COUNT(*) FILTER (WHERE status = 'ok')::NUMERIC / NULLIF(COUNT(*),0)) * 100, 1
    )                                  AS uptime_pct
FROM speed_tests
WHERE tested_at >= NOW() - INTERVAL '90 days'
GROUP BY date_trunc('day', tested_at AT TIME ZONE '{APP_TZ}')
ORDER BY day DESC;
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_stats_day ON daily_stats (day);

CREATE OR REPLACE VIEW heatmap_stats AS
SELECT
    EXTRACT(DOW  FROM tested_at AT TIME ZONE '{APP_TZ}')::INT  AS dow,
    EXTRACT(HOUR FROM tested_at AT TIME ZONE '{APP_TZ}')::INT  AS hour_of_day,
    ROUND(AVG(download)::NUMERIC, 2)   AS avg_download,
    ROUND(AVG(ping)::NUMERIC, 2)       AS avg_ping,
    COUNT(*)                           AS samples
FROM speed_tests
WHERE tested_at >= NOW() - INTERVAL '30 days'
  AND status = 'ok'
GROUP BY dow, hour_of_day;

CREATE OR REPLACE FUNCTION refresh_stats() RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    REFRESH MATERIALIZED VIEW hourly_stats;
    REFRESH MATERIALIZED VIEW daily_stats;
END;
$$;
"""


def get_conn():
    return psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(f"{raw_token}:{SESSION_PEPPER}".encode("utf-8")).hexdigest()


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def run_migrations() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (SCHEMA_VERSION,))
            already_applied = cur.fetchone() is not None
            if not already_applied:
                cur.execute(MIGRATION_SQL)
                cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (SCHEMA_VERSION,))
        conn.commit()


def ensure_default_admin() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users")
            count = cur.fetchone()["c"]
            if count > 0:
                return

            cur.execute(
                """
                INSERT INTO users (username, password_hash, is_active)
                VALUES (%s, %s, TRUE)
                """,
                (DEFAULT_ADMIN_USERNAME, hash_password(DEFAULT_ADMIN_PASSWORD)),
            )
        conn.commit()


@app.on_event("startup")
def startup() -> None:
    run_migrations()
    ensure_default_admin()


def record_login_attempt(
    username: str,
    success: bool,
    ip: str,
    user_agent: str,
    reason: Optional[str] = None,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO login_audit (username, success, ip, user_agent, reason)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (username, success, ip, user_agent, reason),
            )
        conn.commit()


def too_many_failures(username: str, ip: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS failures
                FROM login_audit
                WHERE username = %s
                  AND ip = %s
                  AND success = FALSE
                  AND created_at >= NOW() - (%s * INTERVAL '1 minute')
                """,
                (username, ip, LOGIN_WINDOW_MINUTES),
            )
            failures = cur.fetchone()["failures"]
    return failures >= MAX_LOGIN_FAILS


def require_authenticated_user(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    token_hash = hash_session_token(token)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.username
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = %s
                  AND s.revoked_at IS NULL
                  AND s.expires_at > NOW()
                  AND u.is_active = TRUE
                LIMIT 1
                """,
                (token_hash,),
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    return dict(row)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        if request.method != "OPTIONS":
            try:
                require_authenticated_user(request)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


# ── Models ────────────────────────────────────────────────────────────────────
class SpeedTest(BaseModel):
    id: int
    tested_at: datetime
    download: Optional[float]
    upload: Optional[float]
    ping: Optional[float]
    jitter: Optional[float]
    packet_loss: Optional[float]
    isp: Optional[str]
    status: str


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=256)


def resolve_period_range(
    days: Optional[int], start_date: Optional[date], end_date: Optional[date]
) -> Tuple[datetime, datetime, str]:
    now_utc = datetime.now(timezone.utc)

    if start_date and end_date:
        if end_date < start_date:
            raise HTTPException(status_code=400, detail="end_date must be >= start_date")
        start_local = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
        end_local = datetime.combine(end_date, time.max, tzinfo=LOCAL_TZ)
        return (
            start_local.astimezone(timezone.utc),
            end_local.astimezone(timezone.utc),
            f"{start_date.isoformat()} a {end_date.isoformat()} ({APP_TZ})",
        )

    period_days = days or 30
    return (
        now_utc - timedelta(days=period_days),
        now_utc,
        f"ultimos {period_days} dias ({APP_TZ})",
    )


def fmt_num(value: Optional[float], suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:.2f}{suffix}"


def build_report_pdf(
    period_label: str,
    summary: dict,
    daily_rows: list,
    hourly_rows: list,
    outages: list,
    latest: Optional[dict],
) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=32, leftMargin=32, topMargin=28, bottomMargin=28)
    styles = getSampleStyleSheet()

    story = []
    story.append(Paragraph("NetWatch - Relatorio Completo de Monitoramento", styles["Title"]))
    story.append(Paragraph(f"Periodo: {period_label}", styles["Normal"]))
    story.append(Paragraph(f"Gerado em: {datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M:%S')} ({APP_TZ})", styles["Normal"]))
    story.append(Spacer(1, 14))

    kpi_data = [
        ["KPI", "Valor"],
        ["Download medio", fmt_num(summary.get("avg_download"), " Mbps")],
        ["Upload medio", fmt_num(summary.get("avg_upload"), " Mbps")],
        ["Ping medio", fmt_num(summary.get("avg_ping"), " ms")],
        ["Jitter medio", fmt_num(summary.get("avg_jitter"), " ms")],
        ["Melhor download", fmt_num(summary.get("max_download"), " Mbps")],
        ["Pior download", fmt_num(summary.get("min_download"), " Mbps")],
        ["Uptime", fmt_num(summary.get("uptime_pct"), " %")],
        ["Total de testes", str(summary.get("total_tests", 0))],
        ["Falhas", str(summary.get("failed_tests", 0))],
    ]
    kpi_table = Table(kpi_data, colWidths=[220, 260])
    kpi_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3b73")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ]
        )
    )
    story.append(kpi_table)
    story.append(Spacer(1, 16))

    story.append(Paragraph("Resumo Diario", styles["Heading2"]))
    day_data = [["Dia", "Download", "Upload", "Ping", "Uptime", "Falhas"]]
    for row in daily_rows:
        day_data.append(
            [
                row["day"].strftime("%d/%m/%Y"),
                fmt_num(row.get("avg_download"), " Mbps"),
                fmt_num(row.get("avg_upload"), " Mbps"),
                fmt_num(row.get("avg_ping"), " ms"),
                fmt_num(row.get("uptime_pct"), " %"),
                str(row.get("failed_tests", 0)),
            ]
        )
    day_table = Table(day_data, repeatRows=1)
    day_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(day_table)
    story.append(Spacer(1, 14))

    story.append(Paragraph("Resumo Horario (ultimas 48 horas)", styles["Heading2"]))
    hour_data = [["Hora", "Download", "Upload", "Ping", "Falhas"]]
    for row in hourly_rows:
        hour_data.append(
            [
                row["hour"].strftime("%d/%m %H:00"),
                fmt_num(row.get("avg_download"), " Mbps"),
                fmt_num(row.get("avg_upload"), " Mbps"),
                fmt_num(row.get("avg_ping"), " ms"),
                str(row.get("failed_tests", 0)),
            ]
        )
    hour_table = Table(hour_data, repeatRows=1)
    hour_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(hour_table)
    story.append(Spacer(1, 14))

    story.append(Paragraph("Quedas e Instabilidades", styles["Heading2"]))
    outage_data = [["Inicio", "Fim", "Status", "Ocorrencias"]]
    if outages:
        for row in outages:
            outage_data.append(
                [
                    row["started_at"].astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M"),
                    row["ended_at"].astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M"),
                    row["status"],
                    str(row["count"]),
                ]
            )
    else:
        outage_data.append(["-", "-", "Sem quedas", "0"])

    outage_table = Table(outage_data, repeatRows=1)
    outage_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(outage_table)
    story.append(Spacer(1, 14))

    story.append(Paragraph("Ultimo Teste do Periodo", styles["Heading2"]))
    if latest:
        latest_data = [
            ["Campo", "Valor"],
            ["Data/Hora", latest["tested_at"].astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M:%S")],
            ["Download", fmt_num(latest.get("download"), " Mbps")],
            ["Upload", fmt_num(latest.get("upload"), " Mbps")],
            ["Ping", fmt_num(latest.get("ping"), " ms")],
            ["Jitter", fmt_num(latest.get("jitter"), " ms")],
            ["ISP", latest.get("isp") or "-"],
            ["Servidor", latest.get("server_name") or "-"],
            ["Status", latest.get("status") or "-"],
        ]
    else:
        latest_data = [["Campo", "Valor"], ["Ultimo teste", "Sem dados no periodo"]]

    latest_table = Table(latest_data, colWidths=[160, 320])
    latest_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    story.append(latest_table)

    doc.build(story)
    return buffer.getvalue()


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat(), "tz": APP_TZ}


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request):
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "unknown")

    if too_many_failures(payload.username, ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again in a few minutes.",
        )

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, password_hash, is_active
                FROM users
                WHERE username = %s
                LIMIT 1
                """,
                (payload.username,),
            )
            user = cur.fetchone()

            if not user or not user["is_active"] or not verify_password(payload.password, user["password_hash"]):
                record_login_attempt(payload.username, False, ip, ua, "invalid_credentials")
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

            raw_token = secrets.token_urlsafe(48)
            token_hash = hash_session_token(raw_token)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)

            cur.execute(
                """
                INSERT INTO user_sessions (user_id, token_hash, expires_at, created_ip, user_agent)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user["id"], token_hash, expires_at, ip, ua),
            )
            cur.execute("UPDATE users SET last_login_at = NOW(), updated_at = NOW() WHERE id = %s", (user["id"],))
        conn.commit()

    record_login_attempt(payload.username, True, ip, ua)

    response = JSONResponse(
        {
            "ok": True,
            "user": {
                "id": user["id"],
                "username": user["username"],
            },
        }
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=SESSION_TTL_HOURS * 3600,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite=SESSION_COOKIE_SAMESITE,
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        token_hash = hash_session_token(token)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE user_sessions
                    SET revoked_at = NOW()
                    WHERE token_hash = %s AND revoked_at IS NULL
                    """,
                    (token_hash,),
                )
            conn.commit()

    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/api/auth/me")
def me(request: Request):
    user = require_authenticated_user(request)
    return {"ok": True, "user": user}


@app.get("/api/tests", response_model=List[SpeedTest])
def get_tests(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(500, ge=1, le=2000),
):
    """Raw test results for the timeline chart."""
    sql = """
        SELECT id, tested_at, download, upload, ping, jitter, packet_loss, isp, status
        FROM speed_tests
        WHERE tested_at >= NOW() - (%s * INTERVAL '1 day')
        ORDER BY tested_at ASC
        LIMIT %s
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (days, limit))
            rows = cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/summary")
def get_summary(days: int = Query(30, ge=1, le=90)):
    """KPI cards."""
    sql = """
        SELECT
            ROUND(AVG(download)::NUMERIC, 2)   AS avg_download,
            ROUND(AVG(upload)::NUMERIC, 2)     AS avg_upload,
            ROUND(AVG(ping)::NUMERIC, 2)       AS avg_ping,
            ROUND(AVG(jitter)::NUMERIC, 2)     AS avg_jitter,
            ROUND(MAX(download)::NUMERIC, 2)   AS max_download,
            ROUND(MIN(download)::NUMERIC, 2)   AS min_download,
            ROUND(
              (COUNT(*) FILTER (WHERE status='ok')::NUMERIC /
               NULLIF(COUNT(*),0)) * 100, 1
            )                                  AS uptime_pct,
            COUNT(*)                           AS total_tests,
            COUNT(*) FILTER (WHERE status <> 'ok') AS failed_tests
        FROM speed_tests
        WHERE tested_at >= NOW() - (%s * INTERVAL '1 day')
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (days,))
            row = dict(cur.fetchone())
    row["period_days"] = days
    return row


@app.get("/api/hourly")
def get_hourly(days: int = Query(7, ge=1, le=30)):
    """Hourly aggregates for the line chart."""
    sql = """
        SELECT hour, avg_download, avg_upload, avg_ping, avg_jitter,
               min_download, max_download, total_tests, failed_tests
        FROM hourly_stats
        WHERE hour >= date_trunc('hour', (NOW() AT TIME ZONE %s) - (%s * INTERVAL '1 day'))
        ORDER BY hour ASC
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (APP_TZ, days))
            rows = cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/daily")
def get_daily(days: int = Query(30, ge=1, le=90)):
    """Daily aggregates for the bar/trend chart."""
    sql = """
        SELECT day, avg_download, avg_upload, avg_ping, avg_jitter,
               min_download, max_download, best_ping, worst_ping,
               total_tests, failed_tests, uptime_pct
        FROM daily_stats
        WHERE day >= date_trunc('day', (NOW() AT TIME ZONE %s) - (%s * INTERVAL '1 day'))
        ORDER BY day ASC
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (APP_TZ, days))
            rows = cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/heatmap")
def get_heatmap():
    """DOW × Hour heatmap (last 30 days)."""
    sql = """
        SELECT dow, hour_of_day, avg_download, avg_ping, samples
        FROM heatmap_stats
        ORDER BY dow, hour_of_day
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/outages")
def get_outages(days: int = Query(30, ge=1, le=90)):
    """Consecutive failed tests grouped as outage events."""
    sql = """
        WITH ranked AS (
          SELECT tested_at, status,
                 ROW_NUMBER() OVER (ORDER BY tested_at) -
                 ROW_NUMBER() OVER (PARTITION BY status ORDER BY tested_at) AS grp
          FROM speed_tests
          WHERE tested_at >= NOW() - (%s * INTERVAL '1 day')
        )
        SELECT
            MIN(tested_at) AS started_at,
            MAX(tested_at) AS ended_at,
            COUNT(*)        AS count,
            status
        FROM ranked
        WHERE status <> 'ok'
        GROUP BY grp, status
        HAVING COUNT(*) >= 1
        ORDER BY started_at DESC
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (days,))
            rows = cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/latest")
def get_latest():
    """Most recent single test result."""
    sql = """
        SELECT id, tested_at, download, upload, ping, jitter,
               packet_loss, isp, server_name, status
        FROM speed_tests
        ORDER BY tested_at DESC
        LIMIT 1
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No tests yet")
    return dict(row)


@app.get("/api/report/pdf")
def get_report_pdf(
    days: Optional[int] = Query(30, ge=1, le=365),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
):
    start_dt, end_dt, period_label = resolve_period_range(days, start_date, end_date)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ROUND(AVG(download)::NUMERIC, 2) AS avg_download,
                    ROUND(AVG(upload)::NUMERIC, 2) AS avg_upload,
                    ROUND(AVG(ping)::NUMERIC, 2) AS avg_ping,
                    ROUND(AVG(jitter)::NUMERIC, 2) AS avg_jitter,
                    ROUND(MAX(download)::NUMERIC, 2) AS max_download,
                    ROUND(MIN(download)::NUMERIC, 2) AS min_download,
                    ROUND((COUNT(*) FILTER (WHERE status='ok')::NUMERIC / NULLIF(COUNT(*),0)) * 100, 1) AS uptime_pct,
                    COUNT(*) AS total_tests,
                    COUNT(*) FILTER (WHERE status <> 'ok') AS failed_tests
                FROM speed_tests
                WHERE tested_at BETWEEN %s AND %s
                """,
                (start_dt, end_dt),
            )
            summary = dict(cur.fetchone())

            cur.execute(
                """
                SELECT
                    date_trunc('day', tested_at AT TIME ZONE %s) AS day,
                    ROUND(AVG(download)::NUMERIC, 2) AS avg_download,
                    ROUND(AVG(upload)::NUMERIC, 2) AS avg_upload,
                    ROUND(AVG(ping)::NUMERIC, 2) AS avg_ping,
                    ROUND((COUNT(*) FILTER (WHERE status='ok')::NUMERIC / NULLIF(COUNT(*),0)) * 100, 1) AS uptime_pct,
                    COUNT(*) FILTER (WHERE status <> 'ok') AS failed_tests
                FROM speed_tests
                WHERE tested_at BETWEEN %s AND %s
                GROUP BY 1
                ORDER BY 1 ASC
                """,
                (APP_TZ, start_dt, end_dt),
            )
            daily_rows = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    date_trunc('hour', tested_at AT TIME ZONE %s) AS hour,
                    ROUND(AVG(download)::NUMERIC, 2) AS avg_download,
                    ROUND(AVG(upload)::NUMERIC, 2) AS avg_upload,
                    ROUND(AVG(ping)::NUMERIC, 2) AS avg_ping,
                    COUNT(*) FILTER (WHERE status <> 'ok') AS failed_tests
                FROM speed_tests
                WHERE tested_at BETWEEN %s AND %s
                GROUP BY 1
                ORDER BY 1 DESC
                LIMIT 48
                """,
                (APP_TZ, start_dt, end_dt),
            )
            hourly_rows = [dict(r) for r in cur.fetchall()]
            hourly_rows.reverse()

            cur.execute(
                """
                WITH ranked AS (
                  SELECT tested_at, status,
                         ROW_NUMBER() OVER (ORDER BY tested_at) -
                         ROW_NUMBER() OVER (PARTITION BY status ORDER BY tested_at) AS grp
                  FROM speed_tests
                  WHERE tested_at BETWEEN %s AND %s
                )
                SELECT
                    MIN(tested_at) AS started_at,
                    MAX(tested_at) AS ended_at,
                    COUNT(*) AS count,
                    status
                FROM ranked
                WHERE status <> 'ok'
                GROUP BY grp, status
                ORDER BY started_at DESC
                LIMIT 200
                """,
                (start_dt, end_dt),
            )
            outages = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT id, tested_at, download, upload, ping, jitter,
                       packet_loss, isp, server_name, status
                FROM speed_tests
                WHERE tested_at BETWEEN %s AND %s
                ORDER BY tested_at DESC
                LIMIT 1
                """,
                (start_dt, end_dt),
            )
            latest = cur.fetchone()
            latest = dict(latest) if latest else None

    pdf_bytes = build_report_pdf(period_label, summary, daily_rows, hourly_rows, outages, latest)
    filename = f"netwatch-report-{datetime.now(LOCAL_TZ).strftime('%Y%m%d-%H%M%S')}.pdf"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
