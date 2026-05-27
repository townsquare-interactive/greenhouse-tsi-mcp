"""
Greenhouse TSI — MCP Server (Vercel Serverless)
================================================
Implements the MCP JSON-RPC 2.0 protocol over HTTP (stateless / streamable-http
transport), suitable for Vercel serverless functions.

Connect at: claude.ai → Settings → Connectors → Add Custom MCP
  URL: https://<deployment>.vercel.app/mcp   (or /)
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import json, base64, os, urllib.request, urllib.parse

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY  = os.environ["GREENHOUSE_API_KEY"]
BASE_URL = "https://harvest.greenhouse.io/v1"
_auth    = base64.b64encode(f"{API_KEY}:".encode()).decode()
HDR      = {"Authorization": f"Basic {_auth}"}

app = FastAPI(title="Greenhouse TSI MCP", version="1.0.0")


# ── GH HTTP helpers ───────────────────────────────────────────────────────────

def gh_get(path: str, params=None):
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def gh_paginate(path: str, params=None, max_pages: int = 20) -> list:
    results, p = [], dict(params or {})
    p.setdefault("per_page", 500)
    for page in range(1, max_pages + 1):
        p["page"] = page
        batch = gh_get(path, p)
        if not batch:
            break
        results.extend(batch)
        if len(batch) < p["per_page"]:
            break
    return results


def bulk_candidates(cids: list) -> dict:
    out = {}
    for i in range(0, len(cids), 50):
        chunk = cids[i:i + 50]
        params = [("candidate_ids[]", str(c)) for c in chunk]
        params.append(("per_page", "50"))
        url = f"{BASE_URL}/candidates?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=HDR)
        with urllib.request.urlopen(req, timeout=30) as r:
            for c in json.loads(r.read()):
                out[c["id"]] = c
    return out


def extract_recruiter(candidate: dict) -> str:
    rec = candidate.get("recruiter") or {}
    return (rec.get("name", "") if isinstance(rec, dict) else "") or ""


def extract_hiring_class(candidate: dict) -> str:
    cf = candidate.get("custom_fields", {})
    if isinstance(cf, dict):
        v = cf.get("hiring_class")
        if isinstance(v, dict):
            return v.get("name", "") or ""
        return str(v) if v else ""
    if isinstance(cf, list):
        for f in cf:
            if isinstance(f, dict) and f.get("name", "").lower() == "hiring class":
                return str(f.get("value", "")) or ""
    return ""


# ── Tool implementations ──────────────────────────────────────────────────────

def _search_candidates(name="", stage="", recruiter="", position="",
                       hiring_class="", limit=50) -> str:
    """Search active candidates across all open jobs."""
    jobs = gh_paginate("/jobs", {"status": "open"})
    rows = []
    name_q, stage_q, rec_q, pos_q, class_q = (
        name.lower(), stage.lower(), recruiter.lower(),
        position.lower(), hiring_class.lower()
    )
    for job in jobs:
        title = job.get("name", "")
        if pos_q and pos_q not in title.lower():
            continue
        apps = gh_paginate(f"/jobs/{job['id']}/applications", {"status": "active"})
        if not apps:
            continue
        cids = [a["candidate_id"] for a in apps if a.get("candidate_id")]
        if not cids:
            continue
        profiles = bulk_candidates(cids)
        for app in apps:
            cid = app.get("candidate_id")
            if not cid or cid not in profiles:
                continue
            cand = profiles[cid]
            cand_name = f"{cand.get('first_name','')} {cand.get('last_name','')}".strip()
            if name_q and name_q not in cand_name.lower():
                continue
            cand_rec = extract_recruiter(cand)
            if rec_q and rec_q not in cand_rec.lower():
                continue
            hc = extract_hiring_class(cand)
            if class_q and class_q not in hc.lower():
                continue
            cs = app.get("current_stage")
            current_stage = (cs.get("name", "") if isinstance(cs, dict) else "") if cs else ""
            if stage_q and stage_q not in current_stage.lower():
                continue
            rows.append({
                "candidate_id": cid,
                "name": cand_name,
                "stage": current_stage,
                "recruiter": cand_rec,
                "position": title,
                "hiring_class": hc,
                "application_id": app.get("id"),
            })
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    return json.dumps(rows, indent=2)


def _get_candidate(candidate_id: int) -> str:
    """Full profile for a single candidate."""
    cand = gh_get(f"/candidates/{candidate_id}")
    apps = gh_get(f"/applications?candidate_id={candidate_id}&per_page=100")
    app_summaries = []
    for a in (apps if isinstance(apps, list) else []):
        cs = a.get("current_stage") or {}
        job_id = next((jp.get("id") for jp in (a.get("jobs") or [])), None)
        app_summaries.append({
            "application_id": a.get("id"),
            "job_id": job_id,
            "stage": cs.get("name", "") if isinstance(cs, dict) else "",
            "status": a.get("status", ""),
        })
    coord = cand.get("coordinator") or {}
    return json.dumps({
        "id": cand.get("id"),
        "name": f"{cand.get('first_name','')} {cand.get('last_name','')}".strip(),
        "email": next((e["value"] for e in (cand.get("email_addresses") or []) if e.get("value")), ""),
        "phone": next((p["value"] for p in (cand.get("phone_numbers") or []) if p.get("value")), ""),
        "recruiter": extract_recruiter(cand),
        "coordinator": coord.get("name", "") if isinstance(coord, dict) else "",
        "hiring_class": extract_hiring_class(cand),
        "tags": [t.get("name", "") for t in (cand.get("tags") or [])],
        "applications": app_summaries,
        "created_at": cand.get("created_at", ""),
        "updated_at": cand.get("updated_at", ""),
    }, indent=2)


def _get_jobs(keyword="", status="open", recruiter_name="", limit=100) -> str:
    """List jobs filtered by keyword, status, or recruiter."""
    jobs = gh_paginate("/jobs", {"status": status})
    kw, rn, out = keyword.lower(), recruiter_name.lower(), []
    for job in jobs:
        title = job.get("name", "")
        if kw and kw not in title.lower():
            continue
        if rn and rn not in title.lower():
            continue
        out.append({
            "id": job["id"],
            "name": title,
            "status": job.get("status", ""),
            "departments": [d.get("name", "") for d in (job.get("departments") or [])],
            "offices": [o.get("name", "") for o in (job.get("offices") or [])],
            "opened_at": job.get("opened_at", ""),
        })
        if len(out) >= limit:
            break
    return json.dumps(out, indent=2)


def _get_applications(job_id: int, status="active", limit=200) -> str:
    """All applications for a specific job."""
    params = {"per_page": min(limit, 500)}
    if status != "all":
        params["status"] = status
    apps = gh_paginate(f"/jobs/{job_id}/applications", params)[:limit]
    if not apps:
        return json.dumps([])
    cids = list({a["candidate_id"] for a in apps if a.get("candidate_id")})
    profiles = bulk_candidates(cids)
    out = []
    for a in apps:
        cid = a.get("candidate_id")
        cand = profiles.get(cid, {})
        cs = a.get("current_stage") or {}
        out.append({
            "application_id": a.get("id"),
            "candidate_id": cid,
            "name": f"{cand.get('first_name','')} {cand.get('last_name','')}".strip(),
            "stage": cs.get("name", "") if isinstance(cs, dict) else "",
            "status": a.get("status", ""),
            "recruiter": extract_recruiter(cand),
            "hiring_class": extract_hiring_class(cand),
            "applied_at": a.get("applied_at", ""),
            "rejected_at": a.get("rejected_at", ""),
        })
    return json.dumps(out, indent=2)


def _get_offer(candidate_id: int) -> str:
    """Most recent offer for a candidate."""
    apps = gh_get(f"/applications?candidate_id={candidate_id}&per_page=100")
    if not isinstance(apps, list) or not apps:
        return json.dumps({"error": "No applications found."})
    offers = []
    for app in apps:
        try:
            offer = gh_get(f"/applications/{app.get('id')}/offers/current_offer")
            if offer:
                cs = app.get("current_stage") or {}
                offers.append({
                    "application_id": app.get("id"),
                    "offer_id": offer.get("id"),
                    "status": offer.get("status", ""),
                    "sent_at": offer.get("sent_at", ""),
                    "resolved_at": offer.get("resolved_at", ""),
                    "start_date": offer.get("starts_at", ""),
                    "created_at": offer.get("created_at", ""),
                    "application_stage": cs.get("name", "") if isinstance(cs, dict) else "",
                })
        except Exception:
            pass
    if not offers:
        return json.dumps({"message": "No offers found for this candidate."})
    return json.dumps(offers, indent=2)


def _get_recruiters(active_only=True) -> str:
    """List all Greenhouse users with Recruiter or Site Admin role."""
    users = gh_paginate("/users")
    out = []
    for u in users:
        if active_only and u.get("disabled", False):
            continue
        roles = [r.get("name", "") for r in (u.get("roles") or [])]
        if "Recruiter" in roles or "Site Admin" in roles:
            out.append({
                "id": u.get("id"),
                "name": f"{u.get('first_name','')} {u.get('last_name','')}".strip(),
                "email": u.get("primary_email_address", ""),
                "disabled": u.get("disabled", False),
                "roles": roles,
            })
    return json.dumps(out, indent=2)


def _get_talent_pools() -> str:
    """List all prospect / talent pools."""
    pools = gh_paginate("/prospects")
    seen = {}
    for p in pools:
        for job in (p.get("jobs") or []):
            pool_name = job.get("name", "")
            if pool_name not in seen:
                seen[pool_name] = {"name": pool_name, "job_id": job.get("id"), "count": 0}
            seen[pool_name]["count"] += 1
    if not seen:
        jobs = gh_paginate("/jobs", {"status": "open"})
        for j in jobs:
            n = j.get("name", "")
            if "prospect" in n.lower() or "pool" in n.lower():
                seen[n] = {"name": n, "job_id": j["id"], "count": None}
    return json.dumps(list(seen.values()), indent=2)


def _get_pipeline_summary(position_keyword="", recruiter_name="") -> str:
    """Pipeline counts by stage and recruiter for active candidates."""
    jobs = gh_paginate("/jobs", {"status": "open"})
    pos_q, rec_q = position_keyword.lower(), recruiter_name.lower()
    stage_counts: dict = {}
    recruiter_counts: dict = {}
    total = 0
    for job in jobs:
        title = job.get("name", "")
        if pos_q and pos_q not in title.lower():
            continue
        apps = gh_paginate(f"/jobs/{job['id']}/applications", {"status": "active"})
        if not apps:
            continue
        cids = [a["candidate_id"] for a in apps if a.get("candidate_id")]
        profiles = bulk_candidates(cids)
        for app in apps:
            cid = app.get("candidate_id")
            cand = profiles.get(cid, {})
            rec = extract_recruiter(cand)
            if rec_q and rec_q not in rec.lower():
                continue
            cs = app.get("current_stage") or {}
            stage = (cs.get("name", "Unknown") if isinstance(cs, dict) else "Unknown")
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            recruiter_counts[rec or "Unassigned"] = recruiter_counts.get(rec or "Unassigned", 0) + 1
            total += 1
    return json.dumps({
        "filters": {"position": position_keyword or "all", "recruiter": recruiter_name or "all"},
        "total_active_candidates": total,
        "by_stage": dict(sorted(stage_counts.items(), key=lambda x: x[1], reverse=True)),
        "by_recruiter": dict(sorted(recruiter_counts.items(), key=lambda x: x[1], reverse=True)),
    }, indent=2)


# ── MCP Tool registry ─────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "search_candidates",
        "description": "Search active Greenhouse candidates across all open jobs. Filter by name, stage, recruiter, position type (ISS/AE/CSL), or hiring class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name":          {"type": "string",  "description": "Partial candidate name (case-insensitive)"},
                "stage":         {"type": "string",  "description": "Stage name partial match (e.g. 'Offer', 'Phone Screen', 'Final')"},
                "recruiter":     {"type": "string",  "description": "Recruiter first or full name (e.g. 'Regan', 'Jon Spitler')"},
                "position":      {"type": "string",  "description": "Job title keyword (e.g. 'ISS', 'AE', 'CSL', 'DSM')"},
                "hiring_class":  {"type": "string",  "description": "Hiring class month/year (e.g. 'May 2026', 'April 2026')"},
                "limit":         {"type": "integer", "description": "Max candidates to return (default 50)", "default": 50},
            },
        },
    },
    {
        "name": "get_candidate",
        "description": "Get the full profile for a single Greenhouse candidate — recruiter, hiring class, tags, email, phone, and all applications.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "integer", "description": "Greenhouse numeric candidate ID"},
            },
            "required": ["candidate_id"],
        },
    },
    {
        "name": "get_jobs",
        "description": "List Greenhouse jobs, optionally filtered by title keyword, status (open/closed/draft), or recruiter name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword":        {"type": "string", "description": "Filter job titles by keyword (e.g. 'ISS', 'AE', 'Jon')"},
                "status":         {"type": "string", "description": "open, closed, or draft (default: open)", "default": "open"},
                "recruiter_name": {"type": "string", "description": "Filter to jobs whose title contains this recruiter name"},
                "limit":          {"type": "integer", "description": "Max jobs to return (default 100)", "default": 100},
            },
        },
    },
    {
        "name": "get_applications",
        "description": "Get all applications for a specific Greenhouse job, with candidate name, stage, recruiter, and hiring class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "description": "Greenhouse job ID"},
                "status": {"type": "string",  "description": "active, rejected, hired, or all (default: active)", "default": "active"},
                "limit":  {"type": "integer", "description": "Max applications to return (default 200)", "default": 200},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "get_offer",
        "description": "Get offer details for a candidate — status, start date, sent/resolved timestamps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "integer", "description": "Greenhouse numeric candidate ID"},
            },
            "required": ["candidate_id"],
        },
    },
    {
        "name": "get_recruiters",
        "description": "List all Greenhouse users who have the Recruiter or Site Admin role.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "active_only": {"type": "boolean", "description": "Only return non-disabled users (default true)", "default": True},
            },
        },
    },
    {
        "name": "get_talent_pools",
        "description": "List all prospect / talent pools in Greenhouse with name, job_id, and candidate count.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_pipeline_summary",
        "description": "Get pipeline counts by stage and by recruiter for all active candidates. Optionally filter by position type or a recruiter name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "position_keyword": {"type": "string", "description": "Filter to jobs containing this string (e.g. 'ISS', 'AE')"},
                "recruiter_name":   {"type": "string", "description": "Filter to a specific recruiter's candidates (partial name)"},
            },
        },
    },
]

TOOL_MAP = {
    "search_candidates":   _search_candidates,
    "get_candidate":       _get_candidate,
    "get_jobs":            _get_jobs,
    "get_applications":    _get_applications,
    "get_offer":           _get_offer,
    "get_recruiters":      _get_recruiters,
    "get_talent_pools":    _get_talent_pools,
    "get_pipeline_summary":_get_pipeline_summary,
}


# ── MCP JSON-RPC handler ──────────────────────────────────────────────────────

def _process(body: dict):
    method  = body.get("method", "")
    params  = body.get("params") or {}
    req_id  = body.get("id")

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "greenhouse-tsi", "version": "1.0.0"},
            }

        elif method in ("notifications/initialized", "notifications/cancelled",
                        "notifications/progress"):
            return None  # notifications: no response

        elif method == "ping":
            result = {}

        elif method == "tools/list":
            result = {"tools": TOOLS}

        elif method == "tools/call":
            tool_name = params.get("name", "")
            args      = params.get("arguments") or {}
            if tool_name not in TOOL_MAP:
                raise ValueError(f"Unknown tool: {tool_name!r}")
            text   = TOOL_MAP[tool_name](**args)
            result = {"content": [{"type": "text", "text": text}], "isError": False}

        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": str(exc)},
        }


# ── FastAPI routes ─────────────────────────────────────────────────────────────

@app.get("/")
async def health():
    return {
        "status": "ok",
        "server": "greenhouse-tsi-mcp",
        "version": "1.0.0",
        "tools": len(TOOLS),
        "tool_names": [t["name"] for t in TOOLS],
    }


@app.post("/")
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body     = await request.json()
    response = _process(body)
    if response is None:
        return JSONResponse(content={}, status_code=202)
    return JSONResponse(content=response)
