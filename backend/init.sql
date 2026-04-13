-- NetWatch Database Schema

CREATE TABLE IF NOT EXISTS speed_tests (
    id          BIGSERIAL PRIMARY KEY,
    tested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    download    NUMERIC(10,2),   -- Mbps
    upload      NUMERIC(10,2),   -- Mbps
    ping        NUMERIC(8,2),    -- ms
    jitter      NUMERIC(8,2),    -- ms
    packet_loss NUMERIC(5,2),    -- %
    server_name TEXT,
    server_host TEXT,
    isp         TEXT,
    status      TEXT NOT NULL DEFAULT 'ok',  -- ok | error | timeout
    error_msg   TEXT
);

CREATE INDEX idx_speed_tests_tested_at ON speed_tests (tested_at DESC);
CREATE INDEX idx_speed_tests_status    ON speed_tests (status);

-- Aggregated hourly view for fast dashboard queries
CREATE MATERIALIZED VIEW hourly_stats AS
SELECT
    date_trunc('hour', tested_at)      AS hour,
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
GROUP BY date_trunc('hour', tested_at)
ORDER BY hour DESC;

CREATE UNIQUE INDEX ON hourly_stats (hour);

-- Daily stats view
CREATE MATERIALIZED VIEW daily_stats AS
SELECT
    date_trunc('day', tested_at)       AS day,
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
GROUP BY date_trunc('day', tested_at)
ORDER BY day DESC;

CREATE UNIQUE INDEX ON daily_stats (day);

-- Weekday/hour heatmap (last 30 days)
CREATE VIEW heatmap_stats AS
SELECT
    EXTRACT(DOW  FROM tested_at)::INT  AS dow,       -- 0=Sun…6=Sat
    EXTRACT(HOUR FROM tested_at)::INT  AS hour_of_day,
    ROUND(AVG(download)::NUMERIC, 2)   AS avg_download,
    ROUND(AVG(ping)::NUMERIC, 2)       AS avg_ping,
    COUNT(*)                           AS samples
FROM speed_tests
WHERE tested_at >= NOW() - INTERVAL '30 days'
  AND status = 'ok'
GROUP BY dow, hour_of_day;

-- Helper: refresh materialized views (call from collector)
CREATE OR REPLACE FUNCTION refresh_stats() RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY hourly_stats;
    REFRESH MATERIALIZED VIEW CONCURRENTLY daily_stats;
END;
$$;
