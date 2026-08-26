"""Pre-flight, run-level checks executed once before any table is touched
(§2) - distinct from the per-object function-exists/accessible checks each
plugin already does inside its own validate() (§5). Never mutates UC.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config.models import MIN_REQUIRED_DBR_VERSION, RunConfig
from ..uc_gateway.gateway import UnityCatalogGateway, UCGatewayError


@dataclass(frozen=True)
class PreValidationResult:
    passed: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def run_pre_validation(config: RunConfig, uc: UnityCatalogGateway) -> PreValidationResult:
    errors = []
    warnings = []

    # Governed tags require DBR 18.1+ (§13, §16 item 4) - the higher of the
    # two minimum versions this design depends on (CREATE POLICY only needs
    # 16.4+). We can't reliably read the DBR version without a Spark
    # context, so this is a feature-probe rather than a version-string
    # check: if governed tags aren't usable at all, every migration in this
    # run would fail identically and expensively later, so fail fast here.
    try:
        uc.list_governed_tags()
    except UCGatewayError as exc:
        errors.append(
            f"Governed tags are not usable in this workspace (required, DBR "
            f"{MIN_REQUIRED_DBR_VERSION}+): {exc.message}"
        )

    # §16 item 4: account-level CREATE privilege on governed tags is a
    # different privilege domain than workspace admin - only actually
    # provable by attempting a real create, which we deliberately do NOT do
    # here (that would be a mutation during "pre-validation"). Surfaced as
    # a warning instead, resolved for real the first time tag_provisioner
    # actually needs to mint a tag.
    warnings.append(
        "Account-level CREATE privilege on governed tags cannot be confirmed "
        "without attempting a real mutation; if minting a synthetic tag is "
        "ever needed, an insufficient-privilege failure will surface then "
        "(see DESIGN.md §16 item 4)."
    )

    if not config.audit_catalog or not config.audit_schema:
        errors.append("audit_catalog/audit_schema are required and were not provided.")

    return PreValidationResult(passed=not errors, errors=errors, warnings=warnings)
