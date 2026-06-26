"""
CRM Data Enrichment Tool
=========================

This script reads a CRM CSV file, attempts to fill in missing contact
information (such as website URLs and email addresses) based on a company
name and general location, and writes the results back to a new CSV file.

The enrichment is performed by performing a web search for each company
with missing fields, extracting the most likely website from the search
results, and then scraping that site for contact e‑mails.  Because this
script relies on network access to search engines and external websites,
it should be run on a machine with internet connectivity.

Notes
-----
* This script does **not** modify existing data.  If a row already
  contains an email or website, those values are preserved.
* The search is implemented against DuckDuckGo's HTML interface and
  requires a valid User‑Agent header.  Be mindful of the terms of
  service for any search engine you use.
* If no useful information can be found, the corresponding fields
  remain empty.
* Some websites intentionally obfuscate email addresses.  This script
  makes a best‑effort attempt but may not catch every case.

To run the script:

    python crm_enrichment_tool.py --input-file path/to/CRM_Import.csv \
                                  --output-file path/to/CRM_Import_enriched.csv

You can also provide a delay between requests to avoid hitting rate
limits:

    python crm_enrichment_tool.py --input-file leads.csv --delay 2

"""

import csv
import re
import time
import argparse
import os
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

import pandas as pd
import requests
from bs4 import BeautifulSoup


SEARCH_URL = "https://duckduckgo.com/html/?q={query}"

# Base URL for Apollo API.  See docs for more details【200967481361746†L31-L45】.
APOLLO_BASE_URL = "https://api.apollo.io/api/v1"

# Environment variable name used to read the Apollo API key.  This script
# expects the user to create an API key in Apollo and set it in their
# environment.  Refer to Apollo's documentation on how to create API
# keys【396190318733043†L80-L106】.
APOLLO_API_KEY_ENV = "APOLLO_API_KEY"

def apollo_organization_enrich(
    company_name: Optional[str] = None,
    domain: Optional[str] = None,
    website: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    api_key: str = "",
) -> Dict[str, Any]:
    """
    Call Apollo's Organization Enrichment endpoint to enrich data for a
    single company.  At least one of `company_name`, `domain`,
    `website`, or `linkedin_url` must be provided【200967481361746†L31-L90】.

    Parameters
    ----------
    company_name : str, optional
        The company name to search for.
    domain : str, optional
        The company's primary domain (e.g., "example.com").
    website : str, optional
        Full website URL of the company (e.g., "http://www.example.com").
    linkedin_url : str, optional
        LinkedIn profile URL for the company.
    api_key : str
        Apollo API key.  To create an API key, follow the steps
        documented by Apollo【396190318733043†L80-L106】 and set it in your
        environment.

    Returns
    -------
    dict
        Parsed JSON response containing the `organization` object on
        success.

    Raises
    ------
    ValueError
        If no matching parameter is provided.
    Exception
        For any HTTP errors or JSON parsing issues.
    """
    if not (company_name or domain or website or linkedin_url):
        raise ValueError("Apollo enrichment requires at least one identifier (name, domain, website, or LinkedIn URL).")
    url = f"{APOLLO_BASE_URL}/organizations/enrich"
    # Build query parameters dynamically.
    params: Dict[str, str] = {}
    if domain:
        params["domain"] = domain
    if website:
        params["website"] = website
    if linkedin_url:
        params["linkedin_url"] = linkedin_url
    if company_name:
        params["name"] = company_name
    headers = {
        "Accept": "application/json",
        "Api-Key": api_key,
    }
    response = requests.get(url, headers=headers, params=params, timeout=15)
    if response.status_code != 200:
        raise Exception(f"Apollo API returned status {response.status_code}: {response.text}")
    try:
        data = response.json()
    except Exception as exc:
        raise Exception(f"Failed to parse Apollo JSON response: {exc}")
    return data


