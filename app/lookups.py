"""Yativo lookup data (the source of truth for every enum), cached in memory and on disk.

The guide is explicit: "Rule data drifts. Endpoints don't." So nothing here is hardcoded
except values the guide documents as fixed server-side enums (business types, document
purposes, high-risk activities, ...), and even those are only used for validation.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from typing import Any

import httpx
import phonenumbers

from .config import settings

_lock = threading.Lock()
_mem: dict[str, tuple[float, Any]] = {}


class LookupUnavailable(RuntimeError):
    pass


def _cache_file(key: str):
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", key)
    return settings.cache_dir / f"{safe}.json"


def _fetch(path: str) -> Any:
    """GET {base}/api/{path} with memory + disk cache. Stale disk data is used if Yativo is down."""
    key = path
    now = time.time()
    with _lock:
        hit = _mem.get(key)
        if hit and now - hit[0] < settings.lookup_cache_ttl:
            return hit[1]

    url = f"{settings.yativo_base_url}/api/{path}"
    try:
        r = httpx.get(url, timeout=20, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
        try:
            settings.cache_dir.mkdir(parents=True, exist_ok=True)
            _cache_file(key).write_text(json.dumps(data))
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        f = _cache_file(key)
        if f.exists():
            data = json.loads(f.read_text())
        elif hit:
            data = hit[1]
        else:
            raise LookupUnavailable(f"Lookup {path} is unavailable: {exc}") from exc

    with _lock:
        _mem[key] = (now, data)
    return data


def clear_cache() -> None:
    with _lock:
        _mem.clear()
    for f in settings.cache_dir.glob("*.json"):
        f.unlink(missing_ok=True)


# ---------------------------------------------------------------- countries & geography

def countries() -> list[dict[str, str]]:
    return [{"code": c["code"].upper(), "name": c["name"]} for c in _fetch("countries") if c.get("code")]


def country_codes() -> set[str]:
    return {c["code"] for c in countries()}


def country_name(code: str | None) -> str:
    code = (code or "").upper()
    for c in countries():
        if c["code"] == code:
            return c["name"]
    return code


def _cc(code: str) -> str:
    code = (code or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        raise ValueError("Invalid country code")
    return code


def subdivisions(country: str) -> list[dict[str, str]]:
    """Deduplicated [{code, name}] (the endpoint returns lower- and title-case duplicates)."""
    raw = _fetch(f"subdivisions/{_cc(country)}")
    best: dict[str, str] = {}
    for row in raw if isinstance(raw, list) else []:
        code = str(row.get("code") or row.get("subdivision_code") or "").strip().upper()
        name = str(row.get("name") or "").strip()
        if not code:
            continue
        current = best.get(code)
        # Prefer a properly cased name over an all-lowercase duplicate, then the longer one.
        if current is None or (current.islower() and not name.islower()) or (
            current.islower() == name.islower() and len(name) > len(current)
        ):
            best[code] = name
    out = [{"code": c, "name": (n if not n.islower() else n.title())} for c, n in best.items()]
    return sorted(out, key=lambda x: x["name"].lower())


def identification_types(country: str) -> list[dict[str, str]]:
    rows = _fetch(f"identification-types/{_cc(country)}")
    rows = rows if isinstance(rows, list) else []
    if not any(r.get("type") == "other" for r in rows):
        rows.append({"type": "other", "description": "Other Government Issued ID"})
    return rows


def photo_id_types(country: str) -> list[dict[str, str]]:
    """Photo ID document types for a country: universal ones plus any extra photo types Yativo lists."""
    out = {r["type"]: r for r in UNIVERSAL_PHOTO_IDS}
    for r in identification_types(country):
        t = str(r.get("type", "")).lower()
        if t and t not in NON_PHOTO_ID_TYPES and t not in out and t != "other":
            out[t] = {"type": t, "description": r.get("description") or t.replace("_", " ").title()}
    out["other"] = {"type": "other", "description": "Other government-issued photo ID"}
    return list(out.values())


NO_POSTAL_CODE_COUNTRIES = set(
    "AO AG AW BS BZ BJ BW BF BI CM CF KM CG CD CK DJ DM GQ ER FJ GM GD GY HK KI MO MW ML MR NR NU QA RW "
    "KN LC ST SC SL SB SR SY TL TK TO TV UG VU YE ZW".split()
)
GENERIC_POSTAL = r"^[A-Za-z0-9][A-Za-z0-9 \-]{0,18}[A-Za-z0-9]$"


def _php_regex_to_python(rule: str) -> str | None:
    rule = (rule or "").strip()
    if not rule:
        return None
    m = re.fullmatch(r"/(.*)/([a-zA-Z]*)", rule, flags=re.S)
    body = m.group(1) if m else rule
    body = body.replace("\\/", "/")
    try:
        re.compile(body)
    except re.error:
        return None
    return body


def _python_regex_to_js(body: str) -> str:
    return body.replace("\\A", "^").replace("\\Z", "$").replace("\\z", "$")


def postal_format(country: str) -> dict[str, Any]:
    cc = _cc(country)
    uses = cc not in NO_POSTAL_CODE_COUNTRIES
    regex = None
    samples: list[str] = []
    try:
        data = _fetch(f"postal-codes/{cc}")
        validation = data.get("validation") or {}
        regex = _php_regex_to_python(validation.get("rule_regex") or "")
        samples = list(validation.get("rule_samples") or [])
        if data.get("uses_postal_codes") is False or (not regex and not samples):
            uses = uses and bool(regex or samples)
    except LookupUnavailable:
        pass
    except Exception:  # noqa: BLE001 - malformed response; fall back to the generic rule
        pass
    py = regex or GENERIC_POSTAL
    return {
        "country": cc,
        "uses_postal_codes": uses,
        "regex": py,
        "js_regex": _python_regex_to_js(py),
        "samples": samples[:3],
    }


def postal_code_valid(country: str, value: str) -> bool:
    fmt = postal_format(country)
    if not value:
        return not fmt["uses_postal_codes"]
    if not fmt["uses_postal_codes"]:
        return len(value) <= 20
    try:
        return re.search(fmt["regex"], value) is not None
    except re.error:
        return re.search(GENERIC_POSTAL, value) is not None


def calling_codes() -> list[dict[str, str]]:
    out = []
    for c in countries():
        code = phonenumbers.country_code_for_region(c["code"])
        if code:
            out.append({"country": c["code"], "name": c["name"], "dial": f"+{code}"})
    return out


# ---------------------------------------------------------------- big searchable lists

def occupations() -> list[dict[str, str]]:
    return [{"code": str(r["code"]), "label": r["occupation"]} for r in _fetch("occupations")]


def occupation_codes() -> set[str]:
    return {o["code"] for o in occupations()}


def business_industries() -> list[dict[str, str]]:
    return [{"code": str(r["code"]), "label": r["occupation"]} for r in _fetch("business-industries")]


# ---------------------------------------------------------------- flow enums

def _map(path: str) -> dict[str, str]:
    data = _fetch(path)
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def individual_account_purposes() -> dict[str, str]:
    return _map("individual-kyc/account-purposes")


def individual_source_of_funds() -> dict[str, str]:
    return _map("individual-kyc/source-of-funds")


def expected_monthly_payments() -> dict[str, str]:
    raw = _map("individual-kyc/expected-monthly-payments-usd")
    labels = {
        "LessThan5K": "Less than $5,000",
        "From5KTo10K": "$5,000 – $9,999",
        "From10KTo50K": "$10,000 – $49,999",
        "GreaterThan50K": "$50,000 or more",
    }
    return {k: labels.get(k, v) for k, v in raw.items()}


def business_account_purposes() -> dict[str, str]:
    return _map("business-kyc/account-purposes")


def business_source_of_funds() -> dict[str, str]:
    return _map("business-kyc/source-of-funds")


def transaction_currencies() -> list[dict[str, str]]:
    return [{"code": r["code"], "name": r["name"]} for r in _fetch("transaction-currencies")]


def transaction_countries() -> list[dict[str, str]]:
    return [
        {"code": r["code"], "name": r["name"]}
        for r in _fetch("transaction-countries")
        if str(r.get("status", "enabled")).lower() == "enabled"
    ]


# ---------------------------------------------------------------- documented fixed enums

EMPLOYMENT_STATUS = {
    "Employed": "Employed",
    "SelfEmployed": "Self-employed",
    "Unemployed": "Unemployed",
    "Student": "Student",
    "Retired": "Retired",
    "Homemaker": "Homemaker",
    "Exempt": "Exempt",
}
GENDERS = {"male": "Male", "female": "Female"}
IMMIGRATION_STATUS = ["Permanent U.S. Resident", "Non-Permanent U.S. Resident", "Non-Resident of U.S."]
BUSINESS_TYPES = {
    "corporation": "Corporation",
    "llc": "Limited Liability Company (LLC)",
    "partnership": "Partnership",
    "sole_prop": "Sole proprietorship",
    "cooperative": "Cooperative",
    "trust": "Trust",
    "other": "Other",
}
DOCUMENT_PURPOSES = {
    "business_registration": "Business registration / certificate of incorporation",
    "proof_of_address": "Proof of address",
    "tax_documents": "Tax documents",
    "compliance_documents": "Compliance documents / licences",
    "financial_statements": "Financial statements",
    "certificate_of_good_standing": "Certificate of good standing",
    "portfolio_statement": "Portfolio statement",
    "board_minutes": "Board minutes",
}
HIGH_RISK_ACTIVITIES = {
    "none_of_the_above": "None of the above",
    "adult_entertainment": "Adult entertainment",
    "gambling": "Gambling",
    "hold_client_funds": "Holding client funds",
    "investment_services": "Investment services",
    "lending_banking": "Lending / banking",
    "marijuana_or_related_services": "Marijuana or related services",
    "money_services": "Money services",
    "nicotine_tobacco_or_related_services": "Nicotine, tobacco or related services",
    "operate_foreign_exchange_virtual_currencies_brokerage_otc": "FX / virtual currency brokerage or OTC",
    "pharmaceuticals": "Pharmaceuticals",
    "precious_metals_precious_stones_jewelry": "Precious metals, stones or jewellery",
    "safe_deposit_box_rentals": "Safe deposit box rentals",
    "third_party_payment_processing": "Third-party payment processing",
    "weapons_firearms_and_explosives": "Weapons, firearms and explosives",
}
# A single statement of account satisfies all three Yativo document slots.
STATEMENT_OF_ACCOUNT_SLOTS = (
    "proof_of_address",
    "proof_of_funds",
    "proof_of_bank_account_ownership",
)
UPLOADED_DOCUMENT_TYPES = {  # kept for reference / display labels only
    "proof_of_address": "Proof of address",
    "proof_of_funds": "Proof of funds",
    "proof_of_bank_account_ownership": "Bank statement (statement of account)",
}
EXTRA_DOCUMENT_TYPES = {
    "articles_of_association": "Articles of association",
    "shareholding_structure": "Shareholding structure chart",
    "memorandum_of_association": "Memorandum of association",
    "operating_agreement": "Operating agreement",
    "proof_of_funds": "Proof of funds",
    "other": "Other",
}
SENDER_GEOGRAPHIES = ["Africa", "Europe", "North America", "South America", "Asia"]
SENDER_INDUSTRIES = ["E-commerce", "Wholesale", "Retail", "Logistics", "Manufacturing", "Consulting", "Others"]
SENDER_TYPES = {"individuals": "Individuals", "businesses": "Businesses", "both": "Both"}
BENEFICIARY_TYPES = {"individuals": "Individuals", "business": "Businesses", "both": "Both"}
PAYOUT_METHODS = {
    "ach": "ACH",
    "wire": "Wire transfer",
    "sepa": "SEPA",
    "swift": "SWIFT",
    "local_transfer": "Local bank transfer",
    "mobile_money": "Mobile money",
    "crypto": "Crypto",
}
MEETING_MODES = {"video_call": "Video call", "in_person": "In person", "phone": "Phone call", "email": "Email"}

# Residents of these countries have no personal income tax, so a person tax ID is optional (§2.4).
NO_INCOME_TAX_COUNTRIES = set("AE AI BH BM BN BS KN KW KY MC OM QA SA TC VG VU".split())

# Names may never contain these (case-insensitive, anywhere) — the customer id is blocked forever (§2.13).
BANNED_NAME_WORDS = ("test", "yativo", "admin")
BANNED_EMAIL_WORDS = ("test",)

# Identification types that are numbers, not photo documents.
NON_PHOTO_ID_TYPES = {"bvn", "nin", "tin", "tax_id", "ssn", "itin", "cpf", "cnpj", "nit", "rfc", "curp", "cuit", "cuil",
                      "nino", "utr", "nie", "pan", "vat"}
SINGLE_SIDED_ID_TYPES = {"passport"}
# Photo IDs accepted for every country. Some countries' lookup lists only tax numbers (e.g. US
# returns ITIN/SSN), but the API takes any type string and the guide's own example is a US passport.
UNIVERSAL_PHOTO_IDS = [
    {"type": "passport", "description": "Passport"},
    {"type": "national_id", "description": "National ID card"},
    {"type": "drivers_license", "description": "Driver's licence"},
]


def is_tax_id_type(t: str | None) -> bool:
    t = re.sub(r"[\s\-]+", "_", (t or "").strip().lower())
    return t in {"tax_id", "tin", "ssn", "itin", "cpf", "cnpj", "nit", "rfc", "cuit", "cuil", "nie"}


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def match_subdivision(country: str, *candidates: str | None) -> str | None:
    """Best-effort mapping of a free-text region (from autocomplete) to a Yativo subdivision code."""
    try:
        subs = subdivisions(country)
    except Exception:  # noqa: BLE001
        return None
    if not subs:
        return None
    noise = r"\b(state|province|region|county|department|departamento|estado|district|prefecture|governorate|territory|of|the)\b"
    by_code = {s["code"].upper(): s["code"] for s in subs}
    by_name = {normalize_text(s["name"]): s["code"] for s in subs}
    by_core = {re.sub(noise, "", normalize_text(s["name"])).strip(): s["code"] for s in subs}
    for cand in candidates:
        if not cand:
            continue
        raw = cand.strip()
        up = raw.upper()
        if up.startswith(country.upper() + "-"):
            up = up.split("-", 1)[1]
        if up in by_code:
            return by_code[up]
        n = normalize_text(raw)
        if n in by_name:
            return by_name[n]
        core = re.sub(noise, "", n).strip()
        if core and core in by_core:
            return by_core[core]
    for cand in candidates:  # last resort: containment either way
        core = re.sub(noise, "", normalize_text(cand or "")).strip()
        if len(core) < 4:
            continue
        for name, code in by_core.items():
            if name and (core in name or name in core):
                return code
    return None
