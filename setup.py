"""Build script for the abac_migration wheel, packaged and deployed by the
Databricks Asset Bundle (databricks.yml) as a job library. Deliberately a
plain setup.py (rather than pyproject.toml [project] metadata) so it builds
offline with whatever setuptools/wheel is already on the deploying machine -
no PEP 517 build-isolation / package-index round trip required.

Only the runtime package is shipped: `tests/` and `spike/` are development
and one-off verification artifacts, not part of the deployed utility.
"""
from setuptools import find_packages, setup

setup(
    name="abac_migration",
    version="0.1.0",
    description="Databricks Unity Catalog ABAC Migration Utility - converts legacy row filters and column masks to attribute-based (ABAC) policies",
    packages=find_packages(
        include=["abac_migration", "abac_migration.*"],
        exclude=["abac_migration.tests", "abac_migration.tests.*", "abac_migration.spike", "abac_migration.spike.*"],
    ),
    install_requires=["requests>=2.28.0"],
    python_requires=">=3.9",
)
