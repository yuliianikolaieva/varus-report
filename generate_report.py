#!/usr/bin/env python3
"""Generate VARUS report (index.html) from Databricks.

Скелет на основі Hop Hey QBR. Тягне з Databricks:
  - Monthly metrics (з 2026-05-01, виключно завершені місяці)
  - Last 4 full weeks
  - Фінансові + операційні KPI, refunds, campaigns, acceptance/availability, top stores

Ставимо плейсхолдери — агент / користувач замінює:
  VARUS      — точне значення dim_provider_v2.group_name (UPPERCASE як у БД)
  VARUS   — людська назва для логів і JSON
  2026-05-01        — стартова дата monthly даних, формат YYYY-MM-DD
                          (рекомендовано перший місяць коли партнер пішов у живе)
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from databricks import sql as dbsql

_ROOT = Path(__file__).parent


def _load_dotenv():
    """Завантажити локальний .env (НЕ комітимо в git)."""
    env_file = _ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(
            f"Missing {name}. Створіть {_ROOT / '.env'} з .env.example або експортуйте змінну.",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


DATABRICKS_HOST = _require_env("DATABRICKS_HOST")
DATABRICKS_TOKEN = _require_env("DATABRICKS_TOKEN")
if DATABRICKS_TOKEN.startswith("your") or len(DATABRICKS_TOKEN) < 32:
    print(
        "DATABRICKS_TOKEN у .env — заглушка з .env.example.\n"
        "Databricks → User Settings → Developer → Access tokens → Generate new token.\n"
        "Вставте у .env: DATABRICKS_TOKEN=dapi... (без лапок).",
        file=sys.stderr,
    )
    sys.exit(1)
DATABRICKS_HTTP_PATH = os.environ.get("DATABRICKS_HTTP_PATH", "")
DATABRICKS_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")

PARTNER_NAME = "VARUS"
PARTNER_DISPLAY = "VARUS"
DATA_START = "2026-05-01"

TEMPLATE_PATH = _ROOT / "template.html"
OUTPUT_PATH = _ROOT / "index.html"
DATA_PATH = _ROOT / "report_data.json"


def _connect_kwargs():
    """Додаткові kwargs. DATABRICKS_TLS_NO_VERIFY=1 — лише локально на Mac за корпоративним проксі."""
    kwargs = {}
    if os.environ.get("DATABRICKS_TLS_NO_VERIFY", "").strip().lower() in ("1", "true", "yes"):
        kwargs["_tls_no_verify"] = True
    return kwargs


def get_connection():
    extra = _connect_kwargs()
    if DATABRICKS_HTTP_PATH:
        return dbsql.connect(
            server_hostname=DATABRICKS_HOST,
            http_path=DATABRICKS_HTTP_PATH,
            access_token=DATABRICKS_TOKEN,
            **extra,
        )
    return dbsql.connect(
        server_hostname=DATABRICKS_HOST,
        http_path=f"/sql/1.0/warehouses/{DATABRICKS_WAREHOUSE_ID}",
        access_token=DATABRICKS_TOKEN,
        **extra,
    )


def run_query(cursor, query):
    cursor.execute(query)
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def to_serializable(rows):
    out = []
    for row in rows:
        d = {}
        for k, v in row.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
            elif hasattr(v, "as_py"):
                d[k] = v.as_py()
            elif hasattr(v, "__float__"):
                d[k] = float(v)
            elif hasattr(v, "__int__"):
                d[k] = int(v)
            else:
                d[k] = v
        out.append(d)
    return out


def _data_end():
    """Останній день попереднього завершеного місяця (виключаємо поточний місяць)."""
    today = datetime.now().date()
    first_of_current = today.replace(day=1)
    return str(first_of_current - timedelta(days=1))


def _week_boundaries():
    """4 повні тижні Mon–Sun, до останньої завершеної неділі."""
    today = datetime.now().date()
    last_sunday = today - timedelta(days=today.isoweekday())
    four_weeks_ago_monday = last_sunday - timedelta(days=27)
    return str(four_weeks_ago_monday), str(last_sunday)


DATA_END = _data_end()
WEEKLY_START, WEEKLY_END = _week_boundaries()


# ---------------------------------------------------------------------------
# SQL Queries — Monthly
# ---------------------------------------------------------------------------

FINANCIAL_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period,
    COUNT(*) AS orders,
    SUM(f.provider_price_before_discount) AS merchant_price_uah,
    SUM(f.provider_price_before_discount) / NULLIF(COUNT(*), 0) AS merchant_price_per_order,
    SUM(f.order_gmv) AS gmv_uah,
    SUM(f.order_gmv) / NULLIF(COUNT(*), 0) AS aov_uah,
    COUNT(DISTINCT CASE WHEN f.is_first_delivery_order THEN f.user_id END) AS users_activated,
    COUNT(DISTINCT f.user_id) AS active_users,
    SUM(f.total_refunded_amount) / NULLIF(SUM(f.order_gmv), 0) * 100 AS refund_rate_pct,
    SUM(f.order_gmv) / NULLIF(SUM(f.order_gmv_eur), 0) AS eur_uah_rate
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_state = 'delivered'
  AND f.order_created_date >= '{DATA_START}'
  AND f.order_created_date <= '{DATA_END}'
GROUP BY 1
ORDER BY 1
"""

