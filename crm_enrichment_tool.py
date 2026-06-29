"""
CSV Genie - verified CRM enrichment engine.

This module is designed for CRM import CSV files where the original data must
be preserved unless a new value can be proposed with enough confidence.

Priority fields:
1. Website
2. Phone
3. Email

Key safety rules:
- Existing CRM values are never overwritten.
- Row limits only limit how many rows are researched; the output keeps every row.
- Uncertain values are written to proposal/audit columns, not into import fields.
- Apollo is optional and disabled unless explicitly enabled and an API key exists.
"""

from __future__ import annotations

import argparse
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"
APOLLO_API_KEY_ENV = "APOLLO_API_KEY"
BRAVE_SEARCH_API_KEY_ENV = "BRAVE_SEARCH_API_KEY"
SERPAPI_API_KEY_ENV = "SERPAPI_API_KEY"
DUCKDUCKGO_HTML_URL = "https://duckduckgo.com/html/?q={query}"
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"
DEFAULT_SEARCH_LOCATION = "Galway, County Galway, Ireland"

DEFAULT_TIMEOUT = 10
DIRECT_DOMAIN_TIMEOUT = 4
MAX_DDG_QUERIES_PER_ROW = 3
MAX_DIRECT_DOMAIN_GUESSES = 12
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

DIRECTORY_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "yelp.com",
    "goldenpages.ie",
    "find-open.ie",
    "irelandlookup.com",
    "whatclinic.com",
    "ratemds.com",
    "doctify.com",
    "dentalby.com",
    "healthmail.ie",
    "solocheck.ie",
    "vision-net.ie",
    "rip.ie",
    "mapcarta.com",
    "cylex.ie",
    "locallife.ie",
    # Booking, social, directory and map/listing pages should stay candidate-only.
    "fresha.com",
    "booksy.com",
    "treatwell.ie",
    "treatwell.co.uk",
    "setmore.com",
    "simplybook.it",
    "appointy.com",
    "acuityscheduling.com",
    "square.site",
    "wixsite.com",
    "weebly.com",
    "linktr.ee",
    "maps.google.com",
    "google.com",
    "google.ie",
    "bing.com",
    "apple.com",
    "infobel.com",
    "local.infobel.ie",
    "kompass.com",
    "tuugo.info",
    "bizireland.com",
    "cybo.com",
    "odycy.com",
    "phonebook.ie",
    "reviewbritain.com",
    "iscp.ie",
    "page.tl",
    "hotfrog.ie",
    "yelu.ie",
    "ie.near-place.com",
    "ireland724.info",
    # Additional directories to block (Irish local business listings, archives, free hosts)
    "alltrack.org",
    "dir.alltrack.org",
    "infosinfo-ie.com",
    "mummypages.ie",
    "archive.org",
    "archive.today",
    "foot.ie",
    "boards.ie",
    "reddit.com",
    "voicefleet.ai",
}

SUSPICIOUS_PHONE_REASONS = {
    "invalid_length": "Rejected phone candidate: invalid Irish phone length",
    "repeated_digits": "Rejected phone candidate: suspicious repeated digits",
    "sequential_digits": "Rejected phone candidate: suspicious sequential digits",
    "invalid_prefix": "Rejected phone candidate: unexpected Irish phone prefix",
}

CONTACT_PATHS = [
    "contact",
    "contact-us",
    "contact-locations",
    "locations",
    "about",
]

BASE_AUDIT_COLUMNS = [
    "Proposed Website",
    "Website Source URL",
    "Website Confidence",
    "Proposed Phone",
    "Phone Source URL",
    "Phone Confidence",
    "Proposed Email",
    "Email Source URL",
    "Email Confidence",
    "Best Candidate Website",
    "Best Candidate Confidence",
    "Best Candidate Rejected Reason",
    "Decision Needed",
    "Enrichment Notes",
]

CANDIDATE_FIELDS = ["Website", "Phone", "Email"]
MAX_CANDIDATES_PER_FIELD = 3

CANDIDATE_AUDIT_COLUMNS = [
    f"Candidate {field_name} {candidate_no} {suffix}"
    for field_name in CANDIDATE_FIELDS
    for candidate_no in range(1, MAX_CANDIDATES_PER_FIELD + 1)
    for suffix in ["Value", "Source URL", "Confidence", "Rejected Reason"]
]

AUDIT_COLUMNS = BASE_AUDIT_COLUMNS + CANDIDATE_AUDIT_COLUMNS


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    provider_note: str = ""


@dataclass
class CandidateMatch:
    field_name: str
    value: str
    source_url: str
    confidence: float
    reason: str


@dataclass
class FieldProposal:
    value: Optional[str] = None
    source_url: Optional[str] = None
    confidence: float = 0.0
    notes: List[str] = field(default_factory=list)

    def update_if_better(
        self,
        value: Optional[str],
        source_url: Optional[str],
        confidence: float,
        note: Optional[str] = None,
    ) -> None:
        if not value:
            return
        if confidence > self.confidence:
            self.value = clean_cell(value)
            self.source_url = source_url
            self.confidence = round(float(confidence), 2)
            self.notes = [note] if note else []
        elif note:
            self.notes.append(note)


@dataclass
class RowProposal:
    website: FieldProposal = field(default_factory=FieldProposal)
    phone: FieldProposal = field(default_factory=FieldProposal)
    email: FieldProposal = field(default_factory=FieldProposal)
    notes: List[str] = field(default_factory=list)
    candidates: List[CandidateMatch] = field(default_factory=list)

    def add_candidate(
        self,
        field_name: str,
        value: Optional[str],
        source_url: Optional[str],
        confidence: float,
        reason: str,
    ) -> None:
        clean_value = clean_cell(value)
        clean_source = clean_cell(source_url) or clean_value
        if not clean_value:
            return
        confidence = round(float(confidence), 2)
        key_field = field_name.lower().strip()
        key_value = clean_value.lower().strip()
        for existing in self.candidates:
            if existing.field_name.lower() == key_field and existing.value.lower().strip() == key_value:
                if confidence > existing.confidence:
                    existing.confidence = confidence
                    existing.source_url = clean_source or existing.source_url
                    existing.reason = reason
                return
        self.candidates.append(
            CandidateMatch(
                field_name=field_name,
                value=clean_value,
                source_url=clean_source or "",
                confidence=confidence,
                reason=reason,
            )
        )

    def top_candidates(self, field_name: str, limit: int = MAX_CANDIDATES_PER_FIELD) -> List[CandidateMatch]:
        key = field_name.lower().strip()
        matches = [candidate for candidate in self.candidates if candidate.field_name.lower() == key]
        return sorted(matches, key=lambda candidate: candidate.confidence, reverse=True)[:limit]


def clean_cell(value: Any) -> Optional[str]:
    if value is None:
        return None
    if pd.isna(value):
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def has_value(value: Any) -> bool:
    return clean_cell(value) is not None


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def company_tokens(company_name: str) -> List[str]:
    stop_words = {
        "the",
        "and",
        "clinic",
        "clinics",
        "physio",
        "physiotherapy",
        "dental",
        "dentist",
        "dentists",
        "accountant",
        "accountants",
        "galway",
        "ireland",
        "city",
        "ltd",
        "limited",
        "company",
        "practice",
        "health",
        "injury",
        "sports",
        "therapy",
    }
    tokens = normalize_text(company_name).split()
    return [t for t in tokens if len(t) >= 3 and t not in stop_words]


def business_context(company_name: str, category: str = "") -> str:
    return normalize_text(f"{company_name} {category}")


def is_dental_business(company_name: str, category: str = "") -> bool:
    context = business_context(company_name, category)
    return any(term in context.split() for term in ["dental", "dentist", "dentists", "dentistry", "orthodontic"])


