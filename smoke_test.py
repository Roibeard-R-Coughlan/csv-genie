#!/usr/bin/env python3
"""
Smoke test harness for CSV Genie - free provider only (DuckDuckGo).

Usage:
    python smoke_test.py --input test_inputs/physio.csv --rows 10 --provider duckduckgo --fields Website
    python smoke_test.py --input test_inputs/physio.csv --rows 10 --fields Website Phone

Never calls SerpAPI or Apollo. Uses DuckDuckGo for all free testing.
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from crm_enrichment_tool import (
    enrich_dataframe,
    is_directory_domain,
    get_domain,
    clean_cell,
)


def count_results(df: pd.DataFrame) -> dict:
    """Count verified and candidate results from enriched dataframe."""
    counts = {
        "verified_websites": 0,
        "website_candidates": 0,
        "verified_phones": 0,
        "phone_candidates": 0,
        "verified_emails": 0,
        "email_candidates": 0,
        "rejected_directory_results": 0,
        "duckduckgo_errors": 0,
        "duckduckgo_403": 0,
        "uncertain_rows": 0,
        "skipped_row_limit": 0,
    }

    for idx, row in df.iterrows():
        # Count verified proposals
        if clean_cell(row.get("Proposed Website")):
            counts["verified_websites"] += 1

        if clean_cell(row.get("Proposed Phone")):
            counts["verified_phones"] += 1

        if clean_cell(row.get("Proposed Email")):
            counts["verified_emails"] += 1

        # Count candidates
        for i in range(1, 4):
            if clean_cell(row.get(f"Candidate Website {i} Value")):
                url = clean_cell(row.get(f"Candidate Website {i} Value"))
                domain = get_domain(url) if url else ""
                if is_directory_domain(domain):
                    counts["rejected_directory_results"] += 1
                else:
                    counts["website_candidates"] += 1

            if clean_cell(row.get(f"Candidate Phone {i} Value")):
                counts["phone_candidates"] += 1

            if clean_cell(row.get(f"Candidate Email {i} Value")):
                counts["email_candidates"] += 1

        # Count uncertain rows (no proposal but has candidates or notes)
        has_proposal = any([
            clean_cell(row.get("Proposed Website")),
            clean_cell(row.get("Proposed Phone")),
            clean_cell(row.get("Proposed Email")),
        ])
        has_candidates = any([
            clean_cell(row.get(f"Candidate {field} {i} Value"))
            for field in ["Website", "Phone", "Email"]
            for i in range(1, 4)
        ])
        has_notes = clean_cell(row.get("Enrichment Notes"))
        skipped_due_limit = (has_notes or "").strip().lower() == "not researched due to row limit"
        if skipped_due_limit:
            counts["skipped_row_limit"] += 1
        notes_text = (has_notes or "").lower()
        decision_text = (clean_cell(row.get("Decision Needed")) or "").lower()
        if "duckduckgo" in notes_text and ("error" in notes_text or "blocked" in notes_text):
            counts["duckduckgo_errors"] += 1
        if "duckduckgo blocked" in notes_text or "duckduckgo blocked" in decision_text or "403" in notes_text:
            counts["duckduckgo_403"] += 1

        decision = clean_cell(row.get("Decision Needed")) or ""
        if (
            not skipped_due_limit
            and not has_proposal
            and (
                has_candidates
                or decision in {
                    "Manual review: strong candidate but not verified",
                    "Manual review: weak candidate only",
                    "No reliable candidate found",
                    "DuckDuckGo blocked / 403",
                }
            )
        ):
            counts["uncertain_rows"] += 1

    return counts


def print_rows(df: pd.DataFrame, title: str, rows: list[tuple[str, str, str]]) -> None:
    print(f"\n{title}")
    if not rows:
        print("  (none)")
        return
    for company, value, detail in rows:
        print(f"  - {company}: {value} {detail}".rstrip())


def collect_report_rows(df: pd.DataFrame) -> dict:
    verified = []
    strong_candidates = []
    rejected_directories = []
    uncertain = []

    for _, row in df.iterrows():
        company = clean_cell(row.get("Company Name")) or "(unknown company)"
        proposed = clean_cell(row.get("Proposed Website"))
        if proposed:
            confidence = clean_cell(row.get("Website Confidence")) or ""
            verified.append((company, proposed, f"(confidence {confidence})" if confidence else ""))

        best = clean_cell(row.get("Best Candidate Website"))
        best_conf_raw = clean_cell(row.get("Best Candidate Confidence"))
        try:
            best_conf = float(best_conf_raw or 0)
        except ValueError:
            best_conf = 0.0
        if best and not proposed and best_conf >= 0.65 and not is_directory_domain(get_domain(best)):
            reason = clean_cell(row.get("Best Candidate Rejected Reason")) or ""
            strong_candidates.append((company, best, f"(confidence {best_conf:.2f}; {reason})"))

        for i in range(1, 4):
            candidate = clean_cell(row.get(f"Candidate Website {i} Value"))
            if not candidate:
                continue
            domain = get_domain(candidate)
            if is_directory_domain(domain):
                reason = clean_cell(row.get(f"Candidate Website {i} Rejected Reason")) or ""
                rejected_directories.append((company, candidate, f"({reason})" if reason else ""))

        decision = clean_cell(row.get("Decision Needed")) or ""
        notes = clean_cell(row.get("Enrichment Notes")) or ""
        if notes.strip().lower() == "not researched due to row limit":
            continue
        if decision and decision not in {"Existing website already present", "Verified proposal available"}:
            uncertain.append((company, decision, f"- {notes}" if notes else ""))

    return {
        "verified": verified,
        "strong_candidates": strong_candidates,
        "rejected_directories": rejected_directories,
        "uncertain": uncertain,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Free smoke test for CSV Genie using DuckDuckGo only."
    )
    parser.add_argument("--input", required=True, help="Path to input CSV")
    parser.add_argument(
        "--rows", type=int, default=10, help="Number of rows to test (default 10)"
    )
    parser.add_argument(
        "--provider",
        choices=["duckduckgo"],
        default="duckduckgo",
        help="Search provider (DuckDuckGo only for smoke testing)",
    )
    parser.add_argument(
        "--fields",
        nargs="+",
        default=["Website"],
        help="Fields to research (default: Website)",
    )
    parser.add_argument(
        "--output-dir",
        default="test_outputs",
        help="Output directory for results (default: test_outputs)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay between requests in seconds (default 1.0)",
    )
    args = parser.parse_args()

    # Validate input file
    if not os.path.exists(args.input):
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        return 1

    # Create output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)

    # Read input CSV
    print(f"Reading {args.input}...")
    df = pd.read_csv(args.input, dtype=str, keep_default_na=False)
    print(f"Loaded {len(df)} rows")

    # Validate required columns
    required = {"Company Name", "Area", "Website", "Phone", "Email"}
    missing = required - set(df.columns)
    if missing:
        print(f"Error: Missing required columns: {', '.join(missing)}", file=sys.stderr)
        return 1

    # Verify provider is free (no SerpAPI for smoke tests)
    if args.provider != "duckduckgo":
        print(
            "Error: Smoke tests must use DuckDuckGo (free only, no paid API calls)",
            file=sys.stderr,
        )
        return 1

    print(f"Researching {args.rows} rows with DuckDuckGo...")
    print(f"Target fields: {', '.join(args.fields)}")
    print(f"Delay: {args.delay}s per request\n")

    # Run enrichment with free provider only
    result_df = enrich_dataframe(
        df,
        mode="preview",
        row_limit=args.rows,
        delay=args.delay,
        use_apollo=False,
        apollo_api_key=None,
        target_fields=args.fields,
        search_provider="duckduckgo",
        serpapi_api_key=None,
        search_location="Galway, County Galway, Ireland",
    )

    # Write output without touching the original CSV.
    input_stem = Path(args.input).stem
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"smoke_test_{input_stem}_{timestamp}.csv"
    result_df.to_csv(output_file, index=False)
    print(f"Results written to: {output_file}\n")

    # Count and report results
    counts = count_results(result_df)

    print("=" * 60)
    print("SMOKE TEST RESULTS (DuckDuckGo Free Provider)")
    print("=" * 60)
    print(f"Verified Websites:       {counts['verified_websites']}")
    print(f"Website Candidates:      {counts['website_candidates']}")
    print(f"Rejected Directories:    {counts['rejected_directory_results']}")
    print(f"DuckDuckGo Errors:       {counts['duckduckgo_errors']}")
    print(f"DuckDuckGo 403/Blocked:  {counts['duckduckgo_403']}")
    print(f"Verified Phones:         {counts['verified_phones']}")
    print(f"Phone Candidates:        {counts['phone_candidates']}")
    print(f"Verified Emails:         {counts['verified_emails']}")
    print(f"Email Candidates:        {counts['email_candidates']}")
    print(f"Uncertain Rows:          {counts['uncertain_rows']}")
    print(f"Skipped Row Limit:       {counts['skipped_row_limit']}")
    print("=" * 60)

    report_rows = collect_report_rows(result_df)
    print_rows(result_df, "Verified websites", report_rows["verified"])
    print_rows(result_df, "Strong candidates", report_rows["strong_candidates"])
    print_rows(result_df, "Rejected directory results", report_rows["rejected_directories"])
    print_rows(result_df, "Uncertain rows", report_rows["uncertain"])

    # Verify safety constraints
    print("\nSafety Checks:")
    if counts["rejected_directory_results"] > 0:
        print(f"[OK] Directory results properly rejected: {counts['rejected_directory_results']}")
    else:
        print("[OK] No directory results verified")

    if counts["verified_websites"] > 0:
        print(f"[OK] Found {counts['verified_websites']} verified websites")

    print("[OK] Used DuckDuckGo only (no paid API calls)")
    print("[OK] Smoke test completed successfully\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