def perform_search(query: str, max_results: int = 5) -> list[Tuple[str, str]]:
    """
    Perform a DuckDuckGo search for the given query and return a list of
    (title, url) tuples for the top results.  DuckDuckGo's HTML
    interface returns simplified search results that we can parse
    without JavaScript.

    Parameters
    ----------
    query : str
        The search query string.
    max_results : int, optional
        Maximum number of search results to return (default is 5).

    Returns
    -------
    list of tuple
        A list of (title, url) tuples.  The list may be shorter
        than `max_results` if fewer results are found.
    """
    url = SEARCH_URL.format(query=requests.utils.quote(query))
    headers = {
        # Provide a modern User‑Agent to avoid being blocked by DuckDuckGo.
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/110.0",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
    except Exception as exc:
        print(f"[Warning] Search request failed for '{query}': {exc}")
        return []
    if not resp.ok:
        print(f"[Warning] Search request returned status {resp.status_code} for '{query}'")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    # DuckDuckGo results are contained in <a class="result__a"> elements.
    for link in soup.find_all("a", class_="result__a", limit=max_results):
        title = link.get_text(strip=True)
        href = link.get("href")
        if href:
            results.append((title, href))
    return results


def extract_domain(url: str) -> Optional[str]:
    """
    Extract the domain part of a URL.  Strips protocol and subpath.

    Parameters
    ----------
    url : str
        Full URL.

    Returns
    -------
    str or None
        Domain (e.g. "example.com") or None if not found.
    """
    match = re.search(r"https?://([^/]+)", url)
    if match:
        return match.group(1)
    return None


def find_email_in_page(url: str, max_chars: int = 100000) -> Optional[str]:
    """
    Fetch the given URL and search for an email address in its HTML
    content.  Limits the download to `max_chars` characters to avoid
    downloading very large pages.

    Parameters
    ----------
    url : str
        URL to fetch.
    max_chars : int, optional
        Maximum number of characters to consider when scanning the
        response body.

    Returns
    -------
    str or None
        First email address found in the page, or None if none are
        found.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
    }
    try:
        response = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    except Exception as exc:
        print(f"[Warning] Failed to fetch {url}: {exc}")
        return None
    if not response.ok:
        return None
    text = response.text[:max_chars]
    # Simple regex to capture emails.
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    return emails[0] if emails else None


@dataclass
class CompanyRecord:
    name: str
    area: str
    phone: Optional[str]
    email: Optional[str]
    website: Optional[str]


def enrich_record(record: CompanyRecord, delay: float = 0.0) -> CompanyRecord:
    """
    Enrich a single company record by searching for website and email
    information if missing.  Returns an updated CompanyRecord.

    Parameters
    ----------
    record : CompanyRecord
        The record to enrich.
    delay : float
        Number of seconds to wait between network requests to avoid
        triggering rate limits.  Default is 0.0 seconds.

    Returns
    -------
    CompanyRecord
        Updated record with website and email fields potentially filled
        in.
    """
    """
    Enrich a company record by attempting to fill missing website and email
    fields.  The enrichment process works in two phases:

    1. If an Apollo API key is available and the record is missing a
       website, call the Organization Enrichment endpoint to retrieve
       the company's primary domain or website URL【200967481361746†L31-L90】.  This
       consumes Apollo credits and requires a valid API key.
    2. If the website is still unknown, perform a web search via
       DuckDuckGo and heuristically select the best candidate domain.
    3. If the email is missing, attempt to scrape the website for an
       email address.

    Parameters
    ----------
    record : CompanyRecord
        The company record to enrich.
    delay : float
        Number of seconds to wait between network requests.  Useful for
        rate limiting search and scraping.

    Returns
    -------
    CompanyRecord
        An updated record with website and/or email fields filled in.
    """
    # Short-circuit if both website and email are already present.
    if record.website and record.email:
        return record

    # Phase 1: Attempt to use Apollo API for missing website.
    website_url: Optional[str] = record.website
    # Only attempt to enrich via Apollo if the website is missing and
    # an API key is provided via environment.
    apollo_api_key = os.getenv(APOLLO_API_KEY_ENV)
    if not website_url and apollo_api_key:
        try:
            enriched_data = apollo_organization_enrich(
                company_name=record.name,
                domain=None,
                website=None,
                linkedin_url=None,
                api_key=apollo_api_key,
            )
            # The API returns a nested object; website_url is one of the
            # returned fields【200967481361746†L31-L90】.  Prefer `website_url`, but
            # fallback to `primary_domain` if present.
            organization = enriched_data.get("organization", {}) if isinstance(enriched_data, dict) else {}
            website_url = organization.get("website_url") or organization.get("primary_domain")
        except Exception as e:
            # Print a warning but continue gracefully.
            print(f"[Warning] Apollo enrichment failed for {record.name}: {e}")

    # Phase 2: If website is still unknown, perform web search.
    if not website_url:
        query = f"{record.name} {record.area}"
        results = perform_search(query, max_results=5)
        for _, link in results:
            domain = extract_domain(link)
            if not domain:
                continue
            # Skip common directory listings or social sites.
            if any(domain.endswith(bad) for bad in ("facebook.com", "instagram.com", "linkedin.com", "yelp.com", "yellowpages.ie")):
                continue
            website_url = link
            break

    # Phase 3: Attempt to find an email address from the website.
    email: Optional[str] = record.email
    if website_url and not record.email:
        if delay:
            time.sleep(delay)
        possible_email = find_email_in_page(website_url)
        if possible_email:
            email = possible_email

    return CompanyRecord(
        name=record.name,
        area=record.area,
        phone=record.phone,
        email=email,
        website=website_url,
    )


def enrich_csv(input_file: str, output_file: str, delay: float = 0.0) -> None:
    """
    Read a CSV file, enrich missing contact information, and write the
    results to a new CSV file.  The CSV should contain at least the
    columns 'Company Name', 'Area', 'Phone', 'Email', and 'Website'.

    Parameters
    ----------
    input_file : str
        Path to the input CSV file.
    output_file : str
        Path to write the enriched CSV file.
    delay : float, optional
        Number of seconds to wait between network requests.
    """
    df = pd.read_csv(input_file)
    updated_records = []
    for _, row in df.iterrows():
        company = CompanyRecord(
            name=str(row.get("Company Name", "")),
            area=str(row.get("Area", "")),
            phone=str(row.get("Phone", "")) if pd.notna(row.get("Phone")) else None,
            email=str(row.get("Email", "")) if pd.notna(row.get("Email")) and row.get("Email") != "" else None,
            website=str(row.get("Website", "")) if pd.notna(row.get("Website")) and row.get("Website") != "" else None,
        )
        # Only enrich rows missing either email or website.
        if not company.email or not company.website:
            enriched = enrich_record(company, delay=delay)
        else:
            enriched = company
        # Update DataFrame row.
        updated_records.append({
            **row.to_dict(),
            "Email": enriched.email or row.get("Email"),
            "Website": enriched.website or row.get("Website"),
        })
    enriched_df = pd.DataFrame(updated_records)
    enriched_df.to_csv(output_file, index=False)
    print(f"Enrichment completed. Output written to {output_file}")


def main():
    parser = argparse.ArgumentParser(description="Enrich CRM CSV with contact information.")
    parser.add_argument("--input-file", required=True, help="Path to input CRM CSV file")
    parser.add_argument("--output-file", required=True, help="Path to output enriched CSV file")
    parser.add_argument("--delay", type=float, default=0.0, help="Optional delay between network requests (seconds)")
    args = parser.parse_args()
    enrich_csv(args.input_file, args.output_file, delay=args.delay)


if __name__ == "__main__":
    main()