"""Builds the outbound Yativo payload and submits it as multipart/form-data (guide §3.1).

Only documented Yativo fields are forwarded (whitelists below); UI-only keys such as the
declaration checkbox or "same as registered address" never leave this service.
Nested data uses Laravel bracket notation: ``residential_address[city]``,
``identifying_information[0][image_front_file]``. Booleans are sent as 1/0 (§2.9).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import httpx

from . import lookups as L
from .config import settings

FileResolver = Callable[[str], tuple[str, bytes, str]]  # "file:<id>" -> (filename, bytes, mime)

ADDRESS_KEYS = ("street_line_1", "street_line_2", "city", "state", "postal_code", "country")

INDIVIDUAL_KEYS = (
    "customer_id", "first_name", "middle_name", "last_name", "email", "calling_code", "phone", "birth_date",
    "nationality", "gender", "taxId", "current_employer", "immigration_status", "selfie_image", "bvn", "nin",
    "employment_status", "most_recent_occupation_code", "expected_monthly_payments_usd", "annual_salary",
    "source_of_funds", "account_purpose", "account_purpose_other", "acting_as_intermediary",
    "usd_virtual_account", "eur_virtual_account", "eurde_virtual_account", "gbp_virtual_account",
)
INDIVIDUAL_ID_KEYS = ("type", "issuing_country", "number", "date_issued", "expiration_date", "image_front_file", "image_back_file")

BUSINESS_KEYS = (
    "customer_id", "business_legal_name", "business_trade_name", "business_description", "email", "business_type",
    "registration_number", "incorporation_date", "incorporation_country", "tax_id", "statement_descriptor",
    "phone_calling_code", "phone_number", "business_industry", "primary_website", "is_dao",
    "has_material_intermediary_ownership", "account_purpose", "account_purpose_other", "source_of_funds",
    "high_risk_activities", "high_risk_activities_explanation", "conducts_money_services",
    "conducts_money_services_description", "compliance_screening_explanation", "estimated_annual_revenue_usd",
    "expected_monthly_payments_usd", "operates_in_prohibited_countries", "ownership_threshold", "pep_status",
    "third_party_msb_payments", "regulated_activities_description", "primary_regulatory_authority_country",
    "primary_regulatory_authority_name", "license_number", "usd_virtual_account", "eur_virtual_account",
    "eurde_virtual_account", "gbp_virtual_account",
)
PERSON_KEYS = (
    "first_name", "last_name", "birth_date", "nationality", "email", "phone", "title", "ownership_percentage",
    "annual_salary", "relationship_established_at", "has_ownership", "has_control", "is_signer", "is_director",
)
PERSON_ID_KEYS = ("type", "issuing_country", "number", "date_issued", "expiration", "image_front", "image_back")
EBI_KEYS = ("meeting_mode", "industry_vertical", "business_description", "obo_usage", "monthly_volume_usd",
            "avg_transaction_usd", "max_transaction_usd", "primary_account_purpose", "sender_geographies")
COLLECTION_KEYS = ("sender_industries", "sender_types", "top_5_senders", "incoming_from_fintech_wallets",
                   "incoming_fintech_wallet_details", "collection_currencies", "current_collection_provider",
                   "reason_for_switching_collection", "expected_monthly_disbursement_usd",
                   "avg_transaction_amount_collection", "max_transaction_amount_collection")
PAYOUT_KEYS = ("payout_primary_purpose", "beneficiary_geographies", "beneficiary_industries", "beneficiary_types",
               "top_5_beneficiaries", "primary_payout_method", "payout_currencies", "current_payout_provider",
               "reason_for_switching_payout")
BOOL_KEYS = {"usd_virtual_account", "eur_virtual_account", "eurde_virtual_account", "gbp_virtual_account",
             "acting_as_intermediary", "is_dao", "has_material_intermediary_ownership", "conducts_money_services",
             "pep_status", "third_party_msb_payments", "has_ownership", "has_control", "is_signer", "is_director",
             "incoming_from_fintech_wallets"}


def _pick(src: dict, keys) -> dict:
    return {k: src[k] for k in keys if k in src and src[k] not in (None, "", [])}


def _address(a: dict | None, with_proof: bool) -> dict:
    a = a or {}
    out = _pick(a, ADDRESS_KEYS)
    if not out.get("postal_code") and out.get("country") and not L.postal_format(out["country"])["uses_postal_codes"]:
        out["postal_code"] = "00000"  # field is required even where the country has no postal codes
    if with_proof and a.get("proof_of_address_file"):
        out["proof_of_address_file"] = a["proof_of_address_file"]
    return out


def _clean_list(v: Any) -> list:
    return [x for x in (v or []) if x not in (None, "")]


def build_individual(d: dict, customer_id: str) -> dict:
    out = _pick({**d, "customer_id": customer_id}, INDIVIDUAL_KEYS)
    out["residential_address"] = _address(d.get("residential_address"), True)
    out["identifying_information"] = [_pick(i, INDIVIDUAL_ID_KEYS) for i in d.get("identifying_information") or []]
    # One statement of account fans out to all three required Yativo document slots.
    raw_docs = d.get("uploaded_documents") or []
    soa_file = (raw_docs[0].get("file") if raw_docs and isinstance(raw_docs[0], dict) else None)
    if soa_file:
        out["uploaded_documents"] = [
            {"type": slot, "file": soa_file} for slot in L.STATEMENT_OF_ACCOUNT_SLOTS
        ]
    residence = str((d.get("residential_address") or {}).get("country", "")).upper()
    if "NG" not in (str(out.get("nationality", "")).upper(), residence):
        out.pop("bvn", None)
        out.pop("nin", None)
    if str(out.get("account_purpose", "")).lower() != "other":
        out.pop("account_purpose_other", None)
    return out


def build_business(d: dict, customer_id: str) -> dict:
    out = _pick({**d, "customer_id": customer_id}, BUSINESS_KEYS)
    out["high_risk_activities"] = _clean_list(d.get("high_risk_activities"))
    if out["high_risk_activities"] == ["none_of_the_above"]:
        out.pop("high_risk_activities_explanation", None)
    if out.get("account_purpose") != "other":
        out.pop("account_purpose_other", None)
    out["registered_address"] = _address(d.get("registered_address"), True)
    out["physical_address"] = _address(d.get("physical_address"), True)
    persons = []
    for p in d.get("associated_persons") or []:
        person = _pick(p, PERSON_KEYS)
        for k in ("has_ownership", "has_control", "is_signer", "is_director"):
            person[k] = bool(p.get(k))  # the guide asks for all four on every person
        person["residential_address"] = _address(p.get("residential_address"), False)
        ids = []
        for i in p.get("identifying_information") or []:
            entry = _pick(i, PERSON_ID_KEYS)
            if str(entry.get("type", "")).lower() == "bvn" and str(p.get("nationality", "")).upper() != "NG":
                continue
            if entry.get("type") and entry.get("number"):
                ids.append(entry)
        person["identifying_information"] = ids
        persons.append(person)
    out["associated_persons"] = persons
    out["documents"] = [_pick(x, ("purpose", "description", "file")) for x in d.get("documents") or [] if x.get("file")]
    extra = [_pick(x, ("type", "description", "file")) for x in d.get("extra_documents") or [] if x.get("file")]
    if extra:
        out["extra_documents"] = extra
    for key, keys in (("extra_business_info", EBI_KEYS), ("collections_data", COLLECTION_KEYS), ("payouts_data", PAYOUT_KEYS)):
        section = _pick(d.get(key) or {}, keys)
        section = {k: (_clean_list(v) if isinstance(v, list) else v) for k, v in section.items()}
        section = {k: v for k, v in section.items() if v not in (None, "", [])}
        if section:
            out[key] = section
    return out


def build_outbound(flow: str, data: dict, customer_id: str) -> dict:
    out = build_individual(data, customer_id) if flow == "individual" else build_business(data, customer_id)
    for flag in ("usd_virtual_account", "eur_virtual_account", "eurde_virtual_account", "gbp_virtual_account"):
        out[flag] = bool(data.get(flag))  # always send all four, as in the guide's examples
    return out


def _flatten(value: Any, prefix: str, fields: list, files: list, resolve: FileResolver, leaf_key: str = "") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(v, f"{prefix}[{k}]" if prefix else k, fields, files, resolve, k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _flatten(v, f"{prefix}[{i}]", fields, files, resolve, leaf_key)
    elif isinstance(value, str) and value.startswith("file:"):
        files.append((prefix, resolve(value)))
    elif isinstance(value, bool) or leaf_key in BOOL_KEYS:
        fields.append((prefix, "1" if value in (True, "1", 1, "true") else "0"))
    elif value is None:
        return
    elif isinstance(value, float) and value.is_integer():
        fields.append((prefix, str(int(value))))
    else:
        fields.append((prefix, str(value)))


def to_multipart(payload: dict, resolve: FileResolver) -> tuple[list, list]:
    fields: list[tuple[str, str]] = []
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    _flatten(payload, "", fields, files, resolve)
    return fields, files


@dataclass
class YativoResult:
    status_code: int | None
    body: Any
    outcome: str  # submitted | needs_correction | duplicate | blocked | not_provisioned | delivery_failed

    @property
    def ok(self) -> bool:
        return self.outcome == "submitted"

    @property
    def validation_errors(self) -> dict:
        if isinstance(self.body, dict):
            return self.body.get("validation_errors") or self.body.get("errors") or {}
        return {}

    @property
    def warnings(self) -> dict:
        return self.body.get("warnings") or {} if isinstance(self.body, dict) else {}


def submit(flow: str, payload: dict, resolve: FileResolver) -> YativoResult:
    path = "/api/individual-kyc/submit" if flow == "individual" else "/api/business-kyc/submit"
    headers = {"Accept": "application/json"}
    if flow == "business":
        headers["X-Business-Id"] = str(payload.get("customer_id", ""))
    if settings.yativo_api_token:
        headers["Authorization"] = f"Bearer {settings.yativo_api_token}"
    fields, files = to_multipart(payload, resolve)
    try:
        # Every key carries its own index ("a[0][b]"), so field names are unique.
        r = httpx.post(settings.yativo_base_url + path, data=dict(fields), files=files, headers=headers,
                       timeout=settings.yativo_timeout)
    except httpx.HTTPError as exc:
        return YativoResult(None, {"error": f"{type(exc).__name__}: {exc}"}, "delivery_failed")
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:5000]}
    code = r.status_code
    if code in (200, 201) and (not isinstance(body, dict) or body.get("success", True) is not False):
        outcome = "submitted"
    elif code == 422:
        outcome = "needs_correction"
    elif code == 409:
        outcome = "duplicate"
    elif code == 403:
        outcome = "blocked"
    elif code == 404 and flow == "business":
        outcome = "not_provisioned"
    else:
        outcome = "delivery_failed"
    return YativoResult(code, body, outcome)
