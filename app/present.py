"""Turns a decrypted submission payload into labelled sections for the admin review screen."""
from __future__ import annotations

from typing import Any

from . import lookups as L


def _label_map(flow: str) -> dict[str, dict[str, str]]:
    maps: dict[str, dict[str, str]] = {
        "gender": L.GENDERS,
        "employment_status": L.EMPLOYMENT_STATUS,
        "business_type": L.BUSINESS_TYPES,
        "purpose": L.DOCUMENT_PURPOSES,
        "high_risk_activities": L.HIGH_RISK_ACTIVITIES,
        "meeting_mode": L.MEETING_MODES,
        "primary_payout_method": L.PAYOUT_METHODS,
    }
    try:
        if flow == "individual":
            maps["account_purpose"] = L.individual_account_purposes()
            maps["source_of_funds"] = L.individual_source_of_funds()
            maps["expected_monthly_payments_usd"] = L.expected_monthly_payments()
        else:
            maps["account_purpose"] = L.business_account_purposes()
            maps["source_of_funds"] = L.business_source_of_funds()
    except Exception:  # noqa: BLE001 - labels are cosmetic
        pass
    return maps


def _country(v: Any) -> str:
    try:
        name = L.country_name(str(v))
        return f"{name} ({v})" if name and name != v else str(v)
    except Exception:  # noqa: BLE001
        return str(v)


COUNTRY_KEYS = {"nationality", "country", "issuing_country", "incorporation_country",
                "primary_regulatory_authority_country"}
BOOL_HINT = {"usd_virtual_account", "eur_virtual_account", "eurde_virtual_account", "gbp_virtual_account",
             "acting_as_intermediary", "is_dao", "has_material_intermediary_ownership", "conducts_money_services",
             "pep_status", "third_party_msb_payments", "has_ownership", "has_control", "is_signer", "is_director",
             "incoming_from_fintech_wallets", "consent"}

LABELS = {
    "taxId": "Tax ID", "bvn": "BVN", "nin": "NIN", "most_recent_occupation_code": "Occupation",
    "expected_monthly_payments_usd": "Expected monthly payments (USD)", "annual_salary": "Annual income",
    "business_industry": "Industry", "pep_status": "Politically exposed person",
    "third_party_msb_payments": "Third-party / MSB payments", "is_dao": "DAO",
    "tax_id": "Tax ID", "eurde_virtual_account": "EUR (DE) account", "usd_virtual_account": "USD account",
    "eur_virtual_account": "EUR account", "gbp_virtual_account": "GBP account",
    "image_front_file": "Front image", "image_back_file": "Back image", "image_front": "Front image",
    "image_back": "Back image", "proof_of_address_file": "Proof of address", "selfie_image": "Selfie",
    "street_line_1": "Street", "street_line_2": "Line 2", "postal_code": "Postal code",
    "ownership_percentage": "Ownership %", "primary_website": "Website",
}


def label(key: str) -> str:
    return LABELS.get(key) or key.replace("_", " ").strip().capitalize()


def _fmt(key: str, v: Any, maps: dict, occupations: dict, industries: dict) -> Any:
    if isinstance(v, str) and v.startswith("file:"):
        return {"file": v[5:]}
    if key in BOOL_HINT or isinstance(v, bool):
        if v in (True, "1", 1, "true", "yes"):
            return "Yes"
        if v in (False, "0", 0, "false", "no"):
            return "No"
    if key in COUNTRY_KEYS and v:
        return _country(v)
    if key == "most_recent_occupation_code" and v:
        return f"{occupations.get(str(v), '')} ({v})".strip()
    if key == "business_industry" and v:
        return f"{industries.get(str(v), '')} ({v})".strip()
    if key in maps and isinstance(v, str):
        return maps[key].get(v, v)
    if isinstance(v, list):
        return ", ".join(maps.get(key, {}).get(x, x) if isinstance(x, str) else str(x) for x in v if x not in (None, ""))
    return v


def _rows(d: dict, keys: list[str] | None, maps, occ, ind) -> list[dict]:
    out = []
    for k in keys or list(d.keys()):
        if k not in d:
            continue
        v = d[k]
        if v in (None, "", []) or isinstance(v, dict) or (isinstance(v, list) and v and isinstance(v[0], dict)):
            continue
        out.append({"label": label(k), "value": _fmt(k, v, maps, occ, ind)})
    return out


