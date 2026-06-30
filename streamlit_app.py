"""
CSV Genie Streamlit app.

Run locally with:
    python -m streamlit run streamlit_app.py
"""

from __future__ import annotations

import csv
from io import BytesIO
import os
from pathlib import Path
from datetime import datetime
import zipfile
import xml.etree.ElementTree as ET

import pandas as pd
import streamlit as st
from openpyxl import load_workbook

from crm_enrichment_tool import (
    AUDIT_COLUMNS,
    BRAVE_SEARCH_API_KEY_ENV,
    DEFAULT_SEARCH_LOCATION,
    SERPAPI_API_KEY_ENV,
    audit_columns_only,
    clean_cell,
    crm_import_columns_only,
    enrich_dataframe,
    root_url,
)


st.set_page_config(page_title="CSV Genie", layout="wide")

st.title("CSV Genie")
st.caption("Verified CRM import enrichment with candidate review for missing websites, phone numbers and emails.")

research_tab, sync_tab = st.tabs(["Research & Approval", "Sync Excel Workbook"])

# Keep results alive after download clicks/reruns.
if "result_df" not in st.session_state:
    st.session_state.result_df = None
if "source_file_name" not in st.session_state:
    st.session_state.source_file_name = None
if "last_mode" not in st.session_state:
    st.session_state.last_mode = "preview"
if "last_run_settings" not in st.session_state:
    st.session_state.last_run_settings = None
if "source_df" not in st.session_state:
    st.session_state.source_df = None
if "approved_import_df" not in st.session_state:
    st.session_state.approved_import_df = None
if "approval_log_df" not in st.session_state:
    st.session_state.approval_log_df = None
if "synced_workbook_bytes" not in st.session_state:
    st.session_state.synced_workbook_bytes = None
if "synced_workbook_filename" not in st.session_state:
    st.session_state.synced_workbook_filename = None
if "sync_log_df" not in st.session_state:
    st.session_state.sync_log_df = None

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
            "Brave Search + Places - optional paid test",
            "DuckDuckGo - free",
            "SerpAPI Google results - optional paid",
        ],
        index=0,
        help="Brave is recommended when BRAVE_SEARCH_API_KEY is available. DuckDuckGo is the free fallback. Places and SerpAPI may be separately billed.",
    )
    if search_provider_label.startswith("Brave Search + Places"):
        search_provider = "brave_places"
    elif search_provider_label.startswith("Brave"):
        search_provider = "brave"
    elif search_provider_label.startswith("SerpAPI"):
        search_provider = "serpapi"
    else:
        search_provider = "duckduckgo"
    brave_env_key_exists = bool(os.getenv(BRAVE_SEARCH_API_KEY_ENV))
    if brave_env_key_exists:
        st.caption("BRAVE_SEARCH_API_KEY found in environment/.env")
    elif search_provider.startswith("brave"):
        st.warning("Brave Search API selected without BRAVE_SEARCH_API_KEY will fall back to DuckDuckGo.")
    brave_result_count = st.selectbox(
        "Brave web result count",
        [10, 20],
        index=0,
        help="Use 10 by default. Try 20 only when you want a wider candidate set.",
        disabled=not search_provider.startswith("brave"),
    )
    if search_provider == "brave_places":
        st.warning("Brave Place Search may be billed separately from Web Search. Use small row limits while testing.")
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

    st.subheader("Delays")
    default_search_delay = 0.25 if search_provider.startswith("brave") else 1.0
    search_api_delay = st.number_input(
        "Search API delay (seconds)",
        min_value=0.0,
        max_value=10.0,
        value=default_search_delay,
        step=0.25,
        help="Default is 0.25 for Brave and 1.00 for DuckDuckGo.",
    )
    website_fetch_delay = st.number_input(
        "Website/contact-page fetch delay (seconds)",
        min_value=0.0,
        max_value=10.0,
        value=1.0,
        step=0.25,
    )


def build_run_settings(
    *,
    file_name: str | None,
    mode: str,
    row_limit: int,
    min_confidence: float,
    search_api_delay: float,
    website_fetch_delay: float,
    target_fields: list[str],
    search_provider: str,
    search_location: str,
    brave_result_count: int,
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
        "search_api_delay_seconds": round(float(search_api_delay), 2),
        "website_fetch_delay_seconds": round(float(website_fetch_delay), 2),
        "fields_to_research": list(target_fields),
        "search_provider": search_provider,
        "search_location": search_location or DEFAULT_SEARCH_LOCATION,
        "brave_result_count": int(brave_result_count),
    }


