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
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
DUCKDUCKGO_HTML_URL = "https://duckduckgo.com/html/?q={query}"

DEFAULT_TIMEOUT = 15
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
    "healthmail.ie",
    "solocheck.ie",
    "vision-net.ie",
    "rip.ie",
    "mapcarta.com",
    "cylex.ie",
    "locallife.ie",
}

CONTACT_PATHS = [
    "contact",
    "contact-us",
    "contact-locations",
    "locations",
    "about",
]

AUDIT_COLUMNS = [
    "Proposed Website",
    "Website Source URL",
    "Website Confidence",
    "Proposed Phone",
    "Phone Source URL",
    "Phone Confidence",
    "Proposed Email",
    "Email Source URL",
    "Email Confidence",
    "Enrichment Notes",
]


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


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


def ddg_search(query: str, max_results: int = 8) -> List[SearchResult]:
    url = DUCKDUCKGO_HTML_URL.format(query=urllib.parse.quote(query))
    headers = {"User-Agent": USER_AGENT}
    try:
        response = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
    except Exception as exc:
        return [SearchResult(title="SEARCH_ERROR", url="", snippet=str(exc))]

    soup = BeautifulSoup(response.text, "html.parser")
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


def fetch_page(url: str) -> Tuple[Optional[str], Optional[str]]:
    headers = {"User-Agent": USER_AGENT}
    try:
        response = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
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


def extract_irish_phones(text: str) -> List[str]:
    if not text:
        return []
    # Handles +353, 00353, 091, 01, 021, 087, etc. This intentionally keeps the
    # match broad, then filters by digit count.
    raw_matches = re.findall(
        r"(?:(?:\+|00)353[\s\-\(\)]*)?0?\d{1,3}[\s\-\(\)]*\d{3}[\s\-\(\)]*\d{3,4}",
        text,
    )
    phones: List[str] = []
    for match in raw_matches:
        digits = re.sub(r"\D", "", match)
        if digits.startswith("353"):
            normalized = "+353 " + digits[3:]
        elif digits.startswith("00353"):
            normalized = "+353 " + digits[5:]
        elif digits.startswith("0"):
            normalized = digits
        else:
            continue
        # Filter out very short/long false positives.
        digit_count = len(re.sub(r"\D", "", normalized))
        if 9 <= digit_count <= 13 and normalized not in phones:
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
    return [
        f'"{company_name}" "{area}" official website phone',
        f'"{company_name}" Galway Ireland contact',
        f'{company_name} {area} {category} website phone',
    ]


