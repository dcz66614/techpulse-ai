# G20 Offline Business-Validation Plan

This document details the offline business-validation plan for TechPulse-AI, approved under decision `docs/decisions/g20_offline_validation.md` (Ticket DEC, 2026-10-29).

> **Policy Invariant:** Outreach is strictly **OFF**. No external emails, messages, web requests, customer interviews, or third-party communications are permitted. All analytical models and tables in this document use structural placeholders (`[PLACEHOLDER]`); no market figures or conversion rates have been invented.

---

## 1. Operational Constraints & Boundaries

1. **Egress & Network Isolation:** In accordance with the project S20 security policy, the runtime operates with `network: none` across analysis workflows. No third-party API or tracking pixel is contacted.
2. **Zero Paid Spend:** Operational budget cap is $0.00 cloud spend. Validation relies exclusively on local processing and deterministic modeling.
3. **Data Integrity:** Historical baseline and growth metrics must derive only from verified local artifacts or explicitly marked template placeholders. Fabricating sample customer counts, willingness-to-pay numbers, or revenue projections is prohibited.

---

## 2. Pricing-Experiment Design

The objective of the offline pricing experiment is to establish a structured methodology for assessing customer willingness-to-pay (WTP) and optimal tier boundaries without executing live public campaigns.

### 2.1 Value Tier Architecture

TechPulse-AI defines three tier categories for future commercialization:

| Tier | Deployment Mode | Target Audience | Feature Scope | Pricing Model |
|---|---|---|---|---|
| **Community** | Local Single-Machine | Individual researchers, open-source engineers | Read-only API (`/health`, `/v1/reports`, `/v1/trends`), SQLite backend, local O20 runner | Free / Open-source ($0.00) |
| **Professional** | Dedicated Node / Cloud | Technology analysts, venture scouts, boutique consultancies | Multi-source automated sync, customized trend scoring, priority claim verification | Subscription (`[P_PRO_MONTHLY]` / `[P_PRO_ANNUAL]`) |
| **Enterprise** | Private Cluster / VPC | Institutional R&D, corporate strategy, security teams | Full data lineage export, custom collector adapters, SLA & audit trails | Seat + usage license (`[P_ENT_BASE]` + `[P_ENT_SEAT]`) |

### 2.2 Survey & Assessment Methodologies (Offline Templates)

Two complementary offline pricing frameworks are specified:

#### A. Van Westendorp Price Sensitivity Meter (PSM)
For evaluating psychological price thresholds across four standardized questions:
1. **Too Cheap:** At what price would you consider the product to be so cheap that you would question its quality? (`[P_TOO_CHEAP]`)
2. **Bargain / Cheap:** At what price would you consider the product a bargain—a great buy for the money? (`[P_CHEAP]`)
3. **Expensive:** At what price would you consider the product expensive, but still worth considering? (`[P_EXPENSIVE]`)
4. **Too Expensive:** At what price would you consider the product too expensive to consider purchasing? (`[P_TOO_EXPENSIVE]`)

*Intersections to derive from survey responses:*
- **Point of Marginal Cheapness (PMC):** Intersection of "Too Cheap" and "Expensive"
- **Optimal Price Point (OPP):** Intersection of "Too Cheap" and "Too Expensive"
- **Indifference Price Point (IDP):** Intersection of "Cheap" and "Expensive"
- **Point of Marginal Expensiveness (PME):** Intersection of "Cheap" and "Too Expensive"

#### B. Gabor-Granger Purchase Intent Framework
A sequential pricing interrogation model to estimate price elasticity of demand:
- Starting price anchor: `[PRICE_ANCHOR]`
- Upward escalation step: `+[PRICE_DELTA]` if intent >= `[INTENT_THRESHOLD]`
- Downward discount step: `-[PRICE_DELTA]` if intent < `[INTENT_THRESHOLD]`
- Revenue curve formula: $R(p) = p \times \Pr(\text{Buy} \mid p) \times N_{\text{target}}$

---

## 3. Commercial Metrics Definitions

The following key performance indicators are defined for measuring business traction post-launch:

### 3.1 Unit Economics & Cost Metrics
- **Marginal Serving Cost per Report ($C_{\text{report}}$):**
  $$C_{\text{report}} = C_{\text{compute}} + C_{\text{tokens}} + C_{\text{storage}}$$
  Under the v1.0 local architecture: $C_{\text{tokens}} = \$0.00$ (local deterministic embeddings and template generation), $C_{\text{storage}} \approx \$0.00$ (local filesystem).
- **Customer Acquisition Cost (CAC):**
  $$\text{CAC} = \frac{\text{Total Fully Burdened Sales \& Marketing Expense}}{\text{Number of New Paying Customers Acquired}}$$
- **Customer Lifetime Value (LTV):**
  $$\text{LTV} = \frac{\text{ARPU} \times \text{Gross Margin \%}}{\text{Customer Churn Rate}}$$
  where $\text{ARPU}$ is Average Monthly Revenue Per User.
- **LTV / CAC Ratio:** Target benchmark $\ge 3.0$ for sustainable SaaS unit economics.

### 3.2 Recurring Revenue & Conversion Metrics
- **Monthly Recurring Revenue (MRR):**
  $$\text{MRR} = \sum (\text{Active Paying Subscriptions} \times \text{Monthly Fee})$$
- **Annual Recurring Revenue (ARR):**
  $$\text{ARR} = \text{MRR} \times 12$$
- **Free-to-Paid Conversion Rate ($CR_{\text{paid}}$):**
  $$CR_{\text{paid}} = \frac{N_{\text{paid}}}{N_{\text{active\_community}}}$$
- **Churn Rate ($\sigma$):**
  $$\sigma = \frac{N_{\text{cancelled}}}{N_{\text{active\_start}}}$$

---

## 4. Analysis Template (Structural Placeholders)

The following tables provide the structure for capturing experimental validation data once external outreach is formally approved and executed in later phases.

### Table 4.1: Van Westendorp PSM Response Distribution Template

| Candidate Price Bracket ($/mo) | Too Cheap (%) | Cheap / Bargain (%) | Expensive (%) | Too Expensive (%) | Net Acceptability (%) |
|---|:---:|:---:|:---:|:---:|:---:|
| `[BRACKET_01]` | `[VAL_TC_1]` | `[VAL_CH_1]` | `[VAL_EX_1]` | `[VAL_TE_1]` | `[NET_1]` |
| `[BRACKET_02]` | `[VAL_TC_2]` | `[VAL_CH_2]` | `[VAL_EX_2]` | `[VAL_TE_2]` | `[NET_2]` |
| `[BRACKET_03]` | `[VAL_TC_3]` | `[VAL_CH_3]` | `[VAL_EX_3]` | `[VAL_TE_3]` | `[NET_3]` |
| `[BRACKET_04]` | `[VAL_TC_4]` | `[VAL_CH_4]` | `[VAL_EX_4]` | `[VAL_TE_4]` | `[NET_4]` |
| `[BRACKET_05]` | `[VAL_TC_5]` | `[VAL_CH_5]` | `[VAL_EX_5]` | `[VAL_TE_5]` | `[NET_5]` |

### Table 4.2: Cohort Revenue & Retention Worksheet Template

| Cohort Month | Initial Accounts | Month 1 Retention | Month 3 Retention | Month 6 Retention | Expansion MRR | Net Revenue Retention (NRR) |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `[COHORT_M1]` | `[N_ACC_1]` | `[PCT_R1_1]` | `[PCT_R3_1]` | `[PCT_R6_1]` | `[EXP_1]` | `[NRR_1]` |
| `[COHORT_M2]` | `[N_ACC_2]` | `[PCT_R1_2]` | `[PCT_R3_2]` | `[PCT_R6_2]` | `[EXP_2]` | `[NRR_2]` |
| `[COHORT_M3]` | `[N_ACC_3]` | `[PCT_R1_3]` | `[PCT_R3_3]` | `[PCT_R6_3]` | `[EXP_3]` | `[NRR_3]` |

---

## 5. Security & Governance Sign-off

- **Current Operational Status:** Offline Plan Active (Draft / Template Only).
- **External Outreach:** **OFF** (Strictly refused until explicit future sign-off).
- **Third-Party Contacts:** 0.
- **Data Compliance:** Fully compliant with AUTH-04 and AUTH-06 boundaries.