def settings_changed(current: dict, previous: dict | None) -> bool:
    if previous is None:
        return False
    return current != previous


with research_tab:
    uploaded_file = st.file_uploader("Upload a CRM import CSV", type=["csv"])

current_effective_search_provider = search_provider
if search_provider.startswith("brave") and not os.getenv(BRAVE_SEARCH_API_KEY_ENV):
    current_effective_search_provider = "duckduckgo"
elif search_provider == "serpapi" and not (serpapi_key_input.strip() or os.getenv(SERPAPI_API_KEY_ENV)):
    current_effective_search_provider = "duckduckgo"

current_run_settings = build_run_settings(
    file_name=uploaded_file.name if uploaded_file is not None else None,
    mode=mode,
    row_limit=int(row_limit),
    min_confidence=float(min_confidence),
    search_api_delay=float(search_api_delay),
    website_fetch_delay=float(website_fetch_delay),
    target_fields=target_fields,
    search_provider=current_effective_search_provider,
    search_location=search_location,
    brave_result_count=int(brave_result_count),
)

with research_tab:
    st.info(
        "Recommended test: Preview only, 10 rows, Website first, Brave count 10, 0.25s search delay and 1.00s website fetch delay. "
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


def versioned_filename(original_name: str | None, suffix: str, extension: str) -> str:
    path = Path(original_name or "csv_genie_export")
    base = path.stem or "csv_genie_export"
    return f"{base}_{suffix}{extension}"


APPROVAL_REVIEW_COLUMNS = [
    "Company Name",
    "Area",
    "Existing Website",
    "Proposed Website",
    "Best Candidate Website",
    "Best Candidate Confidence",
    "Best Candidate Rejected Reason",
    "Candidate Website 1 Value",
    "Candidate Website 2 Value",
    "Candidate Website 3 Value",
    "Decision Needed",
    "Approve Website?",
    "Approved Website",
    "Approval Note",
]


def nonblank_value(value: object) -> str:
    return clean_cell(value) or ""


def build_website_approval_table(audit_df: pd.DataFrame) -> pd.DataFrame:
    if audit_df.empty:
        return pd.DataFrame(columns=["_source_index", *APPROVAL_REVIEW_COLUMNS])

    signal_cols = [
        "Proposed Website",
        "Best Candidate Website",
        "Best Candidate Rejected Reason",
        "Decision Needed",
        "Candidate Website 1 Value",
        "Candidate Website 2 Value",
        "Candidate Website 3 Value",
        "Candidate Website 1 Rejected Reason",
        "Candidate Website 2 Rejected Reason",
        "Candidate Website 3 Rejected Reason",
    ]

    rows = []
    for idx, row in audit_df.iterrows():
        if not any(nonblank_value(row.get(col)) for col in signal_cols):
            continue

        existing_website = nonblank_value(row.get("Website"))
        proposed_website = nonblank_value(row.get("Proposed Website"))
        best_candidate = nonblank_value(row.get("Best Candidate Website"))
        approved_default = proposed_website or best_candidate
        approve_default = bool(proposed_website and not existing_website)
        decision_needed = nonblank_value(row.get("Decision Needed"))
        if decision_needed == "No reliable candidate found":
            decision_needed = "Manual entry needed"

        rows.append(
            {
                "_source_index": idx,
                "Company Name": nonblank_value(row.get("Company Name")),
                "Area": nonblank_value(row.get("Area")),
                "Existing Website": existing_website,
                "Proposed Website": proposed_website,
                "Best Candidate Website": best_candidate,
                "Best Candidate Confidence": nonblank_value(row.get("Best Candidate Confidence")),
                "Best Candidate Rejected Reason": nonblank_value(row.get("Best Candidate Rejected Reason")),
                "Candidate Website 1 Value": nonblank_value(row.get("Candidate Website 1 Value")),
                "Candidate Website 2 Value": nonblank_value(row.get("Candidate Website 2 Value")),
                "Candidate Website 3 Value": nonblank_value(row.get("Candidate Website 3 Value")),
                "Decision Needed": decision_needed,
                "Approve Website?": approve_default,
                "Approved Website": root_url(approved_default) if approved_default else "",
                "Approval Note": "",
            }
        )

    return pd.DataFrame(rows, columns=["_source_index", *APPROVAL_REVIEW_COLUMNS])


def identify_approval_source(row: pd.Series, approved_website: str) -> str:
    approved_root = root_url(approved_website)
    candidates = [
        ("Proposed Website", row.get("Proposed Website")),
        ("Best Candidate Website", row.get("Best Candidate Website")),
        ("Candidate Website 1", row.get("Candidate Website 1 Value")),
        ("Candidate Website 2", row.get("Candidate Website 2 Value")),
        ("Candidate Website 3", row.get("Candidate Website 3 Value")),
    ]
    for label, value in candidates:
        candidate = nonblank_value(value)
        if candidate and root_url(candidate).lower().rstrip("/") == approved_root.lower().rstrip("/"):
            return label
    return "Manual edit"


def append_manual_note(existing_note: object, approved_website: str, approval_note: str) -> str:
    addition = f"CSV Genie approved website: {approved_website}"
    if approval_note:
        addition = f"{addition}. {approval_note}"
    existing = nonblank_value(existing_note)
    return f"{existing}; {addition}" if existing else addition


def generate_approved_outputs(original_df: pd.DataFrame, approval_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    approved_import_df = original_df.copy()
    log_rows = []
    timestamp = datetime.now().isoformat(timespec="seconds")

    if approval_df.empty:
        return approved_import_df, pd.DataFrame(
            columns=["Company Name", "Approved Website", "Approval Note", "Source candidate/proposal used", "Timestamp", "Applied"]
        )

    for _, approval_row in approval_df.iterrows():
        if not bool(approval_row.get("Approve Website?")):
            continue

        source_index = approval_row.get("_source_index")
        if source_index not in approved_import_df.index:
            continue

        approved_website = nonblank_value(approval_row.get("Approved Website"))
        if not approved_website:
            continue

        approved_website = root_url(approved_website)
        original_has_website = bool(nonblank_value(approved_import_df.at[source_index, "Website"]))
        applied = False
        if not original_has_website:
            approved_import_df.at[source_index, "Website"] = approved_website
            applied = True
            if "Research Status" in approved_import_df.columns:
                approved_import_df.at[source_index, "Research Status"] = "Manually approved website"
            if "Manual Notes" in approved_import_df.columns:
                approved_import_df.at[source_index, "Manual Notes"] = append_manual_note(
                    approved_import_df.at[source_index, "Manual Notes"],
                    approved_website,
                    nonblank_value(approval_row.get("Approval Note")),
                )

        log_rows.append(
            {
                "Company Name": nonblank_value(approval_row.get("Company Name")),
                "Approved Website": approved_website,
                "Approval Note": nonblank_value(approval_row.get("Approval Note")),
                "Source candidate/proposal used": identify_approval_source(approval_row, approved_website),
                "Timestamp": timestamp,
                "Applied": "Yes" if applied else "No - existing website not overwritten",
            }
        )

    return approved_import_df, pd.DataFrame(log_rows)


SYNC_FIELDS = ["Website", "Phone", "Email", "Research Status", "Manual Notes"]
INVALID_SHEET_NAME_CHARS = set('/\\?*[]:')
WORKBOOK_NAMESPACE = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def normalize_match_value(value: object) -> str:
    return nonblank_value(value).casefold()


def build_match_key(company_name: object, area: object) -> tuple[str, str]:
    return (normalize_match_value(company_name), normalize_match_value(area))


def is_manual_approval_row(row: pd.Series) -> bool:
    marker_text = " ".join(
        [
            nonblank_value(row.get("Research Status")),
            nonblank_value(row.get("Manual Notes")),
        ]
    ).casefold()
    return "manually approved" in marker_text or "csv genie approved website" in marker_text


def cell_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def has_invalid_sheet_name_chars(sheet_name: str) -> bool:
    return any(char in INVALID_SHEET_NAME_CHARS for char in sheet_name)


def sanitise_sheet_name(sheet_name: str) -> str:
    cleaned = "".join(" - " if char in INVALID_SHEET_NAME_CHARS else char for char in sheet_name)
    cleaned = " ".join(cleaned.split()).strip()
    return (cleaned or "Sheet")[:31]


def unique_sheet_name(sheet_name: str, used_names: set[str]) -> str:
    base = sheet_name[:31] or "Sheet"
    candidate = base
    suffix_number = 2
    while candidate.casefold() in used_names:
        suffix = f" {suffix_number}"
        candidate = f"{base[: 31 - len(suffix)]}{suffix}"
        suffix_number += 1
    used_names.add(candidate.casefold())
    return candidate


def sanitise_workbook_sheet_names(workbook_bytes: bytes) -> tuple[bytes, list[dict[str, str]]]:
    """Return workbook bytes with invalid sheet names repaired in workbook.xml."""
    try:
        source = BytesIO(workbook_bytes)
        with zipfile.ZipFile(source, "r") as zin:
            workbook_xml = zin.read("xl/workbook.xml")
            root = ET.fromstring(workbook_xml)
            sheets = root.findall("main:sheets/main:sheet", WORKBOOK_NAMESPACE)
            used_names = {
                sheet.attrib.get("name", "").casefold()
                for sheet in sheets
                if not has_invalid_sheet_name_chars(sheet.attrib.get("name", ""))
            }
            changes: list[dict[str, str]] = []
            for sheet in sheets:
                original_name = sheet.attrib.get("name", "")
                if has_invalid_sheet_name_chars(original_name):
                    new_name = unique_sheet_name(sanitise_sheet_name(original_name), used_names)
                    sheet.set("name", new_name)
                    invalid_chars = "".join(char for char in original_name if char in INVALID_SHEET_NAME_CHARS)
                    changes.append(
                        {
                            "original": original_name,
                            "sanitised": new_name,
                            "invalid_chars": invalid_chars,
                        }
                    )

            if not changes:
                return workbook_bytes, []

            ET.register_namespace("", WORKBOOK_NAMESPACE["main"])
            updated_workbook_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            output = BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = updated_workbook_xml if item.filename == "xl/workbook.xml" else zin.read(item.filename)
                    zout.writestr(item, data)
            return output.getvalue(), changes
    except Exception as exc:
        raise ValueError(f"Workbook pre-load sheet-name scan failed: {type(exc).__name__}: {exc}") from exc


def sync_excel_workbook(workbook_bytes: bytes, approved_df: pd.DataFrame) -> tuple[bytes, pd.DataFrame, dict]:
    sanitised_workbook_bytes, sheet_name_changes = sanitise_workbook_sheet_names(workbook_bytes)
    try:
        workbook = load_workbook(BytesIO(sanitised_workbook_bytes))
    except Exception as exc:
        invalid_note = ""
        if sheet_name_changes:
            changed_names = "; ".join(f"{change['original']} -> {change['sanitised']}" for change in sheet_name_changes)
            invalid_note = f" Invalid sheet name(s) detected and sanitised before load: {changed_names}."
        raise ValueError(f"Workbook load failed: {type(exc).__name__}: {exc}.{invalid_note}") from exc
    if "CRM Import" not in workbook.sheetnames:
        raise ValueError('Workbook is missing required sheet "CRM Import"')

    sheet_names_before = list(workbook.sheetnames)
    ws = workbook["CRM Import"]
    headers = {
        cell_text(cell.value): column_index
        for column_index, cell in enumerate(ws[1], start=1)
        if cell_text(cell.value)
    }

    required_match_cols = ["Company Name", "Area"]
    missing_match_cols = [col for col in required_match_cols if col not in headers or col not in approved_df.columns]
    if missing_match_cols:
        raise ValueError(f"Missing required sync columns: {', '.join(missing_match_cols)}")

    workbook_rows: dict[tuple[str, str], int] = {}
    duplicate_keys = set()
    for row_number in range(2, ws.max_row + 1):
        key = build_match_key(
            ws.cell(row=row_number, column=headers["Company Name"]).value,
            ws.cell(row=row_number, column=headers["Area"]).value,
        )
        if not any(key):
            continue
        if key in workbook_rows:
            duplicate_keys.add(key)
            continue
        workbook_rows[key] = row_number

    log_rows = [
        {
            "Company Name": "",
            "Area": "",
            "Field updated": "",
            "Old value": change["original"],
            "New value": change["sanitised"],
            "Status": f"Sheet name sanitised before workbook load; invalid character(s): {change['invalid_chars']}",
        }
        for change in sheet_name_changes
    ]
    updates_applied = 0
    updated_row_numbers = set()

    for _, approved_row in approved_df.iterrows():
        key = build_match_key(approved_row.get("Company Name"), approved_row.get("Area"))
        company_name = nonblank_value(approved_row.get("Company Name"))
        area = nonblank_value(approved_row.get("Area"))

        if key not in workbook_rows:
            log_rows.append(
                {
                    "Company Name": company_name,
                    "Area": area,
                    "Field updated": "",
                    "Old value": "",
                    "New value": "",
                    "Status": "No matching workbook row",
                }
            )
            continue
        if key in duplicate_keys:
            log_rows.append(
                {
                    "Company Name": company_name,
                    "Area": area,
                    "Field updated": "",
                    "Old value": "",
                    "New value": "",
                    "Status": "Skipped duplicate workbook match",
                }
            )
            continue

        row_number = workbook_rows[key]
        manual_approval = is_manual_approval_row(approved_row)

        for field_name in SYNC_FIELDS:
            if field_name not in approved_df.columns:
                continue
            if field_name not in headers:
                log_rows.append(
                    {
                        "Company Name": company_name,
                        "Area": area,
                        "Field updated": field_name,
                        "Old value": "",
                        "New value": nonblank_value(approved_row.get(field_name)),
                        "Status": "Skipped missing workbook column",
                    }
                )
                continue

            new_value = nonblank_value(approved_row.get(field_name))
            if not new_value:
                continue

            cell = ws.cell(row=row_number, column=headers[field_name])
            old_value = cell_text(cell.value)
            if old_value == new_value:
                continue

            is_contact_field = field_name in {"Website", "Phone", "Email"}
            can_overwrite_contact = field_name == "Website" and manual_approval
            if is_contact_field and old_value and not can_overwrite_contact:
                log_rows.append(
                    {
                        "Company Name": company_name,
                        "Area": area,
                        "Field updated": field_name,
                        "Old value": old_value,
                        "New value": new_value,
                        "Status": "Skipped non-empty contact field without field-level manual approval",
                    }
                )
                continue

            cell.value = str(new_value)
            if field_name == "Phone":
                cell.number_format = "@"
            updates_applied += 1
            updated_row_numbers.add(row_number)
            status = "Updated"
            if is_contact_field and old_value:
                status = "Updated non-empty contact field from manual approval"
            log_rows.append(
                {
                    "Company Name": company_name,
                    "Area": area,
                    "Field updated": field_name,
                    "Old value": old_value,
                    "New value": new_value,
                    "Status": status,
                }
            )

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    sync_log_df = pd.DataFrame(
        log_rows,
        columns=["Company Name", "Area", "Field updated", "Old value", "New value", "Status"],
    )
    metadata = {
        "sheet_names_before": sheet_names_before,
        "sheet_names_after": list(workbook.sheetnames),
        "updates_applied": updates_applied,
        "rows_updated": len(updated_row_numbers),
        "sheet_name_changes": sheet_name_changes,
    }
    return output.getvalue(), sync_log_df, metadata


with research_tab:
    if uploaded_file is not None:
        df = pd.read_csv(uploaded_file, dtype=str, keep_default_na=False)
        st.session_state.source_df = df.copy()

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
            if search_provider.startswith("brave") and not brave_key:
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
                    search_api_delay=float(search_api_delay),
                    website_fetch_delay=float(website_fetch_delay),
                    use_apollo=False,
                    apollo_api_key=None,
                    target_fields=target_fields,
                    progress_callback=update_progress,
                    search_provider=effective_search_provider,
                    brave_api_key=brave_key,
                    serpapi_api_key=serpapi_key,
                    search_location=search_location,
                    brave_result_count=int(brave_result_count),
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
                search_api_delay=float(search_api_delay),
                website_fetch_delay=float(website_fetch_delay),
                target_fields=target_fields,
                search_provider=effective_search_provider,
                search_location=search_location,
                brave_result_count=int(brave_result_count),
            )
            st.session_state.approved_import_df = None
            st.session_state.approval_log_df = None
            st.session_state.synced_workbook_bytes = None
            st.session_state.synced_workbook_filename = None
            st.session_state.sync_log_df = None
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
                st.session_state.approved_import_df = None
                st.session_state.approval_log_df = None
                st.session_state.synced_workbook_bytes = None
                st.session_state.synced_workbook_filename = None
                st.session_state.sync_log_df = None
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

        st.subheader("Manual Approval")
        approval_table = build_website_approval_table(audit_df)
        if approval_table.empty:
            st.info("No website proposals or candidates are available for approval yet.")
        else:
            edited_approval_df = st.data_editor(
                approval_table,
                use_container_width=True,
                hide_index=True,
                disabled=[
                    col
                    for col in approval_table.columns
                    if col not in {"Approve Website?", "Approved Website", "Approval Note"}
                ],
                column_config={
                    "_source_index": None,
                    "Approve Website?": st.column_config.CheckboxColumn("Approve Website?"),
                    "Approved Website": st.column_config.TextColumn("Approved Website"),
                    "Approval Note": st.column_config.TextColumn("Approval Note"),
                },
                key="website_approval_editor",
            )

            if st.button("Generate approved CRM import CSV", disabled=is_stale_result or st.session_state.source_df is None):
                approved_import_df, approval_log_df = generate_approved_outputs(
                    st.session_state.source_df,
                    edited_approval_df,
                )
                st.session_state.approved_import_df = approved_import_df
                st.session_state.approval_log_df = approval_log_df
                applied_count = 0 if approval_log_df.empty else int((approval_log_df["Applied"] == "Yes").sum())
                st.success(f"Approved CRM import CSV generated. Websites applied to {applied_count} blank Website field(s).")

            if st.session_state.approved_import_df is not None:
                st.download_button(
                    "Download approved CRM import CSV",
                    data=st.session_state.approved_import_df.to_csv(index=False, quoting=csv.QUOTE_ALL).encode("utf-8-sig"),
                    file_name=versioned_filename(st.session_state.source_file_name, "approved_v0.1", ".csv"),
                    disabled=is_stale_result,
                    mime="text/csv",
                    key="download_approved_crm_csv",
                )

            if st.session_state.approval_log_df is not None:
                st.download_button(
                    "Download approval log CSV",
                    data=st.session_state.approval_log_df.to_csv(index=False, quoting=csv.QUOTE_ALL).encode("utf-8-sig"),
                    file_name="csv_genie_approval_log.csv",
                    disabled=is_stale_result,
                    mime="text/csv",
                    key="download_approval_log_csv",
                )

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


with sync_tab:
    excel_upload = st.file_uploader(
        "Upload original Excel workbook",
        type=["xlsx", "xlsm"],
        key="excel_sync_workbook",
    )
    approved_csv_upload = st.file_uploader(
        "Upload approved CRM import CSV",
        type=["csv"],
        key="excel_sync_approved_csv",
    )

    if st.button(
        "Generate synced Excel workbook",
        disabled=excel_upload is None or approved_csv_upload is None,
    ):
        try:
            approved_sync_df = pd.read_csv(approved_csv_upload, dtype=str, keep_default_na=False)
            workbook_bytes, sync_log_df, sync_metadata = sync_excel_workbook(
                excel_upload.getvalue(),
                approved_sync_df,
            )
            st.session_state.synced_workbook_bytes = workbook_bytes
            st.session_state.synced_workbook_filename = versioned_filename(
                excel_upload.name,
                "synced_v0.1",
                ".xlsx",
            )
            st.session_state.sync_log_df = sync_log_df
            st.success(
                "Synced Excel workbook generated. "
                f"Rows updated: {sync_metadata['rows_updated']}. "
                f"Fields updated: {sync_metadata['updates_applied']}. "
                f"Sheets preserved: {len(sync_metadata['sheet_names_after'])}."
            )
            if sync_metadata["sheet_name_changes"]:
                changed_names = "; ".join(
                    f"{change['original']} -> {change['sanitised']}"
                    for change in sync_metadata["sheet_name_changes"]
                )
                st.warning(f"Invalid sheet name(s) detected and sanitised: {changed_names}")
        except Exception as exc:
            st.error(f"Excel sync failed: {type(exc).__name__}: {exc}")

    if st.session_state.synced_workbook_bytes is not None:
        st.download_button(
            "Download synced Excel workbook",
            data=st.session_state.synced_workbook_bytes,
            file_name=st.session_state.synced_workbook_filename or "csv_genie_synced_v0.1.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="download_synced_workbook",
        )

    if st.session_state.sync_log_df is not None:
        st.download_button(
            "Download sync log CSV",
            data=st.session_state.sync_log_df.to_csv(index=False, quoting=csv.QUOTE_ALL).encode("utf-8-sig"),
            file_name="csv_genie_excel_sync_log.csv",
            mime="text/csv",
            key="download_excel_sync_log",
        )