def is_physio_business(company_name: str, category: str = "") -> bool:
    context = business_context(company_name, category)
    if is_dental_business(company_name, category):
        return False
    return any(term in context.split() for term in ["physio", "physiotherapy", "therapy"])


def location_terms(area: str) -> List[str]:
    """Return a short list of useful local search terms from a CRM area value."""
    area_text = clean_cell(area) or "Galway Ireland"
    normalized = normalize_text(area_text)
    known_places = [
        "galway",
        "oranmore",
        "ballinasloe",
        "tuam",
        "loughrea",
        "knocknacarra",
        "salthill",
        "woodquay",
        "barna",
        "claregalway",
        "athenry",
        "clifden",
        "oughterard",
        "headford",
    ]
    terms = [place for place in known_places if place in normalized]
    if "galway" not in terms and ("county galway" in normalized or "galway" in normalized):
        terms.append("galway")
    if not terms:
        terms = [part for part in normalized.split() if len(part) >= 4 and part not in {"county", "ireland", "city"}][:2]
    if "galway" not in terms:
        terms.append("galway")
    return terms[:3]


def get_domain(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def decode_duckduckgo_url(url: str) -> str:
    if "duckduckgo.com/l/" not in url:
        return url
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    uddg = qs.get("uddg", [None])[0]
    return urllib.parse.unquote(uddg) if uddg else url


def is_directory_domain(domain: str) -> bool:
    domain = domain.lower().removeprefix("www.")
    return any(domain == bad or domain.endswith("." + bad) for bad in DIRECTORY_DOMAINS)


def same_domain(url_a: Optional[str], url_b: Optional[str]) -> bool:
    """Return True when two URLs resolve to the same root domain."""
    if not url_a or not url_b:
        return False
    return get_domain(url_a) == get_domain(url_b)


def is_official_phone_source(source_url: Optional[str], official_website: Optional[str]) -> bool:
    """Phone values are verified only from the proposed/existing official website."""
    if not source_url or not official_website:
        return False
    source_domain = get_domain(source_url)
    official_domain = get_domain(official_website)
    if not source_domain or not official_domain:
        return False
    if is_directory_domain(source_domain):
        return False
    return source_domain == official_domain


def is_probably_official_result(result: SearchResult, company_name: str, area: str) -> Tuple[bool, float, str]:
    domain = get_domain(result.url)
    if not domain or is_directory_domain(domain):
        return False, 0.0, "directory/social/domain skipped"

    haystack = normalize_text(" ".join([result.title, result.snippet, domain, result.url]))
    tokens = company_tokens(company_name)
    token_hits = sum(1 for token in tokens if token in haystack)
    token_ratio = token_hits / max(len(tokens), 1)

    area_text = normalize_text(area or "Galway Ireland")
    area_hit = any(part in haystack for part in area_text.split() if len(part) >= 4)

    confidence = 0.45 + (0.35 * token_ratio) + (0.10 if area_hit else 0.0)

    # Extra signal: domain contains a distinctive company token.
    if any(token in normalize_text(domain) for token in tokens):
        confidence += 0.10

    confidence = min(confidence, 0.95)
    return confidence >= 0.70, confidence, f"{token_hits}/{len(tokens)} distinctive company tokens matched"


def score_candidate_search_result(result: SearchResult, company_name: str, area: str) -> Tuple[float, str]:
    """Score a lower-confidence search result for manual review.

    This is intentionally less strict than verified proposals. It lets the app
    show possible matches to the user without writing them into CRM fields.
    """
    domain = get_domain(result.url)
    if not domain or not result.url:
        return 0.0, "No usable URL"

    haystack = normalize_text(" ".join([result.title, result.snippet, domain, result.url]))
    tokens = company_tokens(company_name)
    token_hits = sum(1 for token in tokens if token in haystack)
    token_ratio = token_hits / max(len(tokens), 1)
    area_text = normalize_text(area or "Galway Ireland")
    area_hit = any(part in haystack for part in area_text.split() if len(part) >= 4) or "galway" in haystack
    domain_hit = any(token in normalize_text(domain) for token in tokens)
    directory = is_directory_domain(domain)

    confidence = 0.30 + (0.35 * token_ratio) + (0.10 if area_hit else 0.0) + (0.10 if domain_hit else 0.0)
    if directory:
        confidence = min(confidence, 0.55)
        label = "Directory/social search result - manual checking only"
    else:
        confidence = min(confidence, 0.85)
        label = "Possible official website candidate"

    reason = f"{label}; {token_hits}/{len(tokens)} distinctive tokens matched"
    if area_hit:
        reason += "; location signal found"
    return round(confidence, 2), reason


def ddg_search(query: str, max_results: int = 8) -> List[SearchResult]:
    url = DUCKDUCKGO_HTML_URL.format(query=urllib.parse.quote(query))
    headers = {"User-Agent": USER_AGENT}
    try:
        response = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
        if response.status_code in {403, 429}:
            return [
                SearchResult(
                    title="SEARCH_ERROR",
                    url="",
                    snippet=f"DuckDuckGo blocked / {response.status_code} rate-limit response",
                )
            ]
        response.raise_for_status()
    except Exception as exc:
        return [SearchResult(title="SEARCH_ERROR", url="", snippet=f"DuckDuckGo error: {exc}")]

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = normalize_text(soup.get_text(" ", strip=True)[:3000])
    if any(marker in page_text for marker in ["unusual traffic", "captcha", "anomaly detected"]):
        return [SearchResult(title="SEARCH_ERROR", url="", snippet="DuckDuckGo blocked / challenge page")]
    results: List[SearchResult] = []
    for result in soup.select("div.result")[:max_results]:
        link = result.select_one("a.result__a")
        snippet = result.select_one("a.result__snippet") or result.select_one("div.result__snippet")
        if not link:
            continue
        href = decode_duckduckgo_url(link.get("href", ""))
        title = link.get_text(" ", strip=True)
        snippet_text = snippet.get_text(" ", strip=True) if snippet else ""
        if href:
            results.append(SearchResult(title=title, url=href, snippet=snippet_text))
    return results


def is_search_error(results: List[SearchResult]) -> bool:
    return len(results) == 1 and results[0].title == "SEARCH_ERROR"


def is_duckduckgo_blocked(message: str) -> bool:
    text = message.lower()
    return "duckduckgo blocked" in text or "403" in text or "429" in text or "rate-limit" in text


def brave_search(
    query: str,
    max_results: int = 5,
    api_key: Optional[str] = None,
) -> List[SearchResult]:
    """Search Brave Web Search API for candidate URLs only."""
    key = api_key or os.getenv(BRAVE_SEARCH_API_KEY_ENV)
    if not key:
        return [SearchResult(title="SEARCH_ERROR", url="", snippet="BRAVE_SEARCH_API_KEY missing")]

    headers = {
        "x-subscription-token": key,
        "accept": "application/json",
        "x-loc-city": "Galway",
        "x-loc-state-name": "County Galway",
        "x-loc-country": "IE",
        "x-loc-timezone": "Europe/Dublin",
    }
    params = {
        "q": query,
        "country": "IE",
        "search_lang": "en",
        "ui_lang": "en-IE",
        "count": min(max(int(max_results), 1), 5),
        "safesearch": "moderate",
        "result_filter": "web,locations",
    }
    provider_note = "Provider used = Brave Search API"
    try:
        response = requests.get(BRAVE_SEARCH_URL, headers=headers, params=params, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 422:
            retry_params = dict(params)
            retry_params.update({"country": "GB", "ui_lang": "en-GB"})
            response = requests.get(BRAVE_SEARCH_URL, headers=headers, params=retry_params, timeout=DEFAULT_TIMEOUT)
            provider_note = "Provider used = Brave Search API; retried with supported GB query localization"
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        error_detail = ""
        if "response" in locals():
            error_detail = f" {response.text[:240]}"
        return [SearchResult(title="SEARCH_ERROR", url="", snippet=f"Brave Search API error: {exc}{error_detail}")]

    results: List[SearchResult] = []
    for item in (data.get("web") or {}).get("results") or []:
        url = item.get("url") or ""
        title = item.get("title") or ""
        snippet = item.get("description") or item.get("snippet") or ""
        if url:
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=str(snippet or ""),
                    provider_note=provider_note,
                )
            )

    locations = (data.get("locations") or {}).get("results") or []
    for item in locations[: max(0, max_results - len(results))]:
        url = item.get("url") or item.get("website") or ""
        title = item.get("title") or item.get("name") or ""
        snippet_parts = [
            str(item.get(key_name, ""))
            for key_name in ["description", "address", "postal_code", "phone"]
            if item.get(key_name)
        ]
        if url:
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=" ".join(snippet_parts),
                    provider_note=provider_note,
                )
            )

    return results[:max_results]


