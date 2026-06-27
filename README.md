# CSV Genie

CSV Genie is a local Streamlit tool for safely researching missing Website, Phone and Email values in CRM import CSV files.

## Safety rules

- Existing CRM values are never overwritten.
- Preview mode never changes CRM import fields.
- Verified-only mode fills only blank fields that meet the confidence threshold.
- Candidate Review shows possible matches for manual checking, even when they are not verified enough to auto-fill.
- Apollo is disabled by default to avoid accidental API credit usage.

## Run locally

```powershell
cd C:\github-repo\csv-genie
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

## Recommended test settings

Use these first:

- Run mode: `Preview only - do not change CRM fields`
- Rows to research: `10`
- Apollo: Off
- Fields: Website + Phone
- Candidate Review: On

## Which file to download

In Preview mode, download:

- `Download audit CSV for review - not for CRM import`

Do not import the Preview CRM file into the website because it is intentionally unchanged.

When the audit CSV shows good verified proposals, rerun with:

- `Verified-only export - fill high-confidence blanks`

Then download:

- `Download CRM import CSV for website import`

Still review the audit file before uploading anything to the CRM website.

## v3 Candidate Review Improvement

This version adds a direct-domain fallback for Candidate Review Mode. If search results do not return usable candidates, CSV Genie now checks likely official domains such as `companyname.ie`, `companyname.com`, `companyphysio.ie`, and similar conservative variants. Values are still written to audit/proposal columns first; Preview mode does not change CRM import fields.

Recommended test settings:
- Preview only
- Rows to research: 10
- Apollo off
- Website only first
- Then Website + Phone
- Show candidate match columns on

