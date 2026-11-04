"""
app.py - self-serve Streamlit UI for the JumpCloud outbound pipeline.

    streamlit run app.py

This file is UI only. Every decision (cleaning, scoring, routing, drafting,
validation, push + log) comes from pipeline.run(), so the CLI and the UI
always behave the same.
"""

import io
import logging
import os
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

import pipeline

SAMPLE_CSV = Path(__file__).resolve().parent / "sample_leads.csv"
LANE_ORDER = ["ROUTE_TO_REP", "MSP_TRACK", "OUTBOUND", "NURTURE", "HUMAN_REVIEW", "HELD", "EXCLUDE"]
TABLE_COLUMNS = ["record_id", "clean_first_name", "clean_last_name", "clean_company", "persona",
                 "lane", "reason_code", "priority", "total_score", "enrichment_conflict", "validation_status"]

# Row highlight colours (light enough to read in both themes).
HIGHLIGHTS = {
    "HELD": "#fff3cd",               # amber
    "HUMAN_REVIEW": "#f8d7da",       # red
    "enrichment_conflict": "#e2d9f3",  # purple
    "template_fallback": "#d1ecf1",  # blue
}


def run_pipeline(csv_bytes, filename, fresh):
    """Save the upload to a temp file, run the real pipeline, capture its log lines."""
    log_buffer = io.StringIO()
    handler = logging.StreamHandler(log_buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    pipeline.log.addHandler(handler)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / Path(filename).name
            path.write_bytes(csv_bytes)
            rows = pipeline.run(str(path), fresh=fresh)
    finally:
        pipeline.log.removeHandler(handler)
    return rows, log_buffer.getvalue()


def to_frame(rows):
    """Turn pipeline rows into a DataFrame using the same columns as leads_output.csv."""
    df = pd.DataFrame([{c: r.get(c, "") for c in pipeline.OUTPUT_COLUMNS} for r in rows])
    df["total_score"] = pd.to_numeric(df["total_score"], errors="coerce")
    df["enrichment_conflict"] = df["enrichment_conflict"].astype(str) == "True"
    return df


def highlight_row(row):
    """Pick one background colour per row; the most serious reason wins."""
    if row["lane"] == "HELD":
        color = HIGHLIGHTS["HELD"]
    elif row["lane"] == "HUMAN_REVIEW":
        color = HIGHLIGHTS["HUMAN_REVIEW"]
    elif row["enrichment_conflict"]:
        color = HIGHLIGHTS["enrichment_conflict"]
    elif row["validation_status"] == "template_fallback":
        color = HIGHLIGHTS["template_fallback"]
    else:
        color = ""
    style = f"background-color: {color}; color: #1f1f1f" if color else ""
    return [style] * len(row)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Lead Pipeline", layout="wide")
st.title("JumpCloud outbound lead pipeline")
st.caption("Upload a raw lead CSV and run all 10 steps: Ingest → Clean + Hygiene Gate → Persona Mapping → "
           "Dedupe + CRM Check → Enrich → Qualify + Score → Route → Draft All Touches Per Blueprint → "
           "Validate Every Touch → Push + Log. Enrichment and CRM are mocked.")

# The key is only ever read from the environment. Never shown, never asked for.
if os.environ.get("OPENROUTER_API_KEY"):
    st.success(f"OpenRouter key found in the environment. Drafts will be written by {pipeline.C.LLM_MODELS[0]} "
               "and checked by code.")
else:
    st.warning("No OPENROUTER_API_KEY in the environment. Drafts will use the labeled safe template fallback.")

with st.sidebar:
    st.header("Input")
    upload = st.file_uploader("Lead CSV", type=["csv"],
                              help="Columns: " + ", ".join(pipeline.C.EXPECTED_COLUMNS))
    use_sample = st.checkbox("Use the bundled sample_leads.csv", value=upload is None)
    fresh = st.checkbox("Start fresh", value=True,
                        help="Clears leads_output.csv and audit_log.jsonl first. Untick to merge into the "
                             "previous output with fill-blank-only logic (existing values are never overwritten).")
    run_clicked = st.button("Run pipeline", type="primary", width="stretch")

if run_clicked:
    if upload is not None and not use_sample:
        data, name = upload.getvalue(), upload.name
    elif use_sample:
        data, name = SAMPLE_CSV.read_bytes(), SAMPLE_CSV.name
    else:
        st.error("Upload a CSV or tick 'Use the bundled sample_leads.csv'.")
        st.stop()
    with st.spinner("Running the 10 steps (LLM drafting takes a few seconds per lead)..."):
        try:
            rows, run_log = run_pipeline(data, name, fresh)
        except SystemExit as e:  # pipeline raises this for missing columns
            st.error(str(e))
            st.stop()
    st.session_state["rows"] = rows
    st.session_state["log"] = run_log
    st.session_state["source_name"] = name

if "rows" not in st.session_state:
    st.info("Choose a CSV in the sidebar and click **Run pipeline**.")
    st.stop()

rows = st.session_state["rows"]
df = to_frame(rows)
batch_id = rows[0]["batch_id"] if rows else ""
st.subheader(f"Batch {batch_id} · {st.session_state['source_name']} · {len(df)} leads")

# --- Counts per lane -------------------------------------------------------
counts = df["lane"].value_counts()
cols = st.columns(len(LANE_ORDER))
for col, lane in zip(cols, LANE_ORDER):
    col.metric(lane, int(counts.get(lane, 0)))

# --- Filterable table ------------------------------------------------------
st.markdown("### Leads")
f1, f2, f3, f4 = st.columns([2, 2, 2, 2])
lanes = f1.multiselect("Lane", [l for l in LANE_ORDER if l in counts.index])
statuses = f2.multiselect("Validation status", sorted(s for s in df["validation_status"].unique() if s))
reason = f3.text_input("Reason code contains")
min_score = f4.slider("Min total score", 0, 100, 0)

view = df
if lanes:
    view = view[view["lane"].isin(lanes)]
if statuses:
    view = view[view["validation_status"].isin(statuses)]
if reason:
    view = view[view["reason_code"].str.contains(reason, case=False, na=False)]
if min_score:
    view = view[view["total_score"].fillna(-1) >= min_score]

st.dataframe(view[TABLE_COLUMNS].style.apply(highlight_row, axis=1),
             width="stretch", hide_index=True)
st.caption("Highlights: 🟨 HELD · 🟥 HUMAN_REVIEW · 🟪 enrichment conflict · 🟦 template_fallback draft")

# --- Drafts ----------------------------------------------------------------
st.markdown("### Touch 1 drafts (OUTBOUND and MSP_TRACK)")
drafted = [r for r in rows if r["lane"] in pipeline.C.DRAFT_LANES and r.get("draft_subject")]
if not drafted:
    st.write("No drafts in this batch.")
for r in drafted:
    title = f"{r['record_id']} · {pipeline.label(r)} · {r['clean_company']} · {r['lane']} · {r['validation_status']}"
    with st.expander(title):
        st.markdown(f"**Subject:** {r['draft_subject']}")
        st.text(r["draft_body"])
        st.caption(f"facts_used: {r['facts_used']} · prompt_version: {r['prompt_version']}"
                   + (f" · notes: {r['validation_errors']}" if r.get("validation_errors") else ""))

# --- Conflicts and review notes --------------------------------------------
attention = [r for r in rows if r["lane"] in ("HELD", "HUMAN_REVIEW", "EXCLUDE") or r.get("enrichment_conflict")]
if attention:
    st.markdown("### Needs attention")
    for r in attention:
        if r["lane"] == "HELD":
            note = r.get("held_note", "")
        elif r.get("enrichment_conflict"):
            note = r["enrichment_notes"]
        else:
            note = r["hygiene_flags"]
        st.write(f"- **{r['record_id']}** {pipeline.label(r)} ({r['clean_company'] or 'no company'}) "
                 f"→ `{r['lane']}`: {r['reason_code']}" + (f" · {note}" if note else ""))

# --- Downloads -------------------------------------------------------------
st.markdown("### Downloads")
d1, d2 = st.columns(2)
out_csv = pipeline.OUTPUT_DIR / "leads_output.csv"
summary = pipeline.OUTPUT_DIR / "run_summary.md"
if out_csv.exists():
    d1.download_button("Download leads_output.csv", out_csv.read_bytes(), "leads_output.csv", "text/csv",
                       width="stretch")
if summary.exists():
    d2.download_button("Download run_summary.md", summary.read_bytes(), "run_summary.md", "text/markdown",
                       width="stretch")

with st.expander("Run log"):
    st.code(st.session_state["log"], language=None)
