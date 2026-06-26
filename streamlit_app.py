"""
CSV Genie Streamlit app.

Run locally with:
    python -m streamlit run streamlit_app.py
"""

from __future__ import annotations

import os
from io import StringIO

import pandas as pd
import streamlit as st

from crm_enrichment_tool import (
    AUDIT_COLUMNS,
    audit_columns_only,
    crm_import_columns_only,
    enrich_dataframe,
)


st.set_page_config(page_title="CSV Genie", layout="wide")

st.title("CSV Genie")
st.caption("Verified CRM import enrichment for missing websites, phone numbers and emails.")

with st.sidebar:
    st.header("Settings")
    mode_label = st.radio(
        "Run mode",
        ["Preview only - do not change CRM fields", "Verified-only export - fill high-confidence blanks"],
        index=0,
    )
    mode = "preview" if mode_label.startswith("Preview") else "verified_only"

    row_limit = st.number_input(
        "Rows to research",
        min_value=1,
        max_value=500,
        value=5,
        step=1,
        help="This limits researched rows only. The exported CSV still keeps all original rows.",
    )

    min_confidence = st.slider(
        "Minimum confidence for verified-only fill",
        min_value=0.50,
        max_value=1.00,
        value=0.80,
        step=0.05,
    )

    delay = st.number_input(
        "Delay between web requests (seconds)",
        min_value=0.0,
        max_value=10.0,
        value=0.0,
        step=0.5,
    )

    st.subheader("Fields to research")
    target_website = st.checkbox("Website", value=True)
    target_phone = st.checkbox("Phone", value=True)
    target_email = st.checkbox("Email", value=False, help="Email is optional because many Irish SMEs use forms or hide email addresses.")
    target_fields = []
    if target_website:
        target_fields.append("Website")
    if target_phone:
        target_fields.append("Phone")
    if target_email:
        target_fields.append("Email")

    st.subheader("Apollo")
    use_apollo = st.checkbox(
        "Use Apollo API",
        value=False,
        help="Disabled by default to protect credits. Enable only when you want Apollo lookups.",
    )
    apollo_key_input = st.text_input(
        "Apollo API key for this session only",
        type="password",
        value="",
        help="Optional. Leave blank to use APOLLO_API_KEY from .env or environment variables.",
    )
    env_key_exists = bool(os.getenv("APOLLO_API_KEY"))
    if env_key_exists:
        st.caption("APOLLO_API_KEY found in environment/.env")
    else:
        st.caption("No APOLLO_API_KEY found. Apollo will be skipped unless you paste a key above.")

uploaded_file = st.file_uploader("Upload a CRM import CSV", type=["csv"])

st.info(
    "Recommended first test: Preview only, 5 rows, Apollo off, Website + Phone only. "
    "Do not import a CSV until the audit table looks correct."
)

if uploaded_file is not None:
    df = pd.read_csv(uploaded_file)

    st.subheader("Uploaded file preview")
    st.dataframe(df.head(10), use_container_width=True)

    missing_cols = [col for col in ["Company Name", "Area", "Phone", "Email", "Website"] if col not in df.columns]
    if missing_cols:
        st.error(f"Missing required columns: {', '.join(missing_cols)}")
        st.stop()

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Missing websites", int((df["Website"].isna() | (df["Website"].astype(str).str.strip() == "")).sum()))
    with col2:
        st.metric("Missing phones", int((df["Phone"].isna() | (df["Phone"].astype(str).str.strip() == "")).sum()))
    with col3:
        st.metric("Missing emails", int((df["Email"].isna() | (df["Email"].astype(str).str.strip() == "")).sum()))

    if not target_fields:
        st.warning("Select at least one field to research.")
        st.stop()

    if st.button("Run research", type="primary"):
        api_key = apollo_key_input.strip() or os.getenv("APOLLO_API_KEY")
        if use_apollo and not api_key:
            st.warning("Apollo is enabled but no API key is available. Apollo will be skipped for this run.")
            use_apollo_effective = False
        else:
            use_apollo_effective = use_apollo

        with st.spinner("Researching records. This may take a few minutes..."):
            result_df = enrich_dataframe(
                df,
                mode=mode,
                min_confidence=float(min_confidence),
                row_limit=int(row_limit),
                delay=float(delay),
                use_apollo=use_apollo_effective,
                apollo_api_key=api_key,
                target_fields=target_fields,
            )

        st.success("Research completed.")

        st.subheader("Audit review")
        audit_df = audit_columns_only(result_df)
        st.dataframe(audit_df, use_container_width=True)

        final_import_df = crm_import_columns_only(result_df)

        st.subheader("Download")
        st.download_button(
            "Download audit CSV with proposals",
            data=audit_df.to_csv(index=False).encode("utf-8"),
            file_name="csv_genie_audit.csv",
            mime="text/csv",
        )
        st.download_button(
            "Download CRM import CSV",
            data=final_import_df.to_csv(index=False).encode("utf-8"),
            file_name="csv_genie_crm_import.csv",
            mime="text/csv",
        )

        if mode == "preview":
            st.warning("Preview mode keeps CRM fields unchanged. Use the audit CSV to check proposals first.")
        else:
            st.warning("Verified-only mode fills only blank fields that meet the confidence threshold. Still review before importing.")
