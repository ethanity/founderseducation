"""CRO Match MVP: literature-backed CRO matching, RFQ generation, quote normalization, project tracking.
Run: python server.py   ->  http://localhost:8000     (stdlib only; ANTHROPIC_API_KEY optional)"""
import json, os, re, hashlib, math, statistics, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cros import CROS, MODELS, ENDPOINT_WORDS

ROOT = Path(__file__).parent
DATA = ROOT / "data" / "projects.json"
LOCK = threading.Lock()
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CRO_BY_ID = {c["id"]: c for c in CROS}
CACHE = {}
NOW_YEAR = date.today().year
LLM_MODEL = "claude-sonnet-5"


# ---------- LLM (optional) ----------
def llm_json(system, user):
    """Return parsed JSON from Claude, or None if no key / any failure (callers fall back to heuristics)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        msg = anthropic.Anthropic().messages.create(
            model=LLM_MODEL, max_tokens=1500, system=system + " Reply with a single JSON object only, no prose.",
            messages=[{"role": "user", "content": user}])
        text = msg.content[0].text
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception as e:
        print("LLM call failed, using fallback:", e)
        return None


# ---------- Step 1: plain language -> structured spec ----------
def heuristic_spec(text):
    t = text.lower()
    model = next((m for m in MODELS if re.search(m["rx"], t)), None)
    spec = {"model_key": model["key"] if model else None,
            "model": model["label"] if model else "Unrecognized model",
            "species": "mouse", "indication": model["indication"] if model else "",
            "area": model["area"] if model else "", "search_terms": model["terms"] if model else [],
            "broad_terms": model["broad"] if model else [], "endpoints": [], "groups": None, "n_per_group": None,
            "duration_weeks": None, "budget_usd": None, "notes": ""}
    if re.search(r"\brats?\b", t): spec["species"] = "rat"
    ends = [e for e in (model["endpoints"] if model else [])
            if any(w in t for w in re.findall(r"[a-z\-]+", e.lower()) if len(w) > 4)]
    spec["endpoints"] = ends or (model["endpoints"] if model else [])
    for w in ENDPOINT_WORDS:
        if w in t and not any(w in e.lower() for e in spec["endpoints"]):
            spec["endpoints"].append(w)
    if m := re.search(r"(\d+)\s*(?:treatment\s*)?(?:groups|arms|cohorts)", t): spec["groups"] = int(m[1])
    if m := re.search(r"(\d+)\s*(?:mice|rats|animals)?\s*(?:per group|/group|per arm|each)", t): spec["n_per_group"] = int(m[1])
    if m := re.search(r"(\d+)\s*(?:-|to)?\s*(?:\d+)?\s*(weeks?|wks?)", t): spec["duration_weeks"] = int(m[1])
    elif m := re.search(r"(\d+)\s*days?", t): spec["duration_weeks"] = max(1, round(int(m[1]) / 7))
    if m := re.search(r"\$\s?([\d,.]+)\s*(k|m)?", t):
        v = float(m[1].replace(",", ""))
        spec["budget_usd"] = int(v * (1000 if m[2] == "k" else 1_000_000 if m[2] == "m" else 1))
    spec["groups"] = spec["groups"] or 4
    spec["n_per_group"] = spec["n_per_group"] or (model["n"] if model else 10)
    spec["duration_weeks"] = spec["duration_weeks"] or (model["weeks"] if model else 4)
    return spec


def make_spec(text):
    system = ("You convert a scientist's plain-language preclinical outsourcing request into a structured spec. "
              "Keys: model (string), species, indication, area (one of oncology|fibrosis|inflammation|metabolic|neuro|respiratory), "
              "search_terms (1-3 literature phrases naming the disease-induction method, e.g. 'bleomycin'), "
              "broad_terms (1-2 broader indication phrases), endpoints (list), groups (int), n_per_group (int), "
              "duration_weeks (int), budget_usd (int or null), notes (string).")
    spec = llm_json(system, text)
    base = heuristic_spec(text)
    if spec:
        base.update({k: v for k, v in spec.items() if v not in (None, "", [])})
        base["source"] = "llm"
    else:
        base["source"] = "heuristic"
    return base


# ---------- Step 2: evidence mining (Europe PMC full-text) ----------
VERBS = ["performed by", "conducted by", "carried out by", "performed at", "conducted at", "carried out at"]


def epmc(query, page_size=8, sort=None, core=False):
    key = (query, page_size, sort, core)
    if key in CACHE:
        return CACHE[key]
    params = {"query": query, "format": "json", "pageSize": page_size, "resultType": "core" if core else "lite"}
    if sort: params["sort"] = sort
    url = EPMC + "?" + urllib.parse.urlencode(params)
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                d = json.load(r)
            out = {"hits": d.get("hitCount", 0), "results": d.get("resultList", {}).get("result", [])}
            CACHE[key] = out
            return out
        except Exception as e:
            err = str(e)
            time.sleep(0.5)
    return {"hits": 0, "results": [], "error": err}


def cro_phrase_clause(cro):
    return "(" + " OR ".join(f'"{v} {a}"' for a in cro["aliases"] for v in VERBS) + ")"


def or_clause(terms):
    return "(" + " OR ".join(f'"{t}"' for t in terms) + ")"


def paper(r):
    return {"title": r.get("title", "").rstrip("."), "year": int(r["pubYear"]) if r.get("pubYear", "").isdigit() else None,
            "journal": r.get("journalTitle", ""), "authors": (r.get("authorString") or "")[:80],
            "pmid": r.get("pmid"), "doi": r.get("doi"), "pmcid": r.get("pmcid")}


def evidence_for(cro, spec):
    clause = cro_phrase_clause(cro)
    exact = epmc(f'{clause} AND {or_clause(spec["search_terms"])}', 6, "P_PDATE_D desc") if spec["search_terms"] else {"hits": 0, "results": []}
    broad = epmc(f'{clause} AND {or_clause(spec.get("broad_terms") or spec["search_terms"])}', 3) if (spec.get("broad_terms") or spec["search_terms"]) else {"hits": 0, "results": []}
    papers = [paper(r) for r in exact["results"]]
    years = [p["year"] for p in papers if p["year"]]
    return {"model_hits": exact["hits"], "broad_hits": broad["hits"], "papers": papers,
            "latest_year": max(years) if years else None, "error": exact.get("error") or broad.get("error")}


def performance_summary():
    """Aggregate private outcomes per CRO from tracked projects: the proprietary-data moat."""
    out = {}
    for p in load_projects():
        fb = p.get("feedback")
        if not fb: continue
        s = out.setdefault(p["cro_id"], {"n": 0, "on_time": 0, "clean": [], "reuse": 0, "price_var": [], "sample": 0})
        s["n"] += 1
        s["on_time"] += fb["actual_weeks"] <= p["quote"]["weeks"] * 1.1
        s["clean"].append(fb["data_quality"])
        s["reuse"] += bool(fb["would_reuse"])
        s["price_var"].append(fb["actual_price"] / p["quote"]["total"] - 1)
        s["sample"] += bool(p.get("demo_seed"))
    for s in out.values():
        n = s["n"]
        s["on_time_rate"] = s["on_time"] / n; s["avg_quality"] = sum(s["clean"]) / n
        s["reuse_rate"] = s["reuse"] / n; s["avg_price_var"] = sum(s["price_var"]) / n
        s["score"] = (s["on_time_rate"] + (s["avg_quality"] - 1) / 4 + s["reuse_rate"]) / 3
        del s["clean"], s["price_var"]
    return out


def match(spec):
    perf = performance_summary()
    with ThreadPoolExecutor(6) as ex:
        evs = list(ex.map(lambda c: evidence_for(c, spec), CROS))
    ranked = []
    for c, ev in zip(CROS, evs):
        why, score = [], 0.0
        s = min(1, math.log1p(ev["model_hits"]) / math.log1p(6)) * 50
        score += s
        if ev["model_hits"]:
            why.append(f'{ev["model_hits"]} full-text paper(s) name this CRO as performing work with {", ".join(spec["search_terms"])}')
        else:
            why.append("no published record for this exact model")
        b = min(1, math.log1p(ev["broad_hits"]) / math.log1p(15)) * 15
        score += b
        if ev["broad_hits"]: why.append(f'{ev["broad_hits"]} paper(s) in the broader indication')
        if ev["latest_year"] and ev["latest_year"] >= NOW_YEAR - 2:
            score += 10; why.append(f'recent activity ({ev["latest_year"]})')
        if spec.get("area") in c["areas"]:
            score += 10; why.append(f'advertises {spec["area"]} work')
        p = perf.get(c["id"])
        if p:
            score += p["score"] * 15
            why.append(f'private record: {p["n"]} project(s), {p["on_time_rate"]:.0%} on time, quality {p["avg_quality"]:.1f}/5'
                       + (" (sample data)" if p["sample"] == p["n"] else ""))
        else:
            why.append("no private performance data yet")
        ranked.append({"cro": c, "evidence": ev, "performance": p, "score": round(score, 1), "why": why,
                       "claim": "evidence-backed" if ev["model_hits"] else ("advertised only" if spec.get("area") in c["areas"] else "weak fit")})
    ranked.sort(key=lambda r: -r["score"])
    return ranked


# ---------- Step 3: design insights mined from literature ----------
def design_insights(spec):
    if not spec["search_terms"]:
        return {"papers_scanned": 0}
    r = epmc(f'{or_clause(spec["search_terms"])} AND "mice" AND (ABSTRACT:"per group" OR ABSTRACT:"n =" OR ABSTRACT:"weeks")',
             25, "CITED desc", core=True)
    ns, weeks, days = [], [], []
    for x in r["results"]:
        a = (x.get("abstractText") or "")
        ns += [int(m) for m in re.findall(r"n\s*=\s*(\d{1,2})\b", a) if 3 <= int(m) <= 30]
        ns += [int(m) for m in re.findall(r"(\d{1,2})\s*(?:mice|animals)\s*per group", a)]
        weeks += [int(m) for m in re.findall(r"(\d{1,2})\s*weeks?", a) if int(m) <= 24]
        days += [int(m) for m in re.findall(r"(?:day|days)\s*(\d{1,2})", a)]
    out = {"papers_scanned": len(r["results"]), "total_matching": r["hits"]}
    if ns: out["group_size"] = {"median": statistics.median(ns), "range": [min(ns), max(ns)], "mentions": len(ns)}
    if weeks: out["duration_weeks"] = {"median": statistics.median(weeks), "mentions": len(weeks)}
    if days:
        out["common_days"] = [d for d, _ in sorted(((d, days.count(d)) for d in set(days)), key=lambda x: -x[1])[:5]]
    m = next((m for m in MODELS if m["key"] == spec.get("model_key")), None)
    out["watchouts"] = {
        "bleo": ["Bleomycin dose/strain (C57BL/6) drive variability: pilot a dose", "Dose therapeutically (day 7-10), not prophylactically, for translational relevance",
                 "Spontaneous resolution after ~day 28 can blur late endpoints"],
        "ccl4": ["Ensure CCl4 batch/vehicle control", "Plan for early mortality at high doses; budget spare animals"],
        "mc38": ["Randomize on tumor volume (~80-100 mm3) rather than day", "Pre-define humane endpoints for ulceration"],
        "xeno": ["Randomize on tumor volume", "Confirm cell line authentication"],
        "dss": ["DSS lot and mouse vendor strongly affect severity: request a pilot"],
    }.get(spec.get("model_key"), ["Ask each CRO for historical control-group variability (the best predictor of statistical power)"])
    return out


# ---------- Step 4: RFQ, simulated quotes, quote parsing ----------
def build_rfq(spec, cro, evidence, org="[Your company]"):
    total = spec["groups"] * spec["n_per_group"]
    ev = ""
    if evidence and evidence["papers"]:
        ps = "; ".join(f'{p["title"][:90]} ({p["year"]}, PMID {p["pmid"]})' for p in evidence["papers"][:2])
        ev = (f"\nWe noticed {cro['name']} is credited in {evidence['model_hits']} publication(s) for this model, e.g. {ps}. "
              "Please confirm whether the same team, protocol and historical control data would apply.\n")
    return f"""Subject: RFQ: {spec['model']} ({spec['species']}, {total} animals, ~{spec['duration_weeks']} wks)

