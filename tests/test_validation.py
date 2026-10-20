"""Validator tests. Lookups are stubbed so the suite runs offline."""
import copy
import os
from datetime import date

import pytest

os.environ.setdefault("SECRET_KEY", "x" * 40)

from app import lookups as L  # noqa: E402
from app import validation as V  # noqa: E402
from app import yativo_client as Y  # noqa: E402

TODAY = date(2026, 10, 14)
F = "file:" + "1" * 8 + "-1111-1111-1111-" + "1" * 12
ACCOUNTS = {"usd": True, "eur": True, "gbp": True, "eurde": False}


@pytest.fixture(autouse=True)
def stub_lookups(monkeypatch):
    monkeypatch.setattr(L, "country_codes", lambda: {"US", "NG", "GB", "AE"})
    monkeypatch.setattr(L, "country_name", lambda c: c)
    subs = {"US": [{"code": "CA", "name": "California"}, {"code": "PA", "name": "Pennsylvania"}],
            "NG": [{"code": "LA", "name": "Lagos"}], "GB": [{"code": "LND", "name": "London"}], "AE": []}
    monkeypatch.setattr(L, "subdivisions", lambda c: subs.get(c, []))
    postal = {"US": r"^\d{5}(?:-\d{4})?$", "NG": r"\A\d{6}\Z", "GB": r"^[A-Z0-9 ]{5,8}$"}
    monkeypatch.setattr(L, "postal_format", lambda c: {"uses_postal_codes": c != "AE", "regex": postal.get(c, L.GENERIC_POSTAL),
                                                       "js_regex": "", "samples": [], "country": c})
    ids = [{"type": t} for t in ("passport", "national_id", "drivers_license", "bvn", "nin", "other", "tax_id")]
    monkeypatch.setattr(L, "identification_types", lambda c: ids if c != "US" else [{"type": "ssn"}, {"type": "itin"}])
    monkeypatch.setattr(L, "occupation_codes", lambda: {"151252"})
    monkeypatch.setattr(L, "business_industries", lambda: [{"code": "423620", "label": "x"}])
    monkeypatch.setattr(L, "expected_monthly_payments", lambda: {"LessThan5K": "a"})
    monkeypatch.setattr(L, "individual_source_of_funds", lambda: {"Salary": "Salary"})
    monkeypatch.setattr(L, "individual_account_purposes", lambda: {"ReceiveSalary": "x", "Other": "Other"})
    monkeypatch.setattr(L, "business_account_purposes", lambda: {"purchase_goods_and_services": "x", "other": "Other"})
    monkeypatch.setattr(L, "business_source_of_funds", lambda: {"SalesOfGoodsAndServices": "x"})
    monkeypatch.setattr(L, "transaction_currencies", lambda: [{"code": "USD"}, {"code": "EUR"}])
    monkeypatch.setattr(L, "transaction_countries", lambda: [{"code": "US"}, {"code": "GB"}])


INDIVIDUAL = {
    "first_name": "Alex", "last_name": "Smith", "email": "alex.smith@example.com", "phone": "5551234567",
    "calling_code": "+1", "gender": "male", "nationality": "US", "birth_date": "1990-01-01", "taxId": "998877665",
    "selfie_image": F,
    "residential_address": {"street_line_1": "123 Main Street", "city": "Anytown", "state": "CA", "postal_code": "90210",
                            "country": "US", "proof_of_address_file": F},
    "identifying_information": [{"type": "passport", "issuing_country": "US", "number": "P00012345",
                                 "date_issued": "2020-01-01", "expiration_date": "2030-01-01", "image_front_file": F}],
    "employment_status": "Employed", "most_recent_occupation_code": "151252", "expected_monthly_payments_usd": "LessThan5K",
    "source_of_funds": "Salary", "account_purpose": "ReceiveSalary", "usd_virtual_account": True, "consent": True,
    "uploaded_documents": [{"type": "proof_of_bank_account_ownership",
                            "file": "file:" + "a" * 8 + "-1111-1111-1111-" + "a" * 12}],
}


