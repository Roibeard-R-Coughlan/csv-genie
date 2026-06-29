"""
CSV Genie Streamlit app.

Run locally with:
    python -m streamlit run streamlit_app.py
"""

from __future__ import annotations

import csv
import os

import pandas as pd
import streamlit as st

from crm_enrichment_tool import (
    AUDIT_COLUMNS,
    BRAVE_SEARCH_API_KEY_ENV,
    DEFAULT_SEARCH_LOCATION,
    SERPAPI_API_KEY_ENV,
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
if "last_run_settings" not in st.session_state:
    st.session_state.last_run_settings = None

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
        value=1.0,
        step=0.5,
        help="Default is 1.00 because testing showed it gives more reliable results than 0.00.",
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

    st.subheader("Search provider")
    search_provider_label = st.selectbox(
        "Web search backend",
        [
            "Brave Search API - recommended",
            "DuckDuckGo - free",
            "SerpAPI Google results - optional paid",
        ],
        index=0,
        help="Brave is recommended when BRAVE_SEARCH_API_KEY is available. DuckDuckGo is the free fallback. SerpAPI is optional paid.",
    )
    if search_provider_label.startswith("Brave"):
        search_provider = "brave"
    elif search_provider_label.startswith("SerpAPI"):
        search_provider = "serpapi"
    else:
        search_provider = "duckduckgo"
    brave_env_key_exists = bool(os.getenv(BRAVE_SEARCH_API_KEY_ENV))
    if brave_env_key_exists:
        st.caption("BRAVE_SEARCH_API_KEY found in environment/.env")
    else:
        st.warning("Brave Search API selected without BRAVE_SEARCH_API_KEY will fall back to DuckDuckGo.")
    serpapi_key_input = st.text_input(
        "SerpAPI key for this session only",
        type="password",
        value="",
        help="Optional. Leave blank to use SERPAPI_API_KEY from .env or environment variables.",
    )
    st.warning("SerpAPI is a paid API provider. Do not use for bulk testing unless you intend to spend credits.")
    serpapi_env_key_exists = bool(os.getenv(SERPAPI_API_KEY_ENV))
    if serpapi_env_key_exists:
        st.caption("SERPAPI_API_KEY found in environment/.env")
    else:
        st.caption("No SERPAPI_API_KEY found. SerpAPI will fall back to DuckDuckGo unless you paste a key above.")

    search_location = st.text_input(
        "Search location bias",
        value=DEFAULT_SEARCH_LOCATION,
        help="Used by SerpAPI/Google. Keep this local, e.g. Galway, County Galway, Ireland, to reduce irrelevant directory results.",
    )


def build_run_settings(
    *,
    file_name: str | None,
    mode: str,
    row_limit: int,
    min_confidence: float,
    delay: float,
    target_fields: list[str],
    search_provider: str,
    search_location: str,
) -> dict:
    """Settings that affect the actual enrichment result.

    Display-only controls such as audit-table filters are intentionally excluded,
    so toggling those will not mark a result as stale.
    """
    return {
        "source_file": file_name or "",
        "mode": mode,
        "rows_to_research": int(row_limit),
        "minimum_confidence": round(float(min_confidence), 2),
        "delay_seconds": round(float(delay), 2),
        "fields_to_research": list(target_fields),
        "search_provider": search_provider,
        "search_location": search_location or DEFAULT_SEARCH_LOCATION,
    }


def settings_changed(current: dict, previous: dict | None) -> bool:
    if previous is None:
        return False
    return current != previous


uploaded_file = st.file_uploader("Upload a CRM import CSV", type=["csv"])

current_effective_search_provider = search_provider
if search_provider == "brave" and not os.getenv(BRAVE_SEARCH_API_KEY_ENV):
    current_effective_search_provider = "duckduckgo"
elif search_provider == "serpapi" and not (serpapi_key_input.strip() or os.getenv(SERPAPI_API_KEY_ENV)):
    current_effective_search_provider = "duckduckgo"

current_run_settings = build_run_settings(
    file_name=uploaded_file.name if uploaded_file is not None else None,
    mode=mode,
    row_limit=int(row_limit),
    min_confidence=float(min_confidence),
    delay=float(delay),
    target_fields=target_fields,
    search_provider=current_effective_search_provider,
    search_location=search_location,
)

st.info(
    "Recommended test: Preview only, 10 rows, 1.00s delay, Website first. "
    "Use Brave Search API when available, with DuckDuckGo as the free fallback. Keep Search location bias set to Galway, County Galway, Ireland. Use the audit CSV for review."
)
st.caption(
    "If a 25-row run seems to stall, watch the progress line after clicking Run research. "
    "The audit/CSV download appears only after the run fully completes."
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
            or col.startswith("Best Candidate ")
            or col == "Decision Needed"
            or col == "Enrichment Notes"
        ]
        if signal_cols:
            mask = pd.Series(False, index=visible.index)
            for col in signal_cols:
                mask = mask | ~is_blank_series(visible[col])
            visible = visible[mask]
    return visible


if uploaded_file is not None:
    df = pd.read_csv(uploaded_file, dtype=str, keep_default_na=False)

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

    selected_missing = 0
    for field in target_fields:
        if field in df.columns:
            selected_missing += int(is_blank_series(df[field]).sum())
    if selected_missing and int(row_limit) > selected_missing:
        st.info(f"Only {selected_missing} blank selected fields are currently available to research. A lower row limit may be faster.")

    if not target_fields:
        st.warning("Select at least one field to research.")
        st.stop()

    if st.button("Run research", type="primary"):
        brave_key = os.getenv(BRAVE_SEARCH_API_KEY_ENV)
        serpapi_key = serpapi_key_input.strip() or os.getenv(SERPAPI_API_KEY_ENV)
        effective_search_provider = search_provider
        if search_provider == "brave" and not brave_key:
            st.warning("Brave Search API is selected but BRAVE_SEARCH_API_KEY is missing. Falling back to DuckDuckGo for this run.")
            effective_search_provider = "duckduckgo"
        if search_provider == "serpapi" and not serpapi_key:
            st.warning("SerpAPI is selected but no key is available. Falling back to DuckDuckGo for this run.")
            effective_search_provider = "duckduckgo"

        progress_bar = st.progress(0, text="Starting research...")
        status_box = st.empty()

        def update_progress(done: int, total: int, company_name: str) -> None:
            if total <= 0:
                progress_bar.progress(0, text="No rows need research.")
                return
            pct = min(max(done / total, 0.0), 1.0)
            progress_bar.progress(pct, text=f"Researching {done}/{total}: {company_name}")
            status_box.caption(f"Current/last row: {company_name}")

        with st.spinner("Researching records. This may take a few minutes. Keep this tab open..."):
            result_df = enrich_dataframe(
                df,
                mode=mode,
                min_confidence=float(min_confidence),
                row_limit=int(row_limit),
                delay=float(delay),
                use_apollo=False,
                apollo_api_key=None,
                target_fields=target_fields,
                progress_callback=update_progress,
                search_provider=effective_search_provider,
                brave_api_key=brave_key,
                serpapi_api_key=serpapi_key,
                search_location=search_location,
            )
        progress_bar.progress(1.0, text="Research completed.")
        status_box.empty()

        st.session_state.result_df = result_df
        st.session_state.source_file_name = uploaded_file.name
        st.session_state.last_mode = mode
        st.session_state.last_run_settings = build_run_settings(
            file_name=uploaded_file.name,
            mode=mode,
            row_limit=int(row_limit),
            min_confidence=float(min_confidence),
            delay=float(delay),
            target_fields=target_fields,
            search_provider=effective_search_provider,
            search_location=search_location,
        )
        st.success("Research completed.")

if st.session_state.result_df is not None:
    result_df = st.session_state.result_df
    is_stale_result = settings_changed(current_run_settings, st.session_state.last_run_settings)

    if st.session_state.last_run_settings:
        with st.expander("Last run settings", expanded=False):
            st.json(st.session_state.last_run_settings)

    if is_stale_result:
        st.warning(
            "The table and downloads below are from a previous run. "
            "Your current sidebar settings are different. Run research again before downloading/importing."
        )
        if st.button("Clear old results"):
            st.session_state.result_df = None
            st.session_state.last_run_settings = None
            st.rerun()

    audit_df = audit_columns_only(result_df)
    final_import_df = crm_import_columns_only(result_df)
    visible_audit = build_visible_audit(
        audit_df,
        review_rows_only=show_review_rows_only,
        candidate_columns=show_candidate_columns,
    )

    proposed_website_count = 0 if "Proposed Website" not in audit_df.columns else int((~is_blank_series(audit_df["Proposed Website"])).sum())
    strong_candidate_count = 0
    if "Best Candidate Confidence" in audit_df.columns:
        best_conf = pd.to_numeric(audit_df["Best Candidate Confidence"], errors="coerce").fillna(0)
        strong_candidate_count = int(((best_conf >= 0.65) & is_blank_series(audit_df.get("Proposed Website", pd.Series("", index=audit_df.index)))).sum())
    candidate_website_count = int(
        sum((~is_blank_series(audit_df[col])).sum() for col in audit_df.columns if col.startswith("Candidate Website") and col.endswith("Value"))
    )
    candidate_phone_count = int(
        sum((~is_blank_series(audit_df[col])).sum() for col in audit_df.columns if col.startswith("Candidate Phone") and col.endswith("Value"))
    )

    st.subheader("Audit review")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Verified website proposals", proposed_website_count)
    c2.metric("Strong website candidates", strong_candidate_count)
    c3.metric("Website candidates", candidate_website_count)
    c4.metric("Phone candidates", candidate_phone_count)

    if visible_audit.empty:
        st.warning("No proposals, candidates or research notes are visible with the current filters. Untick the sidebar filter to show all rows.")
    else:
        st.dataframe(visible_audit, use_container_width=True)

    st.subheader("Download")
    st.download_button(
        "Download audit CSV for review - not for CRM import",
        data=audit_df.to_csv(index=False, quoting=csv.QUOTE_ALL).encode("utf-8-sig"),
        file_name="csv_genie_audit.csv",
        disabled=is_stale_result,
        mime="text/csv",
        key="download_audit_csv",
    )

    if st.session_state.last_mode == "preview":
        st.warning("Preview mode keeps CRM fields unchanged. The audit CSV is the useful file at this stage; the CRM import CSV is disabled to avoid importing unchanged data by mistake.")
        st.download_button(
            "Download CRM import CSV - disabled in Preview mode",
            data=final_import_df.to_csv(index=False, quoting=csv.QUOTE_ALL).encode("utf-8-sig"),
            file_name="csv_genie_crm_import_preview_unchanged.csv",
            mime="text/csv",
            disabled=True,
            key="download_crm_disabled",
        )
    else:
        st.warning("Verified-only mode fills only blank fields that meet the confidence threshold. Still review the audit CSV before importing.")
        st.download_button(
            "Download CRM import CSV for website import",
            data=final_import_df.to_csv(index=False, quoting=csv.QUOTE_ALL).encode("utf-8-sig"),
            file_name="csv_genie_crm_import.csv",
            mime="text/csv",
            disabled=is_stale_result,
            key="download_crm_csv",
        )
