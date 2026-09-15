#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Дані для внутрішньої версії MBR (mbr-internal.html).

Тягне з Databricks те, чого немає в готових JSON:
  - банерні та інші джерела переходів у магазин VARUS,
  - промо-інвестиції Bolt проти VARUS (item promo + delivery fee),
  - доступність магазинів разом з NDR і часом доставки.

CVP-фанел і ретеншн беремо з локальних файлів:
  ../UA-CVP/cvp_data.json, ../Partner-Ops-Analysis/output/VARUS/ops_data.json

Запуск:
  python3 fetch_mbr_internal.py            # → mbr_internal_data.json
"""
import json
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT.parent / "Partner-Ops-Analysis"))

from dbx import run_cols, rows_to_dicts  # noqa: E402

PARTNER = "VARUS"
START = "2026-04-01"
END = "2026-08-31"
MONTH_START = "2026-06-01"  # для розрізів по магазинах беремо робочий режим

BASE_JOIN = """
FROM main.ng_delivery.fact_order_delivery f
JOIN main.ng_delivery.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua' AND p.group_name = '{partner}'
  AND f.order_created_date >= DATE'{start}' AND f.order_created_date <= DATE'{end}'
"""


def _base(start=START, end=END):
    return BASE_JOIN.format(partner=PARTNER, start=start, end=end)


# --- 1. Промо-інвестиції та комісія по місяцях -------------------------
def q_promo_monthly():
    return f"""
SELECT DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS month,
  COUNT(*) AS orders,
  SUM(CASE WHEN f.order_state = 'delivered' THEN 1 ELSE 0 END) AS delivered,
  ROUND(SUM(CASE WHEN f.order_state = 'delivered' THEN COALESCE(f.order_gmv_eur,0) ELSE 0 END), 0) AS gmv_eur,
  ROUND(SUM(COALESCE(f.commission_eur,0)), 0) AS commission_eur,
  ROUND(SUM(COALESCE(f.total_order_item_discount_eur,0)), 0) AS item_promo_eur,
  ROUND(SUM(COALESCE(f.delivery_price_before_discount_eur,0) - COALESCE(f.delivery_price_after_discount_eur,0)), 0) AS df_promo_eur,
  ROUND(SUM(COALESCE(f.delivery_price_after_discount_eur,0)), 0) AS df_paid_by_user_eur,
  SUM(CASE WHEN f.order_state = 'delivered'
        AND f.delivery_price_before_discount_eur > f.delivery_price_after_discount_eur
      THEN 1 ELSE 0 END) AS df_promo_delivered,
  ROUND(SUM(CASE WHEN f.order_state = 'delivered'
        AND f.delivery_price_before_discount_eur > f.delivery_price_after_discount_eur
      THEN COALESCE(f.order_gmv_eur,0) ELSE 0 END), 0) AS df_promo_gmv_eur,
  SUM(CASE WHEN f.order_state = 'delivered'
        AND f.delivery_price_after_discount_eur = 0 AND f.delivery_price_before_discount_eur > 0
      THEN 1 ELSE 0 END) AS free_delivery_delivered,
  ROUND(AVG(CASE WHEN f.order_state = 'delivered'
        AND f.delivery_price_before_discount_eur > f.delivery_price_after_discount_eur
      THEN f.order_gmv_eur END), 2) AS aov_with_df_promo_eur,
  ROUND(AVG(CASE WHEN f.order_state = 'delivered'
        AND f.delivery_price_before_discount_eur <= f.delivery_price_after_discount_eur
      THEN f.order_gmv_eur END), 2) AS aov_without_df_promo_eur
{_base()}
GROUP BY 1 ORDER BY 1
"""


# --- 2. Хто фінансує промо: об'єктив кампанії + cost share ------------
def q_promo_funding():
    return f"""
SELECT DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS month,
  COALESCE(d.campaign_spend_objective, 'unknown') AS objective,
  MAX(c.cost_share_percentage) AS cost_share_pct,
  COUNT(DISTINCT c.order_id) AS orders,
  ROUND(SUM(c.price_before_discount - c.price_after_discount), 0) AS discount_local
FROM main.ng_delivery.etl_delivery_order_campaign c
JOIN main.ng_delivery.fact_order_delivery f ON f.order_id = c.order_id
JOIN main.ng_delivery.dim_provider_v2 p ON f.provider_id = p.provider_id
LEFT JOIN main.ng_delivery.dim_campaign_delivery_v2 d ON d.campaign_id = c.campaign_id
WHERE p.country_code = 'ua' AND p.group_name = '{PARTNER}'
  AND c.created_date >= '{START}' AND c.created_date <= '{END}'
  AND f.order_created_date >= DATE'{START}' AND f.order_created_date <= DATE'{END}'
