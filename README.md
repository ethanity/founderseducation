# CRO Match MVP

    python server.py        # http://localhost:8000  (Python 3.9+, no dependencies)

Optional: set `ANTHROPIC_API_KEY` for LLM spec parsing and quote parsing (falls back to keyword/regex without it).

| Step | What's real | What's stubbed |
|---|---|---|
| 1 Describe | LLM or keyword parsing into an editable spec | Model vocabulary covers 11 common models |
| 2 Evidence | Live Europe PMC full-text search for "performed/conducted/carried out by/at <CRO>" + model terms | Only open-access full text; phrase match needs human verification |
| 3 Design | Group size / duration mined from abstracts of top-cited papers | Failure-point watch-outs are a curated list |
| 4 RFQ & quotes | RFQ generated per CRO, citing its own papers; quote paste + normalization; comparison | Sending is manual; "Simulate replies" fabricates quotes |
| 5 Track | Milestones + outcome capture, feeds back into step 2 ranking | 6 sample projects are seeded (labelled "sample") |

Data lives in `data/projects.json`. CRO directory and model list are in `cros.py`.
