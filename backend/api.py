#!/usr/bin/env python3
"""
NetWatch — REST API  (FastAPI)
Exposes endpoints consumed by the dashboard frontend.
"""

import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="NetWatch API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_DSN = (
    f"host={os.getenv('DB_HOST','db')} "
    f"port={os.getenv('DB_PORT','5432')} "
    f"dbname={os.getenv('DB_NAME','netwatch')} "
    f"user={os.getenv('DB_USER','netwatch')} "
    f"password={os.getenv('DB_PASSWORD','netwatch_secret')}"
)


def get_conn():
    return psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)


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


class Summary(BaseModel):
    avg_download: Optional[float]
    avg_upload: Optional[float]
    avg_ping: Optional[float]
    avg_jitter: Optional[float]
    max_download: Optional[float]
    min_download: Optional[float]
    uptime_pct: Optional[float]
    total_tests: int
    failed_tests: int
    period_days: int


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/tests", response_model=List[SpeedTest])
def get_tests(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(500, ge=1, le=2000),
):
    """Raw test results for the timeline chart."""
    sql = """
        SELECT id, tested_at, download, upload, ping, jitter, packet_loss, isp, status
        FROM speed_tests
        WHERE tested_at >= NOW() - INTERVAL '%s days'
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
        WHERE tested_at >= NOW() - INTERVAL '%s days'
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
        WHERE hour >= NOW() - INTERVAL '%s days'
        ORDER BY hour ASC
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (days,))
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
        WHERE day >= NOW() - INTERVAL '%s days'
        ORDER BY day ASC
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (days,))
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
          WHERE tested_at >= NOW() - INTERVAL '%s days'
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
