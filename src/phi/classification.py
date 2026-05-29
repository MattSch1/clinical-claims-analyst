"""
PHI data-classification map for the Synthea clinical+claims schema.

Every column in the base tables is classified as one of three tiers, keyed to the
HIPAA Safe Harbor de-identification standard (45 CFR 164.514(b)(2)):

    IDENTIFIER        - a direct identifier (one of the 18 Safe Harbor elements).
                        NEVER exposed in a de-identified view; never allowed to
                        reach a prompt or a response.
    QUASI_IDENTIFIER  - not a direct identifier alone, but re-identifying in
                        combination (dates finer than year, full ZIP, age >89,
                        geography below state). Exposed ONLY in transformed form
                        (age banded / capped at 90, dates shifted or reduced to
                        year, ZIP truncated to 3 digits, geography to state).
    ANALYTIC          - safe to expose as-is (codes, costs, encounter class,
                        coded clinical facts, random surrogate keys).

NOTE: the data here is SYNTHETIC (Synthea). This map exists so the system is
*architected as if* it handled PHI. That discipline is the point.

Column names assume the loader lowercases all Synthea CSV headers (recommended;
see views.sql). Codes (SNOMED/LOINC/RxNorm/CVX) must be loaded as TEXT.
"""

from __future__ import annotations

from enum import Enum


class DataClass(str, Enum):
    IDENTIFIER = "IDENTIFIER"
    QUASI_IDENTIFIER = "QUASI_IDENTIFIER"
    ANALYTIC = "ANALYTIC"


# The 18 HIPAA Safe Harbor identifier categories — referenced for documentation
# and so reviewers can see the classification is standards-based, not ad hoc.
SAFE_HARBOR_IDENTIFIERS: tuple[str, ...] = (
    "Names",
    "Geographic subdivisions smaller than a state (street, city, county, ZIP — "
    "ZIP may be retained to 3 digits if the population is >20,000)",
    "All date elements (except year) directly related to an individual; all ages "
    ">89 and all elements of dates indicative of such age",
    "Telephone numbers",
    "Fax numbers",
    "Email addresses",
    "Social Security numbers",
    "Medical record numbers",
    "Health plan beneficiary numbers",
    "Account numbers",
    "Certificate/license numbers",
    "Vehicle identifiers and serial numbers (incl. license plates)",
    "Device identifiers and serial numbers",
    "Web URLs",
    "IP addresses",
    "Biometric identifiers (finger/voice prints)",
    "Full-face photographs and comparable images",
    "Any other unique identifying number, characteristic, or code",
)

ID = DataClass.IDENTIFIER
QI = DataClass.QUASI_IDENTIFIER
AN = DataClass.ANALYTIC

# table -> {column: DataClass}. Patient surrogate keys (random UUIDs) are ANALYTIC
# because they are not real identifiers and are needed as join keys; they are
# retained in views as opaque keys but never surfaced to a user as "the patient."
CLASSIFICATION: dict[str, dict[str, DataClass]] = {
    "patients": {
        "id": AN,                  # random surrogate UUID (join key)
        "birthdate": QI,           # derive age band; never expose raw DOB
        "deathdate": QI,
        "ssn": ID,
        "drivers": ID,
        "passport": ID,
        "prefix": QI,
        "first": ID,
        "last": ID,
        "suffix": QI,
        "maiden": ID,
        "marital": AN,
        "race": AN,
        "ethnicity": AN,
        "gender": AN,
        "birthplace": QI,
        "address": ID,
        "city": QI,
        "state": AN,               # state is permitted under Safe Harbor
        "county": QI,
        "zip": QI,                 # expose only 3-digit prefix
        "lat": ID,
        "lon": ID,
        "healthcare_expenses": AN,
        "healthcare_coverage": AN,
        "income": AN,
    },
    "encounters": {
        "id": AN,
        "start": QI,
        "stop": QI,
        "patient": AN,
        "organization": AN,
        "provider": AN,
        "payer": AN,
        "encounterclass": AN,
        "code": AN,
        "description": AN,
        "base_encounter_cost": AN,
        "total_claim_cost": AN,
        "payer_coverage": AN,
        "reasoncode": AN,
        "reasondescription": AN,
    },
    "conditions": {
        "start": QI, "stop": QI, "patient": AN, "encounter": AN,
        "system": AN, "code": AN, "description": AN,
    },
    "medications": {
        "start": QI, "stop": QI, "patient": AN, "payer": AN, "encounter": AN,
        "code": AN, "description": AN, "base_cost": AN, "payer_coverage": AN,
        "dispenses": AN, "totalcost": AN, "reasoncode": AN, "reasondescription": AN,
    },
    "procedures": {
        "start": QI, "stop": QI, "patient": AN, "encounter": AN, "system": AN,
        "code": AN, "description": AN, "base_cost": AN,
        "reasoncode": AN, "reasondescription": AN,
    },
    "observations": {
        "date": QI, "patient": AN, "encounter": AN, "category": AN,
        "code": AN, "description": AN, "value": AN, "units": AN, "type": AN,
    },
    "immunizations": {
        "date": QI, "patient": AN, "encounter": AN,
        "code": AN, "description": AN, "base_cost": AN,
    },
    "payers": {
        "id": AN, "name": AN, "ownership": AN,
        "address": QI, "city": QI, "state_headquartered": AN, "zip": QI,
        "phone": QI, "amount_covered": AN, "amount_uncovered": AN, "revenue": AN,
        "covered_encounters": AN, "uncovered_encounters": AN,
        "covered_medications": AN, "uncovered_medications": AN,
        "covered_procedures": AN, "uncovered_procedures": AN,
        "covered_immunizations": AN, "uncovered_immunizations": AN,
        "unique_customers": AN, "qols_avg": AN, "member_months": AN,
    },
}


def classify(table: str, column: str) -> DataClass:
    """Return the DataClass for a column; unknown columns default to IDENTIFIER
    (fail-closed: anything we haven't explicitly vetted is treated as unsafe)."""
    return CLASSIFICATION.get(table.lower(), {}).get(column.lower(), DataClass.IDENTIFIER)


def is_identifier(table: str, column: str) -> bool:
    return classify(table, column) is DataClass.IDENTIFIER


def identifier_columns(table: str) -> list[str]:
    cols = CLASSIFICATION.get(table.lower(), {})
    return [c for c, k in cols.items() if k is DataClass.IDENTIFIER]


def safe_display_columns(table: str) -> list[str]:
    """Columns safe to surface directly (ANALYTIC only)."""
    cols = CLASSIFICATION.get(table.lower(), {})
    return [c for c, k in cols.items() if k is DataClass.ANALYTIC]


def all_identifier_columns() -> set[str]:
    """Every column name classified as a direct identifier anywhere in the schema.
    The PHI leakage scanner uses this to flag any such column slipping into a
    query, prompt, or response."""
    out: set[str] = set()
    for cols in CLASSIFICATION.values():
        out |= {c for c, k in cols.items() if k is DataClass.IDENTIFIER}
    return out


if __name__ == "__main__":
    # Quick self-report for sanity-checking the map.
    for tbl, cols in CLASSIFICATION.items():
        ids = [c for c, k in cols.items() if k is DataClass.IDENTIFIER]
        qis = [c for c, k in cols.items() if k is DataClass.QUASI_IDENTIFIER]
        print(f"{tbl}: {len(cols)} cols | identifiers={ids} | quasi={qis}")
