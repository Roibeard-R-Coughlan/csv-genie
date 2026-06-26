"""
Streamlit app for CRM Data Enrichment
=====================================

This Streamlit application provides a simple web interface to the
CRM data enrichment tool.  It allows users to upload a CSV file,
configure enrichment parameters, and download the enriched data.

Usage
-----
Install dependencies via pip:

    pip install streamlit pandas requests beautifulsoup4 python-dotenv

Set your Apollo API key as an environment variable (see instructions
in the README or the main script).  Then run:

    streamlit run streamlit_app.py

In the browser, upload your CRM import CSV and click “Run
enrichment” to process the data.  Once complete, you can view the
enriched DataFrame and download the CSV.
"""

import os
import tempfile

import pandas as pd
import streamlit as st

from crm_enrichment_tool import enrich_csv


def main() -> None:
    st.set_page_config(page_title="CRM Data Enrichment Tool", layout="wide")
    st.title("CRM Data Enrichment Tool")
    st.markdown(
        """
        Upload a CRM import CSV file and let this tool fill missing
        website, phone, and email fields.  The enrichment uses both
        Apollo's Organization Enrichment API (if an API key is set)
        and a web search fallback.

        **Usage tips:**

        1. Set your Apollo API key as an environment variable
           `APOLLO_API_KEY` or store it in a `.env` file before
           running the app.
        2. Use the **Row limit** setting to test the workflow on a
           smaller subset of rows and conserve API credits.
        3. Adjust the **Delay between requests** to avoid hitting
           rate limits when scraping websites.
        """
    )

    uploaded_file = st.file_uploader(
        "Upload CRM Import CSV", type=["csv"], help="CSV file with columns such as Company Name, Area, Phone, Email, Website",
    )

    delay = st.number_input(
        "Delay between requests (seconds)", min_value=0.0, max_value=10.0, value=0.0, step=0.5,
        help="Wait this many seconds between network requests to avoid rate limits",
    )

    limit = st.number_input(
        "Row limit for testing", min_value=1, max_value=10000, value=50, step=1,
        help="Only process the first N rows; set a lower number during testing to conserve credits",
    )

    # Option to skip Apollo enrichment to conserve credits or when not needed
    use_apollo = st.checkbox(
        "Use Apollo API for organization enrichment",
        value=True,
        help="Uncheck to disable Apollo lookups and use only web search",
    )

    if uploaded_file is not None:
        # Show a preview of the uploaded data
        df = pd.read_csv(uploaded_file)
        st.subheader("Preview of uploaded data")
        st.dataframe(df.head(10))

        if st.button("Run enrichment"):
            with st.spinner("Running enrichment, please wait..."):
                # Write uploaded file to a temporary location
                with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp_in:
                    tmp_in.write(uploaded_file.getbuffer())
                    tmp_in_path = tmp_in.name
                # Create a temporary file for the output
                with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp_out:
                    tmp_out_path = tmp_out.name
                # Call enrich_csv with the temporary files
                enrich_csv(
                    tmp_in_path,
                    tmp_out_path,
                    delay=float(delay),
                    max_rows=int(limit),
                    skip_apollo=not use_apollo,
                )
                # Read the enriched data
                enriched_df = pd.read_csv(tmp_out_path)
                # Display results
                st.success("Enrichment completed!")
                st.subheader("Enriched Data (first 10 rows)")
                st.dataframe(enriched_df.head(10))
                # Provide download button
                st.download_button(
                    label="Download Enriched CSV",
                    data=enriched_df.to_csv(index=False).encode("utf-8"),
                    file_name="enriched_output.csv",
                    mime="text/csv",
                )


if __name__ == "__main__":
    main()