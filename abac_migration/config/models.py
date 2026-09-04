"""RunConfig and the enums that drive the whole pipeline. See DESIGN.md §11.

No component outside `config/` should construct a RunConfig directly from
raw widget strings/dicts - that parsing lives in config_loader.py. This
module only defines the typed shape and its own internal validation.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum


class Mode(str, Enum):
    INVENTORY = "INVENTORY"
    MIGRATE = "MIGRATE"
    INVENTORY_AND_MIGRATE = "INVENTORY_AND_MIGRATE"
    # Isolated-phase modes (requested to let a large migration be run as 3
    # separate, independently-schedulable steps instead of one atomic
    # MIGRATE): APPLY_ABAC creates the governed tags + ABAC policy for every
    # eligible object but deliberately leaves the legacy row filter/column
    # mask in place (table ends up with BOTH mechanisms active - see
    # StepStatus.ABAC_APPLIED), then FINALIZE removes the legacy mechanism
    # for objects already in that state and performs the final verification.
    # MIGRATE/INVENTORY_AND_MIGRATE still do both in one shot (unchanged).
    APPLY_ABAC = "APPLY_ABAC"
    FINALIZE = "FINALIZE"
    VERIFY = "VERIFY"
    RECONCILE = "RECONCILE"
    ROLLBACK = "ROLLBACK"


class ScopeType(str, Enum):
    ALL_CATALOGS = "ALL_CATALOGS"
    SELECTED_CATALOGS = "SELECTED_CATALOGS"
    ALL_SCHEMAS = "ALL_SCHEMAS"
    SELECTED_SCHEMAS = "SELECTED_SCHEMAS"
    SPECIFIC_TABLES = "SPECIFIC_TABLES"


class PolicyScope(str, Enum):
    """Selects which `migration/policy_strategy.py` implementation a run
    uses (§7.3) - the user-facing "Table level application" vs "Catalog
    level application" choice, set once per run via YAML/job parameter
    (`policy_scope`), never mixed within one run.

    TABLE: `TableBasedPolicyStrategy` - one ABAC policy per table (per
    masked column for COLUMN_MASK). The default; unchanged pre-existing
    behavior.

    CATALOG: `CatalogBasedPolicyStrategy` - one ABAC policy per distinct
    legacy function, `ON CATALOG`, shared by every table in that catalog
    the function used to govern. Fewer policy objects for a large
    migration; each one now governs many tables at once."""
    TABLE = "TABLE"
    CATALOG = "CATALOG"


DEFAULT_POLICY_TO_PRINCIPALS = ["account users"]
DEFAULT_POLICY_EXCEPT_PRINCIPALS: list = []

# A pay-per-token Foundation Model API endpoint, invoked via the `ai_query()`
# SQL function directly from the SQL Statement Execution API client - no
# extra serving infra to stand up, consistent with "everything is a SQL
# statement through the gateway" (§1). Overridable per-workspace/run in case
# this specific endpoint name isn't enabled on a given account.
DEFAULT_PII_LLM_ENDPOINT = "databricks-meta-llama-3-3-70b-instruct"

# DBR 16.4+ is required for CREATE POLICY; DBR 18.1+ is required for
# CREATE/ALTER GOVERNED TAG. Since governed tags are now a hard dependency
# (§7.4), pre_validation must check for the higher of the two (§16 item 4).
MIN_REQUIRED_DBR_VERSION = "18.1"


class ConfigError(ValueError):
    """Raised for any invalid/missing RunConfig parameter."""


@dataclass(frozen=True)
class RunConfig:
    mode: Mode = Mode.INVENTORY
    scope_type: ScopeType = ScopeType.SELECTED_CATALOGS
    catalogs: list = field(default_factory=list)
    schemas: dict = field(default_factory=dict)
    tables: list = field(default_factory=list)
    exclude_schema_regex: str = ""

    dry_run: bool = True
    continue_on_error: bool = True
    max_parallelism: int = 4

    audit_catalog: str = ""
    audit_schema: str = ""
    audit_table: str = "migration_audit"
    inventory_table: str = "inventory"

    policy_scope: PolicyScope = PolicyScope.TABLE
    policy_to_principals: list = field(default_factory=lambda: list(DEFAULT_POLICY_TO_PRINCIPALS))
    # Principals (users/groups/service principals) fully exempted from every
    # ABAC policy this run creates (`EXCEPT principal [, ...]`, confirmed
    # live grammar - e.g. a service principal running unmasked ETL). Empty
    # by default: no EXCEPT clause is added, identical to prior behavior.
    policy_except_principals: list = field(default_factory=lambda: list(DEFAULT_POLICY_EXCEPT_PRINCIPALS))
    prefer_existing_tags: bool = True

    # INVENTORY-only: best-effort LLM classification of each legacy row-filter
    # /column-mask function's likely PII category, from its name + governed
    # columns alone (never touches row data). Off by default - it's an
    # advisory/reporting aid, not a migration decision input, and adds an
    # ai_query() round trip per distinct function during inventory.
    enable_llm_pii_tagging: bool = False
    pii_llm_endpoint: str = DEFAULT_PII_LLM_ENDPOINT

    run_id: str = ""

    def __post_init__(self):
        if not self.run_id:
            object.__setattr__(self, "run_id", str(uuid.uuid4()))
        self._validate()

    def _validate(self) -> None:
        if not self.audit_catalog or not self.audit_schema:
            raise ConfigError(
                "audit_catalog and audit_schema are required parameters with "
                "no hard-coded default (DESIGN.md §11) - the caller must "
                "supply them explicitly."
            )
        if self.max_parallelism < 1:
            raise ConfigError("max_parallelism must be >= 1")
        if self.exclude_schema_regex:
            try:
                re.compile(self.exclude_schema_regex)
            except re.error as exc:
                raise ConfigError(f"exclude_schema_regex is not a valid regex: {exc}") from exc

        if self.scope_type == ScopeType.SELECTED_CATALOGS and not self.catalogs:
            raise ConfigError("scope_type=SELECTED_CATALOGS requires a non-empty 'catalogs' list")
        if self.scope_type == ScopeType.SELECTED_SCHEMAS and not self.schemas:
            raise ConfigError("scope_type=SELECTED_SCHEMAS requires a non-empty 'schemas' mapping")
        if self.scope_type == ScopeType.SPECIFIC_TABLES and not self.tables:
            raise ConfigError("scope_type=SPECIFIC_TABLES requires a non-empty 'tables' list")

        if self.mode == Mode.ROLLBACK and not self.run_id:
            raise ConfigError("ROLLBACK mode requires a run_id identifying the run to roll back")

    @property
    def audit_full_schema(self) -> str:
        return f"{self.audit_catalog}.{self.audit_schema}"

    @property
    def audit_table_fqn(self) -> str:
        return f"{self.audit_full_schema}.{self.audit_table}"

    @property
    def inventory_table_fqn(self) -> str:
        return f"{self.audit_full_schema}.{self.inventory_table}"

    @classmethod
    def from_dict(cls, raw: dict) -> "RunConfig":
        """Builds a RunConfig from a plain dict (as produced by config_loader
        from either notebook widgets or a test harness). All JSON-shaped
        string fields (catalogs/schemas/tables/policy_to_principals) may
        arrive either already-parsed (list/dict) or as JSON-encoded strings
        (the shape widgets actually produce) - both are accepted here so the
        same loader path works from a notebook or a unit test.
        """
        data = dict(raw)

        def _maybe_json(value, default):
            if value is None or value == "":
                return default
            if isinstance(value, str):
                return json.loads(value)
            return value

        data["catalogs"] = _maybe_json(data.get("catalogs"), [])
        data["schemas"] = _maybe_json(data.get("schemas"), {})
        data["tables"] = _maybe_json(data.get("tables"), [])
        data["policy_to_principals"] = _maybe_json(
            data.get("policy_to_principals"), list(DEFAULT_POLICY_TO_PRINCIPALS)
        )
        data["policy_except_principals"] = _maybe_json(
            data.get("policy_except_principals"), list(DEFAULT_POLICY_EXCEPT_PRINCIPALS)
        )

        if "mode" in data and not isinstance(data["mode"], Mode):
            data["mode"] = Mode(data["mode"])
        if "scope_type" in data and not isinstance(data["scope_type"], ScopeType):
            data["scope_type"] = ScopeType(data["scope_type"])
        if "policy_scope" in data and not isinstance(data["policy_scope"], PolicyScope):
            data["policy_scope"] = PolicyScope(data["policy_scope"])

        for bool_field in ("dry_run", "continue_on_error", "prefer_existing_tags", "enable_llm_pii_tagging"):
            if bool_field in data and isinstance(data[bool_field], str):
                data[bool_field] = data[bool_field].strip().lower() == "true"
        if "max_parallelism" in data and isinstance(data["max_parallelism"], str):
            data["max_parallelism"] = int(data["max_parallelism"])

        known_fields = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)