FINANCIAL_WEEKLY = f"""
SELECT
    DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd') AS period,
    COUNT(*) AS orders,
    SUM(f.provider_price_before_discount) AS merchant_price_uah,
    SUM(f.provider_price_before_discount) / NULLIF(COUNT(*), 0) AS merchant_price_per_order,
    SUM(f.order_gmv) AS gmv_uah,
    SUM(f.order_gmv) / NULLIF(COUNT(*), 0) AS aov_uah,
    COUNT(DISTINCT CASE WHEN f.is_first_delivery_order THEN f.user_id END) AS users_activated,
    COUNT(DISTINCT f.user_id) AS active_users,
    SUM(f.total_refunded_amount) / NULLIF(SUM(f.order_gmv), 0) * 100 AS refund_rate_pct,
    SUM(f.order_gmv) / NULLIF(SUM(f.order_gmv_eur), 0) AS eur_uah_rate
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_state = 'delivered'
  AND f.order_created_date >= '{WEEKLY_START}'
  AND f.order_created_date <= '{WEEKLY_END}'
GROUP BY 1
ORDER BY 1
"""

OPERATIONAL_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period,
    COUNT(*) AS delivered_orders,
    COUNT(DISTINCT f.provider_id) AS active_stores,
    SUM(CASE WHEN f.is_honey_order THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) * 100 AS honey_order_rate,
    SUM(CASE WHEN f.is_bad_order THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) * 100 AS bad_order_rate,
    SUM(CASE WHEN f.is_order_delivered_5_min_late THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) * 100 AS late_delivery_rate,
    SUM(CASE WHEN f.is_order_late_to_partner_5_min THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) * 100 AS late_pickup_rate,
    AVG(f.order_delivery_minutes) AS avg_delivery_minutes,
    AVG(f.courier_delivery_time_min) AS avg_courier_delivery_min
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_state = 'delivered'
  AND f.order_created_date >= '{DATA_START}'
  AND f.order_created_date <= '{DATA_END}'
GROUP BY 1
ORDER BY 1
"""

OPERATIONAL_WEEKLY = OPERATIONAL_MONTHLY.replace(
    "DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period",
    "DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd') AS period",
).replace(
    f"AND f.order_created_date >= '{DATA_START}'\n  AND f.order_created_date <= '{DATA_END}'",
    f"AND f.order_created_date >= '{WEEKLY_START}'\n  AND f.order_created_date <= '{WEEKLY_END}'",
)

REPLACEMENT_ADJUSTMENT_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.metric_timestamp_local, 'yyyy-MM') AS period,
    ROUND(SUM(f.order_item_adjustment_rate_value * f.order_item_adjustment_rate_weight)
        / NULLIF(SUM(f.order_item_adjustment_rate_weight), 0) * 100, 2) AS adjustment_rate,
    ROUND(SUM(f.order_item_replacement_rate_value * f.order_item_replacement_rate_weight)
        / NULLIF(SUM(f.order_item_replacement_rate_weight), 0) * 100, 2) AS replacement_rate
FROM hive_metastore.ng_delivery_spark.fact_provider_weekly f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.metric_timestamp_local >= '{DATA_START}'
  AND f.metric_timestamp_local <= '{DATA_END}'
GROUP BY 1
ORDER BY 1
"""