def test_supporting_documents_required_once_each():
    d = copy.deepcopy(INDIVIDUAL)
    d["uploaded_documents"] = d["uploaded_documents"][:2]
    assert "uploaded_documents.2.file" in run_ind(d)
    d = copy.deepcopy(INDIVIDUAL)
    d["uploaded_documents"].append({"type": "proof_of_funds", "file": F})
    assert "uploaded_documents" in run_ind(d)


def test_phone_with_own_country_code_wins():
    norm = V.normalize("individual", {**INDIVIDUAL, "calling_code": "+590", "phone": "+1 (874) 929-8027"})
    assert norm["calling_code"] == "+1" and norm["phone"] == "8749298027"


def run_ind(d):
    norm = V.normalize("individual", copy.deepcopy(d))
    return V.validate_individual(norm, ACCOUNTS, today=TODAY)


def test_valid_individual_passes():
    assert run_ind(INDIVIDUAL) == {}


@pytest.mark.parametrize("name", ["Tester", "YativoAdmin", "ADMIN", "contest"])
def test_banned_names(name):
    errs = run_ind({**INDIVIDUAL, "first_name": name})
    assert "first_name" in errs


def test_banned_email_word():
    assert "email" in run_ind({**INDIVIDUAL, "email": "mytest@example.com"})


def test_underage_and_expired_document():
    d = copy.deepcopy(INDIVIDUAL)
    d["birth_date"] = "2010-01-01"
    d["identifying_information"][0]["expiration_date"] = "2026-01-01"
    errs = run_ind(d)
    assert "birth_date" in errs and "identifying_information.0.expiration_date" in errs


def test_bvn_nin_only_for_nigerian_nationals():
    d = copy.deepcopy(INDIVIDUAL)
    d["residential_address"].update(country="NG", state="LA", postal_code="100001")
    errs = run_ind(d)  # US national living in Nigeria: Yativo still requires both
    assert "bvn" in errs and "nin" in errs
    d["residential_address"].update(country="US", state="CA", postal_code="90210")
    assert "bvn" not in run_ind(d)
    d["nationality"] = "NG"
    errs = run_ind(d)
    assert "bvn" in errs and "nin" in errs
    d.update(bvn="22222222222", nin="70123456789", immigration_status="")
    assert "bvn" not in run_ind(d)


def test_postal_and_state_checked():
    d = copy.deepcopy(INDIVIDUAL)
    d["residential_address"].update(state="ZZ", postal_code="ABC")
    errs = run_ind(d)
    assert "residential_address.state" in errs and "residential_address.postal_code" in errs


def test_national_id_needs_back_image():
    d = copy.deepcopy(INDIVIDUAL)
    d["identifying_information"][0]["type"] = "national_id"
    assert "identifying_information.0.image_back_file" in run_ind(d)


def test_phone_trunk_prefix_removed():
    norm = V.normalize("individual", {**INDIVIDUAL, "calling_code": "234", "phone": "0803 123 4567"})
    assert norm["calling_code"] == "+234" and norm["phone"] == "8031234567"


def test_eurde_rules():
    errs = V.validate_individual(V.normalize("individual", {**INDIVIDUAL, "eurde_virtual_account": True}),
                                 {**ACCOUNTS, "eurde": True}, today=TODAY)
    assert "eurde_virtual_account" in errs  # combined with USD, and closed to new customers


