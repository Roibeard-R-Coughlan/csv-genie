"""
CSV Genie Streamlit app.

Run locally with:
    python -m streamlit run streamlit_app.py
"""

from __future__ import annotations

import os

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
st.caption("Verified CRM import enrichment with candidate review for missing websites, phone numbers and emails.")

# Keep results alive after download clicks/reruns.
if "result_df" not in st.session_state:
    st.session_state.result_df = None
if "source_file_name" not in st.session_state:
    st.session_state.source_file_name = None
if "last_mode" not in st.session_state:
    st.session_state.last_mode = "preview"

with st.sidebar:
    st.header("Settings")
    mode_label = st.radio(
        "Run mode",
        [
            "Preview only - do not change CRM fields",
            "Verified-only export - fill high-confidence blanks",
        ],
        index=0,
    )
    mode = "preview" if mode_label.startswith("Preview") else "verified_only"

    row_limit = st.number_input(
        "Rows to research",
        min_value=1,
        max_value=500,
        value=10,
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

    st.subheader("Candidate review")
    show_review_rows_only = st.checkbox(
        "Show only researched rows / candidates",
        value=True,
        help="Keeps the audit table focused on rows with proposals, candidate matches or research notes.",
    )
    show_candidate_columns = st.checkbox(
        "Show candidate match columns",
        value=True,
        help="Shows possible matches even when they are not verified enough to auto-fill CRM fields.",
    )

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
    "Recommended test: Preview only, 10 rows, Apollo off, Website + Phone only. "
    "Use the audit CSV for review. Use the CRM import CSV only after verified-only mode shows good changes."
)


def is_blank_series(series: pd.Series) -> pd.Series:
    return series.isna() | (series.astype(str).str.strip() == "") | (series.astype(str).str.lower().isin(["nan", "none", "null"]))


def build_visible_audit(audit_df: pd.DataFrame, *, review_rows_only: bool, candidate_columns: bool) -> pd.DataFrame:
    visible = audit_df.copy()

    if not candidate_columns:
        visible = visible[[col for col in visible.columns if not col.startswith("Candidate ")]]

    if review_rows_only:
        signal_cols = [
            col
            for col in visible.columns
            if col.startswith("Proposed ")
            or col.startswith("Candidate ")
            or col == "Enrichment Notes"
        ]
        if signal_cols:
            mask = pd.Series(False, index=visible.index)
            for col in signal_cols:
                mask = mask | ~is_blank_series(visible[col])
            visible = visible[mask]
    return visible


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
        st.metric("Missing websites", int(is_blank_series(df["Website"]).sum()))
    with col2:
        st.metric("Missing phones", int(is_blank_series(df["Phone"]).sum()))
    with col3:
        st.metric("Missing emails", int(is_blank_series(df["Email"]).sum()))

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

        st.session_state.result_df = result_df
        st.session_state.source_file_name = uploaded_file.name
        st.session_state.last_mode = mode
        st.success("Research completed.")

if st.session_state.result_df is not None:
    result_df = st.session_state.result_df
    audit_df = audit_columns_only(result_df)
    final_import_df = crm_import_columns_only(result_df)
    visible_audit = build_visible_audit(
        audit_df,
        review_rows_only=show_review_rows_only,
        candidate_columns=show_candidate_columns,
    )

    proposed_website_count = 0 if "Proposed Website" not in audit_df.columns else int((~is_blank_series(audit_df["Proposed Website"])).sum())
    candidate_website_count = int(
        sum((~is_blank_series(audit_df[col])).sum() for col in audit_df.columns if col.startswith("Candidate Website") and col.endswith("Value"))
    )
    candidate_phone_count = int(
        sum((~is_blank_series(audit_df[col])).sum() for col in audit_df.columns if col.startswith("Candidate Phone") and col.endswith("Value"))
    )

    st.subheader("Audit review")
    c1, c2, c3 = st.columns(3)
    c1.metric("Verified website proposals", proposed_website_count)
    c2.metric("Website candidates", candidate_website_count)
    c3.metric("Phone candidates", candidate_phone_count)

    if visible_audit.empty:
        st.warning("No proposals, candidates or research notes are visible with the current filters. Untick the sidebar filter to show all rows.")
    else:
        st.dataframe(visible_audit, use_container_width=True)

    st.subheader("Download")
    st.download_button(
        "Download audit CSV for review - not for CRM import",
        data=audit_df.to_csv(index=False).encode("utf-8"),
        file_name="csv_genie_audit.csv",
        mime="text/csv",
        key="download_audit_csv",
    )

    if st.session_state.last_mode == "preview":
        st.warning("Preview mode keeps CRM fields unchanged. The audit CSV is the useful file at this stage; the CRM import CSV is disabled to avoid importing unchanged data by mistake.")
        st.download_button(
            "Download CRM import CSV - disabled in Preview mode",
            data=final_import_df.to_csv(index=False).encode("utf-8"),
            file_name="csv_genie_crm_import_preview_unchanged.csv",
            mime="text/csv",
            disabled=True,
            key="download_crm_disabled",
        )
    else:
        st.warning("Verified-only mode fills only blank fields that meet the confidence threshold. Still review the audit CSV before importing.")
        st.download_button(
            "Download CRM import CSV for website import",
            data=final_import_df.to_csv(index=False).encode("utf-8"),
            file_name="csv_genie_crm_import.csv",
            mime="text/csv",
            key="download_crm_csv",
        )
