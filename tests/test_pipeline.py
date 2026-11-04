"""Fast, offline tests for pipeline.py. No network: OPENROUTER_API_KEY is removed."""
import csv
import json
from pathlib import Path

import pytest

import config as C
import pipeline as P

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_leads.csv"


@pytest.fixture(autouse=True)
def _clean_audit():
    P.AUDIT.clear()
    yield
    P.AUDIT.clear()


def make_row(**overrides):
    row = {
        "first_name": "Marcus", "last_name": "Delgado", "title": "IT Director",
        "company": "Brightline Logistics", "company_domain": "brightlinelogistics.com",
        "email": "mdelgado@brightlinelogistics.com", "employee_count": "240",
        "industry": "Transportation", "source": "Webinar - Zero Trust",
        "batch_id": "BTEST", "record_id": "R01",
    }
    row.update(overrides)
    return row


def gate(**overrides):
    return P.step_02_clean_hygiene_gate([make_row(**overrides)])[0]


# ---------------------------------------------------------------- Step 2
@pytest.mark.parametrize("raw, expected", [
    ("sarah", "Sarah"),
    ("o'neil", "O'Neil"),
    ("ANGELA", "Angela"),
    ("McDonald", "McDonald"),
])
def test_fix_name_case(raw, expected):
    assert P.fix_name_case(raw) == expected


def test_fix_title_case():
    assert P.fix_title_case("it admin") == "IT Admin"
    assert P.fix_title_case("Senior IT Analyst") == "Senior IT Analyst"


def test_gate_clean_row_passes():
    r = gate()
    assert r["gate"] == "PASS" and r["gate_reason"] == ""


def test_gate_junk_test_domain_excluded():
    r = gate(first_name="asdf", last_name="test", title="test", company="test co",
             company_domain="test.com", email="test@test.com")
    assert r["gate"] == "EXCLUDE"
    assert "JUNK" in r["gate_reason"]


def test_gate_team_inbox_no_person_excluded():
    r = gate(first_name="", last_name="", title="", company="Acme Widgets",
             company_domain="acmewidgets.com", email="info@acmewidgets.com")
    assert r["gate"] == "EXCLUDE"
    assert "NO_PERSON" in r["gate_reason"] and "TEAM_INBOX" in r["gate_reason"]


def test_gate_personal_email_review():
    r = gate(company_domain="gmail.com", email="marcus@gmail.com")
    assert r["gate"] == "HUMAN_REVIEW"
    assert "PERSONAL_EMAIL" in r["gate_reason"]


def test_gate_domain_mismatch_review():
    r = gate(email="mdelgado@otherco.com")
    assert r["gate"] == "HUMAN_REVIEW"
    assert r["gate_reason"] == "DOMAIN_MISMATCH"


def test_gate_missing_core_fields_review():
    r = gate(last_name="", company="", company_domain="", email="")
    assert r["gate"] == "HUMAN_REVIEW"
    assert "MISSING_CORE_FIELDS" in r["gate_reason"]


# ---------------------------------------------------------------- Step 3
@pytest.mark.parametrize("title, industry, expected", [
    ("IT Director", "Transportation", "IT Decision Maker"),
    ("Sr. Systems Administrator", "Healthcare", "IT Practitioner"),
    ("Head of IT", "Technology", "IT Decision Maker"),
    ("Managed Services Lead", "MSP / IT Services", "MSP"),
    ("IT Manager", "Software", "IT Decision Maker"),
    ("VP Engineering", "Software", "Technical Executive"),
    ("Director of Information Technology", "Healthcare", "IT Decision Maker"),
    ("it admin", "Healthcare", "IT Practitioner"),
    ("Chief Technology Officer", "Financial Services", "Technical Executive"),
    ("Network Administrator", "Retail", "IT Practitioner"),
    ("IT Support Manager", "Education", "IT Decision Maker"),
    ("Owner / Principal Consultant", "MSP / IT Services", "MSP"),  # industry rule
    ("Senior IT Analyst", "Manufacturing", "IT Practitioner"),
    ("Marketing Coordinator", "Software", "Non-buyer"),
    ("Chief Happiness Officer", "Software", None),  # no rule -> AI fallback
])
def test_map_persona_by_rules(title, industry, expected):
    assert P.map_persona_by_rules(title, industry) == expected


