# RIL Sales ABAC Demo

Deploys a small "sales" domain (5 tables with intentional dummy PII) into a
new `sales_abac_demo` schema inside every `ril_*` catalog on the **source**
workspace (`uc_source` profile), then applies Unity Catalog **column
masking** and **row-level security (RLS)** and proves isolation between two
test identities in two different "groups".

## Layout

- `config.py` – catalogs, schema name, business units, group/user names.
- `data_gen.py` – deterministic dummy data generator (Indian PII: PAN,
  Aadhaar, phone, email, card numbers, addresses — all fake).
- `ddl.py` – table DDL, masking functions, RLS function, grants.
- `sql_client.py` – minimal SQL Statement Execution API client.
- `identity.py` – creates the 2 groups + 2 test users via SCIM, adds members.
- `deploy.py` – orchestrates schema/table/mask/RLS/grant deployment per catalog.
- `notebooks/verify_isolation.py` – notebook run via Jobs `run_as` to prove
  isolation (queries tables *as* each test user).
- `test_isolation.py` – submits the verification notebook impersonating each
  test user, diffs results.

## Data model (5 tables, per catalog)

`customers`, `sales_reps`, `products`, `orders`, `payments` — every table
carries a `business_unit` column (`Retail` or `O2C`) used for RLS, and PII
columns (name, email, phone, PAN, Aadhaar, address, card number) used for
masking.

## Deploy

```bash
python3 -m abac_sales_demo.identity   # one-time: create groups + test users
python3 -m abac_sales_demo.deploy ril_raw ril_sandbox ril_bulk ril_curated ril_migration
python3 -m abac_sales_demo.test_isolation ril_raw   # repeat per catalog
```

## Design notes / limitations

- **Groups**: Unity Catalog `GRANT ... TO <group>` and `is_account_group_member()`
  only resolve real **account-level** groups. The PAT/SCIM access available
  here (`/api/2.0/preview/scim/v2/Groups`) can only create **workspace-local**
  groups, which UC cannot resolve (`PRINCIPAL_DOES_NOT_EXIST`) — this was
  confirmed both for newly created groups and pre-existing ones
  (`ril_data_engineers`, etc.). As a result:
  - Table/catalog/schema `GRANT`s target the two test **users** directly
    (this does resolve correctly).
  - The RLS function still implements a **group-level filter**: it consults
    a `group_membership` control table (`group_name`, `member_email`) via a
    helper function `is_group_member(p_group_name)`, and also checks the real
    `is_account_group_member()` first so it becomes a no-op once real
    account-level groups exist. To manage membership going forward, insert/
    delete rows in `<catalog>.sales_abac_demo.group_membership` instead of
    SCIM group membership.
  - **Production recommendation**: create `bu_retail_group` / `bu_o2c_group`
    as real account-level groups via the Databricks Account Console/Account
    API, then simplify the row filter to only use `is_account_group_member()`.
- **Test identities**: two workspace users (`retail.test.user@ril-abac-demo.com`,
  `o2c.test.user@ril-abac-demo.com`) were created via SCIM. Since they have no
  real login/SSO, isolation is verified by an admin submitting a Databricks
  Job with `run_as` set to each user (a supported admin capability), running
  a notebook that queries the tables and returns the results — this proves
  the *actual* Unity Catalog enforcement (RLS + masking) applies per-identity.
