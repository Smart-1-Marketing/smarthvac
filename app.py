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
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as _canvas
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
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
# Your GHL / calendar booking link, shown as the primary CTA on the report.
BOOKING_URL = os.getenv("BOOKING_URL", "").strip()

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

NAVY = colors.HexColor("#0d2240")
TEAL = colors.HexColor("#16bcae")
BLUE = colors.HexColor("#1f9ed6")
HEAT = colors.HexColor("#f2683c")
LINE = colors.HexColor("#e4e9ef")
MUTED = colors.HexColor("#6b7c8e")
CARD = colors.HexColor("#f8fbfd")
INK = colors.HexColor("#25364b")
QUOTE = colors.HexColor("#eaf7fb")
LEADC = colors.HexColor("#33465a")
PAGE_W, PAGE_H = letter

# Fixed HVAC package menu (name/price must match generate_report + index.html).
PACKAGE_TIERS = [
    ("Starter", "$749/mo", "$749/month", "Comfort Starter",
     "Get found first — Google LSA + high-intent Search foundation."),
    ("Foundation", "$1,499/mo", "$1,499/month", "Local Comfort Foundation",
     "Dominate one city — LSA + Search, LBB, Missed-Call Text Back, reviews."),
    ("★ Recommended", "$3,499/mo", "$3,499/month", "Omnichannel Market Leader",
     "Adds SmartClimate CTV + New-Mover display for replacement demand."),
    ("Scale", "$6,500/mo", "$6,500/month", "Regional Domination",
     "Full weather-triggered omnichannel across multiple markets."),
]


def _trigger_kind(label: str) -> str:
    l = (label or "").lower()
    if any(k in l for k in ["heat", "humid", "warm", "90", "85", "sun"]):
        return "hot"
    if any(k in l for k in ["freez", "frost", "cold", "snow", "ice", "32"]):
        return "cold"
    return "neutral"


