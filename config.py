"""
config.py - every business rule the pipeline uses, in one place.

Marketing / RevOps can tune personas, scoring, thresholds and messaging here
without touching pipeline.py.
"""

# ---------------------------------------------------------------------------
# Input contract (Step 1 Ingest)
# ---------------------------------------------------------------------------
EXPECTED_COLUMNS = [
    "first_name", "last_name", "title", "company", "company_domain",
    "email", "employee_count", "industry", "source",
]

# ---------------------------------------------------------------------------
# Hygiene rules (Step 2 Clean + Hygiene Gate)
# ---------------------------------------------------------------------------
PERSONAL_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com"}
TEAM_INBOX_PREFIXES = {"info", "sales", "support", "admin", "it"}
JUNK_VALUES = {"test", "asdf", "qwerty", "xxx", "n/a", "none", "null"}
JUNK_DOMAINS = {"test.com", "example.com"}
# A record missing any of these goes to HUMAN_REVIEW.
CORE_FIELDS = ["first_name", "last_name", "company", "company_domain", "email"]

# ---------------------------------------------------------------------------
# Personas (Step 3 Persona Mapping)
# Rules are checked top to bottom; first match wins. Patterns are regexes
# matched against the lowercased title (word boundaries matter: "it").
# Only if nothing matches do we ask the LLM to pick from PERSONAS.
# ---------------------------------------------------------------------------
PERSONAS = [
    "IT Decision Maker",
    "IT Practitioner",
    "Technical Executive",
    "MSP",
    "Non-buyer",
]

PERSONA_RULES = [
    ("Technical Executive", [
        r"\b(cto|cio|ciso)\b",
        r"\bchief (technology|information|information security) officer\b",
        r"\bvp,? (of )?(engineering|it|technology|information technology)\b",
    ]),
    ("MSP", [
        r"\bmanaged services?\b",
        r"\bmsp\b",
    ]),
    ("IT Decision Maker", [
        r"\b(director|head|manager)\b.*\b(it|information technology|infrastructure)\b",
        r"\b(it|information technology|infrastructure)\b.*\b(director|manager|head)\b",
    ]),
    ("IT Practitioner", [
        r"\b(sys(tems)? ?admin(istrator)?|network admin(istrator)?|it admin(istrator)?)\b",
        r"\b(it analyst|it specialist|it technician|help ?desk|desktop support|it support)\b",
    ]),
    ("Non-buyer", [
        r"\b(marketing|sales|recruit\w*|hr|human resources|finance|accountant|intern|student)\b",
    ]),
]

# Industry-level rule: anyone at an MSP / IT services firm maps to the MSP
# persona even if their title is generic (e.g. "Owner / Principal Consultant").
MSP_INDUSTRIES = {"msp / it services"}

# ---------------------------------------------------------------------------
# Scoring (Step 6 Qualify + Score)
# Fit 0-50 = persona (0-25) + employee band (0-15) + industry (0-10)
# Intent 0-50 = source weight
# ---------------------------------------------------------------------------
ICP_MIN_EMPLOYEES = 50
ICP_MAX_EMPLOYEES = 1000

PERSONA_FIT_POINTS = {
    "IT Decision Maker": 25,
    "Technical Executive": 22,
    "MSP": 20,
    "IT Practitioner": 15,
    "Non-buyer": 0,
}

BAND_POINTS_IN_ICP = 15       # 50 - 1,000 employees
BAND_POINTS_NEAR_ICP = 5      # 20-49 or 1,001-2,000
BAND_POINTS_OUTSIDE = 0

PRIORITY_INDUSTRIES = {
    "healthcare", "financial services", "education", "software",
    "technology", "msp / it services",
}
INDUSTRY_POINTS_PRIORITY = 10
INDUSTRY_POINTS_OTHER = 5
INDUSTRY_POINTS_MISSING = 0