def sections(flow: str, d: dict) -> list[dict]:
    maps = _label_map(flow)
    try:
        occ = {o["code"]: o["label"] for o in L.occupations()} if flow == "individual" else {}
        ind = {o["code"]: o["label"] for o in L.business_industries()} if flow == "business" else {}
    except Exception:  # noqa: BLE001
        occ, ind = {}, {}

    def rows(src, keys=None):
        return _rows(src or {}, keys, maps, occ, ind)

    out: list[dict] = []
    if flow == "individual":
        out.append({"title": "Personal details", "rows": rows(d, [
            "first_name", "middle_name", "last_name", "birth_date", "gender", "nationality", "email", "calling_code",
            "phone", "taxId", "bvn", "nin", "immigration_status"])})
        out.append({"title": "Residential address", "rows": rows(d.get("residential_address"))})
        for i, doc in enumerate(d.get("identifying_information") or []):
            out.append({"title": f"Identity document {i + 1}", "rows": rows(doc)})
        out.append({"title": "Selfie", "rows": rows(d, ["selfie_image"])})
        out.append({"title": "Financial profile", "rows": rows(d, [
            "employment_status", "most_recent_occupation_code", "current_employer", "annual_salary",
            "expected_monthly_payments_usd", "source_of_funds", "account_purpose", "account_purpose_other",
            "acting_as_intermediary"])})
        for i, doc in enumerate(d.get("uploaded_documents") or []):
            out.append({"title": f"Additional document {i + 1}", "rows": rows(doc)})
    else:
        out.append({"title": "Company", "rows": rows(d, [
            "business_legal_name", "business_trade_name", "business_type", "registration_number", "incorporation_date",
            "incorporation_country", "tax_id", "email", "phone_calling_code", "phone_number", "business_industry",
            "primary_website", "statement_descriptor", "is_dao", "business_description"])})
        out.append({"title": "Registered address", "rows": rows(d.get("registered_address"))})
        out.append({"title": "Operating address", "rows": rows(d.get("physical_address"))})
        for i, p in enumerate(d.get("associated_persons") or []):
            name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or f"Person {i + 1}"
            out.append({"title": f"Associated person — {name}", "rows": rows(p, [
                "first_name", "last_name", "birth_date", "nationality", "email", "phone_calling_code", "phone", "title",
                "ownership_percentage", "has_ownership", "has_control", "is_signer", "is_director",
                "relationship_established_at", "annual_salary"])})
            out.append({"title": f"{name} — residential address", "rows": rows(p.get("residential_address")), "sub": True})
            for j, doc in enumerate(p.get("identifying_information") or []):
                if doc.get("type") or doc.get("number"):
                    out.append({"title": f"{name} — {label(doc.get('type') or 'document')}", "rows": rows(doc), "sub": True})
        out.append({"title": "Activity & compliance", "rows": rows(d, [
            "account_purpose", "account_purpose_other", "source_of_funds", "high_risk_activities",
            "high_risk_activities_explanation", "conducts_money_services", "conducts_money_services_description",
            "compliance_screening_explanation", "estimated_annual_revenue_usd", "expected_monthly_payments_usd",
            "operates_in_prohibited_countries", "ownership_threshold", "has_material_intermediary_ownership",
            "pep_status", "third_party_msb_payments", "regulated_activities_description",
            "primary_regulatory_authority_country", "primary_regulatory_authority_name", "license_number"])})
        for key, title in (("extra_business_info", "Business profile"), ("collections_data", "Collections"),
                           ("payouts_data", "Payouts")):
            r = rows(d.get(key))
            if r:
                out.append({"title": title, "rows": r})
        for i, doc in enumerate(d.get("documents") or []):
            if doc.get("file"):
                out.append({"title": f"Document — {maps['purpose'].get(doc.get('purpose'), doc.get('purpose'))}",
                            "rows": rows(doc)})
        for i, doc in enumerate(d.get("extra_documents") or []):
            if doc.get("file"):
                out.append({"title": f"Extra document {i + 1}", "rows": rows(doc)})
    out.append({"title": "Accounts requested", "rows": rows(d, [
        "usd_virtual_account", "eur_virtual_account", "gbp_virtual_account", "eurde_virtual_account"])})
    return [s for s in out if s["rows"]]
