"""DDL builders: tables, column-masking functions, and RLS row-filter
functions for the sales ABAC demo. All objects are namespaced inside
<catalog>.sales_abac_demo so they never collide with real business data.
"""
from __future__ import annotations

from .config import ADMIN_GROUP, GROUPS, TEST_USERS
from .data_gen import sql_str

GROUP_MEMBERSHIP_TABLE = "group_membership"

TABLE_DDL = {
    "customers": """
CREATE TABLE {c}.{s}.customers (
  customer_id BIGINT,
  full_name STRING COMMENT 'PII: full name',
  email STRING COMMENT 'PII: email address',
  phone_number STRING COMMENT 'PII: phone number',
  pan_number STRING COMMENT 'PII: India PAN (tax id)',
  aadhaar_number STRING COMMENT 'PII: India Aadhaar (national id)',
  address STRING COMMENT 'PII: street address',
  city STRING,
  state STRING,
  business_unit STRING COMMENT 'ABAC attribute used for row filtering',
  signup_date DATE
) COMMENT 'Sales ABAC demo - customers (contains intentional dummy PII)'
""",
    "sales_reps": """
CREATE TABLE {c}.{s}.sales_reps (
  rep_id BIGINT,
  rep_name STRING COMMENT 'PII: full name',
  rep_email STRING COMMENT 'PII: email address',
  rep_phone STRING COMMENT 'PII: phone number',
  business_unit STRING COMMENT 'ABAC attribute used for row filtering',
  hire_date DATE,
  region STRING
) COMMENT 'Sales ABAC demo - sales reps (contains intentional dummy PII)'
""",
    "products": """
CREATE TABLE {c}.{s}.products (
  product_id BIGINT,
  product_name STRING,
  category STRING,
  unit_price DECIMAL(12,2),
  business_unit STRING COMMENT 'ABAC attribute used for row filtering'
) COMMENT 'Sales ABAC demo - products catalog'
""",
    "orders": """
CREATE TABLE {c}.{s}.orders (
  order_id BIGINT,
  customer_id BIGINT,
  rep_id BIGINT,
  product_id BIGINT,
  order_date DATE,
  quantity INT,
  order_amount DECIMAL(14,2),
  business_unit STRING COMMENT 'ABAC attribute used for row filtering',
  shipping_address STRING COMMENT 'PII: shipping address'
) COMMENT 'Sales ABAC demo - orders (contains intentional dummy PII)'
""",
    "payments": """
CREATE TABLE {c}.{s}.payments (
  payment_id BIGINT,
  order_id BIGINT,
  payment_method STRING,
  card_number STRING COMMENT 'PII: full card number',
  card_holder_name STRING COMMENT 'PII: full name',
  amount DECIMAL(14,2),
  payment_date DATE,
  business_unit STRING COMMENT 'ABAC attribute used for row filtering'
) COMMENT 'Sales ABAC demo - payments (contains intentional dummy PII)'
""",
}

INSERT_COLUMNS = {
    "customers": ["customer_id", "full_name", "email", "phone_number", "pan_number",
                  "aadhaar_number", "address", "city", "state", "business_unit", "signup_date"],
    "sales_reps": ["rep_id", "rep_name", "rep_email", "rep_phone", "business_unit", "hire_date", "region"],
    "products": ["product_id", "product_name", "category", "unit_price", "business_unit"],
    "orders": ["order_id", "customer_id", "rep_id", "product_id", "order_date", "quantity",
               "order_amount", "business_unit", "shipping_address"],
    "payments": ["payment_id", "order_id", "payment_method", "card_number", "card_holder_name",
                 "amount", "payment_date", "business_unit"],
}

DATE_COLUMNS = {"signup_date", "hire_date", "order_date", "payment_date"}


def create_schema_sql(catalog: str, schema: str) -> str:
    return f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}"


