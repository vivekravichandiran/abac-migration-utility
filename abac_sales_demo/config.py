"""Static configuration for the RIL ABAC sales demo deployment."""

PROFILE = "uc_source"
WAREHOUSE_ID = "525de76b2ccdd7d5"  # Serverless Starter Warehouse

SCHEMA_NAME = "sales_abac_demo"

# Every ril_* catalog discovered on the source workspace (excluding
# information_schema which is not a real user catalog).
CATALOGS = [
    "ril_raw",
    "ril_sandbox",
    "ril_bulk",
    "ril_curated",
    "ril_migration",
]

# The two business units used to demonstrate row-level security. Every row in
# every table carries a business_unit value that is one of these two.
BUSINESS_UNITS = ["Retail", "O2C"]

# Privileged group whose members bypass masking and RLS (in addition to
# metastore admins). Left unused by default; metastore admin check covers it.
ADMIN_GROUP = "metastore_admins"

GROUPS = {
    "Retail": "bu_retail_group",
    "O2C": "bu_o2c_group",
}

TEST_USERS = {
    "Retail": {
        "user_name": "retail.test.user@ril-abac-demo.com",
        "display_name": "Retail Test User",
    },
    "O2C": {
        "user_name": "o2c.test.user@ril-abac-demo.com",
        "display_name": "O2C Test User",
    },
}

TABLES = ["customers", "sales_reps", "products", "orders", "payments"]