def test_persona_fallback_without_ai_is_non_buyer():
    class NoAI:
        available = False
        model = None
        def generate_json(self, *a, **k):
            return None
    r = gate(title="Chief Happiness Officer")
    P.step_03_persona_mapping([r], NoAI())
    assert r["persona"] == "Non-buyer" and r["persona_source"] == "default_no_ai"


# ---------------------------------------------------------------- Step 6
@pytest.mark.parametrize("source, pts", [
    ("Webinar - Zero Trust", 40),
    ("Inbound Demo Request", 50),
    ("Content Syndication", 30),
    ("Trade Show", 25),
    ("List Purchase", 10),
    ("Cold Call", 0),
])
def test_intent_points_prefix(source, pts):
    assert P.intent_points(source) == pts


# ---------------------------------------------------------------- Step 9 validation
FACTS = {"first_name": "Sarah", "company": "Cedar Peak Health", "title": "IT Director",
         "persona": "IT Decision Maker", "industry": "Healthcare", "source": "Webinar"}


def good_draft(**overrides):
    d = {
        "subject": "Quick idea for Cedar Peak Health",
        "opener": "Hi Sarah, thanks for joining our webinar on behalf of Cedar Peak Health.",
        "value_line": "JumpCloud gives IT leaders one place to manage identities and devices.",
        "cta": "Open to a short call next week?",
        "facts_used": ["first_name", "company"],
    }
    d.update(overrides)
    return d


def test_validate_good_draft_passes():
    assert P.validate_touch(good_draft(), FACTS) == []


@pytest.mark.parametrize("overrides, expected_fragment", [
    ({"value_line": "We are SOC 2 ready."}, "banned claim: 'soc 2'"),
    ({"value_line": "Teams switch to us from Okta."}, "banned claim: 'okta'"),
    ({"value_line": "Save 30% on IT time."}, "numbers or percentages"),
    ({"opener": "Hi {{first_name}}, a note for Cedar Peak Health."}, "placeholder brackets"),
    ({"facts_used": ["first_name", "email"]}, "non-allowlisted facts_used"),
    ({"opener": "Hi SARAH, a note for Cedar Peak Health."}, "first name missing or altered"),
    ({"subject": "x" * 60}, "subject 60 chars"),
    ({"value_line": " ".join(["word"] * 120)}, "words (limit"),
])
def test_validate_failures(overrides, expected_fragment):
    errs = P.validate_touch(good_draft(**overrides), FACTS)
    assert any(expected_fragment in e for e in errs), errs


@pytest.mark.parametrize("persona", list(C.MESSAGING_LIBRARY))
def test_template_draft_passes_validation_for_every_persona(persona):
    facts = dict(FACTS, persona=persona)
    d = P.template_draft(facts, C.MESSAGING_LIBRARY[persona])
    assert P.validate_touch(d, facts) == []


# ---------------------------------------------------------------- Step 9 retry logic
class FakeLLM:
    available = True
    model = "fake"
    status = "fake"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate_json(self, prompt, schema, name="answer", retries=3):
        self.calls += 1
        return self.responses.pop(0) if self.responses else None


def drafted_row(draft):
    snippet = C.MESSAGING_LIBRARY["IT Decision Maker"]
    touch = C.SEQUENCE_BLUEPRINT[0]
    return {
        "batch_id": "BTEST", "record_id": "R01",
        "clean_first_name": "Sarah", "clean_last_name": "O'Neil", "clean_company": "Cedar Peak Health",
        "lane": "OUTBOUND", "reason_code": "SCORE_85_CLEAN_WORK_EMAIL",
        "draft": draft, "draft_source": "llm", "prompt_version": C.PROMPT_VERSION,
        "_facts": FACTS, "_snippet": snippet, "_touch": touch,
    }