GROUP BY 1, 2 ORDER BY 1, discount_local DESC
"""


# --- 3. Джерела переходів у магазин (банери проти решти) --------------
def q_traffic_sources():
    return f"""
SELECT DATE_FORMAT(s.session_start_date, 'yyyy-MM') AS month,
  COALESCE(s.source, 'unknown') AS source,
  COUNT(*) AS provider_views,
  SUM(COALESCE(s.has_product_added_event,0)) AS carts,
  SUM(COALESCE(s.has_order_placed,0)) AS orders,
  ROUND(SUM(COALESCE(s.has_order_placed,0)) * 100.0 / COUNT(*), 1) AS conv_pct,
  ROUND(SUM(COALESCE(s.total_price_before_discount_eur,0)), 0) AS gmv_eur,
  SUM(COALESCE(s.is_first_order_by_user,0)) AS first_orders
FROM main.ng_delivery.etl_delivery_eater_session_provider_viewed_sources s
JOIN main.ng_delivery.dim_provider_v2 p ON s.provider_id = p.provider_id
WHERE p.country_code = 'ua' AND p.group_name = '{PARTNER}'
  AND s.session_start_date >= DATE'{MONTH_START}' AND s.session_start_date <= DATE'{END}'
GROUP BY 1, 2 ORDER BY 1, provider_views DESC
"""


# --- 4. Доступність магазину + NDR + час доставки ---------------------
def q_store_health():
    return f"""
WITH orders AS (
  SELECT f.provider_id,
    MAX(p.provider_name) AS store,
    MAX(p.city_name) AS city,
    COUNT(*) AS total,
    SUM(CASE WHEN f.order_state IN ('failed','rejected') THEN 1 ELSE 0 END) AS failed,
    ROUND(SUM(CASE WHEN f.order_state IN ('failed','rejected') THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS ndr_pct,
    ROUND(SUM(CASE WHEN f.order_state IN ('failed','rejected') THEN COALESCE(f.order_gmv_eur,0) ELSE 0 END), 0) AS lost_gmv_eur,
    ROUND(PERCENTILE(f.order_delivery_minutes, 0.5), 1) AS dt_p50
  {_base(MONTH_START, END)}
  GROUP BY f.provider_id
  HAVING COUNT(*) >= 100
),
avail AS (
  SELECT a.provider_id,
    ROUND(AVG(a.availability) * 100, 1) AS availability_pct,
    ROUND(MIN(a.availability) * 100, 1) AS worst_day_pct
  FROM main.ng_delivery.etl_delivery_provider_daily_availability a
  WHERE a.created_date >= DATE'{MONTH_START}' AND a.created_date <= DATE'{END}'
  GROUP BY a.provider_id
)
SELECT o.store, o.city, o.total, o.failed, o.ndr_pct, o.lost_gmv_eur, o.dt_p50,
  v.availability_pct, v.worst_day_pct
FROM orders o LEFT JOIN avail v ON v.provider_id = o.provider_id
ORDER BY o.lost_gmv_eur DESC
LIMIT 20
"""


QUERIES = [
    ("promo_monthly", q_promo_monthly),
    ("promo_funding", q_promo_funding),
    ("traffic_sources", q_traffic_sources),
    ("store_health", q_store_health),
]


def _num(v):
    if v is None or isinstance(v, (int, float)):
        return v
    try:
        return int(v) if "." not in v and "e" not in v.lower() else float(v)
    except (TypeError, ValueError):
        return v


def main():
    data = {
        "meta": {
            "partner": PARTNER,
            "data_start": START,
            "data_end": END,
            "store_period_start": MONTH_START,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "definitions": {
                "delivered": "order_state = 'delivered'",
                "ndr": "order_state IN ('failed','rejected') / усі створені",
                "item_promo_eur": "total_order_item_discount_eur — знижка на товари",
                "df_promo_eur": "delivery_price_before_discount_eur − delivery_price_after_discount_eur",
                "cost_share_pct": "частка знижки, яку покриває мерчант (V2 cost share)",
                "provider_views": "сесії з переглядом магазину VARUS у розрізі джерела переходу",
            },
        }
    }

    for name, fn in QUERIES:
        print(f"  → {name}...")
        cols, rows = run_cols(fn())
        data[name] = [{k: _num(v) for k, v in row.items()} for row in rows_to_dicts(cols, rows)]

    out = _ROOT / "mbr_internal_data.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ {out.name} — {out.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
