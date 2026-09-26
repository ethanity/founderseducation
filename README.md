# CRO Match (MVP demo)

Describe a preclinical experiment in plain language. The app shows which contract research organizations (CROs) have published evidence of running that exact model, drafts standardized quote requests, compares quotes side by side, and tracks project outcomes so future matches get better.

## Quick start

Requires Python 3.9+. No packages to install.

```
git clone https://github.com/ethanity/founderseducation.git
cd founderseducation
python server.py
```

Open http://localhost:8000.

Optional: set `ANTHROPIC_API_KEY` before starting to use Claude for parsing your description and pasted quotes. Without it, the app uses keyword and regex parsing, which works for the supported models below.

```
# Windows PowerShell
$env:ANTHROPIC_API_KEY = "sk-ant-..."; python server.py
# macOS / Linux
ANTHROPIC_API_KEY=sk-ant-... python server.py
```

## Walkthrough (about 3 minutes)

1. **Describe.** Click **Use example** (or write your own: model, groups, animals per group, duration, endpoints, budget), then **Structure it**. Check the structured spec and edit anything that looks wrong, especially the *literature search terms*, since they drive step 2.
2. **Evidence.** Click **Find CROs with evidence**. Wait 10-20 seconds while it searches Europe PMC full text. You get a ranked CRO table. Open "most recent papers" on any row to verify the evidence yourself. Tick the CROs you want quotes from (top 3 are pre-selected).
3. **Design.** See typical group size, duration and failure points from the literature, to sanity-check your plan before requesting quotes.
4. **RFQ & quotes.**
   - Expand a CRO to read and copy its RFQ email. Sending is manual.
   - Click **Simulate CRO replies** to fill the comparison table with fake quotes (labelled *simulated*), or paste a real reply under "Paste a real quote" and click **Normalize**.
   - The highlighted row is the best blend of price (40%), speed (20%) and evidence (40%). Click **Award** to start a project.
5. **Track.** Tick milestones as they finish. When done, open "Close out & record outcome" and enter actual weeks, price, data quality and whether you'd reuse the CRO. Go back to step 2 and re-run: that CRO's score now includes your private performance data.

## What's real and what's not

| Step | Real | Stubbed |
|---|---|---|
| Evidence | Live Europe PMC full-text search for "performed/conducted/carried out by/at <CRO>" plus model terms | Open-access papers only. A phrase match is evidence to check, not proof |
| Design | Group size and duration mined from top-cited abstracts | Failure-point watch-outs are a curated list |
| Quotes | Pasted quotes are normalized | "Simulate replies" is fabricated |
| Track | Milestones and outcomes are stored and feed the ranking | 6 sample projects are pre-seeded and labelled *sample* |

Nothing is emailed, and there are no accounts or payments.

## Supported models

Bleomycin lung fibrosis, CCl4 liver fibrosis, UUO kidney fibrosis, diet-induced NASH, diet-induced obesity, DSS colitis, collagen-induced arthritis, EAE, MC38 syngeneic tumors, xenografts, and HDM/OVA asthma. For anything else, edit the search terms by hand in step 1 (or use an API key).

## Customizing

- **CROs and models:** edit [cros.py](cros.py). The CRO list and advertised areas are rough seed data, so verify them.
- **Ranking weights:** `match()` in [server.py](server.py).
- **Data:** stored in `data/projects.json`. Use **Reset sample data** in step 5, or delete the file, to start over.

## Files

- `server.py`: backend and API
- `cros.py`: CRO directory and model vocabulary
- `index.html`: the whole UI
