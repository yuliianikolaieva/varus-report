# VARUS — Bolt Food UA

Автоматичний бізнес-огляд партнера **VARUS** на Bolt Food UA.

**Живий звіт:** https://yuliianikolaieva.github.io/varus-report/

**Операційний аналіз (failed/bad orders, users):** https://yuliianikolaieva.github.io/varus-report/ops-quality-analysis.html

**Кешбек-тест вересня 2026 (результати + калібрування моделі):** https://yuliianikolaieva.github.io/varus-report/cashback-september-analysis.html

**Генерація даних для іншого партнера:** див. `Reports GIT HUB/Partner-Ops-Analysis/` (`fetch_ops_data.py` + `DATA_SCHEMA.md`).

Звіт оновлюється автоматично щопонеділка о 05:00 UTC (08:00 за Києвом) через GitHub Actions —
дані тягнуться напряму з Databricks (`group_name = 'VARUS'`, лише `order_state = 'delivered'`).

Дані: фінансові та операційні KPI, невдалі замовлення, кампанії, топ-заклади мережі.
