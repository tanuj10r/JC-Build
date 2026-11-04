# JumpCloud Outbound Lead Workflow

## What it does

`pipeline.py` takes a raw lead list and runs it through 10 steps. Each lead is cleaned, gated for hygiene, mapped to a persona, deduped and checked against the CRM, enriched, scored, and routed to one lane. Leads in the outbound lanes get a touch-1 email draft, the draft is validated, and everything is written out with a full audit trail. Enrichment and CRM are **mocked** (`mock_enrichment.json`, `mock_crm.json`). An LLM (Gemini Flash, called through OpenRouter) is used for only two things: picking a persona when no rule matches, and drafting touch 1. Without an API key the run still completes, using a clearly labeled template fallback. All business rules live in `config.py`.

## How to run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export OPENROUTER_API_KEY=...                 # optional; without it, drafts use the template fallback
export OPENROUTER_MODEL=google/gemini-2.5-flash # optional; puts this model first in the list

python pipeline.py sample_leads.csv          # merge into existing output (fill-blank-only)
python pipeline.py sample_leads.csv --fresh  # delete leads_output.csv + audit_log.jsonl and rebuild
pytest                                       # offline tests, no network
```

### Self-serve UI (Streamlit)

```bash
streamlit run app.py
```

This opens a browser page. In the sidebar, a marketing ops user makes two choices and clicks **Run pipeline**:
1. **Leads to run:** "Sample leads (15 rows)" or "Upload my own CSV". The upload box only appears for the second option, and Run stays disabled until a file is chosen.
2. **Previous results:** "Clear previous results first" (default) rebuilds the outputs from scratch. Untick it to add to the last run, where existing values are never overwritten and only blank fields are filled.

Before a run, the main area shows the workflow design diagram (`assets/workflow_design.png`, a snapshot of the [Whimsical board](https://whimsical.com/KuLbj7DSxCxpkCUAUsVYMk)), so users can see every step. After a run, the page has a **Results** tab and a **Workflow design** tab. Results shows:
- counts per lane
- a filterable leads table (lane, reason_code, total_score, validation_status), with HELD, HUMAN_REVIEW, enrichment-conflict and template_fallback rows highlighted
- expandable touch-1 drafts for OUTBOUND and MSP_TRACK rows
- download buttons for `leads_output.csv` and `run_summary.md`

`app.py` is UI only and calls `pipeline.run()`, so the CLI and the UI give identical results and write to the same `output/` folder. The OpenRouter key is read from `OPENROUTER_API_KEY` in the environment. The UI never asks for it or displays it; it only says whether a key was found.

The sample data came as `gtm-ai-ops-assessment-sample-data.xlsx` (left untouched). It was converted to `sample_leads.csv`, which is the input the pipeline reads.

## The 10 steps

| Step | Function | What it decides |
|---|---|---|
| Step 1 Ingest | `step_01_ingest` | Checks the expected columns, assigns `batch_id` and `record_id`, and saves a byte-for-byte raw copy. |
| Step 2 Clean + Hygiene Gate | `step_02_clean_hygiene_gate` | Trims and fixes case for names and titles, and lowercases emails. Gates each row: **EXCLUDE** (junk, no person, team inbox), **HUMAN_REVIEW** (missing core fields, personal email, domain mismatch, invalid email), or **PASS**. |
| Step 3 Persona Mapping | `step_03_persona_mapping` | Regex title rules run first, then an industry rule (MSP / IT Services → MSP). The LLM is called only if nothing matches; with no key the persona defaults to Non-buyer. |
| Step 4 Dedupe + CRM Check | `step_04_dedupe_crm_check` | Dedupes on exact email. Groups contacts by domain and picks one primary per account (highest intent, then persona fit). Reads owner, open opp, customer/partner status and opt-out from the mock CRM. |
| Step 5 Enrich | `step_05_enrich` | Looks up mock firmographics by domain. Fills blanks only. Never overwrites CSV values; a disagreement sets `enrichment_conflict`. |
| Step 6 Qualify + Score | `step_06_qualify_score` | Fit (0-50) + Intent (0-50) = Total (0-100). |
| Step 7 Route | `step_07_route` | Assigns one lane per row (`route_one`). Then sets non-primary contacts at accounts routed to a rep to **HELD**. |
| Step 8 Draft All Touches Per Blueprint | `step_08_draft_all_touches_per_blueprint` | Reads the 3-touch blueprint but generates **touch 1 only**, and only for OUTBOUND and MSP_TRACK. The LLM sees allowlisted fields only. |
| Step 9 Validate Every Touch | `step_09_validate_every_touch` | Plain-code checks for banned claims and competitors, digits and percentages, placeholders, non-allowlisted facts, exact name and company, subject < 60 chars, and body < 120 words. A failed LLM draft gets one retry, then the template. If the template also fails, the row goes to HUMAN_REVIEW. |
| Step 10 Push + Log | `step_10_push_log` | Writes `leads_output.csv` (fill-blank-only merge), appends to `audit_log.jsonl`, and writes `run_summary.md`. |

### Scoring (`config.py`)

| Component | Rule | Points |
|---|---|---|
| Persona | IT Decision Maker / Technical Executive / MSP / IT Practitioner / Non-buyer | 25 / 22 / 20 / 15 / 0 |
| Employee band | 50-1,000 (ICP) · 20-49 or 1,001-2,000 (near) · other or blank | 15 · 5 · 0 |
| Industry | Priority (Healthcare, Financial Services, Education, Software, Technology, MSP / IT Services) · other · missing | 10 · 5 · 0 |
| Intent (source, prefix match) | Inbound Demo Request · Webinar · Content Syndication · Trade Show · Form Fill / List Purchase | 50 · 40 · 30 · 25 · 10 |

### Routing order (`route_one`, first match wins)

1. Gate EXCLUDE, duplicate email, or CRM opt-out → **EXCLUDE**
2. Gate HUMAN_REVIEW → **HUMAN_REVIEW** (`HYGIENE_*`)
3. Inbound Demo Request → **ROUTE_TO_REP**. Score only sets priority (P1 at 80 or above, otherwise P2). The reason code gets `_MSP`, `_ABOVE_ICP` or `_ENRICHMENT_CONFLICT` suffixes. *Because this check comes early, an inbound demo outranks the MSP and >1,000-employee rules below.*
4. Existing CRM owner, customer or partner → **HUMAN_REVIEW**
5. Enrichment conflict → **HUMAN_REVIEW**
6. More than 1,000 employees → **HUMAN_REVIEW** (`ABOVE_ICP_BAND`)
7. MSP persona or industry → **MSP_TRACK**
8. Non-buyer → **NURTURE**
9. Score of 60 or more with a clean gate → **OUTBOUND**, otherwise **NURTURE**

After routing: if an account's primary contact went **ROUTE_TO_REP**, its other contacts are set to **HELD** (`ACCOUNT_OWNED_BY_REP`).

## Outputs (all in `output/`)

| File | Contents |
|---|---|
| `leads_output.csv` | One row per lead: original columns, `clean_*` columns, flags, persona, CRM, enrichment, scores, lane, reason, priority, draft, validation status and prompt version. |
| `audit_log.jsonl` | One JSON line per record per step: batch_id, record_id, step, decision, reason, prompt_version and timestamp. That makes 10 lines per record. It is append-only across runs (`--fresh` deletes it first). |
| `run_summary.md` | Human-readable report: counts per lane, every lead with its lane and reason, validation issues, enrichment conflicts, excluded/held/review rows, and the drafted emails. |
| `raw_input_<batch_id>.csv` | Untouched copy of each input file, one per run. |

## Results on the sample (15 rows)

| Lead | Lane | Why |
|---|---|---|
| R01 Marcus Delgado | OUTBOUND | IT Director, webinar, score 85 |
| R02 Sarah O'Neil | HELD | Non-primary at Cedar Peak, whose primary (Tom) went to a rep |
| R03 Raj | HUMAN_REVIEW | Missing last name, company, domain and email |
| R04 Jennifer Wu | ROUTE_TO_REP P1 | Inbound demo at an MSP (`INBOUND_DEMO_MSP`) |
| R05 David Karlsson | OUTBOUND | IT Manager, webinar, score 90 |
| R06 Priya Raman | HUMAN_REVIEW | Gmail address, which doesn't match loopware.ai |
| R07 Acme Widgets | EXCLUDE | `info@` inbox, no person |
| R08 Tom Becker | ROUTE_TO_REP P1 | Inbound demo, score 100 |
| R09 Angela Martinez | OUTBOUND | IT admin, webinar, score 70 (name and title case fixed) |
| R10 Kevin Liu | ROUTE_TO_REP P1 | CTO, inbound demo, score 97 |
| R11 asdf test | EXCLUDE | Junk (test.com) |
| R12 Olivia Grant | HUMAN_REVIEW | Enrichment conflict: CSV says 150 employees, mock says 2,400 |
| R13 Daniel Osei | OUTBOUND | IT Support Manager, score 90 (low-confidence enrichment ignored) |
| R14 Rebecca Flynn | MSP_TRACK | MSP via the industry rule, score 55 |
| R15 Hiro Tanaka | ROUTE_TO_REP P2 | Inbound demo above the ICP size (`INBOUND_DEMO_ABOVE_ICP`) |

Totals: ROUTE_TO_REP 4, OUTBOUND 4, MSP_TRACK 1, HUMAN_REVIEW 3, HELD 1, EXCLUDE 2, NURTURE 0.

## Assumptions

- **Jennifer (R04):** the inbound demo outranks MSP, so she goes ROUTE_TO_REP with reason `INBOUND_DEMO_MSP` rather than MSP_TRACK.
- **Hiro (R15):** the inbound demo outranks the >1,000-employee review rule, so he is routed to a rep at P2 with `INBOUND_DEMO_ABOVE_ICP`.
- **Rebecca (R14):** "Owner / Principal Consultant" matches no title rule, so she is mapped to MSP by the industry rule (MSP / IT Services).
- IT Manager and IT Support Manager count as **IT Decision Maker**.
- If no title rule matches and there is no API key, the persona defaults to **Non-buyer** (`persona_source=default_no_ai`), which can never reach OUTBOUND.
- Employee counts that differ by more than **25%** are treated as an enrichment conflict, and the CSV value is kept.
- Enrichment with confidence **below 0.6** is ignored (e.g. Meridian Schools at 0.5).
- **Fill-blank-only merge:** the previous `leads_output.csv` is treated as the existing CRM state, so a rerun never overwrites a non-empty value, even after a rule change. Use `--fresh` to rebuild.
- `audit_log.jsonl` is append-only across runs; `batch_id` tells the runs apart.

## Known limitations

- Enrichment and CRM are mocked with hand-written JSON. There is no real Salesforce push; "push" means writing to `leads_output.csv`.
- Only touch 1 (email) is generated. Touches 2 (LinkedIn) and 3 (call script) exist in the blueprint but are not generated.
- Persona rules are English-keyword regexes.
- The numbers ban also blocks legitimate digits outside first name and company (e.g. a title like "Tier 2 Support").
- Dedupe matches exact email only; there is no fuzzy matching on name or company.
- LLM calls go through OpenRouter (`requests`, no vendor SDK). The model list is in `config.LLM_MODELS`: `google/gemini-2.5-flash` first, with OpenRouter falling back to the next model automatically if one is down. JSON output is enforced with a strict JSON schema, and only providers that support it are used.
- We moved from the free Gemini API to OpenRouter because the free tier allowed only 20 requests per model per day and some models were overloaded (503) or retired (404).
- Rate limits and server errors get a short backoff (Retry-After if given, capped at 30s). A rejected key or no credits (401/402) switches the rest of the run to the template fallback instead of retrying.
- The retry-after-validation path and AI persona fallback are covered by unit tests with a fake client; on the sample data, every title matches a rule, so the persona fallback is never called.