class ProposalCanvas(_canvas.Canvas):
    """Draws the navy header band (tall title band on page 1, slim on later
    pages) plus a footer with a page counter on every page."""

    company = ""
    title_line = "HVAC Comfort & Command Proposal"
    subtitle = "Weather-triggered advertising plan prepared for your market"
    footer = ("Smart 1 Marketing  ·  Weather-triggered plan based on AI "
              "market estimates. Verify locations and figures before activation.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for i, state in enumerate(self._saved):
            self.__dict__.update(state)
            self._draw_decoration(i + 1, total)
            super().showPage()
        super().save()

    def _draw_decoration(self, page, total):
        w, h = PAGE_W, PAGE_H
        band = 78 if page == 1 else 54
        # header band + teal accent
        self.setFillColor(NAVY)
        self.rect(0, h - band, w, band, fill=1, stroke=0)
        self.setFillColor(TEAL)
        self.rect(0, h - band - 5, w, 5, fill=1, stroke=0)
        # logo lockup: SMART(1)MARKETING with a teal "1"
        y = h - 30
        self.setFont("Helvetica-Bold", 13)
        x = 40
        self.setFillColor(colors.white)
        self.drawString(x, y, "SMART")
        x += self.stringWidth("SMART", "Helvetica-Bold", 13)
        self.setFillColor(TEAL)
        self.drawString(x, y, "1")
        x += self.stringWidth("1", "Helvetica-Bold", 13)
        self.setFillColor(colors.white)
        self.drawString(x, y, "MARKETING")
        if page == 1:
            self.setFont("Helvetica-Bold", 17)
            self.setFillColor(colors.white)
            self.drawString(40, h - 56, self.title_line)
            self.setFont("Helvetica", 9)
            self.setFillColor(colors.HexColor("#b9c6d6"))
            self.drawString(40, h - 70, self.subtitle)
        else:
            self.setFont("Helvetica-Bold", 9)
            self.setFillColor(colors.HexColor("#c3d0de"))
            self.drawRightString(w - 40, h - 27, self.company or "")
        # footer
        self.setStrokeColor(colors.HexColor("#e0e6ec"))
        self.setLineWidth(0.5)
        self.line(40, 42, w - 40, 42)
        self.setFont("Helvetica", 7)
        self.setFillColor(colors.HexColor("#9aa7b4"))
        self.drawString(40, 30, self.footer)
        self.drawRightString(w - 40, 30, f"{page} / {total}")


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
    grey = colors.HexColor("#93a0ad")

    def S(name, **kw):
        return ParagraphStyle(name, parent=body, **kw)

    return dict(
        body=body, h2=h2, title=title,
        by=S("by", fontName="Helvetica", fontSize=8.5, textColor=MUTED, spaceAfter=2),
        lead=S("lead", fontName="Helvetica", fontSize=9.5, leading=12.5, textColor=LEADC,
               alignment=TA_JUSTIFY, spaceAfter=4),
        seclabel=S("sl", fontName="Helvetica-Bold", fontSize=7.5, textColor=grey, leading=10),
        metaL=S("ml", fontName="Helvetica-Bold", fontSize=6, textColor=grey, leading=8),
        metaV=S("mv", fontName="Helvetica-Bold", fontSize=9.5, textColor=NAVY, leading=13),
        badge=S("bd", fontName="Helvetica-Bold", fontSize=9, textColor=colors.white, leading=12),
        statN=S("sn", fontName="Helvetica-Bold", fontSize=13, textColor=NAVY, alignment=TA_CENTER, leading=16),
        statL=S("stl", fontName="Helvetica", fontSize=7, textColor=MUTED, alignment=TA_CENTER, leading=9),
        tcap=S("tc", fontName="Helvetica-Bold", fontSize=6, textColor=grey, alignment=TA_CENTER, leading=8),
        tcapR=S("tcr", fontName="Helvetica-Bold", fontSize=6, textColor=HEAT, alignment=TA_CENTER, leading=8),
        tprice=S("tp", fontName="Helvetica-Bold", fontSize=12, textColor=NAVY, alignment=TA_CENTER, leading=15),
        tname=S("tn", fontName="Helvetica-Bold", fontSize=7.5, textColor=INK, alignment=TA_CENTER, leading=9),
        tblurb=S("tb", fontName="Helvetica", fontSize=6.2, textColor=MUTED, alignment=TA_CENTER, leading=8),
        rec=S("rc", fontName="Helvetica", fontSize=9.5, leading=14, textColor=INK),
        note=S("nt", fontName="Helvetica", fontSize=8.5, leading=13, textColor=colors.HexColor("#2c3e50")),
        pay=S("py", fontName="Helvetica", fontSize=8.8, leading=13, textColor=INK),
        darkEye=S("de", fontName="Helvetica-Bold", fontSize=7, textColor=TEAL, leading=10),
        darkH=S("dh", fontName="Helvetica-Bold", fontSize=12.5, textColor=colors.white, leading=15, spaceAfter=4),
        darkP=S("dp", fontName="Helvetica", fontSize=8.5, leading=13, textColor=colors.HexColor("#c3d0de")),
        darkLi=S("dl", fontName="Helvetica", fontSize=8.3, leading=13, textColor=colors.HexColor("#dbe4ee")),
        tile=S("ti", fontName="Helvetica-Bold", fontSize=9, textColor=colors.white, alignment=TA_CENTER),
        mc=S("mc", fontName="Helvetica-Bold", fontSize=8.5, textColor=NAVY, leading=11),
        subH=S("sh", fontName="Helvetica-Bold", fontSize=8.3, textColor=NAVY, leading=11, spaceBefore=6),
        subP=S("sp", fontName="Helvetica", fontSize=8.3, leading=12, textColor=colors.HexColor("#4a5b6c")),
        cellw=S("cw", fontName="Helvetica-Bold", fontSize=7.5, textColor=colors.white, leading=10),
        cell=S("cl", fontName="Helvetica", fontSize=8, leading=10.5, textColor=LEADC),
        cellb=S("cb", fontName="Helvetica-Bold", fontSize=8, leading=10.5, textColor=NAVY),
        cellm=S("cm", fontName="Helvetica", fontSize=7, leading=9.5, textColor=MUTED),
        ctaH=S("ch", fontName="Helvetica-Bold", fontSize=13, textColor=colors.white, leading=16, spaceAfter=3),
        ctaP=S("cta_p", fontName="Helvetica", fontSize=8.6, leading=13, textColor=colors.HexColor("#c3d0de")),
        ctaA=S("ca", fontName="Helvetica-Bold", fontSize=9, textColor=TEAL, spaceBefore=6),
        disc=S("ds", fontName="Helvetica-Oblique", fontSize=7, leading=10, textColor=grey),
    )


def build_report_pdf(report: dict, company: str, base_url: str, inputs: dict = None) -> str:
    """Render the report JSON to a branded multi-page proposal PDF, save under
    static/reports, and return its absolute public URL (or '' on failure)."""
    if not ENABLE_PDF:
        return ""
    try:
        inputs = inputs or {}
        os.makedirs(REPORT_DIR, exist_ok=True)
        st = _pdf_styles()
        fmt = lambda n: f"{int(n):,}" if n is not None else "—"
        rng = lambda a, b: f"{fmt(a)}–{fmt(b)}"
        m = report.get("market_profile", {}) or {}
        rp = report.get("recommended_package", {}) or {}
        CW = PAGE_W - 80  # content width between 40pt margins

        filename = f"{_slug(company)}-{int(time.time())}.pdf"
        path = os.path.join(REPORT_DIR, filename)

        def seclabel(txt):
            return [Spacer(1, 5), Paragraph(txt.upper(), st["seclabel"]),
                    HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=4, spaceAfter=7)]

        story = []
        # ---- PAGE 1 ----
        story.append(Paragraph(company or "Market Plan", st["title"]))
        story.append(Paragraph("Prepared by Smart 1 Marketing  ·  " + time.strftime("%B %d, %Y"), st["by"]))

        lsa_map = {"yes_active": "Running now", "paused": "Paused",
                   "no": "Not yet running", "unsure": "To be confirmed"}
        market_cell = f"{inputs.get('company_zip', '—')} · {inputs.get('service_radius', '—')}"
        meta = [
            ("Market", market_cell),
            ("Est. Replacement-Ready HH", rng(m.get("estimated_replacement_opportunity_low"), m.get("estimated_replacement_opportunity_high"))),
            ("Recommended Plan", rp.get("monthly_investment", "—")),
            ("Media Approach", "Weather-triggered"),
            ("Budget Pacing", "Peak / Shoulder / Mild"),
            ("Google LSA Status", lsa_map.get(inputs.get("lsa_status", ""), "To be confirmed")),
        ]
        mcell = lambda l, v: [Paragraph(l.upper(), st["metaL"]), Spacer(1, 2), Paragraph(v, st["metaV"])]
        meta_tbl = Table(
            [[mcell(*meta[0]), mcell(*meta[1]), mcell(*meta[2])],
             [mcell(*meta[3]), mcell(*meta[4]), mcell(*meta[5])]],
            colWidths=[CW / 3] * 3)
        meta_tbl.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.8, LINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 13), ("BOTTOMPADDING", (0, 0), (-1, -1), 13),
        ]))
        story.append(Spacer(1, 10))
        story.append(meta_tbl)

        btext = report.get("market_type", "Market")
        bw = min(CW, len(btext) * 5.7 + 34)
        badge = Table([[Paragraph(btext, st["badge"])]], colWidths=[bw])
        badge.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NAVY),
            ("LEFTPADDING", (0, 0), (-1, -1), 16), ("RIGHTPADDING", (0, 0), (-1, -1), 16),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(Spacer(1, 8))
        story.append(badge)
        story.append(Spacer(1, 5))
        story.append(Paragraph(report.get("market_type_description", ""), st["lead"]))
        story.append(Paragraph(report.get("market_summary", ""), st["lead"]))

        story.extend(seclabel("Market Estimate"))
        scell = lambda num, lab: [Paragraph(num, st["statN"]), Spacer(1, 4), Paragraph(lab, st["statL"])]
        gap, cw3 = 12, (CW - 24) / 3
        stat = Table([[
            scell(rng(m.get("estimated_population_low"), m.get("estimated_population_high")), "Estimated population"), "",
            scell(rng(m.get("estimated_homeowner_households_low"), m.get("estimated_homeowner_households_high")), "Homeowner households"), "",
            scell(rng(m.get("estimated_replacement_opportunity_low"), m.get("estimated_replacement_opportunity_high")), "Replacement-ready homes"),
        ]], colWidths=[cw3, gap, cw3, gap, cw3])
        stat.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 11), ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
        ] + [c for i in (0, 2, 4) for c in (
            ("BACKGROUND", (i, 0), (i, 0), CARD), ("BOX", (i, 0), (i, 0), 0.8, LINE))]))
        story.append(stat)

        story.extend(seclabel("Your Market Opportunity"))
        story.append(Paragraph(report.get("market_opportunity", ""), st["lead"]))

        story.extend(seclabel("Investment Tiers (Foundation / Recommended / Scale)"))
        rec_i = 2
        for i, tier in enumerate(PACKAGE_TIERS):
            if (rp.get("package_name", "").strip().lower() == tier[3].lower()
                    or rp.get("monthly_investment", "").replace(" ", "") == tier[2].replace(" ", "")):
                rec_i = i
        tgap, tcw = 8, (CW - 24) / 4

        def tier_cell(tier, is_rec):
            return [Paragraph(tier[0], st["tcapR"] if is_rec else st["tcap"]), Spacer(1, 5),
                    Paragraph(tier[1], st["tprice"]), Spacer(1, 3),
                    Paragraph(tier[3], st["tname"]), Spacer(1, 6),
                    Paragraph(tier[4], st["tblurb"])]
        trow = []
        for i, tier in enumerate(PACKAGE_TIERS):
            trow.append(tier_cell(tier, i == rec_i))
            if i < 3:
                trow.append("")
        tiers = Table([trow], colWidths=[tcw, tgap, tcw, tgap, tcw, tgap, tcw])
        tstyle = [("VALIGN", (0, 0), (-1, -1), "TOP"),
                  ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                  ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8)]
        for ci in (0, 2, 4, 6):
            tstyle.append(("BOX", (ci, 0), (ci, 0), 0.8, LINE))
        rc = rec_i * 2
        tstyle.append(("BACKGROUND", (rc, 0), (rc, 0), colors.HexColor("#fff8f4")))
        tstyle.append(("BOX", (rc, 0), (rc, 0), 1.4, HEAT))
        tiers.setStyle(TableStyle(tstyle))
        story.append(tiers)

        story.append(Spacer(1, 10))
        story.append(Paragraph(f"<b>Recommended: {rp.get('monthly_investment','')} — {rp.get('package_name','')}.</b> {rp.get('description','')}", st["rec"]))
        mo = _money_to_int(rp.get("monthly_investment", ""))
        if mo:
            jobs = max(1, -(-mo // 6000))
            story.append(Spacer(1, 5))
            story.append(Paragraph(
                f'<b>Does this pay for itself?</b> At a $6,000 average completed-job value, about '
                f'<font color="#f2683c"><b>{jobs} new job{"s" if jobs > 1 else ""} a month</b></font> '
                f'covers the entire {rp.get("monthly_investment","")} plan — everything after that is added profit.',
                st["pay"]))
        nb = Table([[Paragraph("This is a suggested budget based on your market's size. In a quick consult we'll tailor a budget and plan that fits your company's goals, capacity, and cash flow.", st["note"])]], colWidths=[CW])
        nb.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), QUOTE), ("LINEBEFORE", (0, 0), (0, -1), 3, TEAL),
            ("LEFTPADDING", (0, 0), (-1, -1), 16), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 11), ("BOTTOMPADDING", (0, 0), (-1, -1), 11)]))
        story.append(Spacer(1, 9))
        story.append(nb)

        # ---- PAGE 2 ----
        story.append(PageBreak())
        story.extend(seclabel("How This Saves You Money"))
        bullets = [
            "Pauses spend on mild, low-demand days instead of paying for empty impressions.",
            "Targets in-market homeowners and new movers instead of broadcasting to the whole metro.",
            "Rolls unused shoulder-season budget forward instead of losing it to a fixed monthly buy.",
            "Covers emergency repair, system replacement, and retention from one budget — not three campaigns.",
        ]
        dark_flow = [Paragraph("WHY THIS SAVES YOU MONEY", st["darkEye"]),
                     Paragraph("Spend on the days demand is real, not on a flat calendar.", st["darkH"]),
                     Paragraph("Traditional radio, print, and always-on digital bill you every week regardless of the forecast. A weather-triggered plan concentrates budget on high-intent days.", st["darkP"]),
                     Spacer(1, 6)]
        for b in bullets:
            dark_flow.append(Paragraph(f'<font color="#16bcae"><b>+</b></font>&nbsp;&nbsp;{b}', st["darkLi"]))
            dark_flow.append(Spacer(1, 3))
        dk = Table([[dark_flow]], colWidths=[CW])
        dk.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NAVY),
            ("LEFTPADDING", (0, 0), (-1, -1), 22), ("RIGHTPADDING", (0, 0), (-1, -1), 22),
            ("TOPPADDING", (0, 0), (-1, -1), 20), ("BOTTOMPADDING", (0, 0), (-1, -1), 16)]))
        story.append(dk)

        chans = report.get("media_channels", []) or []
        if chans:
            story.extend(seclabel("Recommended Media Channels"))
            mgap = 12
            mcw = (CW - mgap) / 2

            def channel_card(name, idx):
                letter = next((c for c in name if c.isalnum()), "•").upper()
                tile = Table([[Paragraph(letter, st["tile"])]], colWidths=[22], rowHeights=[22])
                tile.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), TEAL if idx % 2 else BLUE),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
                card = Table([[tile, Paragraph(name, st["mc"])]], colWidths=[30, mcw - 30 - 24])
                card.setStyle(TableStyle([
                    ("BOX", (0, 0), (-1, -1), 0.8, LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                    ("TOPPADDING", (0, 0), (-1, -1), 11), ("BOTTOMPADDING", (0, 0), (-1, -1), 11)]))
                return card
            cards = [channel_card(c, i) for i, c in enumerate(chans)]
            mrows = []
            for i in range(0, len(cards), 2):
                mrows.append([cards[i], "", cards[i + 1] if i + 1 < len(cards) else ""])
            mtbl = Table(mrows, colWidths=[mcw, mgap, mcw])
            mtbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                      ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                                      ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
            story.append(mtbl)

        if report.get("streaming_audio_note"):
            story.extend(seclabel("Streaming Audio — Neighborhood Daypart"))
            story.append(Paragraph(report.get("streaming_audio_note", ""), st["lead"]))

        trigs = report.get("weather_triggers", []) or []
        if trigs:
            story.extend(seclabel("Recommended Weather Triggers"))
            toks = []
            for t in trigs:
                k = _trigger_kind(t)
                col = "#b6482a" if k == "hot" else ("#166a8f" if k == "cold" else "#0d2240")
                toks.append(f'<font color="{col}"><b>{t}</b></font>')
            story.append(Paragraph("&nbsp;&nbsp;·&nbsp;&nbsp;".join(toks), st["mc"]))

        # ---- PAGE 3 ----
        plan = report.get("monthly_plan", []) or []
        if plan:
            story.append(PageBreak())
            story.extend(seclabel("Month-by-Month Campaign Plan"))
            rows = [[Paragraph("Month", st["cellw"]), Paragraph("Focus &amp; Message", st["cellw"]), Paragraph("Budget Pacing", st["cellw"])]]
            for x in plan:
                fm = [Paragraph(x.get("focus", ""), st["cellb"]), Paragraph(x.get("message", ""), st["cellm"])]
                rows.append([Paragraph(x.get("month", ""), st["cell"]), fm, Paragraph(x.get("pacing", ""), st["cell"])])
            t = Table(rows, colWidths=[78, CW - 78 - 150, 150], repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7fafc")]),
                ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 11),
                ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
            story.append(t)

        # ---- PAGE 4 ----
        geo = sorted(report.get("geofence_locations", []) or [], key=lambda g: g.get("priority", 3))
        story.append(PageBreak())
        if geo:
            story.extend(seclabel("Sample Targets &amp; Geofences"))
            rows = [[Paragraph(h, st["cellw"]) for h in ("P", "Location", "Category", "Method", "Radius")]]
            for g in geo:
                loc = [Paragraph(g.get("name", ""), st["cellb"]), Paragraph(g.get("city_state", ""), st["cellm"])]
                rows.append([
                    Paragraph(f"P{g.get('priority','')}", st["cell"]), loc,
                    Paragraph(g.get("category", ""), st["cell"]),
                    Paragraph(str(g.get("recommended_method", "")).replace("_", " "), st["cell"]),
                    Paragraph(f"{g.get('recommended_radius_miles','')} mi", st["cell"]),
                ])
            t = Table(rows, colWidths=[36, CW - 36 - 150 - 108 - 58, 150, 108, 58], repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7fafc")]),
                ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
            story.append(t)

        story.extend(seclabel("Talk to a Strategist"))
        book = BOOKING_URL or "https://smart1marketing.com"
        cta_flow = [Paragraph("Ready to turn this into a live campaign?", st["ctaH"]),
                    Paragraph("A Smart 1 strategist will verify these locations, build your audiences, and tailor a budget that fits your company — free, no obligation.", st["ctaP"]),
                    Paragraph(f"Book a consult:&nbsp; {book}", st["ctaA"])]
        cta = Table([[cta_flow]], colWidths=[CW])
        cta.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NAVY),
            ("LEFTPADDING", (0, 0), (-1, -1), 24), ("RIGHTPADDING", (0, 0), (-1, -1), 24),
            ("TOPPADDING", (0, 0), (-1, -1), 22), ("BOTTOMPADDING", (0, 0), (-1, -1), 22)]))
        story.append(cta)
        story.append(Spacer(1, 14))
        story.append(Paragraph(report.get("disclaimer", ""), st["disc"]))

        ProposalCanvas.company = company or ""
        doc = SimpleDocTemplate(path, pagesize=letter, title=f"{company} HVAC Comfort & Command Proposal",
                                leftMargin=40, rightMargin=40, topMargin=86, bottomMargin=48)
        doc.build(story, canvasmaker=ProposalCanvas)

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