PERSON = {
    "first_name": "John", "last_name": "Doe", "birth_date": "1980-01-01", "nationality": "US",
    "email": "john.doe@northwind.com", "ownership_percentage": 60, "has_ownership": True, "is_signer": True,
    "residential_address": {"street_line_1": "456 Pine", "city": "Philadelphia", "state": "PA", "country": "US", "postal_code": "19106"},
    "identifying_information": [
        {"type": "passport", "issuing_country": "US", "number": "P1", "expiration": "2031-02-28", "image_front": F},
        {"type": "tax_id", "number": "", "issuing_country": "US"},
    ],
}
BUSINESS = {
    "business_legal_name": "Northwind Trading Limited", "business_trade_name": "Northwind",
    "business_description": "N" * 150, "email": "compliance@northwind.com", "business_type": "corporation",
    "registration_number": "5512345", "incorporation_date": "2020-01-15", "incorporation_country": "US", "tax_id": "12-3456789",
    "phone_calling_code": "+1", "phone_number": "2025550143", "business_industry": "423620",
    "registered_address": {"street_line_1": "1200 Market", "city": "Philadelphia", "state": "PA", "country": "US", "postal_code": "19107"},
    "physical_address": {"street_line_1": "1200 Market", "city": "Philadelphia", "state": "PA", "country": "US",
                         "postal_code": "19107", "proof_of_address_file": F},
    "associated_persons": [PERSON],
    "account_purpose": "purchase_goods_and_services", "source_of_funds": "SalesOfGoodsAndServices",
    "high_risk_activities": ["none_of_the_above"], "conducts_money_services": False, "expected_monthly_payments_usd": "50000",
    "operates_in_prohibited_countries": "no", "pep_status": False, "third_party_msb_payments": False,
    "documents": [{"purpose": "business_registration", "description": "Cert", "file": F},
                  {"purpose": "tax_documents", "description": "Tax doc"}],
    "consent": True,
}


def run_biz(d):
    return V.validate_business(V.normalize("business", copy.deepcopy(d)), ACCOUNTS, today=TODAY)


def test_valid_business_passes():
    assert run_biz(BUSINESS) == {}


def test_business_rules():
    d = copy.deepcopy(BUSINESS)
    d["business_description"] = "too short"
    d["documents"] = [{"purpose": "tax_documents", "description": "x", "file": F}]
    d["high_risk_activities"] = ["gambling", "none_of_the_above"]
    p2 = copy.deepcopy(PERSON)
    p2["ownership_percentage"] = 50
    d["associated_persons"] = [PERSON, p2]
    errs = run_biz(d)
    for k in ("business_description", "documents", "high_risk_activities", "high_risk_activities_explanation", "associated_persons"):
        assert k in errs, k


def test_person_needs_photo_id():
    d = copy.deepcopy(BUSINESS)
    d["associated_persons"][0]["identifying_information"] = [{"type": "tax_id", "number": "123"}]
    assert "associated_persons.0.identifying_information" in run_biz(d)


def test_non_owner_gets_zero_percent():
    d = copy.deepcopy(BUSINESS)
    p = copy.deepcopy(PERSON)
    p.pop("ownership_percentage")
    p["has_ownership"] = False
    d["associated_persons"] = [p]
    norm = V.normalize("business", d)
    assert norm["associated_persons"][0]["ownership_percentage"] == 0


def test_outbound_multipart_shape():
    norm = V.normalize("business", copy.deepcopy(BUSINESS))
    out = Y.build_outbound("business", norm, "cust-1")
    assert "consent" not in out and out["customer_id"] == "cust-1"
    assert len(out["associated_persons"][0]["identifying_information"]) == 1  # empty tax id entry dropped
    assert [d["purpose"] for d in out["documents"]] == ["business_registration"]
    fields, files = Y.to_multipart(out, lambda ref: ("f.jpg", b"x", "image/jpeg"))
    fd = dict(fields)
    assert fd["registered_address[state]"] == "PA"
    assert fd["associated_persons[0][has_ownership]"] == "1"
    assert fd["associated_persons[0][has_control]"] == "0"
    assert fd["high_risk_activities[0]"] == "none_of_the_above"
    assert fd["pep_status"] == "0"
    names = [f[0] for f in files]
    assert "documents[0][file]" in names and "associated_persons[0][identifying_information][0][image_front]" in names
