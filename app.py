import io
import json
import os
import re
import time
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from openai import OpenAI

# reportlab is pure-Python (no system libraries) so the PDF builder deploys
# cleanly on Render's native Python runtime with no Docker/apt changes.
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

load_dotenv()

app = Flask(__name__)

MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
WEBHOOK_URL = os.getenv("SMART1_WEBHOOK_URL", "").strip()
# Absolute base used to build the public report_pdf_url (e.g. https://smart1hvac.onrender.com).
# If empty, the app derives it from the incoming request.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
ENABLE_PDF = os.getenv("ENABLE_PDF", "1").strip() not in ("0", "false", "False", "")
REPORT_DIR = os.path.join(app.static_folder, "reports")

SYSTEM_PROMPT = """
You are the Smart 1 Marketing HVAC "Comfort & Command" Market Intelligence Architect.
Create a practical, sales-oriented market, demand, and weather-trigger marketing plan for a
residential (and optionally light-commercial) HVAC contractor.

IMPORTANT ACCURACY RULES
- You do not have live access to maps, permit databases, or exact census tables unless supplied.
- Use geographic knowledge and conservative planning assumptions. Never claim a location or statistic was live-verified.
- Clearly label all population, household, homeowner, and system-age figures as AI planning estimates.
- Give ranges, confidence levels, and a short explanation of the assumptions.
- Do not invent precise street addresses. Use recognizable place names plus city/state. An address field may be null.
- Avoid duplicate locations.

CORE STRATEGY — TWO DEMAND PIPELINES
1. Always-On Emergency Capture: Google Local Services Ads (LSA / "Google Guaranteed") + high-intent Search/PPC,
   backed by Missed-Call Text Back so a call they cannot answer never becomes a competitor's job.
2. SmartClimate Weather-Triggered Demand: CTV/OTT and programmatic display that automatically switch ON during
   extreme heat and cold and PAUSE in mild weather, preserving budget for high-value days. Layer New-Mover and
   home-improver audience data to build a replacement-system pipeline.

LSA AWARENESS
- The input includes lsa_status. If it is 'no', 'paused', or 'unsure', the plan MUST prioritize claiming and
  optimizing the Google LSA (Google Guaranteed) profile as the #1 move. Do not assume they already have it.
- If lsa_status is 'yes_active', treat this as a conquest scenario: they already own the LSA spot, so lead with
  SmartClimate CTV, New-Mover, and programmatic display to expand beyond LSA.

ALLOWED CHANNELS / DATA ONLY
- Google LSA, high-intent Search/PPC, CTV/OTT, programmatic / data-driven targeted display, New-Mover &
  home-improver audience data, streaming audio, YouTube / online video, website retargeting, Local Business Boost
  (50+ directory sync for the Maps 3-Pack), Missed-Call Text Back, Unified Conversation Inbox, Automated Review Generation.
- NEVER recommend paid social advertising (Facebook, Instagram, TikTok, LinkedIn, Snapchat, Pinterest, X). Smart 1's
  HVAC program is search-, weather-, and data-driven, not social. Do not recommend email or SMS blasts as media.

WEATHER-TRIGGER RULES
- Build practical triggers around HVAC-relevant weather: cooling signals (85 degrees F+, 90 degrees F+ heat waves, heat
  advisories, humidity spikes, extended warm stretches) and heating signals (first freeze, hard freeze warnings, cold
  snaps, first frost, extended cold). Include holiday/weekend forecasts.
- Keep trigger labels short and punchy (e.g. "90 degrees+ heat wave", "Heat advisory", "First 32 degrees freeze",
  "Hard freeze warning", "Cold snap", "Holiday weekend forecast").
- Do not imply weather guarantees demand. Treat it as a budget-pacing and timing signal.

DEMAND ESTIMATION METHOD
Estimate the population and households in the requested market, then owner-occupied (homeowner) households as the
primary target, then a replacement-opportunity subset (households with aging systems, roughly 10+ years old) using
regional housing-age assumptions. Adjust for climate severity, income, homeownership rate, and requested services.
Return low/base/high estimates, confidence, and assumptions. Do not present estimates as verified counts.

TARGET LOCATION / AUDIENCE TYPES TO CONSIDER
1. Recently-built and new-construction neighborhoods (aging-in soon / warranty expiry)
2. Established owner-occupied ZIP clusters with older housing stock (replacement pipeline)
3. Competing HVAC companies (for conquest geofencing)
4. Big-box home-improvement retailers (Home Depot / Lowe's) and hardware stores
5. Property-management / multifamily offices and light-commercial corridors (if commercial selected)
6. 55+ / retirement communities and high-cooling-load or high-heating-load areas

GEOFENCE / TARGETING GUIDANCE
- Recommend a practical method and radius for each location or audience cluster.
- Separate "audience / location lookback" targeting from "real-time / proximity" targeting.
- Rank locations Priority 1, 2, or 3.

OUTPUT
Return only valid JSON matching the requested schema. Do not use markdown fences.
"""