REPLACEMENT_ADJUSTMENT_WEEKLY = f"""
SELECT
    DATE_FORMAT(f.metric_timestamp_local, 'yyyy-MM-dd') AS period,
    ROUND(SUM(f.order_item_adjustment_rate_value * f.order_item_adjustment_rate_weight)
        / NULLIF(SUM(f.order_item_adjustment_rate_weight), 0) * 100, 2) AS adjustment_rate,
    ROUND(SUM(f.order_item_replacement_rate_value * f.order_item_replacement_rate_weight)
        / NULLIF(SUM(f.order_item_replacement_rate_weight), 0) * 100, 2) AS replacement_rate
FROM hive_metastore.ng_delivery_spark.fact_provider_weekly f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.metric_timestamp_local >= '{WEEKLY_START}'
  AND f.metric_timestamp_local <= '{WEEKLY_END}'
GROUP BY 1
ORDER BY 1
"""

FAILED_ORDERS_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period,
    COUNT(*) AS total_placed,
    SUM(CASE WHEN f.order_state = 'delivered' THEN 1 ELSE 0 END) AS delivered,
    SUM(CASE WHEN f.order_state != 'delivered' THEN 1 ELSE 0 END) AS failed_total,
    SUM(CASE WHEN f.order_state != 'delivered' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) * 100 AS failed_rate_pct
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_created_date >= '{DATA_START}'
  AND f.order_created_date <= '{DATA_END}'
GROUP BY 1
ORDER BY 1
"""

FAILED_ORDERS_WEEKLY = FAILED_ORDERS_MONTHLY.replace(
    "DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period",
    "DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd') AS period",
).replace(
    f"AND f.order_created_date >= '{DATA_START}'\n  AND f.order_created_date <= '{DATA_END}'",
    f"AND f.order_created_date >= '{WEEKLY_START}'\n  AND f.order_created_date <= '{WEEKLY_END}'",
)

FAILED_REASONS_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period,
    r.reason,
    r.actor_type,
    COUNT(*) AS cnt
FROM hive_metastore.ng_delivery_spark.delivery_order_order_resolution r
    JOIN hive_metastore.ng_delivery_spark.fact_order_delivery f ON r.order_id = f.order_id
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_created_date >= '{DATA_START}'
  AND f.order_created_date <= '{DATA_END}'
  AND f.order_state != 'delivered'
GROUP BY 1, r.reason, r.actor_type
ORDER BY 1, cnt DESC
"""

FAILED_REASONS_WEEKLY = f"""
SELECT
    DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd') AS period,
    r.reason,
    r.actor_type,
    COUNT(*) AS cnt
FROM hive_metastore.ng_delivery_spark.delivery_order_order_resolution r
    JOIN hive_metastore.ng_delivery_spark.fact_order_delivery f ON r.order_id = f.order_id
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_created_date >= '{WEEKLY_START}'
  AND f.order_created_date <= '{WEEKLY_END}'
  AND f.order_state != 'delivered'
GROUP BY 1, r.reason, r.actor_type
ORDER BY 1, cnt DESC
"""

CAMPAIGNS_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period,
    SUM(f.demand_incentives_local) AS campaigns_discount_uah,
    SUM(
        COALESCE(f.bolt_spend_am_spend_campaign, 0)
        + COALESCE(f.bolt_spend_liquidity_campaign, 0)
        + COALESCE(f.bolt_spend_marketing_campaign, 0)
        + COALESCE(f.bolt_spend_user_lifecycle_campaign, 0)
        + COALESCE(f.bolt_spend_merchant_lifecycle_campaign, 0)
        + COALESCE(f.bolt_spend_other_campaign, 0)
    ) AS bolt_spend_eur,
    SUM(
        COALESCE(f.provider_spend_am_spend_campaign, 0)
        + COALESCE(f.provider_spend_liquidity_campaign, 0)
        + COALESCE(f.provider_spend_marketing_campaign, 0)
        + COALESCE(f.provider_spend_user_lifecycle_campaign, 0)
        + COALESCE(f.provider_spend_merchant_lifecycle_campaign, 0)
        + COALESCE(f.provider_spend_other_campaign, 0)
    ) AS merchant_spend_eur,
    COUNT(CASE WHEN f.demand_incentives_local > 0 THEN 1 END) AS campaign_orders
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_state = 'delivered'
  AND f.order_created_date >= '{DATA_START}'
  AND f.order_created_date <= '{DATA_END}'
