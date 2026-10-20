"""Server-side validation that mirrors Yativo's KYC/KYB rules (guide §2, §3, §5, §6, §8).

Two layers:
  * Hard Yativo rules (what returns a 422/403 from the API).
  * Stricter "approval readiness" rules from the guide's advice — e.g. strict postal/state checks
    on the individual flow, enum checks on fields Yativo only warns about, photo-ID back images,
    a proof of address for businesses. A submission that passes here should also be approvable.

Errors are keyed by the same dotted paths Yativo uses (e.g. ``associated_persons.0.email``) so
the browser can attach both our errors and any Yativo 422 errors to the right input.

File fields hold references in the form ``"file:<uuid>"`` until the payload is sent to Yativo.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable

from . import lookups as L

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*\.[^\s]{2,}$", re.I)
FILE_REF_RE = re.compile(r"^file:[0-9a-f-]{36}$")
EURDE_CLOSED_FROM = date(2026, 9, 2)


class Errors(dict):
    def add(self, path: str, msg: str) -> None:
        self.setdefault(path, [])
        if msg not in self[path]:
            self[path].append(msg)


# ---------------------------------------------------------------------------- helpers

def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v).strip()


def to_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return None
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return None


def parse_date(v: Any) -> date | None:
    s = _s(v)
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date() if "T" in s else date.fromisoformat(s[:10])
    except ValueError:
        return None


def age_on(d: date, today: date) -> int:
    return today.year - d.year - ((today.month, today.day) < (d.month, d.day))


def is_file_ref(v: Any) -> bool:
    return isinstance(v, str) and bool(FILE_REF_RE.match(v))


def banned_name(value: str) -> bool:
    v = value.lower()
    return any(w in v for w in L.BANNED_NAME_WORDS)


def banned_email(value: str) -> bool:
    v = value.lower()
    return any(w in v for w in L.BANNED_EMAIL_WORDS)


NAME_BANNED_MSG = "This name can't be accepted. Please enter your legal name exactly as it appears on your ID."
EMAIL_BANNED_MSG = "This email address can't be accepted. Please use a different email address."


class Ctx:
    """Validation context: error bag + cached lookups."""

    def __init__(self, today: date | None = None) -> None:
        self.e = Errors()
        self.today = today or date.today()
        self._countries: set[str] | None = None

    @property
    def countries(self) -> set[str]:
        if self._countries is None:
            self._countries = L.country_codes()
        return self._countries

    # --- primitive checks
    def required(self, data: dict, key: str, path: str, label: str, max_len: int | None = None) -> str:
        v = _s(data.get(key))
        if not v:
            self.e.add(path, f"{label} is required.")
        elif max_len and len(v) > max_len:
            self.e.add(path, f"{label} must be at most {max_len} characters.")
        return v

    def optional(self, data: dict, key: str, path: str, label: str, max_len: int | None = None) -> str:
        v = _s(data.get(key))
        if v and max_len and len(v) > max_len:
            self.e.add(path, f"{label} must be at most {max_len} characters.")
        return v

    def name(self, data: dict, key: str, path: str, label: str, max_len: int, required: bool = True) -> str:
        v = self.required(data, key, path, label, max_len) if required else self.optional(data, key, path, label, max_len)
        if v:
            if banned_name(v):
                self.e.add(path, NAME_BANNED_MSG)
            elif not re.search(r"[^\W\d_]", v):
                self.e.add(path, f"{label} must contain letters.")
        return v

    def email(self, data: dict, key: str, path: str, required: bool = True) -> str:
        v = _s(data.get(key))
        if not v:
            if required:
                self.e.add(path, "Email address is required.")
            return v
        if len(v) > 254 or not EMAIL_RE.match(v):
            self.e.add(path, "Enter a valid email address.")
        elif banned_email(v):
            self.e.add(path, EMAIL_BANNED_MSG)
        return v

    def country(self, data: dict, key: str, path: str, label: str, required: bool = True) -> str:
        v = _s(data.get(key)).upper()
        if not v:
            if required:
                self.e.add(path, f"{label} is required.")
        elif v not in self.countries:
            self.e.add(path, f"Select a valid {label.lower()}.")
        return v

    def date(self, data: dict, key: str, path: str, label: str, required: bool = True,
             past: bool = False, future: bool = False, not_future: bool = False) -> date | None:
        raw = _s(data.get(key))
        if not raw:
            if required:
                self.e.add(path, f"{label} is required.")
            return None
        d = parse_date(raw)
        if d is None:
            self.e.add(path, f"{label} must be a valid date (YYYY-MM-DD).")
            return None
        if past and d >= self.today:
            self.e.add(path, f"{label} must be in the past.")
        if future and d <= self.today:
            self.e.add(path, f"{label} must be in the future. Expired documents can't be accepted.")
        if not_future and d > self.today:
            self.e.add(path, f"{label} can't be in the future.")
        return d

    def birth_date(self, data: dict, key: str, path: str) -> None:
        d = self.date(data, key, path, "Date of birth")
        if d:
            a = age_on(d, self.today)
            if a < 18:
                self.e.add(path, "You must be at least 18 years old.")
            elif a > 120:
                self.e.add(path, "Enter a valid date of birth.")

    def choice(self, data: dict, key: str, path: str, label: str, allowed, required: bool = True) -> str:
        v = _s(data.get(key))
        if not v:
            if required:
                self.e.add(path, f"{label} is required.")
        elif v not in allowed:
            self.e.add(path, f"Select a valid {label.lower()}.")
        return v

    def boolean(self, data: dict, key: str, path: str, label: str, required: bool = False) -> bool | None:
        raw = data.get(key)
        b = to_bool(raw)
        if b is None:
            if required:
                self.e.add(path, f"Please answer: {label}")
            elif raw not in (None, ""):
                self.e.add(path, f"{label} must be yes or no.")
        return b

    def file(self, data: dict, key: str, path: str, label: str, required: bool = True) -> str:
        v = data.get(key)
        if not v:
            if required:
                self.e.add(path, f"{label} is required.")
            return ""
        if not is_file_ref(v):
            self.e.add(path, f"{label}: please upload the file again.")
            return ""
        return v

    def digits(self, data: dict, key: str, path: str, label: str, n: int | None = None,
               min_len: int | None = None, max_len: int | None = None, required: bool = True) -> str:
        v = _s(data.get(key))
        if not v:
            if required:
                self.e.add(path, f"{label} is required.")
            return v
        if not v.isdigit():
            self.e.add(path, f"{label} must contain digits only.")
        elif n and len(v) != n:
            self.e.add(path, f"{label} must be exactly {n} digits.")
        elif min_len and len(v) < min_len:
            self.e.add(path, f"{label} must be at least {min_len} digits.")
        elif max_len and len(v) > max_len:
            self.e.add(path, f"{label} must be at most {max_len} digits.")
        return v

    def number(self, data: dict, key: str, path: str, label: str, lo: float | None = None,
               hi: float | None = None, required: bool = False, integer: bool = False) -> float | None:
        raw = _s(data.get(key))
        if raw == "":
            if required:
                self.e.add(path, f"{label} is required.")
            return None
        try:
            val = float(raw)
            if integer and not val.is_integer():
                raise ValueError
        except ValueError:
            self.e.add(path, f"{label} must be a {'whole ' if integer else ''}number.")
            return None
        if lo is not None and val < lo:
            self.e.add(path, f"{label} must be at least {lo:g}.")
        if hi is not None and val > hi:
            self.e.add(path, f"{label} must be at most {hi:g}.")
        return val

    def address(self, addr: Any, path: str, label: str, max_line: int, max_city: int,
                proof: str = "none") -> None:
        """proof: none | optional | required"""
        if not isinstance(addr, dict):
            self.e.add(f"{path}.street_line_1", f"{label} is required.")
            return
        country = self.country(addr, "country", f"{path}.country", "Country")
        self.required(addr, "street_line_1", f"{path}.street_line_1", "Street address", max_line)
        self.optional(addr, "street_line_2", f"{path}.street_line_2", "Address line 2", max_line)
        self.required(addr, "city", f"{path}.city", "City", max_city)
        state = _s(addr.get("state")).upper()
        if country and country in self.countries:
            subs = L.subdivisions(country)
            if subs:
                if not state:
                    self.e.add(f"{path}.state", "State / region is required.")
                elif state not in {s["code"].upper() for s in subs}:
                    self.e.add(f"{path}.state", "Select a valid state / region for the selected country.")
            elif not state:
                self.e.add(f"{path}.state", "State / region is required.")
            postal = _s(addr.get("postal_code"))
            fmt = L.postal_format(country)
            if fmt["uses_postal_codes"]:
                if not postal:
                    self.e.add(f"{path}.postal_code", "Postal code is required.")
                elif not L.postal_code_valid(country, postal):
                    ex = f" e.g. {', '.join(fmt['samples'])}" if fmt["samples"] else ""
                    self.e.add(f"{path}.postal_code", f"Enter a valid postal code for {L.country_name(country)}.{ex}")
            elif postal and len(postal) > 20:
                self.e.add(f"{path}.postal_code", "Postal code is too long.")
        if proof != "none":
            self.file(addr, "proof_of_address_file", f"{path}.proof_of_address_file", "Proof of address",
                      required=proof == "required")

    def virtual_accounts(self, data: dict, allowed: dict[str, bool], is_new: bool) -> None:
        flags = {}
        for f in ("usd", "eur", "eurde", "gbp"):
            key = f"{f}_virtual_account"
            b = self.boolean(data, key, key, f"{f.upper()} account")
            flags[f] = bool(b)
            if b and not allowed.get(f, False):
                self.e.add(key, "This account type is not available.")
        if flags["eurde"] and (flags["usd"] or flags["eur"]):
            self.e.add("eurde_virtual_account", "EUR (DE) can't be combined with USD or EUR accounts.")
        if flags["eurde"] and is_new and self.today >= EURDE_CLOSED_FROM:
            self.e.add("eurde_virtual_account", "EUR (DE) accounts are no longer available to new customers.")


# ---------------------------------------------------------------------------- normalisation

def _strip_strings(v: Any) -> Any:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        return [_strip_strings(x) for x in v]
    if isinstance(v, dict):
        return {k: _strip_strings(x) for k, x in v.items()}
    return v


def _norm_phone(calling_code: str, phone: str) -> tuple[str, str]:
    cc = re.sub(r"[^\d]", "", calling_code or "")
    cc = f"+{cc}" if cc else ""
    digits = re.sub(r"\D", "", phone or "")
    if (phone or "").strip().startswith("+"):
        # The number was typed with its own country code: trust it over the dropdown.
        try:
            import phonenumbers

            num = phonenumbers.parse(phone.strip(), None)
            return f"+{num.country_code}", str(num.national_number)
        except Exception:  # noqa: BLE001
            pass
    if cc and digits.startswith(cc[1:]) and len(digits) > len(cc[1:]) + 6 and (phone or "").strip().startswith("+"):
        digits = digits[len(cc[1:]):]
    if cc and digits:
        try:
            import phonenumbers

            num = phonenumbers.parse(cc + digits, None)
            nsn = str(num.national_number)
            if 7 <= len(nsn) <= 15:
                digits = nsn  # drops a national trunk prefix such as the leading 0 in 080...
        except Exception:  # noqa: BLE001
            pass
    return cc, digits


def _norm_address(a: Any) -> Any:
    if not isinstance(a, dict):
        return a
    a = dict(a)
    country = _s(a.get("country")).upper()
    a["country"] = country
    state = _s(a.get("state")).upper()
    if country and state.startswith(country + "-"):
        state = state.split("-", 1)[1]
    a["state"] = state
    a["postal_code"] = _s(a.get("postal_code")).upper() if country in {"GB", "CA", "NL", "IE"} else _s(a.get("postal_code"))
    return a


def normalize(flow: str, data: dict) -> dict:
    d = _strip_strings(dict(data or {}))
    if flow == "individual":
        for k in ("nationality",):
            d[k] = _s(d.get(k)).upper()
        d["email"] = _s(d.get("email")).lower()
        d["calling_code"], d["phone"] = _norm_phone(_s(d.get("calling_code")), _s(d.get("phone")))
        d["residential_address"] = _norm_address(d.get("residential_address"))
        for k in ("bvn", "nin", "annual_salary"):
            if d.get(k) is not None:
                d[k] = re.sub(r"[\s,]", "", _s(d.get(k)))
        ids = d.get("identifying_information") or []
        for i in ids if isinstance(ids, list) else []:
            if isinstance(i, dict):
                i["issuing_country"] = _s(i.get("issuing_country")).upper()
    else:
        d["email"] = _s(d.get("email")).lower()
        d["incorporation_country"] = _s(d.get("incorporation_country")).upper()
        d["phone_calling_code"], d["phone_number"] = _norm_phone(_s(d.get("phone_calling_code")), _s(d.get("phone_number")))
        d["registered_address"] = _norm_address(d.get("registered_address"))
        d["physical_address"] = _norm_address(d.get("physical_address"))
        if d.get("primary_website") and not re.match(r"^https?://", d["primary_website"], re.I):
            d["primary_website"] = "https://" + d["primary_website"]
        persons = d.get("associated_persons") or []
        for p in persons if isinstance(persons, list) else []:
            if not isinstance(p, dict):
                continue
            p["email"] = _s(p.get("email")).lower()
            p["nationality"] = _s(p.get("nationality")).upper()
            p["residential_address"] = _norm_address(p.get("residential_address"))
            cc, ph = _norm_phone(_s(p.get("phone_calling_code")), _s(p.get("phone")))
            p["phone_calling_code"], p["phone"] = cc, ph
            if not to_bool(p.get("has_ownership")) and _s(p.get("ownership_percentage")) == "":
                p["ownership_percentage"] = 0  # required by the API even for non-owners
            for i in p.get("identifying_information") or []:
                if isinstance(i, dict):
                    i["issuing_country"] = _s(i.get("issuing_country")).upper()
                    if _s(i.get("type")).lower() == "bvn":
                        i["number"] = re.sub(r"\D", "", _s(i.get("number")))
    return d


# ---------------------------------------------------------------------------- individual

INDIVIDUAL_STEPS: dict[str, tuple[str, ...]] = {
    "personal": ("first_name", "middle_name", "last_name", "email", "calling_code", "phone", "birth_date",
                 "nationality", "gender", "taxId", "bvn", "nin"),
    "address": ("residential_address", "bvn", "nin"),
    "identity": ("identifying_information",),
    "selfie": ("selfie_image",),
    "profile": ("employment_status", "most_recent_occupation_code", "current_employer", "annual_salary",
                "expected_monthly_payments_usd", "source_of_funds", "account_purpose", "account_purpose_other",
                "acting_as_intermediary", "immigration_status"),
    "extras": ("uploaded_documents", "usd_virtual_account", "eur_virtual_account", "eurde_virtual_account",
               "gbp_virtual_account"),
    "review": ("consent",),
}


def validate_individual(d: dict, accounts: dict[str, bool], is_new: bool = True, today: date | None = None) -> Errors:
    c = Ctx(today)
    e = c.e

    c.name(d, "first_name", "first_name", "First name", 1024)
    c.name(d, "middle_name", "middle_name", "Middle name", 1024, required=False)
    c.name(d, "last_name", "last_name", "Last name", 1024)
    c.email(d, "email", "email")
    cc = _s(d.get("calling_code"))
    if not cc:
        e.add("calling_code", "Country code is required.")
    elif not re.fullmatch(r"\+\d{1,4}", cc):
        e.add("calling_code", "Select a valid country code.")
    phone = _s(d.get("phone"))
    if not phone:
        e.add("phone", "Phone number is required.")
    elif not re.fullmatch(r"\d{8,15}", phone):
        e.add("phone", "Enter a valid phone number (8–15 digits, without the country code).")
    c.birth_date(d, "birth_date", "birth_date")
    nationality = c.country(d, "nationality", "nationality", "Nationality")
    c.choice(d, "gender", "gender", "Gender", L.GENDERS)
    c.required(d, "taxId", "taxId", "Tax identification number", 100)

    addr = d.get("residential_address") if isinstance(d.get("residential_address"), dict) else {}
    res_country = _s(addr.get("country")).upper()
    if "NG" in (nationality, res_country):  # Yativo requires both for Nigerian nationals AND residents
        c.digits(d, "bvn", "bvn", "BVN", n=11)
        c.digits(d, "nin", "nin", "NIN", n=11)

    c.address(d.get("residential_address"), "residential_address", "Residential address", 256, 256, proof="required")

    ids = d.get("identifying_information")
    if not isinstance(ids, list) or not ids:
        e.add("identifying_information", "Add at least one identity document.")
        ids = []
    for i, doc in enumerate(ids):
        p = f"identifying_information.{i}"
        if not isinstance(doc, dict):
            e.add(p, "Invalid document.")
            continue
        t = c.required(doc, "type", f"{p}.type", "Document type").lower()
        if t in L.NON_PHOTO_ID_TYPES:
            e.add(f"{p}.type", "Choose a photo ID document (e.g. passport, national ID card, driver's licence).")
        ic = c.country(doc, "issuing_country", f"{p}.issuing_country", "Issuing country")
        if ic and t and ic in c.countries and t not in L.NON_PHOTO_ID_TYPES:
            if t not in {r["type"] for r in L.photo_id_types(ic)}:
                e.add(f"{p}.type", "This document type isn't available for the selected country.")
        c.required(doc, "number", f"{p}.number", "Document number", 100)
        issued = c.date(doc, "date_issued", f"{p}.date_issued", "Issue date", past=True)
        expires = c.date(doc, "expiration_date", f"{p}.expiration_date", "Expiry date", future=True)
        if issued and expires and expires <= issued:
            e.add(f"{p}.expiration_date", "Expiry date must be after the issue date.")
        c.file(doc, "image_front_file", f"{p}.image_front_file", "Front of the document")
        needs_back = t not in L.SINGLE_SIDED_ID_TYPES and t != "other"
        c.file(doc, "image_back_file", f"{p}.image_back_file", "Back of the document", required=needs_back)

    c.file(d, "selfie_image", "selfie_image", "Selfie")

    c.choice(d, "employment_status", "employment_status", "Employment status", L.EMPLOYMENT_STATUS)
    occ = c.required(d, "most_recent_occupation_code", "most_recent_occupation_code", "Occupation")
    if occ and occ not in L.occupation_codes():
        e.add("most_recent_occupation_code", "Select your occupation from the list.")
    c.optional(d, "current_employer", "current_employer", "Employer", 512)
    sal = _s(d.get("annual_salary"))
    if sal and (not sal.isdigit() or len(sal) > 20):
        e.add("annual_salary", "Annual income must be a whole number without symbols or separators.")
    c.choice(d, "expected_monthly_payments_usd", "expected_monthly_payments_usd", "Expected monthly volume",
             L.expected_monthly_payments())
    c.choice(d, "source_of_funds", "source_of_funds", "Source of funds", L.individual_source_of_funds())
    purpose = c.choice(d, "account_purpose", "account_purpose", "Account purpose", L.individual_account_purposes())
    if purpose.lower() == "other":
        c.required(d, "account_purpose_other", "account_purpose_other", "Account purpose description", 1000)
    c.boolean(d, "acting_as_intermediary", "acting_as_intermediary", "Acting as an intermediary")
    imm = _s(d.get("immigration_status"))
    if imm and imm not in L.IMMIGRATION_STATUS:
        e.add("immigration_status", "Select a valid immigration status.")
    if res_country == "US" and nationality and nationality != "US" and not imm:
        e.add("immigration_status", "Immigration status is required for non-U.S. citizens living in the U.S.")

    # A single statement of account covers all required document slots.
    docs = d.get("uploaded_documents") if isinstance(d.get("uploaded_documents"), list) else []
    soa = docs[0] if docs and isinstance(docs[0], dict) else {}
    c.file(soa, "file", "uploaded_documents.0.file", "Statement of account")

    c.virtual_accounts(d, accounts, is_new)
    if not to_bool(d.get("consent")):
        e.add("consent", "Please confirm the declaration to continue.")
    return e


# ---------------------------------------------------------------------------- business

BUSINESS_STEPS: dict[str, tuple[str, ...]] = {
    "company": ("business_legal_name", "business_trade_name", "business_description", "email", "business_type",
                "registration_number", "incorporation_date", "incorporation_country", "tax_id",
                "phone_calling_code", "phone_number", "business_industry", "primary_website", "is_dao",
                "statement_descriptor"),
    "addresses": ("registered_address", "physical_address"),
    "people": ("associated_persons",),
    "activity": ("account_purpose", "account_purpose_other", "source_of_funds", "high_risk_activities",
                 "high_risk_activities_explanation", "conducts_money_services",
                 "conducts_money_services_description", "compliance_screening_explanation",
                 "estimated_annual_revenue_usd", "expected_monthly_payments_usd",
                 "operates_in_prohibited_countries", "ownership_threshold", "has_material_intermediary_ownership",
                 "pep_status", "third_party_msb_payments", "regulated_activities_description",
                 "primary_regulatory_authority_country", "primary_regulatory_authority_name", "license_number"),
    "operations": ("extra_business_info", "collections_data", "payouts_data"),
    "documents": ("documents", "extra_documents"),
    "extras": ("usd_virtual_account", "eur_virtual_account", "eurde_virtual_account", "gbp_virtual_account"),
    "review": ("consent",),
}


def _person(c: Ctx, p: dict, i: int) -> None:
    e = c.e
    base = f"associated_persons.{i}"
    c.name(p, "first_name", f"{base}.first_name", "First name", 100)
    c.name(p, "last_name", f"{base}.last_name", "Last name", 100)
    c.birth_date(p, "birth_date", f"{base}.birth_date")
    nationality = c.country(p, "nationality", f"{base}.nationality", "Nationality")
    c.email(p, "email", f"{base}.email")
    phone = _s(p.get("phone"))
    if phone and not re.fullmatch(r"\d{7,15}", phone):
        e.add(f"{base}.phone", "Enter a valid phone number (7–15 digits, without the country code).")
    c.optional(p, "title", f"{base}.title", "Job title", 255)
    c.number(p, "ownership_percentage", f"{base}.ownership_percentage", "Ownership percentage", 0, 100, required=True)
    sal = _s(p.get("annual_salary"))
    if sal and (not sal.isdigit() or len(sal) > 20):
        e.add(f"{base}.annual_salary", "Annual income must be a whole number without symbols or separators.")
    c.date(p, "relationship_established_at", f"{base}.relationship_established_at", "Relationship start date",
           required=False, not_future=True)
    roles = {}
    for k in ("has_ownership", "has_control", "is_signer", "is_director"):
        roles[k] = c.boolean(p, k, f"{base}.{k}", k.replace("_", " "))
    if not any(roles.values()):
        e.add(f"{base}.roles", "Select at least one role for this person.")
    own = to_bool(p.get("has_ownership"))
    pct = parse_float(p.get("ownership_percentage"))
    if own and (pct is None or pct <= 0):
        e.add(f"{base}.ownership_percentage", "Owners must have an ownership percentage above 0.")

    c.address(p.get("residential_address"), f"{base}.residential_address", "Residential address", 255, 100)
    res_country = _s((p.get("residential_address") or {}).get("country")).upper() if isinstance(p.get("residential_address"), dict) else ""

    ids = p.get("identifying_information")
    if not isinstance(ids, list):
        ids = []
    photo_ids = 0
    for j, doc in enumerate(ids):
        q = f"{base}.identifying_information.{j}"
        if not isinstance(doc, dict):
            continue
        t = _s(doc.get("type")).lower()
        if not t:
            e.add(f"{q}.type", "Document type is required.")
            continue
        if t == "bvn":
            if _s(doc.get("number")):
                c.digits(doc, "number", f"{q}.number", "BVN", n=11)
            continue
        if L.is_tax_id_type(t):
            c.optional(doc, "number", f"{q}.number", "Tax ID", 100)
            if _s(doc.get("issuing_country")):
                c.country(doc, "issuing_country", f"{q}.issuing_country", "Issuing country")
            continue
        photo_ids += 1
        if t in L.NON_PHOTO_ID_TYPES:
            e.add(f"{q}.type", "Choose a photo ID document (e.g. passport, national ID card, driver's licence).")
        ic = c.country(doc, "issuing_country", f"{q}.issuing_country", "Issuing country")
        if ic and ic in c.countries and t not in L.NON_PHOTO_ID_TYPES and t not in {r["type"] for r in L.photo_id_types(ic)}:
            e.add(f"{q}.type", "This document type isn't available for the selected country.")
        c.required(doc, "number", f"{q}.number", "Document number", 100)
        issued = c.date(doc, "date_issued", f"{q}.date_issued", "Issue date", required=False, past=True)
        expires = c.date(doc, "expiration", f"{q}.expiration", "Expiry date", future=True)
        if issued and expires and expires <= issued:
            e.add(f"{q}.expiration", "Expiry date must be after the issue date.")
        c.file(doc, "image_front", f"{q}.image_front", "Front of the document")
        c.file(doc, "image_back", f"{q}.image_back", "Back of the document",
               required=t not in L.SINGLE_SIDED_ID_TYPES and t != "other")
    if photo_ids == 0:
        e.add(f"{base}.identifying_information",
              "At least one photo ID (e.g. passport, national ID card) is required for each person.")
    del nationality, res_country


def parse_float(v: Any) -> float | None:
    try:
        return float(_s(v))
    except ValueError:
        return None


def validate_business(d: dict, accounts: dict[str, bool], is_new: bool = True, today: date | None = None) -> Errors:
    c = Ctx(today)
    e = c.e

    # --- company
    c.name(d, "business_legal_name", "business_legal_name", "Legal business name", 255)
    c.name(d, "business_trade_name", "business_trade_name", "Trading name", 255)
    desc = c.required(d, "business_description", "business_description", "Business description", 1000)
    if desc and len(desc) < 140:
        e.add("business_description", f"Please describe the business in at least 140 characters ({len(desc)} so far).")
    c.email(d, "email", "email")
    c.choice(d, "business_type", "business_type", "Business type", L.BUSINESS_TYPES)
    c.required(d, "registration_number", "registration_number", "Registration number", 100)
    c.date(d, "incorporation_date", "incorporation_date", "Incorporation date", past=True)
    c.country(d, "incorporation_country", "incorporation_country", "Country of incorporation")
    c.required(d, "tax_id", "tax_id", "Company tax ID", 100)
    pcc = _s(d.get("phone_calling_code"))
    if not pcc:
        e.add("phone_calling_code", "Country code is required.")
    elif not re.fullmatch(r"\+[1-9]\d{0,3}", pcc):
        e.add("phone_calling_code", "Select a valid country code.")
    pn = _s(d.get("phone_number"))
    if not pn:
        e.add("phone_number", "Business phone number is required.")
    elif not re.fullmatch(r"\d{7,15}", pn):
        e.add("phone_number", "Enter a valid phone number (7–15 digits, without the country code).")
    ind = c.required(d, "business_industry", "business_industry", "Industry")
    if ind and ind not in {i["code"] for i in L.business_industries()}:
        e.add("business_industry", "Select the industry from the list.")
    web = _s(d.get("primary_website"))
    if web and not URL_RE.match(web):
        e.add("primary_website", "Enter a valid website address.")
    c.boolean(d, "is_dao", "is_dao", "DAO")
    c.optional(d, "statement_descriptor", "statement_descriptor", "Statement descriptor", 22)

    # --- addresses
    c.address(d.get("registered_address"), "registered_address", "Registered address", 255, 100)
    c.address(d.get("physical_address"), "physical_address", "Operating address", 255, 100, proof="required")

    # --- people
    persons = d.get("associated_persons")
    if not isinstance(persons, list) or not persons:
        e.add("associated_persons", "Add at least one owner, director or authorised signer.")
        persons = []
    total = 0.0
    for i, p in enumerate(persons):
        if not isinstance(p, dict):
            continue
        _person(c, p, i)
        if to_bool(p.get("has_ownership")):
            total += parse_float(p.get("ownership_percentage")) or 0
    if total > 100.0001:
        e.add("associated_persons", f"Total ownership across all owners is {total:g}% — it can't exceed 100%.")
    if persons and not any(to_bool(p.get("is_signer")) or to_bool(p.get("has_control")) for p in persons if isinstance(p, dict)):
        e.add("associated_persons", "At least one person must be an authorised signer or have control of the business.")

    # --- activity & compliance
    purpose = c.choice(d, "account_purpose", "account_purpose", "Account purpose", L.business_account_purposes())
    if purpose == "other":
        c.required(d, "account_purpose_other", "account_purpose_other", "Account purpose description", 1000)
    c.choice(d, "source_of_funds", "source_of_funds", "Source of funds", L.business_source_of_funds())
    hra = d.get("high_risk_activities")
    if not isinstance(hra, list) or not hra:
        e.add("high_risk_activities", "Select the activities that apply, or “None of the above”.")
        hra = []
    bad = [x for x in hra if x not in L.HIGH_RISK_ACTIVITIES]
    if bad:
        e.add("high_risk_activities", "Select valid activities.")
    if "none_of_the_above" in hra and len(hra) > 1:
        e.add("high_risk_activities", "“None of the above” can't be combined with other activities.")
    if any(x != "none_of_the_above" for x in hra):
        v = c.required(d, "high_risk_activities_explanation", "high_risk_activities_explanation",
                       "Explanation of these activities", 5000)
        del v
    ms = c.boolean(d, "conducts_money_services", "conducts_money_services", "Does the business provide money services?", required=True)
    if ms:
        c.required(d, "conducts_money_services_description", "conducts_money_services_description",
                   "Money services description", 5000)
        c.required(d, "compliance_screening_explanation", "compliance_screening_explanation",
                   "Compliance screening explanation", 5000)
    rev = _s(d.get("estimated_annual_revenue_usd"))
    if rev and not re.fullmatch(r"\d{1,15}", rev):
        e.add("estimated_annual_revenue_usd", "Enter the amount in whole US dollars, digits only.")
    c.number(d, "expected_monthly_payments_usd", "expected_monthly_payments_usd", "Expected monthly payments", 0,
             required=True, integer=True)
    c.choice(d, "operates_in_prohibited_countries", "operates_in_prohibited_countries",
             "Operations in sanctioned countries", {"yes", "no"})
    c.number(d, "ownership_threshold", "ownership_threshold", "Ownership threshold", 5, 100, integer=True)
    c.boolean(d, "has_material_intermediary_ownership", "has_material_intermediary_ownership", "Intermediary ownership")
    c.boolean(d, "pep_status", "pep_status", "Is any owner or director a politically exposed person?", required=True)
    c.boolean(d, "third_party_msb_payments", "third_party_msb_payments",
              "Will you process payments on behalf of third parties?", required=True)
    c.optional(d, "regulated_activities_description", "regulated_activities_description", "Regulated activities", 5000)
    c.country(d, "primary_regulatory_authority_country", "primary_regulatory_authority_country",
              "Regulator country", required=False)
    c.optional(d, "primary_regulatory_authority_name", "primary_regulatory_authority_name", "Regulator name", 255)
    c.optional(d, "license_number", "license_number", "Licence number", 255)

    # --- operations profile (optional nested sections)
    ebi = d.get("extra_business_info") if isinstance(d.get("extra_business_info"), dict) else {}
    nd = _s(ebi.get("business_description"))
    if nd and not 140 <= len(nd) <= 5000:
        e.add("extra_business_info.business_description", "Use 140–5000 characters.")
    c.choice(ebi, "meeting_mode", "extra_business_info.meeting_mode", "Preferred meeting mode", L.MEETING_MODES, required=False)
    c.optional(ebi, "industry_vertical", "extra_business_info.industry_vertical", "Industry vertical", 255)
    c.choice(ebi, "obo_usage", "extra_business_info.obo_usage", "On-behalf-of usage", {"yes", "no"}, required=False)
    for k in ("monthly_volume_usd", "avg_transaction_usd", "max_transaction_usd"):
        c.number(ebi, k, f"extra_business_info.{k}", k.replace("_", " ").replace("usd", "(USD)").capitalize(), 0)
    avg, mx = parse_float(ebi.get("avg_transaction_usd")), parse_float(ebi.get("max_transaction_usd"))
    if avg is not None and mx is not None and avg > mx:
        e.add("extra_business_info.avg_transaction_usd", "Average transaction can't exceed the maximum transaction.")
    c.optional(ebi, "primary_account_purpose", "extra_business_info.primary_account_purpose", "Primary purpose", 255)
    for g in ebi.get("sender_geographies") or []:
        if g not in L.SENDER_GEOGRAPHIES:
            e.add("extra_business_info.sender_geographies", "Select valid regions.")

    col = d.get("collections_data") if isinstance(d.get("collections_data"), dict) else {}
    currencies = {x["code"] for x in L.transaction_currencies()}
    for k in col.get("sender_industries") or []:
        if k not in L.SENDER_INDUSTRIES:
            e.add("collections_data.sender_industries", "Select valid industries.")
    c.choice(col, "sender_types", "collections_data.sender_types", "Sender types", L.SENDER_TYPES, required=False)
    senders = [x for x in col.get("top_5_senders") or [] if _s(x)]
    if len(senders) > 5:
        e.add("collections_data.top_5_senders", "List at most 5 senders.")
    c.boolean(col, "incoming_from_fintech_wallets", "collections_data.incoming_from_fintech_wallets", "Fintech wallets")
    c.optional(col, "incoming_fintech_wallet_details", "collections_data.incoming_fintech_wallet_details", "Wallet details", 1000)
    for x in col.get("collection_currencies") or []:
        if x not in currencies:
            e.add("collections_data.collection_currencies", "Select valid currencies.")
    c.optional(col, "current_collection_provider", "collections_data.current_collection_provider", "Current provider", 255)
    c.optional(col, "reason_for_switching_collection", "collections_data.reason_for_switching_collection", "Reason", 1000)
    for k in ("expected_monthly_disbursement_usd", "avg_transaction_amount_collection", "max_transaction_amount_collection"):
        c.number(col, k, f"collections_data.{k}", "Amount", 0)

    pay = d.get("payouts_data") if isinstance(d.get("payouts_data"), dict) else {}
    tcountries = {x["code"] for x in L.transaction_countries()}
    c.optional(pay, "payout_primary_purpose", "payouts_data.payout_primary_purpose", "Payout purpose", 255)
    for x in pay.get("beneficiary_geographies") or []:
        if x not in tcountries:
            e.add("payouts_data.beneficiary_geographies", "Select supported countries.")
    for x in pay.get("beneficiary_industries") or []:
        if len(_s(x)) > 100:
            e.add("payouts_data.beneficiary_industries", "Each industry must be at most 100 characters.")
    c.choice(pay, "beneficiary_types", "payouts_data.beneficiary_types", "Beneficiary types", L.BENEFICIARY_TYPES, required=False)
    if len([x for x in pay.get("top_5_beneficiaries") or [] if _s(x)]) > 5:
        e.add("payouts_data.top_5_beneficiaries", "List at most 5 beneficiaries.")
    c.choice(pay, "primary_payout_method", "payouts_data.primary_payout_method", "Payout method", L.PAYOUT_METHODS, required=False)
    for x in pay.get("payout_currencies") or []:
        if x not in currencies:
            e.add("payouts_data.payout_currencies", "Select valid currencies.")
    c.optional(pay, "current_payout_provider", "payouts_data.current_payout_provider", "Current provider", 255)
    c.optional(pay, "reason_for_switching_payout", "payouts_data.reason_for_switching_payout", "Reason", 1000)

    # --- documents
    docs = d.get("documents")
    if not isinstance(docs, list):
        docs = []
    docs = [x for x in docs if isinstance(x, dict) and (x.get("file") or x.get("purpose") == "business_registration")]
    if not any(x.get("purpose") == "business_registration" and x.get("file") for x in docs):
        e.add("documents", "Upload your business registration / certificate of incorporation.")
    for i, doc in enumerate(d.get("documents") or []):
        # Fixed optional slots (tax document, good standing) are skipped when left empty;
        # rows the customer added themselves ("_user") must be completed or removed.
        if not isinstance(doc, dict) or not (doc.get("file") or doc.get("_user")):
            continue
        p = f"documents.{i}"
        c.choice(doc, "purpose", f"{p}.purpose", "Document type", L.DOCUMENT_PURPOSES)
        c.required(doc, "description", f"{p}.description", "Description", 255)
        c.file(doc, "file", f"{p}.file", "File")
    for i, doc in enumerate(d.get("extra_documents") or []):
        if not isinstance(doc, dict) or not (doc.get("file") or doc.get("type")):
            continue
        p = f"extra_documents.{i}"
        c.choice(doc, "type", f"{p}.type", "Document type", L.EXTRA_DOCUMENT_TYPES)
        c.optional(doc, "description", f"{p}.description", "Description", 255)
        c.file(doc, "file", f"{p}.file", "File")

    c.virtual_accounts(d, accounts, is_new)
    if not to_bool(d.get("consent")):
        e.add("consent", "Please confirm the declaration to continue.")
    return e


# ---------------------------------------------------------------------------- dispatch

STEPS: dict[str, dict[str, tuple[str, ...]]] = {"individual": INDIVIDUAL_STEPS, "business": BUSINESS_STEPS}
VALIDATORS: dict[str, Callable[..., Errors]] = {"individual": validate_individual, "business": validate_business}


def step_of(flow: str, path: str) -> str | None:
    head = path.split(".", 1)[0]
    for step, keys in STEPS[flow].items():
        if head in keys:
            return step
    return None


def filter_errors(flow: str, errors: dict, steps: list[str]) -> dict:
    keys = {k for s in steps for k in STEPS[flow].get(s, ())}
    return {p: m for p, m in errors.items() if p.split(".", 1)[0] in keys}


def validate(flow: str, data: dict, accounts: dict[str, bool], is_new: bool = True) -> tuple[dict, Errors]:
    norm = normalize(flow, data)
    return norm, VALIDATORS[flow](norm, accounts, is_new)