def create_group_membership_table_sql(catalog: str, schema: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.{GROUP_MEMBERSHIP_TABLE} (
  group_name STRING,
  member_email STRING
) COMMENT 'ABAC control table: maps a logical group to member user emails. Used by RLS
  functions via is_group_member() because this workspace''s PAT/SCIM access can only manage
  workspace-local groups, which Unity Catalog GRANT/is_account_group_member cannot resolve.
  Production deployments should instead create real account-level groups (Account Console/
  Account API) and use is_account_group_member() directly.'
"""


def seed_group_membership_sql(catalog: str, schema: str) -> str:
    rows = []
    for bu, group_name in GROUPS.items():
        user_name = TEST_USERS[bu]["user_name"]
        rows.append(f"({sql_str(group_name)}, {sql_str(user_name)})")
    values_sql = ",\n  ".join(rows)
    return (
        f"INSERT OVERWRITE TABLE {catalog}.{schema}.{GROUP_MEMBERSHIP_TABLE} "
        f"(group_name, member_email) VALUES\n  {values_sql}"
    )


def is_group_member_function_sql(catalog: str, schema: str) -> str:
    # NOTE: the parameter must NOT be named `group_name` - it previously
    # collided with the group_membership table's `group_name` column inside
    # the EXISTS subquery, so `m.group_name = group_name` silently bound to
    # `m.group_name = m.group_name` (always true) instead of comparing
    # against the caller's argument, letting every member through every group.
    return f"""
CREATE OR REPLACE FUNCTION {catalog}.{schema}.is_group_member(p_group_name STRING)
RETURNS BOOLEAN
COMMENT 'Checks membership in the logical group_membership control table for current_user()'
RETURN EXISTS (
  SELECT 1 FROM {catalog}.{schema}.{GROUP_MEMBERSHIP_TABLE} m
  WHERE m.group_name = p_group_name AND m.member_email = current_user()
)
"""


def create_table_sql(catalog: str, schema: str, table: str) -> str:
    return TABLE_DDL[table].format(c=catalog, s=schema)


def insert_sql(catalog: str, schema: str, table: str, rows: list[dict]) -> str:
    cols = INSERT_COLUMNS[table]
    value_rows = []
    for row in rows:
        vals = []
        for col in cols:
            v = row[col]
            if col in DATE_COLUMNS:
                vals.append(f"DATE{sql_str(v)}")
            elif isinstance(v, (int, float)):
                vals.append(str(v))
            else:
                vals.append(sql_str(v))
        value_rows.append("(" + ", ".join(vals) + ")")
    cols_sql = ", ".join(cols)
    values_sql = ",\n  ".join(value_rows)
    return f"INSERT INTO {catalog}.{schema}.{table} ({cols_sql}) VALUES\n  {values_sql}"


# ---------------------------------------------------------------------------
# Column masking functions
# ---------------------------------------------------------------------------

def masking_functions_sql(catalog: str, schema: str) -> list[str]:
    c, s = catalog, schema
    admin = ADMIN_GROUP
    return [
        f"""
CREATE OR REPLACE FUNCTION {c}.{s}.mask_email(email STRING)
RETURNS STRING
COMMENT 'Column mask: reveals full email only to metastore admins, else masks local part'
RETURN CASE
  WHEN is_account_group_member('{admin}') THEN email
  ELSE CONCAT(LEFT(email, 1), '***@', SPLIT(email, '@')[1])
END
""",
        f"""
CREATE OR REPLACE FUNCTION {c}.{s}.mask_phone(phone STRING)
RETURNS STRING
COMMENT 'Column mask: shows only last 4 digits of phone number to non-admins'
RETURN CASE
  WHEN is_account_group_member('{admin}') THEN phone
  ELSE CONCAT('******', RIGHT(phone, 4))
END
""",
        f"""
CREATE OR REPLACE FUNCTION {c}.{s}.mask_redact(val STRING)
RETURNS STRING
COMMENT 'Column mask: fully redacts highly sensitive PII (PAN/Aadhaar/card number) for non-admins'
RETURN CASE
  WHEN is_account_group_member('{admin}') THEN val
  ELSE 'REDACTED'
END
""",
        f"""
CREATE OR REPLACE FUNCTION {c}.{s}.mask_address(addr STRING)
RETURNS STRING
COMMENT 'Column mask: truncates address for non-admins'
RETURN CASE
  WHEN is_account_group_member('{admin}') THEN addr
  ELSE 'REDACTED'
END
""",
    ]


MASK_ASSIGNMENTS = {
    "customers": [
        ("email", "mask_email"), ("phone_number", "mask_phone"),
        ("pan_number", "mask_redact"), ("aadhaar_number", "mask_redact"),
        ("address", "mask_address"),
    ],
    "sales_reps": [("rep_email", "mask_email"), ("rep_phone", "mask_phone")],
    "products": [],
    "orders": [("shipping_address", "mask_address")],
    "payments": [("card_number", "mask_redact"), ("card_holder_name", "mask_redact")],
}


def alter_masks_sql(catalog: str, schema: str, table: str) -> list[str]:
    out = []
    for col, fn in MASK_ASSIGNMENTS.get(table, []):
        out.append(
            f"ALTER TABLE {catalog}.{schema}.{table} "
            f"ALTER COLUMN {col} SET MASK {catalog}.{schema}.{fn}"
        )
    return out


# ---------------------------------------------------------------------------
# Row-level security (RLS) function: business-unit group filter
# ---------------------------------------------------------------------------

def row_filter_function_sql(catalog: str, schema: str) -> str:
    admin = ADMIN_GROUP
    retail_group = GROUPS["Retail"]
    o2c_group = GROUPS["O2C"]
    return f"""
CREATE OR REPLACE FUNCTION {catalog}.{schema}.rf_business_unit(business_unit STRING)
RETURNS BOOLEAN
COMMENT 'RLS: restricts rows to the caller''s business-unit group; admins see everything.
  Group membership is resolved via is_group_member() against the group_membership control
  table (falls back to real is_account_group_member() first, for forward compatibility with
  real account-level groups if/when they are created).'
RETURN
  is_account_group_member('{admin}')
  OR (business_unit = 'Retail' AND (
        is_account_group_member('{retail_group}')
        OR {catalog}.{schema}.is_group_member('{retail_group}')
      ))
  OR (business_unit = 'O2C' AND (
        is_account_group_member('{o2c_group}')
        OR {catalog}.{schema}.is_group_member('{o2c_group}')
      ))
"""


def alter_row_filter_sql(catalog: str, schema: str, table: str) -> str:
    return (
        f"ALTER TABLE {catalog}.{schema}.{table} "
        f"SET ROW FILTER {catalog}.{schema}.rf_business_unit ON (business_unit)"
    )


def grant_sql(catalog: str, schema: str) -> list[str]:
    """Grants coarse-grained access to both test users directly.

    Note: GRANT ... TO `<workspace-scim-group>` fails with
    PRINCIPAL_DOES_NOT_EXIST on this workspace because Unity Catalog can only
    resolve real account-level principals, and this PAT/SCIM access can only
    create workspace-local groups. Both test users, however, resolve fine as
    UC principals, so grants target them directly; the actual per-group row
    restriction is enforced by the RLS row filter (see is_group_member()).
    """
    stmts = []
    for bu, info in TEST_USERS.items():
        user = info["user_name"]
        stmts.append(f"GRANT USE CATALOG ON CATALOG {catalog} TO `{user}`")
        stmts.append(f"GRANT USE SCHEMA ON SCHEMA {catalog}.{schema} TO `{user}`")
        for table in INSERT_COLUMNS:
            stmts.append(f"GRANT SELECT ON TABLE {catalog}.{schema}.{table} TO `{user}`")
    return stmts
