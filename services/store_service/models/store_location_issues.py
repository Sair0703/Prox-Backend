# services/store_service/models/store_location_issues.py

from __future__ import annotations

from services.store_service.models.base import IssueType


# ----------------------------------------------------------------------
# Missing / Incomplete
# ----------------------------------------------------------------------

MISSING_ADDRESS = IssueType(
    category="missing",
    name="missing_address",
    description="The address is missing.",
    route_to="llm",
    repair_hint=(
        "Infer the street address from full_address, zip code, city/state, "
        "OSM evidence, or other external sources. If the address cannot be "
        "reliably recovered, return unverifiable."
    ),
    repair_fields=("address",),
)

MISSING_FULL_ADDRESS = IssueType(
    category="missing",
    name="missing_full_address",
    description="The full address is missing.",
    route_to="llm",
    repair_hint=(
        "Reconstruct the full address from address, city, state, zip code, "
        "and external evidence. If the result is uncertain, return unverifiable."
    ),
    repair_fields=("full_address",),
)

MISSING_CITY = IssueType(
    category="missing",
    name="missing_city",
    description="The city is missing.",
    route_to="llm",
    repair_hint=(
        "Infer the city from full_address, zip code, coordinates, OSM evidence, "
        "or retailer context. If uncertain, return unverifiable."
    ),
    repair_fields=("city",),
)

MISSING_STATE = IssueType(
    category="missing",
    name="missing_state",
    description="The state is missing.",
    route_to="llm",
    repair_hint=(
        "Infer the state from zip code, city, coordinates, OSM evidence, or "
        "retailer context. If uncertain, return unverifiable."
    ),
    repair_fields=("state",),
)

MISSING_COORDINATES = IssueType(
    category="missing",
    name="missing_coordinates",
    description="The coordinates are missing.",
    route_to="llm",
    repair_hint=(
        "Recover latitude and longitude from the address, full_address, zip code, "
        "or external sources such as OSM. If reliable coordinates cannot be found, "
        "return unverifiable."
    ),
    repair_fields=("latitude", "longitude"),
)

MISSING_STORE_ID = IssueType(
    category="missing",
    name="missing_store_id",
    description="The retailer-specific store ID is missing.",
    route_to="llm",
    repair_hint=(
        "Recover the retailer-specific store ID from retailer-specific sources, "
        "OSM evidence, or other trusted store metadata. If it cannot be recovered, "
        "leave it unresolved."
    ),
    repair_fields=(),
)

MISSING_RETAILER_KEY = IssueType(
    category="missing",
    name="missing_retailer_key",
    description="The retailer key is missing.",
    route_to="auto",
    repair_hint=(
        "Generate the canonical retailer_key from the retailer name."
    ),
    repair_fields=("retailer_key",),
)


# ----------------------------------------------------------------------
# Invalid / Outlier
# ----------------------------------------------------------------------

INVALID_COORDINATES = IssueType(
    category="invalid",
    name="invalid_coordinates",
    description="The coordinates are invalid.",
    route_to="llm",
    repair_hint=(
        "Replace the invalid coordinates with valid coordinates derived from the "
        "address, full_address, or external evidence. If no reliable replacement is "
        "available, return unverifiable."
    ),
    repair_fields=("latitude", "longitude"),
)

ZERO_COORDINATES = IssueType(
    category="invalid",
    name="zero_coordinates",
    description="The coordinates are (0, 0), which is invalid.",
    route_to="llm",
    repair_hint=(
        "Treat (0, 0) as a placeholder and recover valid coordinates from the "
        "address, full_address, zip code, or external sources such as OSM. "
        "If the true coordinates cannot be recovered, return unverifiable."
    ),
    repair_fields=("latitude", "longitude"),
)

NON_US_COORDINATES = IssueType(
    category="invalid",
    name="non_us_coordinates",
    description="The coordinates are outside the United States.",
    route_to="llm",
    repair_hint=(
        "Determine whether the store is actually outside the United States. If not, "
        "replace the coordinates with valid US coordinates derived from the address "
        "or external evidence. If the location is genuinely non-US or uncertain, "
        "return unverifiable."
    ),
    repair_fields=("latitude", "longitude"),
)

IMPLAUSIBLE_ADDRESS = IssueType(
    category="invalid",
    name="implausible_address",
    description="The address appears implausible or malformed.",
    route_to="llm",
    repair_hint=(
        "Normalize or reconstruct the address using the available fields, retailer "
        "context, and external evidence. If the address cannot be made plausible "
        "with confidence, return unverifiable."
    ),
    repair_fields=("address", "full_address", "city", "state", "zip_code"),
)


# ----------------------------------------------------------------------
# Data Quality
# ----------------------------------------------------------------------

CASE_VARIATION = IssueType(
    category="data_quality",
    name="case_variation",
    description="The value differs only by letter casing.",
    route_to="auto",
    repair_hint="Normalize casing without changing the underlying meaning.",
    repair_fields=("store_name", "address", "full_address", "city", "state"),
)

PUNCTUATION_VARIATION = IssueType(
    category="data_quality",
    name="punctuation_variation",
    description="The value differs only by punctuation.",
    route_to="auto",
    repair_hint="Remove or normalize punctuation without changing semantics.",
    repair_fields=("store_name", "address", "full_address", "city", "state"),
)

WHITESPACE_VARIATION = IssueType(
    category="data_quality",
    name="whitespace_variation",
    description="The value differs only by whitespace.",
    route_to="auto",
    repair_hint="Collapse whitespace and preserve the semantic content.",
    repair_fields=("store_name", "address", "full_address", "city", "state"),
)

