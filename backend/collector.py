#!/usr/bin/env python3
"""
NetWatch — Speed-test collector
Runs speedtest-cli at a configurable interval and stores results in PostgreSQL.
"""

import os
import time
import json
import logging
import subprocess
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import execute_values

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("collector")

# ── Config ────────────────────────────────────────────────────────────────────
DB_DSN = (
    f"host={os.getenv('DB_HOST','db')} "
    f"port={os.getenv('DB_PORT','5432')} "
    f"dbname={os.getenv('DB_NAME','netwatch')} "
    f"user={os.getenv('DB_USER','netwatch')} "
    f"password={os.getenv('DB_PASSWORD','netwatch_secret')}"
)
INTERVAL = int(os.getenv("TEST_INTERVAL_MINUTES", "15")) * 60


# ── DB helpers ────────────────────────────────────────────────────────────────
def get_conn():
    for attempt in range(10):
        try:
            return psycopg2.connect(DB_DSN)
        except psycopg2.OperationalError as e:
            log.warning("DB not ready (%s), retry %d/10…", e, attempt + 1)
            time.sleep(5)
    raise RuntimeError("Cannot connect to database after 10 attempts")


def insert_result(conn, row: dict):
    sql = """
        INSERT INTO speed_tests
            (tested_at, download, upload, ping, jitter, packet_loss,
             server_name, server_host, isp, status, error_msg)
        VALUES
            (%(tested_at)s, %(download)s, %(upload)s, %(ping)s, %(jitter)s,
             %(packet_loss)s, %(server_name)s, %(server_host)s, %(isp)s,
             %(status)s, %(error_msg)s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, row)
    conn.commit()


def refresh_views(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT refresh_stats()")
        conn.commit()
        log.info("Materialized views refreshed.")
    except Exception as e:
        log.warning("Could not refresh views: %s", e)
        conn.rollback()


# ── Speed test ────────────────────────────────────────────────────────────────
def run_speedtest() -> dict:
    log.info("Running speed test…")
    try:
        result = subprocess.run(
            ["speedtest-cli", "--json", "--secure"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "speedtest-cli returned non-zero")

        data = json.loads(result.stdout)

        return {
            "tested_at":   datetime.now(timezone.utc),
            "download":    round(data["download"] / 1_000_000, 2),   # bps → Mbps
            "upload":      round(data["upload"]   / 1_000_000, 2),
            "ping":        round(data["ping"], 2),
            "jitter":      round(data.get("jitter", 0), 2),
            "packet_loss": round(data.get("packetLoss", 0.0), 2),
            "server_name": data.get("server", {}).get("name", ""),
            "server_host": data.get("server", {}).get("host", ""),
            "isp":         data.get("client", {}).get("isp", ""),
            "status":      "ok",
            "error_msg":   None,
        }

    except subprocess.TimeoutExpired:
        log.error("Speed test timed out")
        return _error_row("timeout", "Speed test timed out after 120s")
    except Exception as e:
        log.error("Speed test failed: %s", e)
        return _error_row("error", str(e))


def _error_row(status: str, msg: str) -> dict:
    return {
        "tested_at": datetime.now(timezone.utc),
        "download": None, "upload": None, "ping": None,
        "jitter": None, "packet_loss": None,
        "server_name": None, "server_host": None, "isp": None,
        "status": status, "error_msg": msg,
    }


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("NetWatch collector starting. Interval: %d min", INTERVAL // 60)
    conn = get_conn()
    log.info("Connected to database.")

    while True:
        row = run_speedtest()
        try:
            insert_result(conn, row)
            log.info(
                "Saved: ↓%.1f Mbps  ↑%.1f Mbps  ping=%.0f ms  status=%s",
                row["download"] or 0, row["upload"] or 0,
                row["ping"] or 0, row["status"],
            )
            refresh_views(conn)
        except Exception as e:
            log.error("DB error: %s", e)
            try:
                conn.rollback()
                conn = get_conn()
            except Exception:
                pass

        log.info("Sleeping %d minutes…", INTERVAL // 60)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