def test_step9_bad_then_good_is_pass_after_retry():
    bad = good_draft(value_line="Guaranteed SOC 2 coverage.")
    gem = FakeLLM([good_draft()])
    r = P.step_09_validate_every_touch([drafted_row(bad)], gem)[0]
    assert gem.calls == 1
    assert r["validation_status"] == "pass_after_retry"
    assert r["draft_subject"] == "Quick idea for Cedar Peak Health"
    assert r["prompt_version"] == C.PROMPT_VERSION


def test_step9_always_bad_falls_back_to_template():
    bad = good_draft(value_line="Cheaper than Okta by 50%.")
    gem = FakeLLM([bad])
    r = P.step_09_validate_every_touch([drafted_row(bad)], gem)[0]
    assert r["validation_status"] == "template_fallback"
    assert r["draft_source"] == "template"
    assert r["prompt_version"] == C.TEMPLATE_VERSION
    assert r["lane"] == "OUTBOUND"
    assert "retry (llm)" in r["validation_errors"]


def test_step9_good_first_draft_passes():
    gem = FakeLLM([])
    r = P.step_09_validate_every_touch([drafted_row(good_draft())], gem)[0]
    assert r["validation_status"] == "pass" and gem.calls == 0


# ---------------------------------------------------------------- end to end
@pytest.fixture
def e2e(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(P, "OUTPUT_DIR", tmp_path)
    rows = P.run(SAMPLE, fresh=True)
    return {r["record_id"]: r for r in rows}, tmp_path


def test_end_to_end_lanes(e2e):
    rows, _ = e2e
    expected = {
        "R11": "EXCLUDE", "R07": "EXCLUDE",
        "R03": "HUMAN_REVIEW", "R06": "HUMAN_REVIEW", "R12": "HUMAN_REVIEW",
        "R08": "ROUTE_TO_REP", "R10": "ROUTE_TO_REP", "R04": "ROUTE_TO_REP", "R15": "ROUTE_TO_REP",
        "R02": "HELD", "R14": "MSP_TRACK",
    }
    assert {k: rows[k]["lane"] for k in expected} == expected
    assert rows["R02"]["reason_code"] == "ACCOUNT_OWNED_BY_REP"
    assert rows["R04"]["reason_code"] == "INBOUND_DEMO_MSP"
    assert rows["R15"]["reason_code"] == "INBOUND_DEMO_ABOVE_ICP" and rows["R15"]["priority"] == "P2"
    assert rows["R12"]["enrichment_conflict"] is True
    assert rows["R12"]["reason_code"] == "ENRICHMENT_CONFLICT"
    assert rows["R13"]["enrichment_notes"].startswith("ignored: confidence")


def test_end_to_end_drafts_are_template_fallback_without_key(e2e):
    rows, _ = e2e
    drafted = [r for r in rows.values() if r["lane"] in C.DRAFT_LANES]
    assert drafted
    assert all(r["validation_status"] == "template_fallback" for r in drafted)


def test_end_to_end_outputs_and_audit(e2e):
    rows, out = e2e
    for name in ("leads_output.csv", "audit_log.jsonl", "run_summary.md"):
        assert (out / name).exists(), name
    assert list(out.glob("raw_input_*.csv"))

    with open(out / "leads_output.csv", newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == len(rows)

    lines = [json.loads(l) for l in (out / "audit_log.jsonl").read_text().splitlines()]
    batch = next(iter(rows.values()))["batch_id"]
    lines = [l for l in lines if l["batch_id"] == batch]
    per_record = {}
    for l in lines:
        per_record.setdefault(l["record_id"], set()).add(l["step"])
    assert len(lines) == 10 * len(rows)
    assert all(len(steps) == 10 for steps in per_record.values())