GROUP BY 1
ORDER BY 1
"""

CAMPAIGNS_WEEKLY = CAMPAIGNS_MONTHLY.replace(
    "DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period",
    "DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd') AS period",
).replace(
    f"AND f.order_created_date >= '{DATA_START}'\n  AND f.order_created_date <= '{DATA_END}'",
    f"AND f.order_created_date >= '{WEEKLY_START}'\n  AND f.order_created_date <= '{WEEKLY_END}'",
)

ACCEPTANCE_AVAILABILITY = f"""
SELECT
    ROUND(SUM(f.provider_acceptance_rate_value * f.provider_acceptance_rate_weight)
        / NULLIF(SUM(f.provider_acceptance_rate_weight), 0) * 100, 1) AS acceptance_rate,
    ROUND(SUM(f.provider_active_rate_value * f.provider_active_rate_weight)
        / NULLIF(SUM(f.provider_active_rate_weight), 0) * 100, 1) AS availability_rate,
    ROUND(SUM(f.provider_rating_per_order_value * f.provider_rating_per_order_weight)
        / NULLIF(SUM(f.provider_rating_per_order_weight), 0), 3) AS avg_rating
FROM hive_metastore.ng_delivery_spark.fact_provider_weekly f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.metric_timestamp_local >= DATE_SUB(CURRENT_DATE(), 7)
"""

ACCEPTANCE_AVAILABILITY_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.metric_timestamp_local, 'yyyy-MM') AS period,
    ROUND(SUM(f.provider_acceptance_rate_value * f.provider_acceptance_rate_weight)
        / NULLIF(SUM(f.provider_acceptance_rate_weight), 0) * 100, 1) AS acceptance_rate,
    ROUND(SUM(f.provider_active_rate_value * f.provider_active_rate_weight)
        / NULLIF(SUM(f.provider_active_rate_weight), 0) * 100, 1) AS availability_rate,
    ROUND(SUM(f.provider_rating_per_order_value * f.provider_rating_per_order_weight)
        / NULLIF(SUM(f.provider_rating_per_order_weight), 0), 3) AS avg_rating
FROM hive_metastore.ng_delivery_spark.fact_provider_weekly f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.metric_timestamp_local >= '{DATA_START}'
  AND f.metric_timestamp_local <= '{DATA_END}'
GROUP BY 1
ORDER BY 1
"""

ACCEPTANCE_AVAILABILITY_WEEKLY = f"""
SELECT
    DATE_FORMAT(f.metric_timestamp_local, 'yyyy-MM-dd') AS period,
    ROUND(SUM(f.provider_acceptance_rate_value * f.provider_acceptance_rate_weight)
        / NULLIF(SUM(f.provider_acceptance_rate_weight), 0) * 100, 1) AS acceptance_rate,
    ROUND(SUM(f.provider_active_rate_value * f.provider_active_rate_weight)
        / NULLIF(SUM(f.provider_active_rate_weight), 0) * 100, 1) AS availability_rate,
    ROUND(SUM(f.provider_rating_per_order_value * f.provider_rating_per_order_weight)
        / NULLIF(SUM(f.provider_rating_per_order_weight), 0), 3) AS avg_rating
FROM hive_metastore.ng_delivery_spark.fact_provider_weekly f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.metric_timestamp_local >= '{WEEKLY_START}'
  AND f.metric_timestamp_local <= '{WEEKLY_END}'
GROUP BY 1
ORDER BY 1
"""

