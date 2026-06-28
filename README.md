# CSV Genie

Safe CRM import enrichment for local-business lead lists.

## Run

```powershell
cd C:\github-repo\csv-genie
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

## Recommended workflow

1. Run **Preview only** first.
2. Use **Website only** before Phone or Email.
3. Keep **Delay between web requests** at `1.00`.
4. For local Galway businesses, use **SerpAPI Google results** when a `SERPAPI_API_KEY` is available.
5. Keep **Search location bias** set to `Galway, County Galway, Ireland`.
6. Review the audit CSV before using any CRM import export.

## v4.4 changes

- Added SerpAPI location bias so Google-style searches are centred on Galway/Ireland.
- Added `phonebook.ie`, `page.tl`, `reviewbritain.com`, and `iscp.ie` to candidate-only handling.
- Directory, booking, social and free-hosting pages are no longer treated as verified official websites.
- Email proposals are verified only when found on an existing/proposed official website or contact page.
- Query wording is now closer to a human Google search, using exact business name and location.

## API keys

Put optional keys in `.env`:

```env
SERPAPI_API_KEY=your_serpapi_key_here
APOLLO_API_KEY=your_apollo_key_here
```

Apollo remains off by default. For small local Irish businesses, SerpAPI is usually the better fit.

## Phone numbers and Excel

CSV Genie reads and writes CSVs with phone columns as text, but Excel may still strip leading zeros if you manually type phone numbers into a CSV. Avoid manual phone entry in Excel where possible. Import the CSV directly into the CRM, or use Excel's import workflow and set Phone as Text.

## Larger runs

For Streamlit reliability, keep batches small while testing:

- Website: 5–16 rows
- Phone: 5–10 rows
- Email: 2–5 rows

A future background/CLI runner can save progress after every row so long runs do not depend on the browser session staying awake.
