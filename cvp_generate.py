#!/usr/bin/env python3
"""Generate VARUS CVP report data (cvp_data.json) from Databricks.

Побудовано за зразком звіту Копійка Груп (cvp-2026.html), але для одного бренду
VARUS з групуванням ПО МІСТАХ. Дані — Jan–Jun 2026, бенчмарк = середнє по всіх
партнерах Bolt Food UA за 2 квартал (кві–чер).
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from databricks import sql as dbsql

_ROOT = Path(__file__).parent


def _load_env():
    for line in (_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

PARTNER = "VARUS"
ALL_MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
ALL_MONTH_LBL = ["Січ", "Лют", "Бер", "Кві", "Тра", "Чер"]
# VARUS стартував у травні 2026 — реальні місяці визначаємо динамічно нижче.
MONTHS = ALL_MONTHS
MONTH_LBL = ALL_MONTH_LBL
Q2 = ["2026-04", "2026-05", "2026-06"]
DATA_START, DATA_END = "2026-01-01", "2026-06-30"
Q2_START, Q2_END = "2026-04-01", "2026-06-30"

FACT = "hive_metastore.ng_delivery_spark.fact_order_delivery"
DIM = "hive_metastore.ng_delivery_spark.dim_provider_v2"
PMON = "hive_metastore.ng_delivery_spark.fact_provider_monthly"
PWEEK = "hive_metastore.ng_delivery_spark.fact_provider_weekly"


def connect():
    kwargs = {}
    if os.environ.get("DATABRICKS_TLS_NO_VERIFY", "").lower() in ("1", "true", "yes"):
        kwargs["_tls_no_verify"] = True
    return dbsql.connect(
        server_hostname=os.environ["DATABRICKS_HOST"],
        http_path=f"/sql/1.0/warehouses/{os.environ['DATABRICKS_WAREHOUSE_ID']}",
        access_token=os.environ["DATABRICKS_TOKEN"],
        **kwargs,
    )


def q(cur, sql):
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        d = {}
        for k, v in zip(cols, row):
            if hasattr(v, "__float__") and not isinstance(v, bool):
                d[k] = float(v)
            else:
                d[k] = v
        out.append(d)
    return out


# provider -> city map (з fact_order_delivery, VARUS, ua)
PROV_CITY = f"""
SELECT f.provider_id, MAX(f.city_name) AS city_name
FROM {FACT} f JOIN {DIM} p ON f.provider_id = p.provider_id
WHERE p.country_code='ua' AND p.group_name='{PARTNER}'
GROUP BY f.provider_id
"""


def main():
    conn = connect()
    cur = conn.cursor()

    print("Phase 1: cities by Q2 orders...")
    city_orders = q(cur, f"""
      SELECT f.city_name AS city, COUNT(*) AS orders
      FROM {FACT} f JOIN {DIM} p ON f.provider_id=p.provider_id
      WHERE p.country_code='ua' AND p.group_name='{PARTNER}'
        AND f.order_state='delivered'
        AND f.order_created_date BETWEEN '{Q2_START}' AND '{Q2_END}'
      GROUP BY 1 ORDER BY 2 DESC
    """)
    top_cities = [r["city"] for r in city_orders[:4]]
    print("  top cities:", top_cities)
    # SQL CASE: місто -> група
    lst = ",".join("'" + c.replace("'", "''") + "'" for c in top_cities)
    GRP = f"CASE WHEN f.city_name IN ({lst}) THEN f.city_name ELSE 'Інші міста' END"
    GRP_P = f"CASE WHEN cm.city_name IN ({lst}) THEN cm.city_name ELSE 'Інші міста' END"
    groups = top_cities + ["Інші міста"]

    print("Phase 2: business by group/month...")
    biz = q(cur, f"""
      SELECT {GRP} AS grp, DATE_FORMAT(f.order_created_date,'yyyy-MM') AS m,
        COUNT(*) AS orders,
        SUM(f.order_gmv_eur) AS gmv_eur,
        COUNT(DISTINCT f.user_id) AS active_users,
        COUNT(DISTINCT CASE WHEN f.is_first_delivery_order THEN f.user_id END) AS new_users,
        COUNT(DISTINCT f.provider_id) AS merchants
      FROM {FACT} f JOIN {DIM} p ON f.provider_id=p.provider_id
      WHERE p.country_code='ua' AND p.group_name='{PARTNER}'
        AND f.order_state='delivered'
        AND f.order_created_date BETWEEN '{DATA_START}' AND '{DATA_END}'
      GROUP BY 1,2
    """)

    # VARUS стартував у травні — залишаємо лише місяці з реальним обсягом (>50 замовлень).
    global MONTHS, MONTH_LBL
    tot = {}
    for r in biz:
        tot[r["m"]] = tot.get(r["m"], 0) + r["orders"]
    keep = [m for m in ALL_MONTHS if tot.get(m, 0) >= 50]
    MONTHS = keep
    MONTH_LBL = [ALL_MONTH_LBL[ALL_MONTHS.index(m)] for m in keep]
    print("  populated months:", MONTHS)

    print("Phase 2: quality by group/month (fact_provider_monthly)...")
    qual = q(cur, f"""
      WITH cm AS ({PROV_CITY})
      SELECT {GRP_P} AS grp, DATE_FORMAT(m.metric_timestamp_local,'yyyy-MM') AS m,
        SUM(m.order_total_minutes_per_order_value*m.order_total_minutes_per_order_weight) AS dur_vw, SUM(m.order_total_minutes_per_order_weight) AS dur_w,
        SUM(m.late_delivery_order_10min_rate_value*m.late_delivery_order_10min_rate_weight) AS late_vw, SUM(m.late_delivery_order_10min_rate_weight) AS late_w,
        SUM(m.provider_late_preparation_10min_rate_value*m.provider_late_preparation_10min_rate_weight) AS prep_vw, SUM(m.provider_late_preparation_10min_rate_weight) AS prep_w,
        SUM(m.failed_order_rate_value*m.failed_order_rate_weight) AS fail_vw, SUM(m.failed_order_rate_weight) AS fail_w,
        SUM(m.bad_order_rate_value*m.bad_order_rate_weight) AS bad_vw, SUM(m.bad_order_rate_weight) AS bad_w,
        SUM(m.cs_ticket_order_rate_value*m.cs_ticket_order_rate_weight) AS cs_vw, SUM(m.cs_ticket_order_rate_weight) AS cs_w,
        SUM(m.order_delivery_completion_rate_value*m.order_delivery_completion_rate_weight) AS compl_vw, SUM(m.order_delivery_completion_rate_weight) AS compl_w,
        SUM(m.provider_rating_per_order_value*m.provider_rating_per_order_weight) AS rat_vw, SUM(m.provider_rating_per_order_weight) AS rat_w,
        SUM(m.provider_active_rate_value*m.provider_active_rate_weight) AS avail_vw, SUM(m.provider_active_rate_weight) AS avail_w
      FROM {PMON} m JOIN cm ON m.provider_id=cm.provider_id
      WHERE DATE_FORMAT(m.metric_timestamp_local,'yyyy-MM') IN ({",".join("'"+x+"'" for x in MONTHS)})
      GROUP BY 1,2
    """)

    print("Phase 2: adjustment (заміни) by group/month (fact_provider_weekly)...")
    adj = q(cur, f"""
      WITH cm AS ({PROV_CITY})
      SELECT {GRP_P} AS grp, DATE_FORMAT(w.metric_timestamp_local,'yyyy-MM') AS m,
        SUM(w.order_item_adjustment_rate_value*w.order_item_adjustment_rate_weight) AS adj_vw,
        SUM(w.order_item_adjustment_rate_weight) AS adj_w
      FROM {PWEEK} w JOIN cm ON w.provider_id=cm.provider_id
      WHERE DATE_FORMAT(w.metric_timestamp_local,'yyyy-MM') IN ({",".join("'"+x+"'" for x in MONTHS)})
      GROUP BY 1,2
    """)

    print("Phase 3: benchmark (country UA, Q2)...")
    bench_biz = q(cur, f"""
      SELECT COUNT(*) AS orders, SUM(f.order_gmv_eur) AS gmv_eur,
        COUNT(DISTINCT f.user_id) AS active_users,
        COUNT(DISTINCT CASE WHEN f.is_first_delivery_order THEN f.user_id END) AS new_users,
        COUNT(DISTINCT f.provider_id) AS merchants
      FROM {FACT} f JOIN {DIM} p ON f.provider_id=p.provider_id
      WHERE p.country_code='ua' AND p.is_bolt_market_provider=true
        AND f.order_state='delivered'
        AND f.order_created_date BETWEEN '{Q2_START}' AND '{Q2_END}'
    """)[0]
    bench_qual = q(cur, f"""
      SELECT
        SUM(m.order_total_minutes_per_order_value*m.order_total_minutes_per_order_weight)/NULLIF(SUM(m.order_total_minutes_per_order_weight),0) AS dur,
        SUM(m.late_delivery_order_10min_rate_value*m.late_delivery_order_10min_rate_weight)/NULLIF(SUM(m.late_delivery_order_10min_rate_weight),0)*100 AS late,
        SUM(m.provider_late_preparation_10min_rate_value*m.provider_late_preparation_10min_rate_weight)/NULLIF(SUM(m.provider_late_preparation_10min_rate_weight),0)*100 AS prep,
        SUM(m.failed_order_rate_value*m.failed_order_rate_weight)/NULLIF(SUM(m.failed_order_rate_weight),0)*100 AS fail,
        SUM(m.bad_order_rate_value*m.bad_order_rate_weight)/NULLIF(SUM(m.bad_order_rate_weight),0)*100 AS bad,
        SUM(m.cs_ticket_order_rate_value*m.cs_ticket_order_rate_weight)/NULLIF(SUM(m.cs_ticket_order_rate_weight),0)*100 AS cs,
        SUM(m.order_delivery_completion_rate_value*m.order_delivery_completion_rate_weight)/NULLIF(SUM(m.order_delivery_completion_rate_weight),0)*100 AS compl,
        SUM(m.provider_rating_per_order_value*m.provider_rating_per_order_weight)/NULLIF(SUM(m.provider_rating_per_order_weight),0) AS rating,
        SUM(m.provider_active_rate_value*m.provider_active_rate_weight)/NULLIF(SUM(m.provider_active_rate_weight),0)*100 AS avail
      FROM {PMON} m JOIN {DIM} p ON m.provider_id=p.provider_id
      WHERE p.country_code='ua' AND p.is_bolt_market_provider=true
        AND DATE_FORMAT(m.metric_timestamp_local,'yyyy-MM') IN ({",".join("'"+x+"'" for x in Q2)})
    """)[0]
    bench_adj = q(cur, f"""
      SELECT SUM(w.order_item_adjustment_rate_value*w.order_item_adjustment_rate_weight)/NULLIF(SUM(w.order_item_adjustment_rate_weight),0)*100 AS adj
      FROM {PWEEK} w JOIN {DIM} p ON w.provider_id=p.provider_id
      WHERE p.country_code='ua' AND p.is_bolt_market_provider=true
        AND DATE_FORMAT(w.metric_timestamp_local,'yyyy-MM') IN ({",".join("'"+x+"'" for x in Q2)})
    """)[0]

    print("Phase 4: store-level Q2 detail...")
    stores = q(cur, f"""
      WITH cm AS (
        SELECT f.provider_id, MAX(f.city_name) AS city_name, MAX(f.provider_name) AS provider_name
        FROM {FACT} f JOIN {DIM} p ON f.provider_id=p.provider_id
        WHERE p.country_code='ua' AND p.group_name='{PARTNER}'
        GROUP BY f.provider_id
      ),
      adj AS (
        SELECT w.provider_id,
          SUM(w.order_item_adjustment_rate_value*w.order_item_adjustment_rate_weight)/NULLIF(SUM(w.order_item_adjustment_rate_weight),0)*100 AS replacement
        FROM {PWEEK} w
        WHERE DATE_FORMAT(w.metric_timestamp_local,'yyyy-MM') IN ({",".join("'"+x+"'" for x in Q2)})
        GROUP BY w.provider_id
      )
      SELECT cm.provider_name AS addr, cm.city_name AS city,
        SUM(m.delivered_orders_count) AS orders,
        SUM(m.order_total_minutes_per_order_value*m.order_total_minutes_per_order_weight)/NULLIF(SUM(m.order_total_minutes_per_order_weight),0) AS dur,
        SUM(m.late_delivery_order_10min_rate_value*m.late_delivery_order_10min_rate_weight)/NULLIF(SUM(m.late_delivery_order_10min_rate_weight),0)*100 AS late,
        SUM(m.provider_late_preparation_10min_rate_value*m.provider_late_preparation_10min_rate_weight)/NULLIF(SUM(m.provider_late_preparation_10min_rate_weight),0)*100 AS lateprep,
        SUM(m.failed_order_rate_value*m.failed_order_rate_weight)/NULLIF(SUM(m.failed_order_rate_weight),0)*100 AS failed,
        SUM(m.bad_order_rate_value*m.bad_order_rate_weight)/NULLIF(SUM(m.bad_order_rate_weight),0)*100 AS bad,
        MAX(a.replacement) AS replacement,
        SUM(m.cs_ticket_order_rate_value*m.cs_ticket_order_rate_weight)/NULLIF(SUM(m.cs_ticket_order_rate_weight),0)*100 AS cs,
        SUM(m.order_delivery_completion_rate_value*m.order_delivery_completion_rate_weight)/NULLIF(SUM(m.order_delivery_completion_rate_weight),0)*100 AS compl,
        SUM(m.provider_rating_per_order_value*m.provider_rating_per_order_weight)/NULLIF(SUM(m.provider_rating_per_order_weight),0) AS rating
      FROM {PMON} m JOIN cm ON m.provider_id=cm.provider_id
        LEFT JOIN adj a ON a.provider_id=m.provider_id
      WHERE DATE_FORMAT(m.metric_timestamp_local,'yyyy-MM') IN ({",".join("'"+x+"'" for x in Q2)})
      GROUP BY cm.provider_name, cm.city_name
      HAVING SUM(m.delivered_orders_count) > 0
      ORDER BY orders DESC
    """)

    cur.close()
    conn.close()

    # ---- reshape ----
    def idx(rows, key="grp"):
        d = {}
        for r in rows:
            d.setdefault(r[key], {})[r["m"]] = r
        return d

    bizd, quald, adjd = idx(biz), idx(qual), idx(adj)

    def series(gmap, g, fn):
        return [fn(gmap.get(g, {}).get(m)) for m in MONTHS]

    def biz_val(g, field, per_merchant=False, freq=False):
        out = []
        for m in MONTHS:
            r = bizd.get(g, {}).get(m)
            if not r:
                out.append(None); continue
            if freq:
                out.append(round(r["orders"] / r["active_users"], 2) if r["active_users"] else None)
            elif per_merchant:
                v, mm = r[field], r["merchants"]
                out.append(round(v / mm, 1) if mm else None)
            else:
                out.append(r[field])
        return out

    def qw(g, vw, w):
        out = []
        for m in MONTHS:
            r = quald.get(g, {}).get(m)
            out.append(round(r[vw] / r[w], 2) if r and r.get(w) else None)
        return out

    def qwp(g, vw, w):  # *100
        out = []
        for m in MONTHS:
            r = quald.get(g, {}).get(m)
            out.append(round(r[vw] / r[w] * 100, 1) if r and r.get(w) else None)
        return out

    def adjv(g):
        out = []
        for m in MONTHS:
            r = adjd.get(g, {}).get(m)
            out.append(round(r["adj_vw"] / r["adj_w"] * 100, 1) if r and r.get("adj_w") else None)
        return out

    Nq2 = 3.0
    b_merch = bench_biz["merchants"] or 1
    metrics = []

    def add(key, label, dirn, fmt, grp, bench, fn):
        metrics.append({"key": key, "label": label, "dir": dirn, "fmt": fmt,
                        "promo": False, "group": grp, "bench": bench,
                        "brands": {g: fn(g) for g in groups}})

    add("GMV", "GMV (€)", 1, "eur", "Бізнес-масштаб",
        round(bench_biz["gmv_eur"] / b_merch / Nq2, 1),
        lambda g: [round(x) if x is not None else None for x in biz_val(g, "gmv_eur")])
    add("Orders", "Замовлення", 1, "int", "Бізнес-масштаб",
        round(bench_biz["orders"] / b_merch / Nq2, 1),
        lambda g: biz_val(g, "orders"))
    add("Active Users", "Активні клієнти", 1, "int", "Бізнес-масштаб",
        round(bench_biz["active_users"] / b_merch / Nq2, 1),
        lambda g: biz_val(g, "active_users"))
    add("New Users", "Нові клієнти", 1, "int", "Бізнес-масштаб",
        round(bench_biz["new_users"] / b_merch / Nq2, 1),
        lambda g: biz_val(g, "new_users"))
    add("Frequency", "Частота замовлень", 1, "dec", "Бізнес-масштаб",
        round(bench_biz["orders"] / bench_biz["active_users"], 2),
        lambda g: biz_val(g, "orders", freq=True))
    add("Active Merchants", "Активні магазини", 1, "int", "Бізнес-масштаб", None,
        lambda g: biz_val(g, "merchants"))

    add("AOV", "Середній чек (AOV)", 1, "eur",
        "Юніт-економіка", round(bench_biz["gmv_eur"] / bench_biz["orders"], 1),
        lambda g: [round(bizd[g][m]["gmv_eur"] / bizd[g][m]["orders"]) if bizd.get(g, {}).get(m) and bizd[g][m]["orders"] else None for m in MONTHS])
    add("GMV per Merchant", "GMV на магазин", 1, "eur",
        "Юніт-економіка", round(bench_biz["gmv_eur"] / b_merch / Nq2, 1),
        lambda g: biz_val(g, "gmv_eur", per_merchant=True))
    add("Orders per Merchant", "Замовлень на магазин", 1, "int",
        "Юніт-економіка", round(bench_biz["orders"] / b_merch / Nq2, 1),
        lambda g: biz_val(g, "orders", per_merchant=True))

    add("Availability", "Доступність магазинів, %", 1, "pct",
        "Доступність", round(bench_qual["avail"], 1), lambda g: qwp(g, "avail_vw", "avail_w"))

    add("Total Delivery Time", "Час доставки", -1, "min",
        "Швидкість і якість", round(bench_qual["dur"], 1), lambda g: qw(g, "dur_vw", "dur_w"))
    add("Late Delivery 10+", "Запізнення (10+хв)", -1, "pct",
        "Швидкість і якість", round(bench_qual["late"], 1), lambda g: qwp(g, "late_vw", "late_w"))
    add("Late Prep 10+", "Пізня підготовка (10+хв)", -1, "pct",
        "Швидкість і якість", round(bench_qual["prep"], 1), lambda g: qwp(g, "prep_vw", "prep_w"))
    add("Failed", "Зірвані замовлення", -1, "pct",
        "Швидкість і якість", round(bench_qual["fail"], 1), lambda g: qwp(g, "fail_vw", "fail_w"))
    add("Bad Order Rate", "Bad Order Rate", -1, "pct",
        "Швидкість і якість", round(bench_qual["bad"], 1), lambda g: qwp(g, "bad_vw", "bad_w"))
    add("Replacement", "Заміни товарів", -1, "pct",
        "Швидкість і якість", round(bench_adj["adj"], 1) if bench_adj["adj"] else None, lambda g: adjv(g))
    add("CS Ticket", "Звернення в підтримку", -1, "pct",
        "Швидкість і якість", round(bench_qual["cs"], 1), lambda g: qwp(g, "cs_vw", "cs_w"))
    add("Completion", "Завершеність", 1, "pct",
        "Швидкість і якість", round(bench_qual["compl"], 1), lambda g: qwp(g, "compl_vw", "compl_w"))
    add("Rating", "Рейтинг", 1, "dec",
        "Швидкість і якість", round(bench_qual["rating"], 2), lambda g: qw(g, "rat_vw", "rat_w"))

    # highlights per group (VARUS: усі місяці належать Q2, порівнюємо перший→останній місяць)
    def mean_all(a):
        a = [x for x in a if x is not None]
        return sum(a) / len(a) if a else None

    def first_last(a):
        vals = [(i, x) for i, x in enumerate(a) if x is not None]
        if len(vals) < 2:
            return None, None
        return vals[0][1], vals[-1][1]

    lbl_first = MONTH_LBL[0] if MONTH_LBL else ""
    lbl_last = MONTH_LBL[-1] if MONTH_LBL else ""

    highlights = {}
    for g in groups:
        hs = []
        gaps = []
        for m in metrics:
            if m["bench"] is None or m["dir"] == 0:
                continue
            a = mean_all(m["brands"][g])
            if a is None:
                continue
            worse = (m["dir"] == 1 and a < m["bench"]) or (m["dir"] == -1 and a > m["bench"])
            if worse and m["group"] == "Швидкість і якість":
                gaps.append((m["label"], a, m["fmt"]))
        # orders trend (перший→останній місяць)
        a1, a2 = first_last(metrics[1]["brands"][g])
        if a1 is not None:
            lvl = "win" if a2 >= a1 else "watch"
            hs.append({"level": lvl, "kind": "Замовлення",
                       "text": f"Обсяг замовлень {round(a1)}→{round(a2)} ({lbl_first}→{lbl_last}), "
                               f"{'зростання' if a2>=a1 else 'спад'} {round(abs(a2-a1)/a1*100) if a1 else 0}%."})
        # new users trend
        n1, n2 = first_last(metrics[3]["brands"][g])
        if n1 is not None:
            lvl = "win" if n2 >= n1 else "watch"
            hs.append({"level": lvl, "kind": "Нові клієнти",
                       "text": f"Нові клієнти {round(n1)}→{round(n2)} ({lbl_first}→{lbl_last})."})
        if gaps:
            def fmt_g(v, f):
                if f == "min": return f"{round(v)} хв"
                if f == "pct": return f"{round(v)}%"
                if f == "dec": return f"{v:.2f}"
                return str(round(v))
            txt = "; ".join(f"{lab} ({fmt_g(v,f)})" for lab, v, f in sorted(gaps, key=lambda x: -x[1])[:4])
            hs.append({"level": "crit", "kind": "Де посилитись", "text": f"Найбільші розриви з ринком: {txt}."})
        highlights[g] = hs

    # stores reshape: brand = group (city bucket)
    def city_group(c):
        return c if c in top_cities else "Інші міста"
    for s in stores:
        s["brand"] = city_group(s["city"])
        for k in ("orders", "dur", "late", "lateprep", "failed", "bad", "replacement", "cs", "compl", "rating"):
            if s.get(k) is not None:
                s[k] = round(s[k], 2)

    store_cols = [
        {"key": "orders", "label": "Замовлень", "fmt": "int", "dir": 0},
        {"key": "dur", "label": "Час доставки, хв", "fmt": "min", "dir": -1},
        {"key": "late", "label": "Запізнення 10+, %", "fmt": "pct", "dir": -1},
        {"key": "lateprep", "label": "Пізня підготовка 10+, %", "fmt": "pct", "dir": -1},
        {"key": "failed", "label": "Зірвані замовл., %", "fmt": "pct", "dir": -1},
        {"key": "bad", "label": "Bad Order Rate, %", "fmt": "pct", "dir": -1},
        {"key": "replacement", "label": "Заміни товарів, %", "fmt": "pct", "dir": -1},
        {"key": "cs", "label": "CS-тікети, %", "fmt": "pct", "dir": -1},
        {"key": "compl", "label": "Завершеність, %", "fmt": "pct", "dir": 1},
        {"key": "rating", "label": "Рейтинг", "fmt": "dec", "dir": 1},
    ]

    R = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "months": MONTHS, "month_lbl": MONTH_LBL,
        "group": groups,
        "group_lbl": {g: g for g in groups},
        "metrics": metrics,
        "highlights": highlights,
        "stores": stores,
        "store_period": "квітень–червень 2026 (Q2)",
        "store_cols": store_cols,
        "bench_note": f"середнє по продуктових партнерах (Bolt Market) в Україні за Q2 — {int(bench_biz['merchants'])} магазинів",
    }
    (_ROOT / "cvp_data.json").write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Saved cvp_data.json | groups:", groups, "| stores:", len(stores))

    tpl = (_ROOT / "cvp_template.html").read_text(encoding="utf-8")
    html = tpl.replace("/*__CVP_DATA__*/", json.dumps(R, ensure_ascii=False))
    (_ROOT / "cvp-2026.html").write_text(html, encoding="utf-8")
    print("Saved cvp-2026.html")


if __name__ == "__main__":
    main()