ABBREVIATION_VARIATION = IssueType(
    category="data_quality",
    name="abbreviation_variation",
    description="The value differs only by abbreviation or expansion.",
    route_to="auto",
    repair_hint="Normalize common abbreviations to the canonical form used by the system.",
    repair_fields=("store_name", "address", "full_address"),
)

DIRECTION_ALIAS_VARIATION = IssueType(
    category="data_quality",
    name="direction_alias_variation",
    description="The value differs only by a direction alias.",
    route_to="auto",
    repair_hint="Normalize direction aliases such as N/North and NW/Northwest.",
    repair_fields=("address", "full_address"),
)


# ----------------------------------------------------------------------
# Cross-field Inconsistency
# ----------------------------------------------------------------------

ADDRESS_CITY_MISMATCH = IssueType(
    category="inconsistency",
    name="address_city_mismatch",
    description="The address and city do not refer to the same location.",
    route_to="llm",
    repair_hint=(
        "Compare the address, city, zip code, coordinates, and external evidence. "
        "Choose the most plausible city or return unverifiable if the conflict "
        "cannot be resolved confidently."
    ),
    repair_fields=("city",),
)

CITY_STATE_MISMATCH = IssueType(
    category="inconsistency",
    name="city_state_mismatch",
    description="The city and state do not refer to the same location.",
    route_to="llm",
    repair_hint=(
        "Compare city, state, zip code, coordinates, and external evidence. "
        "Choose the most plausible state or return unverifiable if the conflict "
        "remains ambiguous."
    ),
    repair_fields=("state",),
)

ZIP_STATE_MISMATCH = IssueType(
    category="inconsistency",
    name="zip_state_mismatch",
    description="The ZIP code and state do not refer to the same location.",
    route_to="llm",
    repair_hint=(
        "Compare the ZIP code against the state, address, coordinates, and "
        "external evidence. Resolve to the most plausible state or return "
        "unverifiable if the mismatch cannot be fixed."
    ),
    repair_fields=("state",),
)

ADDRESS_COORDINATE_MISMATCH = IssueType(
    category="inconsistency",
    name="address_coordinate_mismatch",
    description="The address and coordinates do not refer to the same location.",
    route_to="llm",
    repair_hint=(
        "Compare the address, coordinates, and external evidence such as OSM. "
        "If the coordinates are wrong, replace them with the best supported "
        "coordinates; otherwise return unverifiable."
    ),
    repair_fields=("latitude", "longitude"),
)

FULL_ADDRESS_PARSE_FAILURE = IssueType(
    category="inconsistency",
    name="full_address_parse_failure",
    description="The full address cannot be parsed into a valid location.",
    route_to="llm",
    repair_hint=(
        "Parse the full address into address, city, state, and zip components. "
        "If parsing fails or the components remain ambiguous, return unverifiable."
    ),
    repair_fields=("address", "full_address", "city", "state", "zip_code"),
)


# ----------------------------------------------------------------------
# Identity / Retailer Resolution
# ----------------------------------------------------------------------

RETAILER_KEY_MISMATCH = IssueType(
    category="identity",
    name="retailer_key_mismatch",
    description="The retailer_key is inconsistent with the retailer name.",
    route_to="auto",
    repair_hint=(
        "Regenerate the canonical retailer_key from the retailer name."
    ),
    repair_fields=("retailer_key",),
)

AMBIGUOUS_RETAILER_IDENTITY = IssueType(
    category="identity",
    name="ambiguous_retailer_identity",
    description="The retailer identity cannot be determined confidently.",
    route_to="llm",
    repair_hint=(
        "Use retailer name, address, store name, and external evidence to resolve "
        "the retailer identity. If the identity remains ambiguous, return unverifiable."
    ),
    repair_fields=(),
)

STORE_IDENTITY_CONFLICT = IssueType(
    category="identity",
    name="store_identity_conflict",
    description="The store identity conflicts across available sources.",
    route_to="llm",
    repair_hint=(
        "Compare all available sources and choose the most credible store identity. "
        "If no source is clearly dominant, return unverifiable."
    ),
    repair_fields=(),
)


# ----------------------------------------------------------------------
# Unverifiable
# ----------------------------------------------------------------------

UNVERIFIABLE = IssueType(
    category="verification",
    name="unverifiable",
    description="The store information cannot be verified with sufficient confidence.",
    route_to="manual",
    repair_hint=(
        "Escalate for manual review. Do not guess a correction when evidence is "
        "insufficient or contradictory."
    ),
    repair_fields=(),
)


ISSUE_TYPES: dict[str, IssueType] = {
    issue.name: issue
    for issue in [
        # Missing
        MISSING_ADDRESS,
        MISSING_FULL_ADDRESS,
        MISSING_CITY,
        MISSING_STATE,
        MISSING_COORDINATES,
        MISSING_STORE_ID,
        MISSING_RETAILER_KEY,
        # Invalid
        INVALID_COORDINATES,
        ZERO_COORDINATES,
        NON_US_COORDINATES,
        IMPLAUSIBLE_ADDRESS,
        # Data Quality
        CASE_VARIATION,
        PUNCTUATION_VARIATION,
        WHITESPACE_VARIATION,
        ABBREVIATION_VARIATION,
        DIRECTION_ALIAS_VARIATION,
        # Inconsistency
        ADDRESS_CITY_MISMATCH,
        CITY_STATE_MISMATCH,
        ZIP_STATE_MISMATCH,
        ADDRESS_COORDINATE_MISMATCH,
        FULL_ADDRESS_PARSE_FAILURE,
        # Identity
        RETAILER_KEY_MISMATCH,
        AMBIGUOUS_RETAILER_IDENTITY,
        STORE_IDENTITY_CONFLICT,
        # Unverifiable
        UNVERIFIABLE,
    ]
}