"""
CSV Genie Test Fixtures - Known Test Cases for Regression Testing

These test cases document expected behavior for challenging Irish business queries.
They serve as regression tests to verify that the tool correctly finds official websites
instead of directory/social/booking pages.

Key safety rules:
- Directory sites (cybo.com, phonebook.ie, page.tl, etc.) must NEVER become verified websites
- Only official SME domains should become verified proposals
- Location evidence (Galway, Ballinasloe, Barna, etc.) should boost confidence
- When multiple results exist, official sites should rank higher than directories
"""


class TestFixture:
    """Base class for a test case."""

    def __init__(
        self,
        company_name: str,
        area: str,
        expected_website_domain: str = None,
        reject_domains: list = None,
        notes: str = "",
    ):
        self.company_name = company_name
        self.area = area
        self.expected_website_domain = expected_website_domain
        self.reject_domains = reject_domains or []
        self.notes = notes

    def validate(self, result_row):
        """Validate enrichment result against fixture expectations."""
        issues = []

        # Check that no rejected domains appear as verified websites
        proposed_website = result_row.get("Proposed Website", "")
        if proposed_website:
            for rejected_domain in self.reject_domains:
                if rejected_domain in proposed_website.lower():
                    issues.append(
                        f"FAIL: {rejected_domain} should not be verified website"
                    )

        # Check that expected domain is found if specified
        if self.expected_website_domain and not proposed_website:
            issues.append(
                f"UNCERTAIN: Expected to find {self.expected_website_domain} but got no proposal"
            )
        elif (
            self.expected_website_domain
            and proposed_website
            and self.expected_website_domain not in proposed_website.lower()
        ):
            issues.append(
                f"UNCERTAIN: Expected {self.expected_website_domain}, got {proposed_website}"
            )

        return issues


# ============================================================================
# PHYSIO/HEALTHCARE TEST CASES
# ============================================================================

FIXTURES = [
    TestFixture(
        company_name="Action Physio",
        area="Ballinasloe, County Galway, Ireland",
        expected_website_domain="actionphysio.ie",
        reject_domains=["phonebook.ie", "cybo.com", "page.tl", "alltrack.org"],
        notes="Manual Google search quickly finds actionphysio.ie. CSVGenie should find official site, not directories.",
    ),
    TestFixture(
        company_name="Barna Physiotherapy",
        area="Barna, County Galway, Ireland",
        expected_website_domain="barna",  # Flexible - could be barna-physio.ie, barnaphysio.ie, etc.
        reject_domains=["phonebook.ie", "page.tl", "cybo.com", "mummypages.ie"],
        notes="Barna is a small village in Connemara. Official site should be found; reject all directories.",
    ),
    TestFixture(
        company_name="Ballinasloe Physiotherapy",
        area="Ballinasloe, County Galway, Ireland",
        reject_domains=["alltrack.org", "cybo.com", "phonebook.ie", "infosinfo-ie.com"],
        notes="Generic CSV name but may be listed under trading name in directories. Use rescue logic to find official site.",
    ),
    TestFixture(
        company_name="Galway Physio",
        area="Galway City, County Galway, Ireland",
        reject_domains=["phonebook.ie", "yell.com", "goldenpages.ie"],
        notes="Must not select unrelated businesses like 'Eden Massage', 'MummyPages'. Location matching is critical.",
    ),
    TestFixture(
        company_name="Advanced Physio West",
        area="Galway, County Galway, Ireland",
        expected_website_domain="advancedphysiowest.ie",
        reject_domains=["phonebook.ie", "cybo.com"],
        notes="If existing Website already contains advancedphysiowest.ie, do not re-propose the same URL.",
    ),
    # ========================================================================
    # DENTAL TEST CASES
    # ========================================================================
    TestFixture(
        company_name="Smile Dental Galway",
        area="Galway, County Galway, Ireland",
        reject_domains=["phonebook.ie", "treatwell.ie", "fresha.com"],
        notes="Dental practices often listed on booking sites. Must find official website, not Treatwell/Fresha.",
    ),
    # ========================================================================
    # ACCOUNTANT/PROFESSIONAL SERVICES
    # ========================================================================
    TestFixture(
        company_name="O'Donnell Accountants",
        area="Galway, County Galway, Ireland",
        reject_domains=["phonebook.ie", "cybo.com"],
        notes="Professional services should have official websites. Directories should be rejected.",
    ),
]


def get_fixture_for_company(company_name: str, area: str = None) -> TestFixture or None:
    """Find a fixture by company name."""
    for fixture in FIXTURES:
        if fixture.company_name.lower() == company_name.lower():
            if area is None or fixture.area.lower() == area.lower():
                return fixture
    return None


def validate_all_fixtures(result_df):
    """
    Validate all fixtures against a result dataframe.

    Returns a summary of validation results.
    """
    results = {
        "total_fixtures": len(FIXTURES),
        "passed": 0,
        "uncertain": 0,
        "failed": 0,
        "details": [],
    }

    for fixture in FIXTURES:
        # Find matching row in results
        matching_rows = result_df[
            (result_df["Company Name"].str.lower() == fixture.company_name.lower())
        ]

        if len(matching_rows) == 0:
            results["details"].append(
                {
                    "fixture": f"{fixture.company_name} ({fixture.area})",
                    "status": "NOT_FOUND",
                    "issues": ["No matching row in results"],
                }
            )
            results["uncertain"] += 1
            continue

        row = matching_rows.iloc[0]
        issues = fixture.validate(row)

        if not issues:
            results["passed"] += 1
            status = "PASS"
        elif any("FAIL" in issue for issue in issues):
            results["failed"] += 1
            status = "FAIL"
        else:
            results["uncertain"] += 1
            status = "UNCERTAIN"

        results["details"].append(
            {
                "fixture": f"{fixture.company_name} ({fixture.area})",
                "status": status,
                "issues": issues,
                "proposed_website": row.get("Proposed Website", ""),
                "website_confidence": row.get("Website Confidence", ""),
            }
        )

    return results


def print_validation_report(results: dict) -> None:
    """Print a formatted validation report."""
    print("\n" + "=" * 80)
    print("TEST FIXTURE VALIDATION REPORT")
    print("=" * 80)
    print(f"Total Fixtures:  {results['total_fixtures']}")
    print(f"Passed:          {results['passed']}")
    print(f"Uncertain:       {results['uncertain']}")
    print(f"Failed:          {results['failed']}")
    print("=" * 80 + "\n")

    for detail in results["details"]:
        print(f"[{detail['status']}] {detail['fixture']}")
        if detail["proposed_website"]:
            print(f"      Website: {detail['proposed_website']}")
            print(f"      Confidence: {detail['website_confidence']}")
        for issue in detail["issues"]:
            print(f"      → {issue}")
        print()

    # Summary
    if results["failed"] > 0:
        print(
            f"\n[FAIL] {results['failed']} test(s) FAILED - Directory sites found as verified websites"
        )
    elif results["uncertain"] > 0:
        print(f"\n[WARN] {results['uncertain']} test(s) UNCERTAIN - Manual review needed")
    else:
        print(f"\n[OK] All {results['passed']} fixtures PASSED - No directory sites verified")