REPORT_SCHEMA = {
    "name": "hvac_market_report",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "market_summary": {"type": "string"},
            "market_type": {"type": "string"},
            "market_type_description": {"type": "string"},
            "market_opportunity": {"type": "string"},
            "market_profile": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "estimated_population_low": {"type": "integer"},
                    "estimated_population_base": {"type": "integer"},
                    "estimated_population_high": {"type": "integer"},
                    "estimated_households_low": {"type": "integer"},
                    "estimated_households_base": {"type": "integer"},
                    "estimated_households_high": {"type": "integer"},
                    "estimated_homeowner_households_low": {"type": "integer"},
                    "estimated_homeowner_households_base": {"type": "integer"},
                    "estimated_homeowner_households_high": {"type": "integer"},
                    "estimated_replacement_opportunity_low": {"type": "integer"},
                    "estimated_replacement_opportunity_base": {"type": "integer"},
                    "estimated_replacement_opportunity_high": {"type": "integer"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "estimated_population_low",
                    "estimated_population_base",
                    "estimated_population_high",
                    "estimated_households_low",
                    "estimated_households_base",
                    "estimated_households_high",
                    "estimated_homeowner_households_low",
                    "estimated_homeowner_households_base",
                    "estimated_homeowner_households_high",
                    "estimated_replacement_opportunity_low",
                    "estimated_replacement_opportunity_base",
                    "estimated_replacement_opportunity_high",
                    "confidence",
                    "assumptions",
                ],
            },
            "recommended_package": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "package_name": {"type": "string"},
                    "monthly_investment": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["package_name", "monthly_investment", "description"],
            },
            "media_channels": {"type": "array", "items": {"type": "string"}},
            "streaming_audio_note": {"type": "string"},
            "weather_triggers": {"type": "array", "items": {"type": "string"}},
            "monthly_plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "month": {"type": "string"},
                        "focus": {"type": "string"},
                        "message": {"type": "string"},
                        "triggers": {"type": "array", "items": {"type": "string"}},
                        "pacing": {"type": "string"},
                    },
                    "required": ["month", "focus", "message", "triggers", "pacing"],
                },
            },
            "geofence_locations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "city_state": {"type": "string"},
                        "address": {"type": ["string", "null"]},
                        "category": {"type": "string"},
                        "area_or_market": {"type": "string"},
                        "priority": {"type": "integer", "enum": [1, 2, 3]},
                        "recommended_method": {
                            "type": "string",
                            "enum": ["audience_lookback", "real_time_proximity", "both"],
                        },
                        "recommended_radius_miles": {"type": "number"},
                        "audience_reason": {"type": "string"},
                        "best_message": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": [
                        "name",
                        "city_state",
                        "address",
                        "category",
                        "area_or_market",
                        "priority",
                        "recommended_method",
                        "recommended_radius_miles",
                        "audience_reason",
                        "best_message",
                        "confidence",
                    ],
                },
            },
            "disclaimer": {"type": "string"},
        },
        "required": [
            "market_summary",
            "market_type",
            "market_type_description",
            "market_opportunity",
            "market_profile",
            "recommended_package",
            "media_channels",
            "streaming_audio_note",
            "weather_triggers",
            "monthly_plan",
            "geofence_locations",
            "disclaimer",
        ],
    },
    "strict": True,
}