Hello {cro['name']} team,

{org} is seeking a quote for the following in vivo study. {ev}
STUDY SPECIFICATION
- Model: {spec['model']}
- Species / strain: {spec['species']} (strain per your validated protocol)
- Design: {spec['groups']} groups x {spec['n_per_group']} animals = {total} animals
- In-life duration: ~{spec['duration_weeks']} weeks
- Endpoints: {', '.join(spec['endpoints'])}
- Target budget: {('$' + format(spec['budget_usd'], ',')) if spec.get('budget_usd') else 'not specified'}
- Notes: {spec.get('notes') or 'none'}

PLEASE QUOTE IN THIS FORMAT (so we can compare like for like)
1. Price per animal (in-life) and price per endpoint assay
2. Fixed fees (study setup, protocol development, pilot if needed)
3. Total price and payment terms
4. Weeks from contract signature to study start, and to final data delivery
5. Deliverables (raw data, report, histology images) and data format
6. Historical control-group variability for this model, and last time you ran it
7. Facility accreditation (AAALAC) and any subcontracted steps

Thank you,
{org}"""


def simulate_quote(spec, cro):
    """Deterministic fake CRO reply so the demo runs end to end. Clearly labeled as simulated in the UI."""
    h = int(hashlib.md5((cro["id"] + str(spec["model"])).encode()).hexdigest(), 16)
    jit = lambda k, lo, hi: lo + ((h >> k) % 1000) / 1000 * (hi - lo)
    animals = spec["groups"] * spec["n_per_group"]
    per_animal = round(90 * cro["base"] * spec["duration_weeks"] * jit(3, 0.85, 1.2) / 5) * 5 + 60
    assays = round(jit(9, 1800, 5200) * cro["base"] / 50) * 50 * max(1, len(spec["endpoints"]) // 2)
    fixed = round(jit(15, 2500, 9000) * cro["base"] / 100) * 100
    weeks_start = round(jit(21, 2, 7)); weeks_total = weeks_start + spec["duration_weeks"] + round(jit(27, 2, 6))
    return {"per_animal": per_animal, "assays": assays, "fixed": fixed, "total": per_animal * animals + assays + fixed,
            "weeks": weeks_total, "weeks_to_start": weeks_start, "animals": animals, "simulated": True,
            "deliverables": "Raw data + report" + (" + histology images" if "istology" in " ".join(spec["endpoints"]) else "")}


def parse_quote(text, spec):
    d = llm_json("Extract a CRO quote into keys: per_animal, assays, fixed, total (USD numbers), weeks (int, contract to final data), "
                 "weeks_to_start (int), deliverables (string). Use null for unknowns.", text)
    if d:
        d["parsed_by"] = "llm"
        return d
    money = lambda s: float(s.replace(",", ""))
    total = re.search(r"total[^$\d]{0,20}\$?\s?([\d,]+(?:\.\d+)?)", text, re.I)
    per = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s*(?:/|per)\s*animal|per animal[^$\d]{0,10}\$?\s?([\d,]+)", text, re.I)
    wk = re.search(r"(\d+)\s*(?:-\s*\d+\s*)?weeks", text, re.I)
    allmoney = [money(m) for m in re.findall(r"\$\s?([\d,]+(?:\.\d+)?)", text)]
    return {"per_animal": money(per[1] or per[2]) if per else None, "assays": None, "fixed": None,
            "total": money(total[1]) if total else (max(allmoney) if allmoney else None),
            "weeks": int(wk[1]) if wk else None, "weeks_to_start": None, "deliverables": "", "parsed_by": "regex"}


# ---------- Step 5: project tracking ----------
MILESTONES = ["Contract signed", "Animals received", "Dosing started", "In-life complete", "Data delivered"]


def load_projects():
    if not DATA.exists():
        seed_projects()
    return json.loads(DATA.read_text())


def save_projects(ps):
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(ps, indent=1))


def seed_projects():
    """A few clearly-labeled sample outcomes so the ranking shows the effect of private performance data."""
    rows = [("melior", "Bleomycin-induced pulmonary fibrosis", 7, 8, 62000, 60000, 5, True),
            ("melior", "Bleomycin-induced pulmonary fibrosis", 7, 7, 58000, 58000, 4, True),
            ("crl", "Bleomycin-induced pulmonary fibrosis", 8, 11, 96000, 118000, 4, True),
            ("pharmaron", "Bleomycin-induced pulmonary fibrosis", 9, 10, 55000, 57000, 4, True),
            ("pharmaron", "MC38 syngeneic tumor model", 8, 12, 41000, 47000, 3, False),
            ("crown", "MC38 syngeneic tumor model", 9, 9, 52000, 51000, 5, True)]
    ps = []
    for i, (c, m, qw, aw, qp, ap, q, reuse) in enumerate(rows):
        ps.append({"id": f"seed{i}", "cro_id": c, "spec": {"model": m}, "quote": {"total": qp, "weeks": qw},
                   "milestones": [{"name": n, "done": True, "date": None} for n in MILESTONES], "status": "complete",
                   "feedback": {"actual_weeks": aw, "actual_price": ap, "data_quality": q, "would_reuse": reuse},
                   "demo_seed": True, "created": "sample"})
    save_projects(ps)


def create_project(body):
    start = date.today()
    weeks = body["quote"].get("weeks") or 8
    ms = [{"name": n, "done": False, "date": None,
           "due": str(start + timedelta(weeks=weeks * f))} for n, f in zip(MILESTONES, [0.05, 0.2, 0.3, 0.8, 1.0])]
    ps = load_projects()
    p = {"id": "p" + str(int(time.time() * 1000)), "cro_id": body["cro_id"], "spec": body["spec"], "quote": body["quote"],
         "milestones": ms, "status": "active", "feedback": None, "created": str(start)}
    ps.append(p); save_projects(ps)
    return p


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, obj, code=200, ctype="application/json"):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self.send((ROOT / "index.html").read_bytes(), ctype="text/html; charset=utf-8")
        with LOCK:
            if path == "/api/projects": return self.send({"projects": load_projects(), "performance": performance_summary(), "names": {c["id"]: c["name"] for c in CROS}})
            if path == "/api/status": return self.send({"llm": bool(os.environ.get("ANTHROPIC_API_KEY")), "cros": len(CROS)})
        self.send({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 2) or b"{}")
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/spec": return self.send(make_spec(body["text"]))
            if path == "/api/match":
                return self.send({"results": match(body["spec"]), "design": design_insights(body["spec"])})
            if path == "/api/rfq":
                return self.send({"rfqs": [{"cro_id": i, "text": build_rfq(body["spec"], CRO_BY_ID[i], (body.get("evidence") or {}).get(i), body.get("org") or "[Your company]"),
                                            "simulated_quote": simulate_quote(body["spec"], CRO_BY_ID[i])} for i in body["cro_ids"]]})
            if path == "/api/quote/parse": return self.send(parse_quote(body["text"], body.get("spec") or {}))
            with LOCK:
                if path == "/api/projects": return self.send(create_project(body))
                if path == "/api/projects/milestone":
                    ps = load_projects()
                    p = next(x for x in ps if x["id"] == body["id"])
                    m = p["milestones"][body["index"]]; m["done"] = body["done"]; m["date"] = str(date.today()) if body["done"] else None
                    save_projects(ps); return self.send(p)
                if path == "/api/projects/feedback":
                    ps = load_projects()
                    p = next(x for x in ps if x["id"] == body["id"])
                    p["feedback"] = {k: body[k] for k in ("actual_weeks", "actual_price", "data_quality", "would_reuse")}
                    p["status"] = "complete"; save_projects(ps); return self.send(p)
                if path == "/api/reset": seed_projects(); return self.send({"ok": True})
            self.send({"error": "not found"}, 404)
        except Exception as e:
            self.send({"error": f"{type(e).__name__}: {e}"}, 500)


if __name__ == "__main__":
    load_projects()
    port = int(os.environ.get("PORT", 8000))
    print(f"CRO Match MVP on http://localhost:{port}  (LLM: {'on' if os.environ.get('ANTHROPIC_API_KEY') else 'off, heuristic fallback'})")
    ThreadingHTTPServer(("", port), H).serve_forever()