def enrich_row(
    row: pd.Series,
    *,
    use_apollo: bool = False,
    apollo_api_key: Optional[str] = None,
    delay: float = 0.0,
    target_fields: Iterable[str] = ("Website", "Phone", "Email"),
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
            if "phone" in targets:
                proposal.phone.update_if_better(phone, "Apollo Organization Enrichment", confidence, "Apollo match")
        elif isinstance(apollo_data, dict) and apollo_data.get("_error"):
            proposal.notes.append(f"Apollo skipped/error: {apollo_data.get('_error')[:120]}")
    elif use_apollo and not apollo_api_key:
        proposal.notes.append("Apollo enabled but no API key found; skipped Apollo")

    # Use existing website as source for phone/email if it exists.
    urls_to_check: List[str] = []
    if existing_website:
        urls_to_check.extend(candidate_contact_urls(existing_website))
        proposal.website.update_if_better(existing_website, existing_website, 1.0, "Existing CRM website")

    # Search for official website and possible snippets.
    search_results: List[SearchResult] = []
    if not existing_website or "phone" in targets or "email" in targets:
        for query in build_queries(company_name, area, category):
            if delay:
                time.sleep(delay)
            results = ddg_search(query, max_results=8)
            search_results.extend(results)
            for result in results:
                if not result.url:
                    continue
                ok, confidence, note = is_probably_official_result(result, company_name, area)
                if ok and "website" in targets:
                    proposal.website.update_if_better(result.url, result.url, confidence, note)
                    urls_to_check.extend(candidate_contact_urls(result.url))
                # Directory snippets can still provide phone numbers, but use lower confidence.
                if "phone" in targets and result.snippet:
                    phones = extract_irish_phones(result.snippet)
                    if phones:
                        snippet_conf = 0.65 if is_directory_domain(get_domain(result.url)) else min(confidence, 0.75)
                        proposal.phone.update_if_better(phones[0], result.url, snippet_conf, "Phone from search result snippet")

    # If Apollo/search found a proposed website, scrape it for phone/email.
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
                conf = 0.88 if proposal.website.confidence >= 0.80 or existing_website else 0.75
                proposal.phone.update_if_better(phones[0], source, conf, "Phone found on website/contact page")

        if "email" in targets and not has_value(row.get("Email")):
            emails = extract_emails(combined)
            if emails:
                preferred = sorted(
                    emails,
                    key=lambda e: (not e.startswith(("info@", "contact@", "hello@", "admin@", "reception@")), e),
                )[0]
                conf = 0.85 if proposal.website.confidence >= 0.80 or existing_website else 0.70
                proposal.email.update_if_better(preferred, source, conf, "Email found on website/contact page")

    if not proposal.notes and not any([proposal.website.value, proposal.phone.value, proposal.email.value]):
        proposal.notes.append("No reliable proposal found")
    return proposal


def add_audit_columns(df: pd.DataFrame) -> pd.DataFrame:
    output = df.copy()
    for col in AUDIT_COLUMNS:
        if col not in output.columns:
            output[col] = ""
    return output


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
        output.at[idx, f"Proposed {prefix}"] = field_proposal.value or ""
        output.at[idx, f"{prefix} Source URL"] = field_proposal.source_url or ""
        output.at[idx, f"{prefix} Confidence"] = f"{field_proposal.confidence:.2f}" if field_proposal.value else ""

    write_audit("Website", proposal.website)
    write_audit("Phone", proposal.phone)
    write_audit("Email", proposal.email)
    output.at[idx, "Enrichment Notes"] = "; ".join(proposal.notes + proposal.website.notes + proposal.phone.notes + proposal.email.notes)

    if mode != "verified_only":
        return

    if "website" in targets and not has_value(output.at[idx, "Website"]):
        if proposal.website.value and proposal.website.confidence >= min_confidence:
            output.at[idx, "Website"] = proposal.website.value

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

    researched = 0
    for idx, row in output.iterrows():
        needs_research = any(
            field in target_fields and not has_value(row.get(field))
            for field in ["Website", "Phone", "Email"]
        )
        if not needs_research:
            continue
        if row_limit is not None and researched >= row_limit:
            output.at[idx, "Enrichment Notes"] = "Not researched due to row limit"
            continue
        proposal = enrich_row(
            row,
            use_apollo=use_apollo,
            apollo_api_key=apollo_api_key,
            delay=delay,
            target_fields=target_fields,
        )
        apply_proposal_to_row(
            output,
            idx,
            proposal,
            mode=mode,
            min_confidence=min_confidence,
            target_fields=target_fields,
        )
        researched += 1
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
) -> None:
    df = pd.read_csv(input_file)
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
    )
    enriched.to_csv(output_file, index=False)
    print(f"Enrichment completed. Output written to {output_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely enrich CRM CSV files.")
    parser.add_argument("--input-file", required=True, help="Path to input CRM CSV file")
    parser.add_argument("--output-file", required=True, help="Path to output CSV file")
    parser.add_argument("--delay", type=float, default=0.0, help="Optional delay between network requests in seconds")
    parser.add_argument("--limit", type=int, default=5, help="Only research the first N incomplete rows while preserving all rows")
    parser.add_argument("--use-apollo", action="store_true", help="Use Apollo if APOLLO_API_KEY is available")
    parser.add_argument("--mode", choices=["preview", "verified_only"], default="preview", help="preview keeps fields unchanged; verified_only fills high-confidence blanks")
    parser.add_argument("--min-confidence", type=float, default=0.80, help="Minimum confidence required for verified_only fill")
    parser.add_argument("--fields", nargs="+", default=["Website", "Phone", "Email"], help="Fields to research, e.g. Website Phone Email")
    args = parser.parse_args()

    enrich_csv(
        args.input_file,
        args.output_file,
        delay=args.delay,
        max_rows=args.limit,
        skip_apollo=not args.use_apollo,
        mode=args.mode,
        min_confidence=args.min_confidence,
        target_fields=args.fields,
    )


if __name__ == "__main__":
    main()