def clean_payload(data: dict) -> dict:
    fields = [
        "company_name",
        "website",
        "company_zip",
        "service_radius",
        "locations",
        "services",
        "revenue_focus",
        "objectives",
        "lsa_status",
        "lead_type",
        "contact_name",
        "contact_email",
        "contact_phone",
        "notes",
    ]
    cleaned = {k: str(data.get(k, "")).strip()[:1500] for k in fields}
    if not re.fullmatch(r"\d{5}(-\d{4})?", cleaned["company_zip"]):
        raise ValueError("A valid U.S. ZIP code is required.")
    return cleaned


def generate_report(payload: dict) -> Any:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    client = OpenAI(api_key=api_key)
    user_prompt = (
        "\nBuild a weather-triggered HVAC Comfort & Command Demand & Targeting Report from these inputs:\n"
        f"{json.dumps(payload, indent=2)}"
        "\n\nThe contractor did NOT provide a climate season, a media budget, weather preferences, or lists of "
        "local neighborhoods and competitors. You must supply all of these yourself:\n"
        "- Assume the heating and cooling seasons and their length from the company's ZIP code and region.\n"
        "- Identify the local new-construction areas, older owner-occupied ZIP clusters, competing HVAC companies, "
        "home-improvement retailers, and (if commercial is selected) property-management corridors yourself from "
        "geographic knowledge of the market, and include the best of them in geofence_locations.\n\n"
        "Populate every field of the schema:\n"
        "- market_summary: one or two sentences framing the weather-triggered HVAC demand opportunity for this "
        "company and market (reference the company name and area).\n"
        "- market_type: a short badge label, e.g. 'Hot-Summer Cooling-Driven Market', 'Cold-Winter Heating-Driven "
        "Market', or 'Dual-Season Heating & Cooling Market'. market_type_description: one sentence on the seasonal pattern.\n"
        "- market_profile: low/base/high estimates for population, households, owner-occupied (homeowner) households, "
        "and a replacement-opportunity subset (households with aging ~10+ year systems), plus confidence and assumptions.\n"
        "- market_opportunity: keep this SIMPLE — one short, plain sentence on the contractor's opportunity in this market.\n"
        "- recommended_package: choose the best-fit package for this market from the Smart 1 HVAC package menu below. "
        "Use its exact NAME and monthly price as monthly_investment (e.g. '$3,499/month'), and write a short description "
        "of what that level buys. Pick the tier based on market size, number of locations, competition, and goals.\n"
        "  SMART 1 HVAC PACKAGE MENU (use these, do not invent prices):\n"
        "    * $749/month — Comfort Starter (brand-new or single-truck: LSA + high-intent Search foundation)\n"
        "    * $1,499/month — Local Comfort Foundation (dominate one city: LSA + Search mgmt, Local Business Boost, "
        "Missed-Call Text Back, automated reviews)\n"
        "    * $3,499/month — Omnichannel Market Leader (adds SmartClimate CTV + New-Mover & home-improver display)\n"
        "    * $6,500/month — Regional Domination (multi-market full weather-triggered omnichannel at scale)\n"
        "- media_channels: ALLOWED channels/data only. ALWAYS include 'Google LSA (Google Guaranteed)' and 'In-Market "
        "HVAC Buyer Audience Data' as chips. Then choose from: high-intent Search/PPC, SmartClimate CTV/OTT, New-Mover & "
        "home-improver display, programmatic targeted display, streaming audio, YouTube/online video, website retargeting, "
        "Local Business Boost, Missed-Call Text Back. Return 5-8 chips total. NEVER include paid social, email, or SMS.\n"
        "- streaming_audio_note: a short recommendation to geotarget streaming audio around home neighborhoods and "
        "home-improvement retail during peak weather dayparts (mornings and evenings when homeowners notice comfort issues).\n"
        "- weather_triggers: 5-8 short trigger labels for this market's climate (mix cooling and heating as appropriate).\n"
        "- monthly_plan: all 12 months (January through December). Each month has a focus title, a short customer-facing "
        "message, 1-2 relevant weather trigger labels drawn from weather_triggers, and a 'pacing' string. Match focus to "
        "the season (summer = cooling demand & AC replacement, winter = heating demand & furnace replacement, "
        "spring/fall = tune-ups, memberships, and early-replacement offers).\n"
        "  BUDGET PACING RULE for the 'pacing' field: the recommended_package monthly_investment is the PEAK / in-season "
        "monthly budget (100%). In shoulder-season months spend 50% of the package; in mild transition months spend 35%. "
        "Classify each month as Peak, Shoulder, or Mild based on this market's climate — hot-summer markets peak roughly "
        "Jun-Sep, cold-winter markets peak roughly Dec-Feb, dual-season markets have two peaks. Set pacing to a short "
        "string with the tier, percent, and computed dollar amount — e.g. 'Peak — 100% ($3,499)', "
        "'Shoulder — 50% ($1,750)', 'Mild — 35% ($1,225)'. Compute the dollars from the chosen package price.\n"
        "- geofence_locations: 10-16 target locations/audiences (new-construction neighborhoods, older owner-occupied ZIP "
        "clusters, competitor HVAC companies, home-improvement retailers, retirement communities, property-management "
        "corridors). Prioritize targets inside the service radius; lower confidence for uncertain ones. Keep text concise.\n"
        "- disclaimer: a short note that figures are AI planning estimates for the market, not exact counts.\n"
    )
    response = client.responses.create(
        model=MODEL,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        text={"format": {"type": "json_schema", **REPORT_SCHEMA}},
        temperature=0.25,
        max_output_tokens=8000,
    )
    text = (response.output_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# PDF report — reportlab, pure Python. Produces a hosted PDF your team can send
# from Smart 1 Suite. Guarded so any failure never blocks the lead/webhook.
# ---------------------------------------------------------------------------

NAVY = colors.HexColor("#0a2240")
COOL = colors.HexColor("#0a9ed6")
HEAT = colors.HexColor("#f2683c")
GREEN = colors.HexColor("#2dbb72")
LINE = colors.HexColor("#dbe5ed")
MUTED = colors.HexColor("#68798c")
AQUA = colors.HexColor("#fff2ec")


def _money_to_int(value: str):
    """'$3,499/month' -> 3499 (int) or None."""
    digits = re.sub(r"[^\d]", "", (value or "").split("/")[0])
    return int(digits) if digits else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "report").lower()).strip("-") or "report"


