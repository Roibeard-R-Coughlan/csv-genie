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

## v4.5 changes (Strict Verification)

**Major safety improvement: False verification prevention.**

- Added `verify_official_website()` function enforcing 8 safety rules
- All search/domain results are candidates first; only strict verifier promotes to verified proposals
- Directory/forum/town sites (phonebook.ie, page.tl, ballinasloe.ie, etc.) are candidate-only
- Generic token matches (physio, clinic, galway, west) alone are rejected
- Result pages (/privacy-policy/, /directory/, Reddit, forum posts) stay candidate-only
- Added "Rejected Reason" column in audit output for every candidate
- SerpAPI fallback improved with explicit error detection
- Phone and email are always candidates; website requires proof of official identity

**Safety guarantee:** No directory or unrelated business will be verified as an official website unless:
- The page title or visible text clearly shows the full company name or very close trading-name match
- The domain structurally supports the business identity
- It's not a town/general portal or result page

Examples of safe rejections:
- voicefleet.ai will NOT be verified for Marmion Sports Injury Clinic
- foot.ie will NOT be verified for RD Athletic Therapy
- ballinasloe.ie will NOT be verified (it's a town portal, not clinic domain)
- Fresha/Treatwell/Yelp results will stay candidate-only

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