def serpapi_search(
    query: str,
    max_results: int = 8,
    api_key: Optional[str] = None,
    search_location: str = DEFAULT_SEARCH_LOCATION,
) -> List[SearchResult]:
    """Search Google through SerpAPI when a key is available.

    SerpAPI is better than scraped DuckDuckGo for exact local-business queries,
    e.g. cases where Google finds the official site but DDG returns directories.
    """
    key = api_key or os.getenv(SERPAPI_API_KEY_ENV)
    if not key:
        return [SearchResult(title="SEARCH_ERROR", url="", snippet="SERPAPI_API_KEY missing")]
    params = {
        "engine": "google",
        "q": query,
        "google_domain": "google.ie",
        "gl": "ie",
        "hl": "en",
        "location": search_location or DEFAULT_SEARCH_LOCATION,
        "num": max_results,
        "api_key": key,
    }
    try:
        response = requests.get(SERPAPI_SEARCH_URL, params=params, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return [SearchResult(title="SEARCH_ERROR", url="", snippet=str(exc))]

    results: List[SearchResult] = []
    for item in (data.get("organic_results") or [])[:max_results]:
        url = item.get("link") or ""
        title = item.get("title") or ""
        snippet = item.get("snippet") or item.get("rich_snippet", {}).get("top", {}).get("detected_extensions", "")
        if isinstance(snippet, dict):
            snippet = " ".join(str(v) for v in snippet.values())
        if url:
            results.append(SearchResult(title=title, url=url, snippet=str(snippet or "")))
    # Include local place result if present because it often contains the official website/phone.
    place = data.get("local_results", {}) or {}
    places = place.get("places") or []
    for item in places[: max(0, max_results - len(results))]:
        url = item.get("website") or item.get("link") or ""
        title = item.get("title") or ""
        snippet = " ".join(str(item.get(k, "")) for k in ["address", "phone", "type"] if item.get(k))
        if url:
            results.append(SearchResult(title=title, url=url, snippet=snippet))
    return results


def search_web(
    query: str,
    max_results: int = 8,
    *,
    provider: str = "duckduckgo",
    brave_api_key: Optional[str] = None,
    serpapi_api_key: Optional[str] = None,
    search_location: str = DEFAULT_SEARCH_LOCATION,
) -> List[SearchResult]:
    provider = (provider or "duckduckgo").lower().strip()
    if provider in {"brave", "brave_search", "brave_search_api"}:
        results = brave_search(query, max_results=min(max_results, 5), api_key=brave_api_key)
        if is_search_error(results):
            fallback = ddg_search(query, max_results=max_results)
            fallback_note = f"Brave Search API unavailable ({results[0].snippet[:80]}). DuckDuckGo fallback used."
            if fallback:
                fallback[0].provider_note = fallback_note
                fallback[0].snippet = f"{fallback_note} {fallback[0].snippet}"
            else:
                return [SearchResult(title="SEARCH_ERROR", url="", snippet=fallback_note, provider_note=fallback_note)]
            return fallback
        return results
    if provider in {"serpapi", "google", "google_serpapi"}:
        results = serpapi_search(query, max_results=max_results, api_key=serpapi_api_key, search_location=search_location)
        # If the paid provider is unavailable, fall back to the free provider instead of failing the row.
        if len(results) == 1 and results[0].title == "SEARCH_ERROR":
            fallback = ddg_search(query, max_results=max_results)
            if fallback:
                fallback[0].snippet = f"SerpAPI unavailable ({results[0].snippet[:80]}). Fallback used. " + fallback[0].snippet
            return fallback
        return results
    return ddg_search(query, max_results=max_results)


def inferred_names_from_directory_result(result: SearchResult, company_name: str, area: str) -> List[str]:
    """Infer possible trading names from directory URLs/titles for rescue searches.

    Example: a directory result for Ballinasloe Physiotherapy may have a slug
    like /action-physio-ballinasloe. This extracts "action physio" and lets
    the tool check actionphysio.ie instead of accepting the directory page.
    """
    domain = get_domain(result.url)
    if not is_directory_domain(domain):
        return []

    parsed = urllib.parse.urlparse(result.url)
    candidates: List[str] = []
    parts_to_check = [parsed.path.replace("/", " "), result.title]
    area_tokens = set(normalize_text(area or "").split()) | {"galway", "ireland", "ie", "biz", "business", "clinic", "clinics", "contact"}
    generic = set(company_tokens(company_name)) | {"physiotherapy", "physio", "therapy", "injury", "sports"}

    for raw in parts_to_check:
        words = [w for w in normalize_text(raw).split() if len(w) >= 3 and w not in area_tokens]
        # Keep meaningful chunks around physio/therapy names, but remove directory noise.
        cleaned = [w for w in words if w not in {"cybo", "odycy", "providers", "provider", "page", "reviews"}]
        if not cleaned:
            continue
        # Prefer two/three-word names that include a physiotherapy-related token or differ from the original row.
        for i in range(0, max(1, len(cleaned) - 1)):
            phrase_words = cleaned[i : i + 3]
            if len(phrase_words) < 2:
                continue
            phrase = " ".join(phrase_words)
            phrase_norm = normalize_text(phrase)
            if phrase_norm == normalize_text(company_name):
                continue
            if any(w in {"physio", "physiotherapy", "therapy"} for w in phrase_words) or not set(phrase_words).issubset(generic):
                candidates.append(phrase)

    # Clean and dedupe; keep short trading-name variants only.
    deduped: List[str] = []
    for candidate in candidates:
        words = [w for w in normalize_text(candidate).split() if w not in area_tokens]
        if len(words) < 2 or len(words) > 4:
            continue
        candidate = " ".join(words)
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    return deduped[:2]


def fetch_page(url: str, *, timeout: Optional[float] = None) -> Tuple[Optional[str], Optional[str]]:
    headers = {"User-Agent": USER_AGENT}
    request_timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    try:
        response = requests.get(url, headers=headers, timeout=request_timeout, allow_redirects=True)
        if not response.ok or "text/html" not in response.headers.get("Content-Type", ""):
            return None, None
        return response.text[:250_000], response.url
    except Exception:
        return None, None


def candidate_contact_urls(base_url: str) -> List[str]:
    parsed = urllib.parse.urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return [base_url]
    root = f"{parsed.scheme}://{parsed.netloc}"
    urls = [base_url, root]
    for path in CONTACT_PATHS:
        urls.append(f"{root}/{path}")
    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def root_url(url: str) -> str:
    """Return scheme + domain for a URL."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}"


def domain_guess_slugs(company_name: str, category: str = "") -> List[str]:
    """Generate conservative domain slugs from a company name.

    This is used only as a fallback for candidate review. It helps with local
    businesses that do not appear reliably in DuckDuckGo HTML results.
    """
    words = normalize_text(company_name).split()
    remove = {
        "the",
        "and",
        "of",
        "ltd",
        "limited",
        "company",
        "services",
        "service",
    }
    words = [word for word in words if word not in remove]
    if not words:
        return []

    full = "".join(words)
    hyphenated = "-".join(words)
    base_variants = [full, hyphenated]
    replacement_variants: List[str] = []

    # Drop generic business suffixes while preserving distinctive brand/place
    # terms such as RDent, Galway Dentists, Knocknacarra and Dental Care Ireland.
    generic_business_words = {"clinic", "clinics", "practice", "centre", "center"}
    without_generic = [word for word in words if word not in generic_business_words]
    if without_generic and without_generic != words:
        base_variants.extend(["".join(without_generic), "-".join(without_generic)])
    without_galway_suffix = [word for word in without_generic if word not in {"galway", "city"}]
    too_generic_location_drop = set(without_galway_suffix).issubset(
        {"dental", "dentist", "dentists", "dentistry", "clinic", "clinics", "physio", "physiotherapy"}
    )
    if len(without_galway_suffix) >= 2 and without_galway_suffix != without_generic and not too_generic_location_drop:
        base_variants.extend(["".join(without_galway_suffix), "-".join(without_galway_suffix)])

    dental = is_dental_business(company_name, category)
    physio = is_physio_business(company_name, category)

    # Common Irish SME naming patterns.
    replacements: List[Tuple[str, str]] = []
    if physio:
        replacements.extend(
            [
                ("physiotherapy", "physio"),
                ("physio", "physiotherapy"),
                ("injuryclinic", "injury"),
                ("sportsinjuryclinic", "sportsinjury"),
            ]
        )
    if dental:
        replacements.extend(
            [
                ("dentists", "dental"),
                ("dental", "dentist"),
                ("dentist", "dentistry"),
            ]
        )
    for old, new in replacements:
        if old in full:
            replacement_variants.append(full.replace(old, new))
        if old in hyphenated:
            replacement_variants.append(hyphenated.replace(old, new))

    # Try removing generic trailing words but keep the full version first.
    trailing_generics = ["clinic", "practice"]
    if physio:
        trailing_generics.append("therapy")
    for generic in trailing_generics:
        if full.endswith(generic) and len(full) > len(generic) + 4:
            replacement_variants.append(full[: -len(generic)])

    # First distinctive word + category catches short local trading names
    # without mixing healthcare categories.
    first = words[0]
    if len(first) >= 4:
        if physio:
            replacement_variants.extend([f"{first}physio", f"{first}physiotherapy", f"{first}-physio"])
        if dental:
            replacement_variants.extend([f"{first}dental", f"{first}dentist", f"{first}-dental"])

    deduped: List[str] = []
    for variant in base_variants + replacement_variants:
        variant = re.sub(r"[^a-z0-9-]", "", variant).strip("-")
        if len(variant.replace("-", "")) >= 5 and variant not in deduped:
            deduped.append(variant)
    return deduped[:6]


def guessed_website_urls(company_name: str, category: str = "") -> List[str]:
    """Return a short list of likely official domains to check directly."""
    urls: List[str] = []
    slugs = domain_guess_slugs(company_name, category)
    for tld in ["ie", "com"]:
        for slug in slugs:
            urls.append(f"https://{slug}.{tld}")
            urls.append(f"https://www.{slug}.{tld}")
    # Keep this deliberately short so a 25-row test does not become slow.
    # Each direct-domain check can otherwise add several seconds when a domain does not exist.
    return urls[:MAX_DIRECT_DOMAIN_GUESSES]


def score_page_match(url: str, html: str, company_name: str, area: str) -> Tuple[float, str]:
    soup = BeautifulSoup(html or "", "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = soup.get_text(" ", strip=True)[:50_000]
    domain = get_domain(url)
    haystack = normalize_text(" ".join([title, text, domain, url]))

    tokens = company_tokens(company_name)
    token_hits = sum(1 for token in tokens if token in haystack)
    token_ratio = token_hits / max(len(tokens), 1)

    company_phrase = normalize_text(company_name)
    phrase_hit = company_phrase in haystack

    area_text = normalize_text(area or "Galway Ireland")
    area_parts = [part for part in area_text.split() if len(part) >= 4]
    area_hit = "galway" in haystack or any(part in haystack for part in area_parts)

    domain_hit = any(token in normalize_text(domain) for token in tokens)

    confidence = 0.35
    confidence += 0.25 * token_ratio
    confidence += 0.15 if domain_hit else 0.0
    confidence += 0.15 if phrase_hit else 0.0
    confidence += 0.10 if area_hit else 0.0
    confidence = min(confidence, 0.92)

    reason_parts = [f"direct domain/page check; {token_hits}/{len(tokens)} distinctive tokens matched"]
    if domain_hit:
        reason_parts.append("domain contains distinctive token")
    if phrase_hit:
        reason_parts.append("company phrase found on page")
    if area_hit:
        reason_parts.append("location signal found")
    return round(confidence, 2), "; ".join(reason_parts)


def verify_official_website(company_name: str, area: str, candidate_url: str, page_text: str, domain: str = "") -> Tuple[bool, float, str, Optional[str]]:
    """Strict official website verification with safety rules.

    Returns (is_verified, confidence, reason, official_root_url).

    Safety rules (from requirements):
    1. Domain is not directory/forum/town/advertising/social/booking/healthcare directory
    2. Page title, H1, contact page, or visible text has full company name OR very close trading-name match
    3. Generic tokens alone (physio, clinic, galway, etc.) do not count as distinctive
    4. Domain supports business identity (not unrelated)
    5. Result pages (privacy-policy, directory pages, forum posts, Reddit) stay candidate-only
    """
    if not candidate_url or not domain:
        domain = get_domain(candidate_url)

    if not domain:
        return False, 0.0, "No valid domain", None

    # Rule 1: Reject directory/forum/town/social/booking domains
    if is_directory_domain(domain):
        return False, 0.0, "blocked directory domain", None

    # Reject town/general portal domains
    if domain in {"ballinasloe.ie", "galway.ie", "connemara.ie", "corrib.ie"}:
        return False, 0.0, "town/general portal, not official business", None

    # Rule 5: Reject result pages like /privacy-policy, /directory/, /forum/, etc.
    url_lower = candidate_url.lower()
    result_page_indicators = ["/privacy", "/terms", "/contact-us", "/directory/", "/forum/", "reddit.com", "/post/", "/profile/"]
    if any(indicator in url_lower for indicator in result_page_indicators):
        return False, 0.0, "result/landing page, not official domain", None

    # Rule 2: Check for company name or close trading-name match in page content
    tokens = company_tokens(company_name)
    if not tokens:
        return False, 0.0, "no distinctive tokens in company name", None

    haystack = normalize_text(" ".join([page_text, domain]))
    company_phrase_normalized = normalize_text(company_name)

    # Strong signal: exact company phrase match
    phrase_hit = company_phrase_normalized in haystack

    # Moderate signal: distinctive token(s) in page (not just generic)
    token_hits = sum(1 for token in tokens if token in haystack)
    has_distinctive_match = token_hits >= len(tokens) * 0.5  # At least 50% of distinctive tokens

    # Rule 3: Generic-token-only rejection
    # If all matches are generic words, reject
    if not phrase_hit and token_hits == 0:
        return False, 0.0, "generic token match only", None

    # Rule 4: Domain should support business identity
    # Check if domain contains at least one distinctive token or part of company name
    domain_normalized = normalize_text(domain)
    domain_has_distinctive = any(token in domain_normalized for token in tokens)
    domain_has_company_phrase = any(word in domain_normalized for word in company_phrase_normalized.split() if len(word) >= 4)

    confidence = 0.0
    reasons = []

    if phrase_hit:
        confidence += 0.50
        reasons.append("company phrase found on page")

    if has_distinctive_match:
        confidence += 0.25
        reasons.append(f"{token_hits}/{len(tokens)} distinctive tokens matched")

    if domain_has_distinctive or domain_has_company_phrase:
        confidence += 0.15
        reasons.append("domain contains company identifier")

    confidence = min(confidence, 0.95)

    # Must have at least one strong signal (phrase match OR distinctive match in domain + page)
    if confidence < 0.60:
        return False, confidence, f"weak business-name match ({'; '.join(reasons)})", None

    official_root = root_url(candidate_url)
    final_reason = "; ".join(reasons) if reasons else "verified official website"
    return True, confidence, final_reason, official_root



def extract_emails(text: str) -> List[str]:
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or "")
    cleaned: List[str] = []
    for email in emails:
        email = email.strip(" .,:;()[]{}<>").lower()
        if email.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            continue
        if email not in cleaned:
            cleaned.append(email)
    return cleaned


def normalize_irish_phone(match: str) -> Optional[str]:
    """Return a safe Irish-style phone string or None for obvious false positives."""
    if not match:
        return None

    digits = re.sub(r"\D", "", match)
    if digits.startswith("00353"):
        digits = "353" + digits[5:]

    if digits.startswith("353"):
        national = "0" + digits[3:]
    elif digits.startswith("0"):
        national = digits
    else:
        return None

    # Irish geographic/mobile numbers are usually 9-10 digits including the 0.
    # This deliberately rejects junk strings like 06666666666.
    if len(national) not in {9, 10}:
        return None

    if not re.match(r"^0[1-9]", national):
        return None

    # Reject obvious placeholders, tracking fragments and repeated-digit junk.
    significant = national[1:]
    if re.search(r"(\d)\1{5,}", significant):
        return None
    if len(set(significant)) <= 2 and len(significant) >= 8:
        return None

    sequential_samples = {
        "0123456789",
        "1234567890",
        "123456789",
        "9876543210",
        "0987654321",
        "987654321",
    }
    if significant in sequential_samples or national in sequential_samples:
        return None

    return national


def extract_irish_phones(text: str) -> List[str]:
    if not text:
        return []
    # Handles +353, 00353, 091, 01, 021, 087, etc. This intentionally keeps the
    # match broad, then normalises and rejects suspicious/placeholder numbers.
    raw_matches = re.findall(
        r"(?:(?:\+|00)353[\s\-\(\)]*)?0?\d{1,3}[\s\-\(\)]*\d{3}[\s\-\(\)]*\d{3,4}",
        text,
    )
    phones: List[str] = []
    for match in raw_matches:
        normalized = normalize_irish_phone(match)
        if normalized and normalized not in phones:
            phones.append(normalized)
    return phones


def apollo_organization_enrich(
    company_name: Optional[str],
    api_key: Optional[str],
    domain: Optional[str] = None,
    website: Optional[str] = None,
    linkedin_url: Optional[str] = None,
) -> Dict[str, Any]:
    if not api_key:
        return {}
    if not (company_name or domain or website or linkedin_url):
        return {}

    params: Dict[str, str] = {}
    if company_name:
        params["name"] = company_name
    if domain:
        params["domain"] = domain
    if website:
        params["website"] = website
    if linkedin_url:
        params["linkedin_url"] = linkedin_url

    headers = {
        "Accept": "application/json",
        # Apollo examples vary by surface; include both common header names.
        "X-Api-Key": api_key,
        "Api-Key": api_key,
    }
    try:
        response = requests.get(
            f"{APOLLO_BASE_URL}/organizations/enrich",
            headers=headers,
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code in {401, 403, 422}:
            return {"_error": response.text}
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        return {"_error": str(exc)}


def score_apollo_match(org: Dict[str, Any], company_name: str, area: str) -> float:
    if not org:
        return 0.0
    org_name = normalize_text(org.get("name", ""))
    haystack = normalize_text(" ".join([org_name, org.get("website_url", ""), org.get("primary_domain", ""), org.get("raw_address", "")]))
    tokens = company_tokens(company_name)
    token_hits = sum(1 for token in tokens if token in haystack)
    token_ratio = token_hits / max(len(tokens), 1)
    area_hit = "galway" in haystack or any(part in haystack for part in normalize_text(area).split() if len(part) >= 4)
    return min(0.55 + (0.30 * token_ratio) + (0.10 if area_hit else 0.0), 0.95)


def build_queries(company_name: str, area: str, category: str = "") -> List[str]:
    area = clean_cell(area) or "Galway Ireland"
    category = clean_cell(category) or ""
    places = location_terms(area)
    primary_place = places[0] if places else "galway"
    place_query = " ".join(dict.fromkeys(places))

    dental = is_dental_business(company_name, category)
    physio = is_physio_business(company_name, category)
    if dental:
        category_query = "dental dentist dentistry"
    elif physio:
        category_query = "physio physiotherapy"
    else:
        category_query = category or "official website"

    simplified = company_name
    for suffix in [
        " clinic",
        " clinics",
        " practice",
        " ltd",
        " limited",
        " company",
    ]:
        if simplified.lower().endswith(suffix):
            simplified = simplified[: -len(suffix)].strip()
            break

    queries = [
        f'"{company_name}" {place_query} {category_query}',
        f'"{company_name}" "{area}"',
        f'"{company_name}" {primary_place} official website contact',
    ]
    if simplified and normalize_text(simplified) != normalize_text(company_name):
        queries.append(f'"{simplified}" {place_query} {category_query}')

    deduped: List[str] = []
    for query in queries:
        query = re.sub(r"\s+", " ", query).strip()
        if query and query not in deduped:
            deduped.append(query)
    return deduped[:MAX_DDG_QUERIES_PER_ROW]


def enrich_row(
    row: pd.Series,
    *,
    use_apollo: bool = False,
    apollo_api_key: Optional[str] = None,
    delay: float = 0.0,
    target_fields: Iterable[str] = ("Website", "Phone", "Email"),
    search_provider: str = "duckduckgo",
    brave_api_key: Optional[str] = None,
    serpapi_api_key: Optional[str] = None,
    search_location: str = DEFAULT_SEARCH_LOCATION,
) -> RowProposal:
    company_name = clean_cell(row.get("Company Name")) or ""
    area = clean_cell(row.get("Area")) or "Galway Ireland"
    category = clean_cell(row.get("Business Category")) or clean_cell(row.get("Website Category")) or ""
    proposal = RowProposal()

    if not company_name:
        proposal.notes.append("Skipped: missing company name")
        return proposal

    targets = {field.lower() for field in target_fields}
    existing_website = clean_cell(row.get("Website"))
    selected_provider = (search_provider or "duckduckgo").lower().strip()

    # Optional Apollo lookup. Disabled unless explicitly enabled and key exists.
    if use_apollo and apollo_api_key:
        apollo_data = apollo_organization_enrich(company_name=company_name, api_key=apollo_api_key, website=existing_website)
        org = apollo_data.get("organization", {}) if isinstance(apollo_data, dict) else {}
        if org:
            confidence = score_apollo_match(org, company_name, area)
            website = org.get("website_url") or org.get("primary_domain")
            phone_obj = org.get("primary_phone") or {}
            phone = phone_obj.get("number") if isinstance(phone_obj, dict) else org.get("phone")
            if "website" in targets:
                proposal.website.update_if_better(website, "Apollo Organization Enrichment", confidence, "Apollo match")
            if "phone" in targets and phone:
                # Apollo phone values are useful candidates, but not verified.
                # Verified phone proposals must come from the official website/contact page.
                normalized_phone = normalize_irish_phone(str(phone))
                if normalized_phone:
                    proposal.add_candidate("Phone", normalized_phone, "Apollo Organization Enrichment", min(confidence, 0.70), "Apollo phone candidate - manual check")
        elif isinstance(apollo_data, dict) and apollo_data.get("_error"):
            proposal.notes.append(f"Apollo skipped/error: {apollo_data.get('_error')[:120]}")
    elif use_apollo and not apollo_api_key:
        proposal.notes.append("Apollo enabled but no API key found; skipped Apollo")

    urls_to_check: List[str] = []

    # Use existing website as source for phone/email if it exists, but do not
    # automatically trust directory/free-hosting pages as official websites.
    if existing_website:
        urls_to_check.extend(candidate_contact_urls(existing_website))
        if is_directory_domain(get_domain(existing_website)):
            proposal.add_candidate(
                "Website",
                existing_website,
                existing_website,
                0.65,
                "Existing CRM website is a directory/free-hosting/listing page - manual review",
            )
            proposal.notes.append("Existing website looks like a directory/free-hosting page; not treated as verified official site")
        else:
            proposal.website.update_if_better(existing_website, existing_website, 1.0, "Existing CRM website")

    # v3 fallback: direct likely-domain checks. This catches SMEs that search
    # engines do not return reliably, while still keeping values in audit review.
    if not existing_website and {"website", "phone", "email"} & targets:
        for guess_url in guessed_website_urls(company_name, category):
            if delay:
                time.sleep(delay)
            html, final_url = fetch_page(guess_url, timeout=DIRECT_DOMAIN_TIMEOUT)
            if not html:
                continue
            source_url = root_url(final_url or guess_url)
            source_domain = get_domain(source_url)
            if is_directory_domain(source_domain):
                continue
            confidence, reason = score_page_match(source_url, html, company_name, area)
            page_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

            # Add as candidate first
            if "website" in targets and confidence >= 0.50:
                proposal.add_candidate("Website", source_url, source_url, confidence, reason)
                urls_to_check.extend(candidate_contact_urls(source_url))

            # Strict verification before promoting to verified proposal
            is_verified, verified_confidence, verified_reason, official_url = verify_official_website(
                company_name, area, source_url, page_text, source_domain
            )
            if is_verified and "website" in targets and verified_confidence >= 0.78:
                proposal.website.update_if_better(official_url or source_url, official_url or source_url, verified_confidence, f"Verified: {verified_reason}")

            combined = html + " " + page_text
            if "phone" in targets and not has_value(row.get("Phone")) and confidence >= 0.55:
                phones = extract_irish_phones(combined)
                if phones:
                    phone_conf = min(0.88, confidence + 0.05)
                    proposal.add_candidate("Phone", phones[0], source_url, phone_conf, "Phone found on direct domain candidate - manual check")
            if "email" in targets and not has_value(row.get("Email")) and confidence >= 0.60:
                emails = extract_emails(combined)
                if emails:
                    preferred = sorted(
                        emails,
                        key=lambda e: (not e.startswith(("info@", "contact@", "hello@", "admin@", "reception@")), e),
                    )[0]
                    email_conf = min(0.86, confidence + 0.03)
                    proposal.add_candidate("Email", preferred, source_url, email_conf, "Email found on direct domain candidate - manual check")


    # Search for official website and possible snippets.
    rescue_names: List[str] = []
    needs_contact_search = ("phone" in targets and not has_value(row.get("Phone"))) or ("email" in targets and not has_value(row.get("Email")))
    needs_website_search = "website" in targets and not existing_website and not proposal.website.value
    search_provider_to_use = search_provider
    if needs_website_search or needs_contact_search:
        duckduckgo_blocked = False
        provider_notes_seen: set[str] = set()
        if selected_provider in {"brave", "brave_search", "brave_search_api"}:
            if brave_api_key or os.getenv(BRAVE_SEARCH_API_KEY_ENV):
                proposal.notes.append("Provider used = Brave Search API")
                provider_notes_seen.add("Provider used = Brave Search API")
            else:
                proposal.notes.append("Brave Search API warning: BRAVE_SEARCH_API_KEY missing. DuckDuckGo fallback used.")
                search_provider_to_use = "duckduckgo"
        for query in build_queries(company_name, area, category):
            if duckduckgo_blocked:
                proposal.notes.append("DuckDuckGo skipped remaining queries after block/rate-limit response")
                break
            if delay:
                time.sleep(delay)
            results = search_web(
                query,
                max_results=6,
                provider=search_provider_to_use,
                brave_api_key=brave_api_key,
                serpapi_api_key=serpapi_api_key,
                search_location=search_location,
            )
            for provider_note in [result.provider_note for result in results if result.provider_note]:
                if provider_note not in provider_notes_seen:
                    provider_notes_seen.add(provider_note)
                    proposal.notes.append(provider_note)
            if is_search_error(results):
                error_msg = results[0].snippet[:120]
                if is_duckduckgo_blocked(error_msg):
                    proposal.notes.append(f"DuckDuckGo blocked / 403: {error_msg}")
                    duckduckgo_blocked = True
                elif "SERPAPI_API_KEY missing" in error_msg or "Quota" in error_msg or "Rate" in error_msg:
                    proposal.notes.append(f"SerpAPI unavailable: {error_msg}. Fallback to DuckDuckGo.")
                else:
                    proposal.notes.append(f"Search error: {error_msg}")
                continue
            if not results:
                proposal.notes.append(f"No search results for query: {query[:80]}")
            for result in results:
                if not result.url:
                    continue

                candidate_confidence, candidate_reason = score_candidate_search_result(result, company_name, area)
                result_domain = get_domain(result.url)

                # Always add as candidate if confidence >= 0.35
                if "website" in targets and candidate_confidence >= 0.35:
                    proposal.add_candidate("Website", result.url, result.url, candidate_confidence, candidate_reason)
                    for inferred_name in inferred_names_from_directory_result(result, company_name, area):
                        if inferred_name not in rescue_names:
                            rescue_names.append(inferred_name)

                # Only verify non-directory domains with high candidate confidence
                if not is_directory_domain(result_domain) and candidate_confidence >= 0.65 and "website" in targets:
                    if delay:
                        time.sleep(delay / 2)
                    html, _ = fetch_page(result.url)
                    if html:
                        page_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
                        is_verified, verified_confidence, verified_reason, official_url = verify_official_website(
                            company_name, area, result.url, page_text, result_domain
                        )
                        if is_verified and verified_confidence >= 0.80:
                            proposal.website.update_if_better(official_url or result.url, official_url or result.url, verified_confidence, f"Verified: {verified_reason}")
                            urls_to_check.extend(candidate_contact_urls(official_url or result.url))

                # Directory snippets can still provide phone numbers, but use lower confidence.
                if "phone" in targets and result.snippet:
                    phones = extract_irish_phones(result.snippet)
                    if phones:
                        snippet_conf = 0.65 if is_directory_domain(result_domain) else min(max(candidate_confidence, 0.65), 0.75)
                        proposal.add_candidate("Phone", phones[0], result.url, snippet_conf, "Phone from search result snippet - manual check only")



    # Rescue pass: if a directory result exposes a likely trading name, try direct domains/searches for that name.
    # This handles rows where the CRM name is generic or slightly wrong, e.g.
    # "Ballinasloe Physiotherapy" appearing in directories as "Action Physio Ballinasloe".
    for rescue_name in rescue_names[:2]:
        if delay:
            time.sleep(delay)
        for guess_url in guessed_website_urls(rescue_name, category)[:4]:
            html, final_url = fetch_page(guess_url, timeout=DIRECT_DOMAIN_TIMEOUT)
            if not html:
                continue
            source_url = root_url(final_url or guess_url)
            source_domain = get_domain(source_url)
            if is_directory_domain(source_domain):
                continue
            confidence, reason = score_page_match(source_url, html, rescue_name, area)
            page_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
            reason = f"Rescue search from directory trading name '{rescue_name}'; {reason}"

            # Add as candidate first
            if "website" in targets and confidence >= 0.50:
                proposal.add_candidate("Website", source_url, source_url, min(confidence, 0.88), reason)
                urls_to_check.extend(candidate_contact_urls(source_url))

            # Strict verification for rescue candidates
            is_verified, verified_confidence, verified_reason, official_url = verify_official_website(
                rescue_name, area, source_url, page_text, source_domain
            )
            if is_verified and verified_confidence >= 0.80 and "website" in targets:
                proposal.website.update_if_better(official_url or source_url, official_url or source_url, min(verified_confidence, 0.90), f"Rescue verified: {verified_reason}")

        rescue_query = f'"{rescue_name}" "{area}" official website contact'
        rescue_results = search_web(
            rescue_query,
            max_results=5,
            provider=search_provider_to_use,
            brave_api_key=brave_api_key,
            serpapi_api_key=serpapi_api_key,
            search_location=search_location,
        )
        for provider_note in [result.provider_note for result in rescue_results if result.provider_note]:
            if provider_note not in proposal.notes:
                proposal.notes.append(provider_note)
        if is_search_error(rescue_results):
            error_msg = rescue_results[0].snippet[:120]
            if is_duckduckgo_blocked(error_msg):
                proposal.notes.append(f"DuckDuckGo blocked / 403 during rescue search: {error_msg}")
            else:
                proposal.notes.append(f"Rescue search error: {error_msg}")
            continue
        for result in rescue_results:
            if not result.url:
                continue
            candidate_confidence, candidate_reason = score_candidate_search_result(result, rescue_name, area)
            candidate_reason = f"Rescue search from directory trading name '{rescue_name}'; {candidate_reason}"
            if "website" in targets and candidate_confidence >= 0.35:
                proposal.add_candidate("Website", result.url, result.url, candidate_confidence, candidate_reason)

            # Strict verification for rescue search results
            result_domain = get_domain(result.url)
            if not is_directory_domain(result_domain) and "website" in targets:
                if delay:
                    time.sleep(delay / 2)
                html, _ = fetch_page(result.url)
                if html:
                    page_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
                    is_verified, verified_confidence, verified_reason, official_url = verify_official_website(
                        rescue_name, area, result.url, page_text, result_domain
                    )
                    if is_verified and verified_confidence >= 0.80:
                        proposal.website.update_if_better(official_url or result.url, official_url or result.url, min(verified_confidence, 0.90), f"Rescue verified: {verified_reason}")
                        urls_to_check.extend(candidate_contact_urls(official_url or result.url))


    # If Apollo/search/domain fallback found a proposed website, scrape it for phone/email.
    if proposal.website.value:
        urls_to_check.extend(candidate_contact_urls(proposal.website.value))

    seen_urls = set()
    for url in urls_to_check:
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        if delay:
            time.sleep(delay)
        html, final_url = fetch_page(url)
        if not html:
            continue
        source = final_url or url
        page_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        combined = html + " " + page_text

        if "phone" in targets and not has_value(row.get("Phone")):
            phones = extract_irish_phones(combined)
            if phones:
                official_source = proposal.website.value or existing_website
                is_verified_source = is_official_phone_source(source, official_source)
                conf = 0.88 if is_verified_source and (proposal.website.confidence >= 0.80 or existing_website) else 0.68
                reason = "Phone found on proposed/existing official website contact page" if is_verified_source else "Phone found on non-official candidate page - manual check only"
                proposal.add_candidate("Phone", phones[0], source, conf, reason)
                if is_verified_source:
                    proposal.phone.update_if_better(phones[0], source, conf, reason)

        if "email" in targets and not has_value(row.get("Email")):
            emails = extract_emails(combined)
            if emails:
                preferred = sorted(
                    emails,
                    key=lambda e: (not e.startswith(("info@", "contact@", "hello@", "admin@", "reception@")), e),
                )[0]
                official_source = proposal.website.value or existing_website
                is_verified_source = is_official_phone_source(source, official_source)
                conf = 0.85 if is_verified_source and (proposal.website.confidence >= 0.80 or existing_website) else 0.65
                reason = "Email found on proposed/existing official website contact page" if is_verified_source else "Email found on non-official candidate page - manual check only"
                proposal.add_candidate("Email", preferred, source, conf, reason)
                if is_verified_source:
                    proposal.email.update_if_better(preferred, source, conf, reason)

    if not any([proposal.website.value, proposal.phone.value, proposal.email.value]):
        if proposal.candidates:
            proposal.notes.append("Candidate matches found for manual review; no verified proposal selected")
        elif not proposal.notes:
            proposal.notes.append("No reliable proposal found")
    return proposal


def add_audit_columns(df: pd.DataFrame) -> pd.DataFrame:
    output = df.copy()
    for col in AUDIT_COLUMNS:
        if col not in output.columns:
            output[col] = pd.Series([""] * len(output), index=output.index, dtype="object")
        else:
            output[col] = output[col].astype("object")
    return output


def choose_decision_needed(row: pd.Series, proposal: RowProposal, targets: set[str]) -> str:
    if "website" in targets and has_value(row.get("Website")):
        return "Existing website already present"
    if proposal.website.value:
        return "Verified proposal available"

    notes_text = " ".join(proposal.notes).lower()
    if "duckduckgo blocked" in notes_text or "403" in notes_text or "rate-limit" in notes_text:
        return "DuckDuckGo blocked / 403"

    best_website = proposal.top_candidates("Website", limit=1)
    if best_website and best_website[0].confidence >= 0.65:
        return "Manual review: strong candidate but not verified"
    if best_website:
        return "Manual review: weak candidate only"
    if "website" in targets:
        return "No reliable candidate found"
    if proposal.candidates:
        return "Manual review: candidate found"
    return "No reliable candidate found"


def apply_proposal_to_row(
    output: pd.DataFrame,
    idx: int,
    proposal: RowProposal,
    *,
    mode: str,
    min_confidence: float,
    target_fields: Iterable[str],
) -> None:
    targets = {field.lower() for field in target_fields}

    def write_audit(prefix: str, field_proposal: FieldProposal) -> None:
        # Canonicalize website URLs to root domain for storage
        value = field_proposal.value
        if prefix == "Website" and value:
            value = root_url(value)
        output.at[idx, f"Proposed {prefix}"] = value or ""
        output.at[idx, f"{prefix} Source URL"] = field_proposal.source_url or ""
        output.at[idx, f"{prefix} Confidence"] = f"{field_proposal.confidence:.2f}" if field_proposal.value else ""

    def write_candidates(prefix: str) -> None:
        for position, candidate in enumerate(proposal.top_candidates(prefix), start=1):
            output.at[idx, f"Candidate {prefix} {position} Value"] = candidate.value
            output.at[idx, f"Candidate {prefix} {position} Source URL"] = candidate.source_url
            output.at[idx, f"Candidate {prefix} {position} Confidence"] = f"{candidate.confidence:.2f}"
            output.at[idx, f"Candidate {prefix} {position} Rejected Reason"] = candidate.reason

    write_audit("Website", proposal.website)
    write_audit("Phone", proposal.phone)
    write_audit("Email", proposal.email)
    write_candidates("Website")
    write_candidates("Phone")
    write_candidates("Email")
    best_website = proposal.top_candidates("Website", limit=1)
    if best_website:
        output.at[idx, "Best Candidate Website"] = root_url(best_website[0].value)
        output.at[idx, "Best Candidate Confidence"] = f"{best_website[0].confidence:.2f}"
        output.at[idx, "Best Candidate Rejected Reason"] = best_website[0].reason
    output.at[idx, "Decision Needed"] = choose_decision_needed(output.loc[idx], proposal, targets)
    output.at[idx, "Enrichment Notes"] = "; ".join(proposal.notes + proposal.website.notes + proposal.phone.notes + proposal.email.notes)

    if mode != "verified_only":
        return

    if "website" in targets and not has_value(output.at[idx, "Website"]):
        if proposal.website.value and proposal.website.confidence >= min_confidence:
            output.at[idx, "Website"] = root_url(proposal.website.value)

    if "phone" in targets and not has_value(output.at[idx, "Phone"]):
        if proposal.phone.value and proposal.phone.confidence >= min_confidence:
            output.at[idx, "Phone"] = proposal.phone.value

    if "email" in targets and not has_value(output.at[idx, "Email"]):
        if proposal.email.value and proposal.email.confidence >= min_confidence:
            output.at[idx, "Email"] = proposal.email.value

    # Conservative research status updates.
    if "Research Status" in output.columns:
        if any(
            [
                proposal.website.value and proposal.website.confidence >= min_confidence,
                proposal.phone.value and proposal.phone.confidence >= min_confidence,
                proposal.email.value and proposal.email.confidence >= min_confidence,
            ]
        ):
            output.at[idx, "Research Status"] = "Partially verified"
    if "Data Confidence" in output.columns:
        high_conf_count = sum(
            1
            for fp in [proposal.website, proposal.phone, proposal.email]
            if fp.value and fp.confidence >= min_confidence
        )
        if high_conf_count >= 2:
            output.at[idx, "Data Confidence"] = "Medium"


def enrich_dataframe(
    df: pd.DataFrame,
    *,
    mode: str = "preview",
    min_confidence: float = 0.80,
    row_limit: Optional[int] = 5,
    delay: float = 0.0,
    use_apollo: bool = False,
    apollo_api_key: Optional[str] = None,
    target_fields: Iterable[str] = ("Website", "Phone", "Email"),
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    search_provider: str = "duckduckgo",
    brave_api_key: Optional[str] = None,
    serpapi_api_key: Optional[str] = None,
    search_location: str = DEFAULT_SEARCH_LOCATION,
) -> pd.DataFrame:
    """
    Enrich a dataframe and return original rows plus audit/proposal columns.

    mode="preview" keeps CRM import fields unchanged.
    mode="verified_only" fills only blank CRM fields where proposal confidence >= min_confidence.
    """
    output = add_audit_columns(df)
    target_fields = list(target_fields)

    required_cols = {"Company Name", "Area", "Phone", "Email", "Website"}
    missing = sorted(required_cols - set(output.columns))
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    rows_needing_research: List[Tuple[int, pd.Series]] = []
    for idx, row in output.iterrows():
        needs_research = any(
            field in target_fields and not has_value(row.get(field))
            for field in ["Website", "Phone", "Email"]
        )
        if needs_research:
            rows_needing_research.append((idx, row))

    max_research = len(rows_needing_research) if row_limit is None else min(int(row_limit), len(rows_needing_research))

    researched = 0
    for idx, row in rows_needing_research:
        if row_limit is not None and researched >= row_limit:
            output.at[idx, "Enrichment Notes"] = "Not researched due to row limit"
            continue

        company_for_progress = clean_cell(row.get("Company Name")) or f"row {idx}"
        if progress_callback:
            progress_callback(researched, max_research, company_for_progress)

        try:
            proposal = enrich_row(
                row,
                use_apollo=use_apollo,
                apollo_api_key=apollo_api_key,
                delay=delay,
                target_fields=target_fields,
                search_provider=search_provider,
                brave_api_key=brave_api_key,
                serpapi_api_key=serpapi_api_key,
                search_location=search_location,
            )
        except Exception as exc:
            proposal = RowProposal()
            proposal.notes.append(f"Research error: {type(exc).__name__}: {str(exc)[:180]}")

        apply_proposal_to_row(
            output,
            idx,
            proposal,
            mode=mode,
            min_confidence=min_confidence,
            target_fields=target_fields,
        )
        researched += 1
        if progress_callback:
            progress_callback(researched, max_research, company_for_progress)
    return output


def audit_columns_only(df: pd.DataFrame) -> pd.DataFrame:
    useful_cols = [
        "Company Name",
        "Area",
        "Phone",
        "Email",
        "Website",
        *AUDIT_COLUMNS,
    ]
    return df[[col for col in useful_cols if col in df.columns]]


def crm_import_columns_only(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=[col for col in AUDIT_COLUMNS if col in df.columns], errors="ignore")


def enrich_csv(
    input_file: str,
    output_file: str,
    *,
    delay: float = 0.0,
    max_rows: Optional[int] = 5,
    skip_apollo: bool = True,
    mode: str = "preview",
    min_confidence: float = 0.80,
    apollo_api_key: Optional[str] = None,
    target_fields: Iterable[str] = ("Website", "Phone", "Email"),
    search_provider: str = "duckduckgo",
    brave_api_key: Optional[str] = None,
    serpapi_api_key: Optional[str] = None,
    search_location: str = DEFAULT_SEARCH_LOCATION,
) -> None:
    df = pd.read_csv(input_file, dtype=str, keep_default_na=False)
    api_key = apollo_api_key or os.getenv(APOLLO_API_KEY_ENV)
    enriched = enrich_dataframe(
        df,
        mode=mode,
        min_confidence=min_confidence,
        row_limit=max_rows,
        delay=delay,
        use_apollo=not skip_apollo,
        apollo_api_key=api_key,
        target_fields=target_fields,
        search_provider=search_provider,
        brave_api_key=brave_api_key or os.getenv(BRAVE_SEARCH_API_KEY_ENV),
        serpapi_api_key=serpapi_api_key,
        search_location=search_location,
    )
    enriched.to_csv(output_file, index=False)
    print(f"Enrichment completed. Output written to {output_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely enrich CRM CSV files.")
    parser.add_argument("--input-file", required=True, help="Path to input CRM CSV file")
    parser.add_argument("--output-file", required=True, help="Path to output CSV file")
    parser.add_argument("--delay", type=float, default=0.0, help="Optional delay between network requests in seconds")
    parser.add_argument("--limit", type=int, default=5, help="Only research the first N incomplete rows while preserving all rows")
    parser.add_argument("--search-provider", choices=["duckduckgo", "brave", "serpapi"], default="duckduckgo", help="Search backend for web discovery")
    parser.add_argument("--brave-key", default=None, help="Optional Brave Search API key; otherwise BRAVE_SEARCH_API_KEY env var is used")
    parser.add_argument("--serpapi-key", default=None, help="Optional SerpAPI key; otherwise SERPAPI_API_KEY env var is used")
    parser.add_argument("--search-location", default=DEFAULT_SEARCH_LOCATION, help="Location bias for SerpAPI/Google local results")
    parser.add_argument("--mode", choices=["preview", "verified_only"], default="preview", help="preview keeps fields unchanged; verified_only fills high-confidence blanks")
    parser.add_argument("--min-confidence", type=float, default=0.80, help="Minimum confidence required for verified_only fill")
    parser.add_argument("--fields", nargs="+", default=["Website", "Phone", "Email"], help="Fields to research, e.g. Website Phone Email")
    args = parser.parse_args()

    enrich_csv(
        args.input_file,
        args.output_file,
        delay=args.delay,
        max_rows=args.limit,
        skip_apollo=True,
        mode=args.mode,
        min_confidence=args.min_confidence,
        target_fields=args.fields,
        search_provider=args.search_provider,
        brave_api_key=args.brave_key or os.getenv(BRAVE_SEARCH_API_KEY_ENV),
        serpapi_api_key=args.serpapi_key or os.getenv(SERPAPI_API_KEY_ENV),
        search_location=args.search_location,
    )


if __name__ == "__main__":
    main()