# Intent: Inbound Demo Request > Webinar > Content Syndication > Trade Show > Form Fill / List Purchase.
# Matched by prefix so "Webinar - Zero Trust" counts as "Webinar".
SOURCE_WEIGHTS = {
    "Inbound Demo Request": 50,
    "Webinar": 40,
    "Content Syndication": 30,
    "Trade Show": 25,
    "Form Fill": 10,
    "List Purchase": 10,
}

# ---------------------------------------------------------------------------
# Routing (Step 7 Route)
# ---------------------------------------------------------------------------
OUTBOUND_MIN_SCORE = 60
REP_PRIORITY_P1_MIN_SCORE = 80   # inbound demo: score only sets priority
INBOUND_DEMO_SOURCE = "Inbound Demo Request"
DRAFT_LANES = {"OUTBOUND", "MSP_TRACK"}

# ---------------------------------------------------------------------------
# Enrichment (Step 5 Enrich)
# ---------------------------------------------------------------------------
ENRICHMENT_MIN_CONFIDENCE = 0.6        # below this we ignore the mock value
EMPLOYEE_COUNT_CONFLICT_TOLERANCE = 0.25  # >25% difference = conflict

# ---------------------------------------------------------------------------
# Drafting + validation (Steps 8 and 9)
# ---------------------------------------------------------------------------
PROMPT_VERSION = "draft_v1"
TEMPLATE_VERSION = "template_v1"
LLM_TEMPERATURE = 0.2

# The ONLY lead fields the LLM ever sees. Step 9 rejects any other field name
# in facts_used.
ALLOWLISTED_FIELDS = ["first_name", "company", "title", "persona", "industry", "source"]

BANNED_CLAIMS = [
    "hipaa", "soc 2", "soc2", "guarantee", "guaranteed", "guarantees",
    # competitor names
    "okta", "microsoft entra", "entra id", "azure ad", "active directory",
    "rippling", "kandji", "jamf", "onelogin", "ping identity", "duo",
]
MAX_SUBJECT_CHARS = 60
MAX_BODY_WORDS = 120

# LLM via OpenRouter (key in OPENROUTER_API_KEY). OpenRouter tries these in
# order, so the later ones are automatic fallbacks. OPENROUTER_MODEL env var
# puts a different model first.
LLM_MODELS = [
    "google/gemini-2.5-flash",
    "google/gemini-3.5-flash-lite",
    "openai/gpt-5-mini",
]
LLM_TIMEOUT_SECONDS = 60

# Approved messaging library: short, claim-free snippets per persona.
# No numbers, no compliance claims, no competitor names.
MESSAGING_LIBRARY = {
    "IT Decision Maker": {
        "value_line": "JumpCloud gives IT leaders one place to manage identities, devices and access, so the team spends less time stitching tools together.",
        "cta": "Open to a short call next week to see if it fits your roadmap?",
    },
    "IT Practitioner": {
        "value_line": "JumpCloud lets admins handle user onboarding, device management and SSO from one console instead of juggling separate tools.",
        "cta": "Want me to send a quick walkthrough of the admin console?",
    },
    "Technical Executive": {
        "value_line": "JumpCloud unifies identity, device and access management on one open platform, which helps simplify the stack as the company grows.",
        "cta": "Worth a brief conversation about where identity fits in your plans?",
    },
    "MSP": {
        "value_line": "JumpCloud's multi-tenant console lets MSPs manage identities and devices across every client from one place.",
        "cta": "Open to a quick chat about how other MSPs package this for clients?",
    },
}

# ---------------------------------------------------------------------------
# Sequence blueprint (Step 8). This build generates TOUCH 1 ONLY.
# ---------------------------------------------------------------------------
SEQUENCE_BLUEPRINT = [
    {"touch": 1, "channel": "email", "day": 0, "goal": "personalized intro using verified fields only", "generate": True},
    {"touch": 2, "channel": "linkedin", "day": 3, "goal": "connection request referencing touch 1", "generate": False},
    {"touch": 3, "channel": "call_script", "day": 7, "goal": "short call script with one discovery question", "generate": False},
]
