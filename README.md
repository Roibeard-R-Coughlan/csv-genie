# CSV Genie

CSV Genie is a local Streamlit app for safely researching and enriching CRM import CSV files.

## What it does

- Preserves existing CRM data.
- Researches missing Website, Phone and optionally Email fields.
- Shows proposed values with confidence and source URLs.
- Exports an audit CSV and a CRM import CSV.
- Keeps all original rows even when you only research a small test limit.
- Apollo is optional and disabled by default to protect credits.

## Setup

```powershell
cd C:\github-repo\csv-genie
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

## Optional Apollo key

Create a `.env` file in the project folder:

```text
APOLLO_API_KEY=your_new_key_here
```

Do not commit `.env` to GitHub. It is included in `.gitignore`.

## Recommended first test

1. Run mode: Preview only.
2. Rows to research: 5.
3. Apollo: Off.
4. Fields: Website + Phone only.
5. Review the audit table before downloading/importing anything.

## CLI example

```powershell
python crm_enrichment_tool.py --input-file leads.csv --output-file audit.csv --limit 5 --mode preview
python crm_enrichment_tool.py --input-file leads.csv --output-file verified.csv --limit 20 --mode verified_only --min-confidence 0.8
```
