# Databricks notebook source
# This notebook is executed via Jobs API with `run_as` set to a specific
# test user, to verify that RLS row filters + column masks are enforced
# per-identity. It runs as whichever identity the job is configured with,
# queries the sales ABAC demo tables, and returns the observed results as
# JSON via dbutils.notebook.exit() so the caller can diff outputs between
# the two test identities.

import json

catalog = dbutils.widgets.get("catalog") if "catalog" in [w for w in []] else None

dbutils.widgets.text("catalog", "ril_raw")
dbutils.widgets.text("schema", "sales_abac_demo")
catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")

result = {}
result["current_user"] = spark.sql("SELECT current_user() AS u").collect()[0]["u"]

# 1) Row-level security check: which business units are visible, and how many rows
bu_counts = spark.sql(
    f"SELECT business_unit, count(*) AS n FROM {catalog}.{schema}.customers GROUP BY business_unit ORDER BY business_unit"
).collect()
result["customers_visible_business_units"] = {r["business_unit"]: r["n"] for r in bu_counts}

order_bu_counts = spark.sql(
    f"SELECT business_unit, count(*) AS n FROM {catalog}.{schema}.orders GROUP BY business_unit ORDER BY business_unit"
).collect()
result["orders_visible_business_units"] = {r["business_unit"]: r["n"] for r in order_bu_counts}

payment_bu_counts = spark.sql(
    f"SELECT business_unit, count(*) AS n FROM {catalog}.{schema}.payments GROUP BY business_unit ORDER BY business_unit"
).collect()
result["payments_visible_business_units"] = {r["business_unit"]: r["n"] for r in payment_bu_counts}

result["total_customers_visible"] = sum(result["customers_visible_business_units"].values())

# 2) Column masking check: sample a row and show whether PII is masked
sample = spark.sql(
    f"SELECT full_name, email, phone_number, pan_number, aadhaar_number, address, business_unit "
    f"FROM {catalog}.{schema}.customers ORDER BY customer_id LIMIT 3"
).collect()
result["customers_sample"] = [r.asDict() for r in sample]

payment_sample = spark.sql(
    f"SELECT payment_id, card_number, card_holder_name, business_unit FROM {catalog}.{schema}.payments ORDER BY payment_id LIMIT 3"
).collect()
result["payments_sample"] = [r.asDict() for r in payment_sample]

dbutils.notebook.exit(json.dumps(result, default=str))