TOP_STORES_LAST_MONTH = f"""
SELECT
    f.provider_name,
    f.city_name,
    COUNT(*) AS orders,
    SUM(f.provider_price_before_discount) AS merchant_price_uah
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_state = 'delivered'
  AND f.order_created_date >= DATE_FORMAT(ADD_MONTHS(DATE_TRUNC('month', CURRENT_DATE()), -1), 'yyyy-MM-dd')
  AND f.order_created_date < DATE_FORMAT(DATE_TRUNC('month', CURRENT_DATE()), 'yyyy-MM-dd')
GROUP BY 1, 2
ORDER BY merchant_price_uah DESC
LIMIT 15
"""


def main():
    print(f"Partner: {PARTNER_DISPLAY} ({PARTNER_NAME})")
    print(f"Monthly window: {DATA_START} — {DATA_END}")
    print(f"Weekly window: {WEEKLY_START} — {WEEKLY_END}")
    print("Connecting to Databricks...")
    conn = get_connection()
    cursor = conn.cursor()

    print("Fetching financial data...")
    fin_m = to_serializable(run_query(cursor, FINANCIAL_MONTHLY))
    fin_w = to_serializable(run_query(cursor, FINANCIAL_WEEKLY))

    print("Fetching operational data...")
    ops_m = to_serializable(run_query(cursor, OPERATIONAL_MONTHLY))
    ops_w = to_serializable(run_query(cursor, OPERATIONAL_WEEKLY))

    print("Fetching replacement/adjustment rates...")
    repl_m = to_serializable(run_query(cursor, REPLACEMENT_ADJUSTMENT_MONTHLY))
    repl_w = to_serializable(run_query(cursor, REPLACEMENT_ADJUSTMENT_WEEKLY))

    print("Fetching failed orders...")
    fail_m = to_serializable(run_query(cursor, FAILED_ORDERS_MONTHLY))
    fail_w = to_serializable(run_query(cursor, FAILED_ORDERS_WEEKLY))

    print("Fetching failed order reasons...")
    fail_reasons_m = to_serializable(run_query(cursor, FAILED_REASONS_MONTHLY))
    fail_reasons_w = to_serializable(run_query(cursor, FAILED_REASONS_WEEKLY))

    print("Fetching campaign data...")
    camp_m = to_serializable(run_query(cursor, CAMPAIGNS_MONTHLY))
    camp_w = to_serializable(run_query(cursor, CAMPAIGNS_WEEKLY))

    print("Fetching acceptance/availability...")
    aa_current = to_serializable(run_query(cursor, ACCEPTANCE_AVAILABILITY))
    aa_m = to_serializable(run_query(cursor, ACCEPTANCE_AVAILABILITY_MONTHLY))
    aa_w = to_serializable(run_query(cursor, ACCEPTANCE_AVAILABILITY_WEEKLY))

    print("Fetching top stores...")
    top_stores = to_serializable(run_query(cursor, TOP_STORES_LAST_MONTH))

    cursor.close()
    conn.close()

    report_data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "data_start": DATA_START,
        "data_end": DATA_END,
        "weekly_start": WEEKLY_START,
        "weekly_end": WEEKLY_END,
        "partner_name": PARTNER_NAME,
        "partner_display": PARTNER_DISPLAY,
        "monthly": {
            "financial": fin_m,
            "operational": ops_m,
            "replacement_adjustment": repl_m,
            "failed_orders": fail_m,
            "failed_reasons": fail_reasons_m,
            "campaigns": camp_m,
            "acceptance_availability": aa_m,
        },
        "weekly": {
            "financial": fin_w,
            "operational": ops_w,
            "replacement_adjustment": repl_w,
            "failed_orders": fail_w,
            "failed_reasons": fail_reasons_w,
            "campaigns": camp_w,
            "acceptance_availability": aa_w,
        },
        "acceptance_current": aa_current,
        "top_stores": top_stores,
    }

    DATA_PATH.write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"Data saved to {DATA_PATH}")

    print("Generating index.html...")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    js_data = f"const REPORT_DATA = {json.dumps(report_data, ensure_ascii=False, default=str)};"
    html = template.replace("/*__REPORT_DATA__*/", js_data)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"Done! Report written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