def _pdf_styles():
    ss = getSampleStyleSheet()
    body = ParagraphStyle("s1body", parent=ss["Normal"], fontName="Helvetica",
                          fontSize=9.5, leading=14, textColor=colors.HexColor("#25364b"))
    h2 = ParagraphStyle("s1h2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                        fontSize=13, leading=16, textColor=NAVY, spaceBefore=16, spaceAfter=6)
    title = ParagraphStyle("s1title", parent=ss["Title"], fontName="Helvetica-Bold",
                           fontSize=22, leading=25, textColor=NAVY, alignment=TA_LEFT, spaceAfter=4)
    eyebrow = ParagraphStyle("s1eye", parent=body, fontName="Helvetica-Bold",
                             fontSize=8, textColor=HEAT, spaceAfter=2)
    small = ParagraphStyle("s1small", parent=body, fontSize=8, textColor=MUTED, leading=11)
    cell = ParagraphStyle("s1cell", parent=body, fontSize=8, leading=10.5)
    cellw = ParagraphStyle("s1cellw", parent=cell, textColor=colors.white)
    return dict(body=body, h2=h2, title=title, eyebrow=eyebrow, small=small, cell=cell, cellw=cellw)


def build_report_pdf(report: dict, company: str, base_url: str) -> str:
    """Render the report JSON to a branded PDF, save under static/reports,
    and return its absolute public URL (or '' on failure)."""
    if not ENABLE_PDF:
        return ""
    try:
        os.makedirs(REPORT_DIR, exist_ok=True)
        st = _pdf_styles()
        fmt = lambda n: f"{int(n):,}" if n is not None else "—"
        rng = lambda a, b: f"{fmt(a)}–{fmt(b)}"
        m = report.get("market_profile", {}) or {}
        rp = report.get("recommended_package", {}) or {}

        filename = f"{_slug(company)}-{int(time.time())}.pdf"
        path = os.path.join(REPORT_DIR, filename)

        story = []
        story.append(Paragraph("SMART 1 MARKETING &nbsp;|&nbsp; HVAC COMFORT &amp; COMMAND PLAN", st["eyebrow"]))
        story.append(Paragraph(company or "Market Plan", st["title"]))
        story.append(Paragraph(report.get("market_summary", ""), st["body"]))
        story.append(Spacer(1, 6))

        if report.get("market_type"):
            story.append(Paragraph(f"<b>{report.get('market_type')}</b> — {report.get('market_type_description','')}", st["small"]))

        stat_data = [[
            Paragraph(f"<b>{rng(m.get('estimated_population_low'), m.get('estimated_population_high'))}</b><br/><font size=7 color='#68798c'>ESTIMATED POPULATION</font>", st["cell"]),
            Paragraph(f"<b>{rng(m.get('estimated_homeowner_households_low'), m.get('estimated_homeowner_households_high'))}</b><br/><font size=7 color='#68798c'>HOMEOWNER HOUSEHOLDS</font>", st["cell"]),
            Paragraph(f"<b>{rng(m.get('estimated_replacement_opportunity_low'), m.get('estimated_replacement_opportunity_high'))}</b><br/><font size=7 color='#68798c'>REPLACEMENT-READY HOMES</font>", st["cell"]),
        ]]
        stat = Table(stat_data, colWidths=[2.4 * inch] * 3)
        stat.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), AQUA),
            ("BOX", (0, 0), (-1, -1), 0.5, LINE),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ]))
        story.append(Spacer(1, 8))
        story.append(stat)

        story.append(Paragraph("Your Market Opportunity", st["h2"]))
        story.append(Paragraph(report.get("market_opportunity", ""), st["body"]))

        story.append(Paragraph("Recommended Package", st["h2"]))
        story.append(Paragraph(f"<b>{rp.get('monthly_investment','')} — {rp.get('package_name','')}</b>", st["body"]))
        story.append(Paragraph(rp.get("description", ""), st["small"]))

        chans = report.get("media_channels", []) or []
        if chans:
            story.append(Paragraph("Recommended Media Channels", st["h2"]))
            story.append(Paragraph(" &nbsp;•&nbsp; ".join(chans), st["body"]))

        trigs = report.get("weather_triggers", []) or []
        if trigs:
            story.append(Paragraph("SmartClimate Weather Triggers", st["h2"]))
            story.append(Paragraph(" &nbsp;•&nbsp; ".join(trigs), st["body"]))

        plan = report.get("monthly_plan", []) or []
        if plan:
            story.append(Paragraph("Month-by-Month Campaign Plan", st["h2"]))
            rows = [[Paragraph("<b>Month</b>", st["cellw"]), Paragraph("<b>Focus</b>", st["cellw"]), Paragraph("<b>Budget Pacing</b>", st["cellw"])]]
            for x in plan:
                rows.append([
                    Paragraph(x.get("month", ""), st["cell"]),
                    Paragraph(x.get("focus", ""), st["cell"]),
                    Paragraph(x.get("pacing", ""), st["cell"]),
                ])
            t = Table(rows, colWidths=[1.1 * inch, 3.3 * inch, 2.8 * inch], repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fbfc")]),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.append(t)

        geo = sorted(report.get("geofence_locations", []) or [], key=lambda g: g.get("priority", 3))
        if geo:
            story.append(Paragraph("Recommended Targets & Geofences", st["h2"]))
            rows = [[Paragraph(f"<b>{h}</b>", st["cellw"]) for h in ("P", "Location", "Category", "Method", "Radius", "Conf.")]]
            for g in geo:
                rows.append([
                    Paragraph(f"P{g.get('priority','')}", st["cell"]),
                    Paragraph(f"<b>{g.get('name','')}</b><br/>{g.get('city_state','')}", st["cell"]),
                    Paragraph(g.get("category", ""), st["cell"]),
                    Paragraph(str(g.get("recommended_method", "")).replace("_", " "), st["cell"]),
                    Paragraph(f"{g.get('recommended_radius_miles','')} mi", st["cell"]),
                    Paragraph(g.get("confidence", ""), st["cell"]),
                ])
            t = Table(rows, colWidths=[0.4 * inch, 2.2 * inch, 1.5 * inch, 1.3 * inch, 0.7 * inch, 0.6 * inch], repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fbfc")]),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(t)

        story.append(Spacer(1, 12))
        story.append(Paragraph(report.get("disclaimer", ""), st["small"]))

        doc = SimpleDocTemplate(path, pagesize=letter, title=f"{company} HVAC Market Plan",
                                leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                                topMargin=0.6 * inch, bottomMargin=0.6 * inch)
        doc.build(story)

        base = base_url or PUBLIC_BASE_URL
        return f"{base.rstrip('/')}/static/reports/{filename}" if base else f"/static/reports/{filename}"
    except Exception:
        app.logger.exception("PDF generation failed")
        return ""


def send_webhook(payload: dict, report: Any, status: str, pdf_url: str = "") -> None:
    if not WEBHOOK_URL:
        return
    report = report or {}
    mp = report.get("market_profile", {}) or {}
    rp = report.get("recommended_package", {}) or {}
    monthly = _money_to_int(rp.get("monthly_investment", ""))
    body = {
        # --- Contact / lead fields ---
        **payload,
        "source": "Smart 1 HVAC Comfort & Command",
        "report_status": status,
        "lead_type": payload.get("lead_type", "qualified"),
        # --- Opportunity fields ---
        "opportunity_name": f"{payload.get('company_name', 'Lead')} — HVAC Comfort & Command",
        "recommended_package": rp.get("package_name", ""),
        "recommended_investment": rp.get("monthly_investment", ""),
        "opportunity_value_monthly": monthly,
        "opportunity_value_annual": monthly * 12 if monthly else None,
        # --- Report custom fields ---
        "market_type": report.get("market_type", ""),
        "market_summary": report.get("market_summary", ""),
        "est_homeowner_households": mp.get("estimated_homeowner_households_base"),
        "est_replacement_opportunity": mp.get("estimated_replacement_opportunity_base"),
        "weather_triggers": ", ".join(report.get("weather_triggers", []) or []),
        "report_pdf_url": pdf_url,
        "report_json": json.dumps(report, separators=(",", ":"))[:60000],
    }
    try:
        requests.post(WEBHOOK_URL, json=body, timeout=12)
    except requests.RequestException:
        app.logger.exception("Webhook delivery failed")


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/api/lead")
def lead():
    """Lightweight capture for the LSA conquest (soft-disqualify) branch.
    No report is generated — the lead is routed to Smart 1 Suite as a conquest target."""
    try:
        data = request.get_json(silent=True) or {}
        payload = {
            "company_name": str(data.get("company_name", "")).strip()[:200],
            "company_zip": str(data.get("company_zip", "")).strip()[:10],
            "contact_name": str(data.get("c_name", data.get("contact_name", ""))).strip()[:200],
            "contact_email": str(data.get("c_email", data.get("contact_email", ""))).strip()[:200],
            "contact_phone": str(data.get("c_phone", data.get("contact_phone", ""))).strip()[:40],
            "lsa_status": "yes_active",
            "lead_type": "conquest_disqualified",
            "notes": "Already running Google LSA — routed to conquest list.",
        }
        send_webhook(payload, None, "conquest_captured")
        return jsonify({"ok": True})
    except Exception:
        app.logger.exception("Lead capture failed")
        return jsonify({"ok": False, "error": "Could not capture the lead."}), 500


@app.post("/api/analyze")
def analyze():
    try:
        payload = clean_payload(request.get_json(silent=True) or {})
        report = generate_report(payload)
        base_url = PUBLIC_BASE_URL or request.url_root
        pdf_url = build_report_pdf(report, payload.get("company_name", "Market Plan"), base_url)
        send_webhook(payload, report, "completed", pdf_url)
        return jsonify({"ok": True, "report": report, "report_pdf_url": pdf_url})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Analysis failed")
        try:
            send_webhook(clean_payload(request.get_json(silent=True) or {}), None, "failed")
        except Exception:
            pass
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "The plan could not be generated. Check the server configuration and try again.",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            ),
            500,
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
