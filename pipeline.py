"""
pipeline.py - JumpCloud outbound lead workflow (10-step working slice).

Run:
    python pipeline.py <input.csv>            # merge into existing outputs (fill-blank-only)
    python pipeline.py <input.csv> --fresh    # start from empty outputs

Steps (names match the design diagram):
    Step 1  Ingest
    Step 2  Clean + Hygiene Gate
    Step 3  Persona Mapping
    Step 4  Dedupe + CRM Check
    Step 5  Enrich
    Step 6  Qualify + Score
    Step 7  Route
    Step 8  Draft All Touches Per Blueprint   (touch 1 only in this build)
    Step 9  Validate Every Touch
    Step 10 Push + Log

Enrichment and CRM are MOCKED (mock_enrichment.json, mock_crm.json).
An LLM (Gemini Flash via OpenRouter) is used only for (a) persona fallback
when no rule matches and (b) drafting touch 1. With no OPENROUTER_API_KEY the
run still completes using a clearly labeled template fallback.
"""

import csv
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

import config as C

HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "output"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

import warnings

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("pipeline")
# Keep the demo console readable: hide the old-Python SSL notice from urllib3.
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

# Audit events collected during the run, flushed to audit_log.jsonl in Step 10.
AUDIT = []

STEP_NAMES = {
    1: "Step 1 Ingest",
    2: "Step 2 Clean + Hygiene Gate",
    3: "Step 3 Persona Mapping",
    4: "Step 4 Dedupe + CRM Check",
    5: "Step 5 Enrich",
    6: "Step 6 Qualify + Score",
    7: "Step 7 Route",
    8: "Step 8 Draft All Touches Per Blueprint",
    9: "Step 9 Validate Every Touch",
    10: "Step 10 Push + Log",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(row, step, decision, reason="", prompt_version=""):
    """Record one audit line (one per record per step) and echo it to the console."""
    AUDIT.append({
        "batch_id": row["batch_id"],
        "record_id": row["record_id"],
        "step": STEP_NAMES[step],
        "decision": decision,
        "reason": reason,
        "prompt_version": prompt_version,
        "timestamp": now_iso(),
    })
    log.info("  [%s] %s %-22s -> %s%s", STEP_NAMES[step], row["record_id"], label(row)[:22],
             decision, f" ({reason})" if reason else "")


def label(row):
    """Short human label for logs: person name, or company if no person."""
    name = f"{row.get('clean_first_name', '')} {row.get('clean_last_name', '')}".strip()
    return name or row.get("clean_company") or row.get("company") or "(blank)"


def is_excluded(row):
    return row.get("gate") == "EXCLUDE" or row.get("duplicate_of")


# ===========================================================================
# LLM helper: OpenRouter (OpenAI-compatible chat API). Never prints or logs the key.
# ===========================================================================
class LLM:
    """Calls OpenRouter with a JSON schema so the answer is always structured JSON.

    Models come from config.LLM_MODELS (OPENROUTER_MODEL env var overrides the
    first one). OpenRouter's own `models` fallback list means that if the first
    model is down or rate limited, OpenRouter retries on the next one for us.
    With no OPENROUTER_API_KEY, or if the key is rejected / out of credits, the
    run continues with the clearly labeled template fallback.
    """

    def __init__(self):
        self.key = os.environ.get("OPENROUTER_API_KEY")
        self.models = list(C.LLM_MODELS)
        if os.environ.get("OPENROUTER_MODEL"):
            self.models.insert(0, os.environ["OPENROUTER_MODEL"])
        self.last_model = ""   # the model OpenRouter actually served on the last call
        self.disabled_reason = "" if self.key else "OPENROUTER_API_KEY not set"

    @property
    def available(self):
        return bool(self.key) and not self.disabled_reason

    @property
    def model(self):
        return self.last_model or self.models[0]

    @property
    def status(self):
        if self.available:
            return f"OpenRouter ({self.model})"
        return f"TEMPLATE FALLBACK ({self.disabled_reason})"

    def generate_json(self, prompt, schema, name="answer", retries=3):
        """Ask for JSON matching `schema`. Returns a dict, or None if the call failed.
        Rate limits and server errors get a short backoff (Retry-After if given)."""
        if not self.available:
            return None
        body = {
            "models": self.models,                       # OpenRouter tries these in order
            "messages": [{"role": "user", "content": prompt}],
            "temperature": C.LLM_TEMPERATURE,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": name, "strict": True, "schema": schema}},
            "provider": {"require_parameters": True},    # only providers that honour the schema
        }
        headers = {"Authorization": f"Bearer {self.key}", "X-Title": "JumpCloud Lead Pipeline"}
        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(OPENROUTER_URL, json=body, headers=headers, timeout=C.LLM_TIMEOUT_SECONDS)
            except requests.RequestException as e:   # timeout, network down...
                log.info("    OpenRouter call failed (%s), attempt %d/%d", type(e).__name__, attempt, retries)
                continue
            if resp.status_code in (401, 402, 403):
                # Bad key or no credits: stop calling for the rest of the run.
                self.disabled_reason = f"OpenRouter rejected the key or credits (HTTP {resp.status_code})"
                log.info("    %s; template fallback for the rest of the run", self.disabled_reason)
                return None
            if resp.status_code in (408, 429, 500, 502, 503, 504):
                if attempt < retries:
                    wait = min(30, float(resp.headers.get("Retry-After") or 2 ** attempt))
                    log.info("    OpenRouter %s, backing off %.0fs", resp.status_code, wait)
                    time.sleep(wait)
                continue
            if resp.status_code != 200:
                log.info("    OpenRouter error %s, giving up on this call", resp.status_code)
                return None
            try:
                data = resp.json()
                self.last_model = data.get("model", self.models[0])
                return json.loads(data["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError, ValueError):
                log.info("    OpenRouter returned non-JSON, giving up on this call")
                return None
        log.info("    OpenRouter still failing after %d attempts, giving up on this call", retries)
        return None


# ===========================================================================
# Step 1 Ingest
# ===========================================================================
def step_01_ingest(input_path):
    """Load the CSV, validate columns, assign batch_id, keep an untouched raw copy."""
    log.info("\n%s", STEP_NAMES[1])
    input_path = Path(input_path)
    # Timestamp + short random suffix so two runs in the same second never collide.
    batch_id = "B" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

    with open(input_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in C.EXPECTED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"Input is missing expected columns: {missing}")
        rows = [dict(r) for r in reader]

    # Byte-for-byte copy of the input, never modified afterwards.
    OUTPUT_DIR.mkdir(exist_ok=True)
    raw_copy = OUTPUT_DIR / f"raw_input_{batch_id}.csv"
    shutil.copyfile(input_path, raw_copy)

    for i, r in enumerate(rows, start=1):
        r["batch_id"] = batch_id
        r["record_id"] = f"R{i:02d}"
        audit(r, 1, "INGESTED", f"row {i} of {input_path.name}")
    log.info("  %d rows loaded, batch_id=%s, raw copy=%s", len(rows), batch_id, raw_copy.name)
    return rows, batch_id


# ===========================================================================
# Step 2 Clean + Hygiene Gate
# ===========================================================================
def fix_name_case(name):
    """sarah o'neil -> Sarah O'Neil, ANGELA -> Angela. Mixed case (McDonald) is left alone."""
    if not name or not (name.islower() or name.isupper()):
        return name

    def cap(part):
        return part[:1].upper() + part[1:].lower()
    # Capitalize after spaces, hyphens and apostrophes.
    return re.sub(r"[A-Za-z]+", lambda m: cap(m.group(0)), name)


TITLE_ACRONYMS = {"it", "cto", "cio", "ciso", "cso", "vp", "svp", "evp", "msp", "hr", "ceo", "coo", "cfo"}
TITLE_SMALL_WORDS = {"of", "and", "the", "for", "to", "in"}


def fix_title_case(title):
    """it admin -> IT Admin, DIRECTOR OF IT -> Director of IT. Mixed case is left alone."""
    if not title or not (title.islower() or title.isupper()):
        return title
    words = []
    for i, w in enumerate(title.lower().split(" ")):
        if w in TITLE_ACRONYMS:
            words.append(w.upper())
        elif w in TITLE_SMALL_WORDS and i > 0:
            words.append(w)
        else:
            words.append(w[:1].upper() + w[1:])
    return " ".join(words)


def is_junk_value(value):
    tokens = re.findall(r"[a-z]+", (value or "").lower())
    return any(t in C.JUNK_VALUES for t in tokens)


def step_02_clean_hygiene_gate(rows):
    """Trim + normalize, then gate each row: PASS, HUMAN_REVIEW or EXCLUDE."""
    log.info("\n%s", STEP_NAMES[2])
    for r in rows:
        changes, flags = [], []

        # --- Clean (original columns are never modified) ---
        for f in C.EXPECTED_COLUMNS:
            r[f"clean_{f}"] = (r.get(f) or "").strip()
        for f in ("first_name", "last_name"):
            fixed = fix_name_case(r[f"clean_{f}"])
            if fixed != r[f"clean_{f}"]:
                changes.append(f"{f} case fixed")
            r[f"clean_{f}"] = fixed
        fixed_title = fix_title_case(r["clean_title"])
        if fixed_title != r["clean_title"]:
            changes.append("title case fixed")
        r["clean_title"] = fixed_title
        if r["clean_email"] != r["clean_email"].lower():
            changes.append("email lowercased")
        r["clean_email"] = r["clean_email"].lower()
        r["clean_company_domain"] = r["clean_company_domain"].lower()

        emp = r["clean_employee_count"].replace(",", "")
        try:
            r["clean_employee_count"] = str(int(float(emp))) if emp else ""
        except ValueError:
            r["clean_employee_count"] = ""
            flags.append("NON_NUMERIC_EMPLOYEE_COUNT")

        # --- Hygiene checks ---
        email = r["clean_email"]
        local, _, email_domain = email.partition("@")
        domain = r["clean_company_domain"]

        junk = (email_domain in C.JUNK_DOMAINS or domain in C.JUNK_DOMAINS
                or local in C.JUNK_VALUES
                or sum(is_junk_value(r[f"clean_{f}"]) for f in ("first_name", "last_name", "title", "company")) >= 2)
        no_person = not r["clean_first_name"] and not r["clean_last_name"]
        team_inbox = local in C.TEAM_INBOX_PREFIXES
        missing = [f for f in C.CORE_FIELDS if not r[f"clean_{f}"]]
        personal = email_domain in C.PERSONAL_EMAIL_DOMAINS
        invalid_email = bool(email) and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email)
        mismatch = bool(email and domain) and not (email_domain == domain or email_domain.endswith("." + domain))

        if junk: flags.append("JUNK")
        if no_person: flags.append("NO_PERSON")
        if team_inbox: flags.append("TEAM_INBOX")
        if missing: flags.append("MISSING_CORE_FIELDS:" + "+".join(missing))
        if personal: flags.append("PERSONAL_EMAIL")
        if invalid_email: flags.append("INVALID_EMAIL")
        if mismatch: flags.append("DOMAIN_MISMATCH")

        # --- Gate ---
        if junk or no_person or team_inbox:
            r["gate"] = "EXCLUDE"
            r["gate_reason"] = "+".join(x for x, on in (("JUNK", junk), ("NO_PERSON", no_person), ("TEAM_INBOX", team_inbox)) if on)
        elif missing or personal or mismatch or invalid_email:
            r["gate"] = "HUMAN_REVIEW"
            r["gate_reason"] = "+".join(f.split(":")[0] for f in flags)
        else:
            r["gate"] = "PASS"
            r["gate_reason"] = ""

        r["hygiene_flags"] = ";".join(flags)
        r["cleaning_changes"] = ";".join(changes)
        audit(r, 2, r["gate"], r["gate_reason"] or ("clean" + (f"; {', '.join(changes)}" if changes else "")))
    return rows


# ===========================================================================
# Step 3 Persona Mapping
# ===========================================================================
PERSONA_SCHEMA = {
    "type": "object",
    "properties": {"persona": {"type": "string", "enum": C.PERSONAS}},
    "required": ["persona"],
    "additionalProperties": False,
}


def map_persona_by_rules(title, industry):
    t = (title or "").lower()
    for persona, patterns in C.PERSONA_RULES:
        if any(re.search(p, t) for p in patterns):
            return persona
    if (industry or "").lower() in C.MSP_INDUSTRIES:
        return "MSP"
    return None


def step_03_persona_mapping(rows, llm):
    """Rules dictionary first; only if no rule matches, ask the LLM to pick from the fixed list."""
    log.info("\n%s", STEP_NAMES[3])
    for r in rows:
        if is_excluded(r):
            r["persona"], r["persona_source"] = "", ""
            audit(r, 3, "SKIPPED", "excluded at gate")
            continue
        persona = map_persona_by_rules(r["clean_title"], r["clean_industry"])
        if persona:
            r["persona"], r["persona_source"] = persona, "rules"
        else:
            prompt = (
                "Classify this B2B contact for an IT / identity management vendor. "
                f"Pick exactly one persona from {C.PERSONAS}.\n"
                f"Title: {r['clean_title']}\nIndustry: {r['clean_industry']}"
            )
            out = llm.generate_json(prompt, PERSONA_SCHEMA, name="persona")
            if out and out.get("persona") in C.PERSONAS:
                r["persona"], r["persona_source"] = out["persona"], "ai"
            else:
                # No AI available: safest default is Non-buyer (it can never reach OUTBOUND).
                r["persona"], r["persona_source"] = "Non-buyer", "default_no_ai"
        audit(r, 3, r["persona"], f"persona_source={r['persona_source']}; title='{r['clean_title']}'")
    return rows


# ===========================================================================
# Step 4 Dedupe + CRM Check
# ===========================================================================
def intent_points(source):
    s = (source or "").lower()
    for name, pts in C.SOURCE_WEIGHTS.items():
        if s.startswith(name.lower()):
            return pts
    return 0


def step_04_dedupe_crm_check(rows, crm):
    """Exact email dedupe, group by company_domain, pick one primary per account, check mock CRM."""
    log.info("\n%s", STEP_NAMES[4])

    # Exact email dedupe: the first occurrence wins.
    seen = {}
    for r in rows:
        r["duplicate_of"] = ""
        e = r["clean_email"]
        if e and not r["gate"] == "EXCLUDE":
            if e in seen:
                r["duplicate_of"] = seen[e]
            else:
                seen[e] = r["record_id"]

    # Group by domain and choose a primary: highest intent first, then persona fit.
    groups = {}
    for r in rows:
        r["account_group"] = r["clean_company_domain"] or f"no-domain:{r['record_id']}"
        r["is_primary"] = False
        if not is_excluded(r):
            groups.setdefault(r["account_group"], []).append(r)
    for members in groups.values():
        # Gate-passed contacts win over review contacts, so a review row never "owns" an account.
        best = max(members, key=lambda m: (m["gate"] == "PASS", intent_points(m["clean_source"]),
                                           C.PERSONA_FIT_POINTS.get(m["persona"], 0)))
        best["is_primary"] = True

    for r in rows:
        if r["gate"] == "EXCLUDE":
            audit(r, 4, "SKIPPED", "excluded at gate")
            continue
        if r["duplicate_of"]:
            audit(r, 4, "DUPLICATE", f"same email as {r['duplicate_of']}")
            continue
        # Mock CRM check: where owner, open opp, customer/partner status and opt-out are checked.
        rec = crm.get(r["clean_company_domain"], {})
        r["crm_owner"] = rec.get("account_owner", "")
        r["crm_open_opportunity"] = bool(rec.get("open_opportunity", False))
        r["crm_customer_status"] = rec.get("customer_status", "")
        r["crm_partner_status"] = rec.get("partner_status", "")
        r["crm_opted_out"] = r["clean_email"] in rec.get("opted_out_emails", [])
        crm_note = "no CRM match (mock)" if not rec else (
            f"owner={r['crm_owner']}, open_opp={r['crm_open_opportunity']}, "
            f"customer={r['crm_customer_status']}, opted_out={r['crm_opted_out']}")
        others = len(groups.get(r["account_group"], [])) - 1
        decision = "PRIMARY" if r["is_primary"] else "NON_PRIMARY"
        audit(r, 4, decision, f"account {r['account_group']} (+{others} other contacts); {crm_note}")
    return rows


# ===========================================================================
# Step 5 Enrich
# ===========================================================================
def step_05_enrich(rows, enrichment):
    """Look up mock firmographics by DOMAIN only. On conflict, flag it and keep the CSV value."""
    log.info("\n%s", STEP_NAMES[5])
    for r in rows:
        r["enrichment_conflict"] = False
        r["enrichment_notes"] = ""
        r["enriched_hq_country"] = ""
        r["enrichment_confidence"] = ""
        if is_excluded(r):
            audit(r, 5, "SKIPPED", "excluded")
            continue
        rec = enrichment.get(r["clean_company_domain"]) if r["clean_company_domain"] else None
        if not rec:
            r["enrichment_notes"] = "no match by domain"
            audit(r, 5, "NO_MATCH", "domain not in mock enrichment")
            continue
        r["enrichment_confidence"] = str(rec.get("confidence", ""))
        if rec.get("confidence", 0) < C.ENRICHMENT_MIN_CONFIDENCE:
            r["enrichment_notes"] = f"ignored: confidence {rec.get('confidence')} below {C.ENRICHMENT_MIN_CONFIDENCE}"
            audit(r, 5, "IGNORED_LOW_CONFIDENCE", r["enrichment_notes"])
            continue

        notes = []
        # Employee count: compare, never overwrite. Fill only if the CSV is blank.
        csv_emp, mock_emp = r["clean_employee_count"], rec.get("employee_count")
        if mock_emp is not None:
            if not csv_emp:
                r["clean_employee_count"] = str(mock_emp)
                notes.append(f"employee_count filled from enrichment ({mock_emp})")
            else:
                diff = abs(int(csv_emp) - mock_emp) / max(int(csv_emp), 1)
                if diff > C.EMPLOYEE_COUNT_CONFLICT_TOLERANCE:
                    r["enrichment_conflict"] = True
                    notes.append(f"employee_count CONFLICT: csv={csv_emp} vs enrichment={mock_emp} (kept csv)")
        # Industry: same rule.
        mock_ind = rec.get("industry", "")
        if mock_ind:
            if not r["clean_industry"]:
                r["clean_industry"] = mock_ind
                notes.append(f"industry filled from enrichment ({mock_ind})")
            elif mock_ind.lower() != r["clean_industry"].lower():
                r["enrichment_conflict"] = True
                notes.append(f"industry CONFLICT: csv={r['clean_industry']} vs enrichment={mock_ind} (kept csv)")
        r["enriched_hq_country"] = rec.get("hq_country", "")
        r["enrichment_notes"] = "; ".join(notes) or "matched, consistent with csv"
        audit(r, 5, "CONFLICT" if r["enrichment_conflict"] else "ENRICHED", r["enrichment_notes"])
    return rows


# ===========================================================================
# Step 6 Qualify + Score
# ===========================================================================
def band_points(emp):
    if not emp:
        return C.BAND_POINTS_OUTSIDE
    n = int(emp)
    if C.ICP_MIN_EMPLOYEES <= n <= C.ICP_MAX_EMPLOYEES:
        return C.BAND_POINTS_IN_ICP
    if 20 <= n < C.ICP_MIN_EMPLOYEES or C.ICP_MAX_EMPLOYEES < n <= 2000:
        return C.BAND_POINTS_NEAR_ICP
    return C.BAND_POINTS_OUTSIDE


def industry_points(industry):
    if not industry:
        return C.INDUSTRY_POINTS_MISSING
    return C.INDUSTRY_POINTS_PRIORITY if industry.lower() in C.PRIORITY_INDUSTRIES else C.INDUSTRY_POINTS_OTHER


def step_06_qualify_score(rows):
    """Fit 0-50 (persona + employee band + industry) + Intent 0-50 (source) = Total 0-100."""
    log.info("\n%s", STEP_NAMES[6])
    for r in rows:
        if is_excluded(r):
            r["fit_score"] = r["intent_score"] = r["total_score"] = ""
            audit(r, 6, "SKIPPED", "excluded")
            continue
        p = C.PERSONA_FIT_POINTS.get(r["persona"], 0)
        b = band_points(r["clean_employee_count"])
        i = industry_points(r["clean_industry"])
        r["fit_score"] = p + b + i
        r["intent_score"] = intent_points(r["clean_source"])
        r["total_score"] = r["fit_score"] + r["intent_score"]
        audit(r, 6, f"SCORE {r['total_score']}",
              f"fit {r['fit_score']} (persona {p}, band {b}, industry {i}) + intent {r['intent_score']}")
    return rows


# ===========================================================================
# Step 7 Route
# ===========================================================================
def route_one(r):
    """Return (lane, reason_code, priority) for one row. Order matters: first match wins."""
    msp = r["persona"] == "MSP" or r["clean_industry"].lower() in C.MSP_INDUSTRIES
    emp = int(r["clean_employee_count"]) if r.get("clean_employee_count") else 0

    if r["gate"] == "EXCLUDE":
        return "EXCLUDE", r["gate_reason"], ""
    if r["duplicate_of"]:
        return "EXCLUDE", "DUPLICATE_EMAIL", ""
    if r.get("crm_opted_out"):
        return "EXCLUDE", "CRM_OPT_OUT", ""
    if r["gate"] == "HUMAN_REVIEW":
        return "HUMAN_REVIEW", "HYGIENE_" + r["gate_reason"], ""
    # Any inbound demo request that passed the gate goes to a rep. Score only sets priority.
    if r["clean_source"].lower().startswith(C.INBOUND_DEMO_SOURCE.lower()):
        reason = "INBOUND_DEMO"
        if msp: reason += "_MSP"
        if emp > C.ICP_MAX_EMPLOYEES: reason += "_ABOVE_ICP"
        if r["enrichment_conflict"]: reason += "_ENRICHMENT_CONFLICT"
        prio = "P1" if r["total_score"] >= C.REP_PRIORITY_P1_MIN_SCORE else "P2"
        return "ROUTE_TO_REP", reason, prio
    if r.get("crm_owner") or r.get("crm_customer_status") in ("customer",) or r.get("crm_partner_status") not in ("", "none", None):
        return "HUMAN_REVIEW", "CRM_EXISTING_RELATIONSHIP", ""
    if r["enrichment_conflict"]:
        return "HUMAN_REVIEW", "ENRICHMENT_CONFLICT", ""
    if emp > C.ICP_MAX_EMPLOYEES:
        return "HUMAN_REVIEW", "ABOVE_ICP_BAND", ""
    if msp:
        return "MSP_TRACK", "MSP_PERSONA_OR_INDUSTRY", ""
    if r["persona"] == "Non-buyer":
        return "NURTURE", "NON_BUYER_PERSONA", ""
    if r["total_score"] >= C.OUTBOUND_MIN_SCORE and r["gate"] == "PASS":
        return "OUTBOUND", f"SCORE_{r['total_score']}_CLEAN_WORK_EMAIL", ""
    return "NURTURE", f"SCORE_{r['total_score']}_BELOW_{C.OUTBOUND_MIN_SCORE}", ""


def step_07_route(rows):
    """One lane per row, then HOLD non-primary contacts at accounts already routed to a rep."""
    log.info("\n%s", STEP_NAMES[7])
    for r in rows:
        r["lane"], r["reason_code"], r["priority"] = route_one(r)

    rep_accounts = {r["account_group"] for r in rows if r["lane"] == "ROUTE_TO_REP" and r["is_primary"]}
    for r in rows:
        # Excluded and review rows keep their lane so a hygiene / CRM flag is never hidden by HELD.
        if r["lane"] not in ("EXCLUDE", "HUMAN_REVIEW") and not r["is_primary"] and r["account_group"] in rep_accounts:
            owner = next(x for x in rows if x["account_group"] == r["account_group"] and x["is_primary"])
            r["lane"], r["reason_code"] = "HELD", "ACCOUNT_OWNED_BY_REP"
            r["held_note"] = f"account owned by rep (primary contact {owner['record_id']} {label(owner)})"
        audit(r, 7, r["lane"], r.get("held_note") or r["reason_code"] + (f" priority={r['priority']}" if r["priority"] else ""))
    return rows


# ===========================================================================
# Step 8 Draft All Touches Per Blueprint
# ===========================================================================
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "opener": {"type": "string"},
        "value_line": {"type": "string"},
        "cta": {"type": "string"},
        "facts_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["subject", "opener", "value_line", "cta", "facts_used"],
    "additionalProperties": False,
}


def allowlisted_facts(r):
    """The ONLY lead data the LLM ever sees."""
    return {
        "first_name": r["clean_first_name"],
        "company": r["clean_company"],
        "title": r["clean_title"],
        "persona": r["persona"],
        "industry": r["clean_industry"],
        "source": r["clean_source"],
    }


def build_draft_prompt(facts, snippet, touch, feedback=""):
    return (
        "You write touch 1 (a short first email) of an outbound sequence for JumpCloud, "
        "an IT / identity and access management platform.\n"
        f"Blueprint goal for this touch: {touch['goal']}.\n\n"
        "VERIFIED FACTS (use only these; never invent anything else about the person or company):\n"
        f"{json.dumps(facts, indent=2)}\n\n"
        "APPROVED MESSAGING (base the value_line and cta on this; light rephrasing only):\n"
        f"{json.dumps(snippet, indent=2)}\n\n"
        "RULES:\n"
        f"- Use first_name and company exactly as written: '{facts['first_name']}', '{facts['company']}'.\n"
        "- No numbers, digits or percentages. No compliance claims (HIPAA, SOC 2). No guarantees. "
        "No competitor names. No placeholders or brackets.\n"
        f"- Subject under {C.MAX_SUBJECT_CHARS} characters. opener + value_line + cta under {C.MAX_BODY_WORDS} words total.\n"
        f"- facts_used: list the field names from VERIFIED FACTS you actually used, chosen from {C.ALLOWLISTED_FIELDS}.\n"
        "Return JSON with keys subject, opener, value_line, cta, facts_used."
        + (f"\n\nYOUR PREVIOUS DRAFT FAILED VALIDATION: {feedback}. Fix it." if feedback else "")
    )


def template_draft(facts, snippet):
    """Safe, deterministic fallback built only from verified fields + the approved library."""
    src = facts["source"].lower()
    if src.startswith("webinar"):
        hook = "thanks for joining our recent webinar."
    elif src.startswith("content syndication"):
        hook = "thanks for checking out some of our recent content."
    elif src.startswith("trade show"):
        hook = "it was good to connect at the recent trade show."
    else:
        hook = "I wanted to reach out with a quick idea."
    subject = f"Quick idea for {facts['company']}"
    if len(subject) >= C.MAX_SUBJECT_CHARS:
        subject = "Quick idea for your IT team"
    return {
        "subject": subject,
        "opener": f"Hi {facts['first_name']}, {hook} Since you're the {facts['title']} at {facts['company']}, this seemed worth sharing.",
        "value_line": snippet["value_line"],
        "cta": snippet["cta"],
        "facts_used": ["first_name", "company", "title", "source"],  # persona only picks the snippet
    }


def body_of(d):
    return "\n\n".join(x for x in (d.get("opener", ""), d.get("value_line", ""), d.get("cta", "")) if x)


def step_08_draft_all_touches_per_blueprint(rows, llm):
    """Use the blueprint from config.py, but GENERATE TOUCH 1 ONLY. Drafts OUTBOUND + MSP_TRACK rows."""
    log.info("\n%s", STEP_NAMES[8])
    touch = next(t for t in C.SEQUENCE_BLUEPRINT if t["touch"] == 1)
    skipped = [t["channel"] for t in C.SEQUENCE_BLUEPRINT if not t["generate"]]
    log.info("  Blueprint: generating touch 1 (%s); not generated in this build: %s", touch["channel"], ", ".join(skipped))
    log.info("  Drafting engine: %s", llm.status)
    for r in rows:
        r["draft"] = None
        r["draft_source"] = ""
        r["prompt_version"] = ""
        if r["lane"] not in C.DRAFT_LANES:
            audit(r, 8, "NOT_DRAFTED", f"lane {r['lane']} is not drafted")
            continue
        facts = allowlisted_facts(r)
        snippet = C.MESSAGING_LIBRARY[r["persona"]] if r["persona"] in C.MESSAGING_LIBRARY else C.MESSAGING_LIBRARY["IT Decision Maker"]
        r["_facts"], r["_snippet"], r["_touch"] = facts, snippet, touch
        out = llm.generate_json(build_draft_prompt(facts, snippet, touch), DRAFT_SCHEMA, name="touch_1_email")
        if out:
            r["draft"], r["draft_source"], r["prompt_version"] = out, "llm", C.PROMPT_VERSION
            audit(r, 8, "DRAFTED_LLM", f"touch 1 {touch['channel']} via {llm.model}", C.PROMPT_VERSION)
        else:
            r["draft"], r["draft_source"], r["prompt_version"] = template_draft(facts, snippet), "template", C.TEMPLATE_VERSION
            why = "no API key" if not llm.available else "LLM call failed"
            audit(r, 8, "DRAFTED_TEMPLATE", f"template fallback: {why}", C.TEMPLATE_VERSION)
    return rows


# ===========================================================================
# Step 9 Validate Every Touch  (plain code, no AI)
# ===========================================================================
def validate_touch(d, facts):
    """Return a list of failure reasons. Empty list = pass."""
    errs = []
    subject, body = d.get("subject", ""), body_of(d)
    text = f"{subject}\n{body}"

    bad_facts = [f for f in d.get("facts_used", []) if f not in C.ALLOWLISTED_FIELDS]
    if bad_facts:
        errs.append(f"non-allowlisted facts_used: {bad_facts}")
    if facts["first_name"] not in body:
        errs.append("first name missing or altered")
    if facts["company"] not in body:
        errs.append("company missing or altered")

    low = text.lower()
    for claim in C.BANNED_CLAIMS:
        if re.search(r"\b" + re.escape(claim) + r"\b", low):
            errs.append(f"banned claim: '{claim}'")
    # Numbers / percentages. Digits that are part of the verified name/company are allowed.
    scrubbed = text.replace(facts["first_name"], "").replace(facts["company"], "")
    if re.search(r"\d|%|\bpercent\b", scrubbed):
        errs.append("contains numbers or percentages")
    if re.search(r"\[|\]|\{|\}", text):
        errs.append("contains placeholder brackets")
    if len(subject) >= C.MAX_SUBJECT_CHARS:
        errs.append(f"subject {len(subject)} chars (limit {C.MAX_SUBJECT_CHARS})")
    words = len(body.split())
    if words >= C.MAX_BODY_WORDS:
        errs.append(f"body {words} words (limit {C.MAX_BODY_WORDS})")
    if not subject or not d.get("opener") or not d.get("cta"):
        errs.append("missing subject/opener/cta")
    return errs


def step_09_validate_every_touch(rows, llm):
    """Validate; on fail retry the LLM once, then fall back to the safe template."""
    log.info("\n%s", STEP_NAMES[9])
    for r in rows:
        r["validation_status"] = ""
        r["validation_errors"] = ""
        if not r.get("draft"):
            audit(r, 9, "NOT_APPLICABLE", "no draft")
            continue
        facts, snippet = r["_facts"], r["_snippet"]
        errs = validate_touch(r["draft"], facts)
        history = []

        if not errs:
            r["validation_status"] = "pass"
        else:
            history.append(f"attempt 1 ({r['draft_source']}): {'; '.join(errs)}")
            retry = None
            if r["draft_source"] == "llm":
                retry = llm.generate_json(build_draft_prompt(facts, snippet, r["_touch"], "; ".join(errs)), DRAFT_SCHEMA, name="touch_1_email")
            if retry and not validate_touch(retry, facts):
                r["draft"], r["validation_status"] = retry, "pass_after_retry"
            else:
                if retry:
                    history.append("retry (llm): " + "; ".join(validate_touch(retry, facts)))
                r["draft"], r["draft_source"], r["prompt_version"] = template_draft(facts, snippet), "template", C.TEMPLATE_VERSION
                tmpl_errs = validate_touch(r["draft"], facts)
                if tmpl_errs:
                    # Even the template failed: do not send anything, a human looks at it.
                    r["validation_status"] = "failed"
                    r["lane"], r["reason_code"] = "HUMAN_REVIEW", "DRAFT_VALIDATION_FAILED"
                    history.append("template: " + "; ".join(tmpl_errs))
                else:
                    r["validation_status"] = "template_fallback"

        # A template used because there was no key is also labeled template_fallback.
        if r["validation_status"] == "pass" and r["draft_source"] == "template":
            r["validation_status"] = "template_fallback"
            history.append("no LLM draft (template used from the start)")

        r["validation_errors"] = " | ".join(history)
        d = r["draft"]
        r["draft_subject"] = d["subject"]
        r["draft_body"] = body_of(d)
        r["facts_used"] = ",".join(d.get("facts_used", []))
        audit(r, 9, r["validation_status"].upper(), r["validation_errors"] or "all checks passed", r["prompt_version"])
    return rows


# ===========================================================================
# Step 10 Push + Log
# ===========================================================================
OUTPUT_COLUMNS = (
    ["batch_id", "last_seen_batch_id", "record_id"]
    + C.EXPECTED_COLUMNS
    + [f"clean_{c}" for c in C.EXPECTED_COLUMNS]
    + ["cleaning_changes", "hygiene_flags", "persona", "persona_source",
       "account_group", "is_primary", "crm_owner", "crm_open_opportunity", "crm_customer_status",
       "enrichment_conflict", "enrichment_notes", "enriched_hq_country", "enrichment_confidence",
       "fit_score", "intent_score", "total_score", "lane", "reason_code", "priority",
       "draft_subject", "draft_body", "facts_used", "validation_status", "validation_errors", "prompt_version"]
)
ALWAYS_UPDATE = {"last_seen_batch_id"}  # bookkeeping column, safe to refresh every run


def record_key(r):
    return r.get("clean_email") or "|".join([r.get("clean_first_name", ""), r.get("clean_last_name", ""), r.get("clean_company", "")]).lower()


def step_10_push_log(rows, batch_id, input_path, llm, fresh=False):
    """Write leads_output.csv (fill-blank-only), audit_log.jsonl and run_summary.md."""
    log.info("\n%s", STEP_NAMES[10])
    out_csv = OUTPUT_DIR / "leads_output.csv"
    audit_path = OUTPUT_DIR / "audit_log.jsonl"
    summary_path = OUTPUT_DIR / "run_summary.md"
    if fresh:
        for p in (out_csv, audit_path):
            if p.exists():
                p.unlink()

    # Existing "CRM" state = the previous leads_output.csv.
    existing = {}
    if out_csv.exists():
        with open(out_csv, newline="", encoding="utf-8") as f:
            for e in csv.DictReader(f):
                existing[record_key(e)] = e

    preserved = 0
    merged_rows = []
    for r in rows:
        r["last_seen_batch_id"] = batch_id
        new = {c: ("" if r.get(c) is None else str(r.get(c))) for c in OUTPUT_COLUMNS}
        old = existing.pop(record_key(r), None)
        kept = 0
        if old:
            for c in OUTPUT_COLUMNS:
                if c not in ALWAYS_UPDATE and old.get(c, "") != "":
                    if old[c] != new[c]:
                        kept += 1
                    new[c] = old[c]  # never overwrite an existing non-empty field
        preserved += kept
        merged_rows.append(new)
        audit(r, 10, "UPDATED_BLANKS_ONLY" if old else "INSERTED",
              f"{kept} existing non-empty fields kept" if old else "new record", r.get("prompt_version", ""))
    merged_rows.extend(existing.values())  # leads from earlier batches stay untouched

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(merged_rows)

    with open(audit_path, "a", encoding="utf-8") as f:
        for a in AUDIT:
            f.write(json.dumps(a) + "\n")

    summary_path.write_text(build_summary(rows, batch_id, input_path, llm, preserved), encoding="utf-8")
    log.info("  wrote %s (%d rows), %s (+%d lines), %s",
             out_csv.name, len(merged_rows), audit_path.name, len(AUDIT), summary_path.name)
    if preserved:
        log.info("  NOTE: %d fields kept from a previous push (fill-blank-only). Use --fresh to rebuild.", preserved)


def build_summary(rows, batch_id, input_path, llm, preserved):
    lanes = ["ROUTE_TO_REP", "MSP_TRACK", "OUTBOUND", "NURTURE", "HUMAN_REVIEW", "HELD", "EXCLUDE"]
    counts = {l: sum(1 for r in rows if r["lane"] == l) for l in lanes}
    L = [f"# Run summary - batch {batch_id}", "",
         f"- Input: `{Path(input_path).name}` ({len(rows)} rows)",
         f"- Generated: {now_iso()}",
         f"- Drafting engine: **{llm.status}**",
         f"- Persona sources: " + ", ".join(f"{s}={sum(1 for r in rows if r.get('persona_source') == s)}"
                                            for s in ("rules", "ai", "default_no_ai")),
         f"- Fields kept from previous push (fill-blank-only): {preserved}", "",
         "## Counts per lane", "", "| Lane | Count |", "|---|---|"]
    L += [f"| {l} | {counts[l]} |" for l in lanes]

    L += ["", "## Leads by lane", "", "| ID | Name | Company | Persona | Score | Lane | Reason |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['record_id']} | {label(r)} | {r['clean_company'] or '-'} | {r.get('persona') or '-'} | "
                 f"{r.get('total_score', '')} | {r['lane']} | {r['reason_code']}{(' ' + r['priority']) if r.get('priority') else ''} |")

    L += ["", "## Validation failures and retries", ""]
    issues = [r for r in rows if r.get("validation_status") and r["validation_status"] != "pass"]
    if not issues:
        L.append("None. Every drafted touch passed on the first try.")
    for r in issues:
        L.append(f"- {r['record_id']} {label(r)}: **{r['validation_status']}** - {r['validation_errors']}")

    L += ["", "## Enrichment conflicts", ""]
    conf = [r for r in rows if r.get("enrichment_conflict")]
    if not conf:
        L.append("None.")
    for r in conf:
        L.append(f"- {r['record_id']} {label(r)} ({r['clean_company_domain']}): {r['enrichment_notes']}")

    L += ["", "## Excluded, held and review rows", ""]
    for r in rows:
        if r["lane"] == "EXCLUDE":
            L.append(f"- {r['record_id']} {label(r)} - EXCLUDE: {r['reason_code']} (flags: {r['hygiene_flags'] or 'none'})")
        elif r["lane"] == "HELD":
            L.append(f"- {r['record_id']} {label(r)} - HELD: {r['held_note']}")
        elif r["lane"] == "HUMAN_REVIEW":
            detail = r["hygiene_flags"] if r["reason_code"].startswith("HYGIENE") else (r.get("enrichment_notes") or r["reason_code"])
            L.append(f"- {r['record_id']} {label(r)} - HUMAN_REVIEW: {r['reason_code']} ({detail})")

    L += ["", "## Drafted touches (touch 1 email)", ""]
    for r in rows:
        if r.get("draft_subject"):
            L += [f"### {r['record_id']} {label(r)} - {r['clean_company']} ({r['lane']}, {r['validation_status']}, {r['prompt_version']})",
                  "", f"**Subject:** {r['draft_subject']}", ""]
            L += ["> " + line if line else ">" for line in r["draft_body"].split("\n")]
            L += ["", f"_facts_used: {r['facts_used']}_", ""]
    return "\n".join(L) + "\n"


# ===========================================================================
# Main
# ===========================================================================
def load_json(name):
    with open(HERE / name, encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}  # drop _note keys


def run(input_path, fresh=False):
    AUDIT.clear()
    llm = LLM()
    log.info("JumpCloud outbound pipeline | AI mode: %s", llm.status)
    rows, batch_id = step_01_ingest(input_path)
    rows = step_02_clean_hygiene_gate(rows)
    rows = step_03_persona_mapping(rows, llm)
    rows = step_04_dedupe_crm_check(rows, load_json("mock_crm.json"))
    rows = step_05_enrich(rows, load_json("mock_enrichment.json"))
    rows = step_06_qualify_score(rows)
    rows = step_07_route(rows)
    rows = step_08_draft_all_touches_per_blueprint(rows, llm)
    rows = step_09_validate_every_touch(rows, llm)
    step_10_push_log(rows, batch_id, input_path, llm, fresh=fresh)
    log.info("\nDone. Outputs in %s/", OUTPUT_DIR.name)
    return rows


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        raise SystemExit("usage: python pipeline.py <input.csv> [--fresh]")
    run(args[0], fresh="--fresh" in sys.argv)
