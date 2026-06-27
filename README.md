# CSV Genie

CSV Genie is a local Streamlit tool for safely enriching CRM import CSV files.

The app is designed for cautious CRM research:

- Existing CRM fields are never overwritten in Preview mode.
- Row limits only limit the number of researched rows; exports keep all original rows.
- Proposed values are written to audit columns first.
- Candidate values are shown for manual review.
- Apollo is disabled by default to protect API credits.

## Recommended test settings

Use these settings first:

```text
Run mode: Preview only - do not change CRM fields
Rows to research: 10 or 25
Delay between web requests: 1.00
Fields: Website + Phone
Apollo: Off
Show only researched rows / candidates: On
Show candidate match columns: On
```

Download the audit CSV first. Do not import the CRM CSV until the audit values have been checked.

## v4 safety improvements

This version adds four quality/safety fixes:

1. **Last run settings / stale result protection**
   - The app records the settings used for the most recent run.
   - If you change important sidebar settings after a run, the app warns that the visible table/downloads are stale.
   - Downloads are disabled until you run the research again or clear old results.

2. **Suspicious phone rejection**
   - Obvious junk numbers such as `06666666666`, repeated digits, bad lengths and sequential placeholder numbers are rejected.
   - Phone numbers are normalised to Irish-style national format where possible, for example `+353 87...` becomes `087...`.

3. **Verified phone rule**
   - Phone numbers are only treated as verified proposals when found on the proposed/existing official website or its contact page.
   - Phone numbers from search snippets, Apollo, directories or non-official pages remain candidate-only.

4. **Directory/social/booking pages stay candidate-only**
   - Pages from Fresha, Facebook, Instagram, Golden Pages, Infobel, Google Maps and similar platforms are not treated as verified business websites.
   - They can still appear as candidates for manual review.

## Install / run

```powershell
cd C:\github-repo\csv-genie
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

## Which CSV should I use?

- **Preview mode:** use the audit CSV only. CRM import download is disabled.
- **Verified-only mode:** use the audit CSV first, then use the CRM import CSV only if the proposed values look correct.


## v4.1 runtime stability fix

- Adds a progress bar during research so long runs do not look frozen.
- Reduces direct-domain timeout and limits direct-domain guesses so 25-row tests finish faster.
- Catches per-row research errors and writes them into `Enrichment Notes` instead of stopping the whole app.
- CSV downloads appear after the progress bar reaches completion.


## v4.2 update

- Delay between web requests now defaults to 1.00 seconds because testing showed more reliable candidate discovery than 0.00.
- Apollo remains optional and off by default.
