# Run summary - batch B20261104-104440-8f4e

- Input: `sample_leads.csv` (15 rows)
- Generated: 2026-11-04T10:44:51+00:00
- Drafting engine: **OpenRouter (google/gemini-2.5-flash)**
- Persona sources: rules=13, ai=0, default_no_ai=0
- Fields kept from previous push (fill-blank-only): 0

## Counts per lane

| Lane | Count |
|---|---|
| ROUTE_TO_REP | 4 |
| MSP_TRACK | 1 |
| OUTBOUND | 4 |
| NURTURE | 0 |
| HUMAN_REVIEW | 3 |
| HELD | 1 |
| EXCLUDE | 2 |

## Leads by lane

| ID | Name | Company | Persona | Score | Lane | Reason |
|---|---|---|---|---|---|---|
| R01 | Marcus Delgado | Brightline Logistics | IT Decision Maker | 85 | OUTBOUND | SCORE_85_CLEAN_WORK_EMAIL |
| R02 | Sarah O'Neil | Cedar Peak Health | IT Practitioner | 70 | HELD | ACCOUNT_OWNED_BY_REP |
| R03 | Raj | - | IT Decision Maker | 60 | HUMAN_REVIEW | HYGIENE_MISSING_CORE_FIELDS |
| R04 | Jennifer Wu | NorthStar MSP | MSP | 85 | ROUTE_TO_REP | INBOUND_DEMO_MSP P1 |
| R05 | David Karlsson | Vanta Corp | IT Decision Maker | 90 | OUTBOUND | SCORE_90_CLEAN_WORK_EMAIL |
| R06 | Priya Raman | Loopware | Technical Executive | 77 | HUMAN_REVIEW | HYGIENE_PERSONAL_EMAIL+DOMAIN_MISMATCH |
| R07 | Acme Widgets | Acme Widgets | - |  | EXCLUDE | NO_PERSON+TEAM_INBOX |
| R08 | Tom Becker | Cedar Peak Health | IT Decision Maker | 100 | ROUTE_TO_REP | INBOUND_DEMO P1 |
| R09 | Angela Martinez | Sunrise Dental Group | IT Practitioner | 70 | OUTBOUND | SCORE_70_CLEAN_WORK_EMAIL |
| R10 | Kevin Liu | Fintrail Payments | Technical Executive | 97 | ROUTE_TO_REP | INBOUND_DEMO P1 |
| R11 | Asdf Test | test co | - |  | EXCLUDE | JUNK |
| R12 | Olivia Grant | Harbor Freight Group | IT Practitioner | 65 | HUMAN_REVIEW | ENRICHMENT_CONFLICT |
| R13 | Daniel Osei | Meridian Schools | IT Decision Maker | 90 | OUTBOUND | SCORE_90_CLEAN_WORK_EMAIL |
| R14 | Rebecca Flynn | Flynn IT Partners | MSP | 55 | MSP_TRACK | MSP_PERSONA_OR_INDUSTRY |
| R15 | Hiro Tanaka | Globex Manufacturing | IT Practitioner | 75 | ROUTE_TO_REP | INBOUND_DEMO_ABOVE_ICP P2 |

## Validation failures and retries

- R01 Marcus Delgado: **pass_after_retry** - attempt 1 (llm): company missing or altered

## Enrichment conflicts

- R12 Olivia Grant (harborfreightgroup.net): employee_count CONFLICT: csv=150 vs enrichment=2400 (kept csv)

## Excluded, held and review rows

- R02 Sarah O'Neil - HELD: account owned by rep (primary contact R08 Tom Becker)
- R03 Raj - HUMAN_REVIEW: HYGIENE_MISSING_CORE_FIELDS (MISSING_CORE_FIELDS:last_name+company+company_domain+email)
- R06 Priya Raman - HUMAN_REVIEW: HYGIENE_PERSONAL_EMAIL+DOMAIN_MISMATCH (PERSONAL_EMAIL;DOMAIN_MISMATCH)
- R07 Acme Widgets - EXCLUDE: NO_PERSON+TEAM_INBOX (flags: NO_PERSON;TEAM_INBOX;MISSING_CORE_FIELDS:first_name+last_name)
- R11 Asdf Test - EXCLUDE: JUNK (flags: JUNK)
- R12 Olivia Grant - HUMAN_REVIEW: ENRICHMENT_CONFLICT (employee_count CONFLICT: csv=150 vs enrichment=2400 (kept csv))

## Drafted touches (touch 1 email)

### R01 Marcus Delgado - Brightline Logistics (OUTBOUND, pass_after_retry, draft_v1)

**Subject:** For Marcus at Brightline Logistics

> Marcus, I noticed you attended the Zero Trust webinar. Given your role as IT Director at Brightline Logistics, I thought you might be interested in how other transportation companies are streamlining their IT.
>
> JumpCloud gives IT leaders one place to manage identities, devices and access, so the team spends less time stitching tools together.
>
> Open to a short call next week to see if it fits your roadmap?

_facts_used: first_name,company,title,source,industry_

### R05 David Karlsson - Vanta Corp (OUTBOUND, pass, draft_v1)

**Subject:** JumpCloud for Vanta Corp

> Hi David, I saw you attended the Zero Trust webinar. As an IT Manager at Vanta Corp, you're likely focused on streamlining your IT operations.
>
> JumpCloud gives IT leaders one place to manage identities, devices and access, so the team spends less time stitching tools together.
>
> Open to a short call next week to see if it fits your roadmap?

_facts_used: first_name,company,title,source_

### R09 Angela Martinez - Sunrise Dental Group (OUTBOUND, pass, draft_v1)

**Subject:** For Angela at Sunrise Dental Group

> Angela, I saw you attended the Zero Trust webinar. For IT Admins like you at Sunrise Dental Group, managing user access and devices can be a real challenge.
>
> JumpCloud lets admins handle user onboarding, device management and SSO from one console instead of juggling separate tools.
>
> Want me to send a quick walkthrough of the admin console?

_facts_used: first_name,company,title,source_

### R13 Daniel Osei - Meridian Schools (OUTBOUND, pass, draft_v1)

**Subject:** JumpCloud for Meridian Schools

> Hi Daniel, I saw you attended the Zero Trust webinar. Given your role as IT Support Manager at Meridian Schools, I thought you might be interested in how other education organizations are simplifying their IT.
>
> JumpCloud gives IT leaders one place to manage identities, devices and access, so the team spends less time stitching tools together.
>
> Open to a short call next week to see if it fits your roadmap?

_facts_used: first_name,source,title,company,industry_

### R14 Rebecca Flynn - Flynn IT Partners (MSP_TRACK, pass, draft_v1)

**Subject:** For Rebecca at Flynn IT Partners

> Hi Rebecca, I hope this email finds you well. I work with MSPs like Flynn IT Partners and noticed your company's focus on IT services.
>
> JumpCloud's multi-tenant console lets MSPs manage identities and devices across every client from one place.
>
> Open to a quick chat about how other MSPs package this for clients?

_facts_used: first_name,company,persona_

