"""Address autocomplete behind one server-side API, so the browser never sees an API key.

Provider priority:
1. Google Places API (New) — when GOOGLE_MAPS_API_KEY is set. Autocomplete + Place Details
   share a session token; the monthly free tier covers typical KYC volumes.
2. OpenStreetMap / Photon — free, no key needed, used when Google is not configured.
3. Bing Maps AutoSuggest — used when USE_BING_AUTOCOMPLETE=true (overrides OSM as the
   fallback). Queries are proxied server-side so credentials never reach the browser.

All providers return the same shape, already mapped onto Yativo's address fields; the state
is matched to a Yativo subdivision code where possible, otherwise the customer picks it.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import lookups as L
from .config import settings

log = logging.getLogger("kyc.address")

GOOGLE_AUTOCOMPLETE = "https://places.googleapis.com/v1/places:autocomplete"
GOOGLE_DETAILS = "https://places.googleapis.com/v1/places/{place_id}"

# Bing Maps AutoSuggest – structured-address mode, postal addresses only.
# Parameters are read from config so the operator can rotate/override them via env vars.
# We request both ptypes but only parse PostalAddress entries; History entries carry a
# full-sentence streetAddress that is not suitable for field-level autofill.
_BING_PTYPES = "favorite,history"
_BING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bing.com/",
}


def provider() -> str:
    if settings.google_maps_api_key:
        return "google"
    if settings.use_bing_autocomplete:
        return "bing"
    return "osm"


def autocomplete(query: str, country: str, session_token: str) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if len(query) < 3:
        return []
    country = (country or "").upper()[:2]
    p = provider()
    if p == "google":
        return _google_autocomplete(query, country, session_token)
    if p == "bing":
        return _bing_autocomplete(query, country)
    return _osm_autocomplete(query, country)


def details(place_id: str, country: str, session_token: str) -> dict[str, Any] | None:
    if provider() == "google":
        return _google_details(place_id, country, session_token)
    return None  # OSM and Bing suggestions already carry the full address inline


# ------------------------------------------------------------------------------ Google

def _google_autocomplete(query: str, country: str, token: str) -> list[dict[str, Any]]:
    body: dict[str, Any] = {"input": query, "sessionToken": token, "languageCode": "en"}
    if country:
        body["includedRegionCodes"] = [country.lower()]
    r = httpx.post(GOOGLE_AUTOCOMPLETE, json=body, timeout=8,
                   headers={"X-Goog-Api-Key": settings.google_maps_api_key})
    r.raise_for_status()
    out = []
    for s in r.json().get("suggestions", []):
        p = s.get("placePrediction")
        if not p:
            continue
        fmt = p.get("structuredFormat") or {}
        out.append({
            "id": p.get("placeId"),
            "main": (fmt.get("mainText") or {}).get("text") or (p.get("text") or {}).get("text", ""),
            "secondary": (fmt.get("secondaryText") or {}).get("text", ""),
        })
    return out[:6]


def _google_details(place_id: str, country: str, token: str) -> dict[str, Any] | None:
    if not place_id or "/" in place_id:
        return None
    r = httpx.get(GOOGLE_DETAILS.format(place_id=place_id), params={"sessionToken": token, "languageCode": "en"},
                  timeout=8, headers={"X-Goog-Api-Key": settings.google_maps_api_key,
                                      "X-Goog-FieldMask": "addressComponents,formattedAddress"})
    r.raise_for_status()
    comps: dict[str, dict[str, str]] = {}
    for c in r.json().get("addressComponents", []):
        for t in c.get("types", []):
            comps.setdefault(t, {"long": c.get("longText", ""), "short": c.get("shortText", "")})

    def g(t: str, short: bool = False) -> str:
        c = comps.get(t)
        return (c["short"] if short else c["long"]) if c else ""

    cc = g("country", True).upper() or country
    number, route = g("street_number"), g("route")
    # House number goes first in most countries; Google keeps the order neutral.
    line1 = line1 or g("premise") or g("establishment") or g("point_of_interest") or g("neighborhood")
    line2 = ", ".join(x for x in (g("subpremise"), g("premise") if line1 != g("premise") else "",
                                  g("sublocality_level_1") or g("sublocality") or g("neighborhood")) if x)
    city = g("locality") or g("postal_town") or g("sublocality_level_1") or g("administrative_area_level_3") \
        or g("administrative_area_level_2")
    a1s, a1l, a2l = g("administrative_area_level_1", True), g("administrative_area_level_1"), g("administrative_area_level_2")
    return {
        "street_line_1": line1,
        "street_line_2": line2 if line2 != line1 else "",
        "city": city,
        "state": L.match_subdivision(cc, a1s, a1l, a2l, city) if cc else None,
        "state_text": a1l or a2l,
        "postal_code": g("postal_code"),
        "country": cc,
    }


# ------------------------------------------------------------------------------ Bing Maps AutoSuggest
def _bing_autocomplete(query: str, country: str) -> list[dict[str, Any]]:
    """Call the Bing Maps AutoSuggest API and return up to 6 structured address results.

    The API returns a mix of _type values ("PostalAddress", "History", etc.).  We only
    surface entries that have a streetAddress so the suggestions are always actionable.
    """
    params: dict[str, Any] = {
        "q": query,
        "appid": settings.bing_autosuggest_appid,
    r = httpx.get(
        settings.bing_autosuggest_url,
        params=params,
        timeout=8,
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    r.raise_for_status()

    out: list[dict[str, Any]] = []
    for item in r.json().get("value", []):
            item.get("postalCode")
            or addr_block.get("postalCode")
            or ""
        )
        cc = (
            item.get("countryIso")
            or addr_block.get("countryIso")
            or ""
        ).upper()

        # Honour the country filter when one is provided
        secondary_parts = [x for x in (city, region_abbr, postal) if x]
        main_text = street
        secondary_text = ", ".join(secondary_parts)

            "id": f"bing:{item.get('id', '')}",
            "main": main_text,
            "secondary": secondary_text,
            "address": {
                "street_line_1": street,
                "street_line_2": "",
                "city": city,
                "state": L.match_subdivision(cc, region_abbr, region_full, city) if cc else None,
                "state_text": region_full or region_abbr,
                "postal_code": postal,
                "country": cc,
            },
        })
        if len(out) >= 6:
            break

    return out


# ------------------------------------------------------------------------------ OpenStreetMap

def _osm_autocomplete(query: str, country: str) -> list[dict[str, Any]]:
    params = {"q": query, "limit": 15, "lang": "en"}
    r = httpx.get(settings.osm_autocomplete_url, params=params, timeout=12,
                  headers={"User-Agent": f"{settings.app_name} KYC address lookup"})
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        cc = str(p.get("countrycode", "")).upper()
        if country and cc != country:
            continue
        street = p.get("street") or (p.get("name") if p.get("type") in {"street", "house"} else "")
        number = p.get("housenumber", "")
        line1 = " ".join(x for x in (number, street) if x) or p.get("name", "")
        city = p.get("city") or p.get("town") or p.get("village") or p.get("district") or p.get("county") or ""
        state_text = p.get("state") or p.get("county") or ""
        main = p.get("name") if p.get("name") and p.get("type") not in {"house"} and not street else line1
        secondary = ", ".join(x for x in (city, state_text, p.get("postcode"), p.get("country")) if x)
        out.append({
            "id": f"osm:{p.get('osm_type', '')}{p.get('osm_id', '')}",
            "main": main or secondary,
            "secondary": secondary,
            "address": {
                "street_line_1": line1 if street else (p.get("name") or ""),
                "street_line_2": p.get("district") if p.get("district") and p.get("district") != city else "",
                "city": city,
                "state": L.match_subdivision(cc, state_text, p.get("county")) if cc else None,
                "state_text": state_text,
                "postal_code": p.get("postcode", ""),
                "country": cc,
            },
        })
        if len(out) >= 6:
            break
    return out