# Value one-pager served to already-on-LSA (conquest) leads so the disqualify
# screen delivers something useful now, not just a "you're on the list" message.
BEYOND_LSA_HTML = """<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>3 Ways to Grow Beyond Google LSA | Smart 1 Marketing</title>
<link href=\"https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700;800&display=swap\" rel=\"stylesheet\">
<style>:root{--navy:#0a2240;--cool:#0a9ed6;--heat:#f2683c;--ink:#25364b;--muted:#68798c;--line:#dbe5ed}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0,#fdeee6 0,rgba(253,238,230,0) 380px),radial-gradient(circle at 85% 0,#dff2fb 0,rgba(223,242,251,0) 420px),#f3f7fa;font-family:Montserrat,Arial,sans-serif;color:var(--ink);line-height:1.6}
.wrap{max-width:820px;margin:0 auto;padding:48px 22px 80px}
.lock{display:inline-flex;align-items:center;gap:9px;font-size:.72rem;font-weight:800;letter-spacing:.14em;color:var(--navy);margin-bottom:20px}
.mark{display:grid;place-items:center;width:32px;height:32px;border-radius:9px;background:var(--navy);color:#fff}
.eyebrow{font-weight:800;letter-spacing:.14em;color:var(--heat);font-size:.72rem}
h1{color:var(--navy);font-size:2.3rem;letter-spacing:-.03em;margin:.4rem 0 .6rem}
.sub{color:var(--muted);font-size:1.02rem;max-width:640px}
.card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:24px 26px;margin:18px 0;box-shadow:0 14px 40px rgba(10,34,64,.07)}
.card h2{color:var(--navy);font-size:1.2rem;margin:0 0 6px;display:flex;gap:12px;align-items:center}
.num{flex:0 0 auto;display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:var(--heat);color:#fff;font-weight:800}
.card p{margin:8px 0 0;color:#3a4b5e}
.card .why{margin-top:10px;font-size:.86rem;color:var(--muted);border-left:3px solid var(--cool);padding-left:12px}
.cta{background:var(--navy);border-radius:16px;padding:26px;text-align:center;color:#fff;margin-top:26px}
.cta h3{margin:0 0 6px;font-size:1.3rem}.cta p{margin:0 0 16px;color:#c7d5e2}
.cta a{display:inline-block;background:var(--heat);color:#fff;text-decoration:none;font-weight:800;padding:14px 26px;border-radius:10px}
.foot{color:var(--muted);font-size:.75rem;margin-top:26px;border-top:1px solid var(--line);padding-top:16px}
@media print{body{background:#fff}.cta{background:#0a2240 !important;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
</style></head><body><div class=\"wrap\">
<div class=\"lock\"><span class=\"mark\">S1</span><span>SMART 1 MARKETING</span></div>
<div class=\"eyebrow\">HVAC GROWTH BRIEF</div>
<h1>3 Ways to Grow Beyond Google LSA</h1>
<p class=\"sub\">You already own the highest-intent spot in search. Here's where the next wave of HVAC demand actually comes from — the homeowners LSA never shows you.</p>
<div class=\"card\"><h2><span class=\"num\">1</span> Catch demand before they search</h2>
<p>LSA only reaches people already typing \"AC repair near me.\" But most replacement decisions start days earlier, when a system struggles on the first 90&deg; day. SmartClimate&trade; Connected TV and display fire automatically on extreme-weather days and put you in the living room before the emergency search ever happens.</p>
<div class=\"why\">Weather-triggered media pauses in mild weather, so you only pay on high-value days.</div></div>
<div class=\"card\"><h2><span class=\"num\">2</span> Own the new-mover window</h2>
<p>Households that moved in the last 6 months are ~5x more likely to buy home services — and they have no HVAC company yet. New-mover and home-improver audience data lets you be first in the door before a competitor's LSA ever gets the call.</p>
<div class=\"why\">This is net-new pipeline LSA structurally cannot reach.</div></div>
<div class=\"card\"><h2><span class=\"num\">3</span> Turn one-time calls into recurring revenue</h2>
<p>Automated review generation after every ticket lifts your Google rating (lowering future acquisition cost), while Missed-Call Text Back and seasonal tune-up automation convert one-off repairs into maintenance-plan members who buy the eventual replacement from you.</p>
<div class=\"why\">Lower cost per lead + higher lifetime value on the customers you already win.</div></div>
<div class=\"cta\"><h3>Want this mapped to your market?</h3>
<p>We'll build a conquest plan around your service area — free, no obligation.</p>
<a href=\"__BOOKING_URL__\">Book my conquest strategy call</a></div>
<p class=\"foot\">Smart 1 Marketing &middot; Media budgets are transparent and separate from software and management fees. Estimates are planning recommendations finalized with a strategist before activation.</p>
</div></body></html>"""


@app.get("/")
def index():
    return render_template("index.html", booking_url=BOOKING_URL)


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/api/capture")
def capture():
    """Capture-on-submit: fire a lead webhook the INSTANT the form is submitted,
    before the AI report runs, so a slow or failed report never loses the lead."""
    try:
        payload = clean_payload(request.get_json(silent=True) or {})
        payload["lead_type"] = payload.get("lead_type") or "qualified"
        send_webhook(payload, None, "lead_received")
        return jsonify({"ok": True})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        app.logger.exception("Capture failed")
        return jsonify({"ok": False}), 500


@app.get("/beyond-lsa")
def beyond_lsa():
    """Value one-pager shown to already-on-LSA (conquest) leads."""
    return BEYOND_LSA_HTML.replace("__BOOKING_URL__", BOOKING_URL or "#")


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
        pdf_url = build_report_pdf(report, payload.get("company_name", "Market Plan"), base_url, payload)
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
