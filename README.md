# Smart 1 HVAC "Comfort & Command" Market Intelligence

A multi-step Smart 1 Marketing lead tool for HVAC contractors. It creates an AI
weather-triggered demand and targeting plan with:

- A recommended investment tier matched to the market and goals
- An always-on emergency-capture pipeline (Google LSA + high-intent Search + Missed-Call Text Back)
- A SmartForecast™ weather-triggered CTV & display plan (activates on extreme heat/cold, pauses in mild weather)
- New-mover and home-improver audience targeting
- Estimated population, homeowner households, and replacement-ready (aging-system) homes
- A 12-month campaign plan with seasonal budget pacing
- Ranked target locations & geofences (new-construction areas, older ZIP clusters, competitor conquest, home-improvement retail)
- Smart 1 Suite webhook payload + branded print-to-PDF report

## The LSA screening gate (soft disqualify → conquest)

Step 1 asks whether the contractor already runs **Google Local Services Ads**.
If they answer **"Yes — running it now,"** the form branches to a friendly
conquest-capture screen instead of the full flow: it still collects their name,
email, and phone and posts to `/api/lead`, which routes the lead to Smart 1 Suite
tagged `lead_type: "conquest_disqualified"` so you can pursue them with a conquest
campaign later. The other answers (paused / no / not sure) continue into the full
plan, which prioritizes claiming and optimizing the LSA profile as the #1 move.

## Important limitation

This version intentionally uses AI planning estimates instead of paid maps,
geocoding, census, or permit APIs. It does not claim live verification. Before
media activation, a strategist should verify locations and build the final
audiences/polygons in the advertising platform.

## Project structure

```
smart1hvac/
├── app.py               # Flask backend + OpenAI report generation + webhook + PDF
├── templates/
│   ├── landing.html     # Marketing landing page (served at /) — funnels into the form
│   └── index.html       # Self-contained multi-step intake form (CSS + JS inlined)
├── static/
│   └── reports/         # Generated PDFs are written here
├── requirements.txt
├── Procfile
├── render.yaml
├── .env.example
└── .gitignore
```

Both pages live in `templates/` and are served with Flask's `render_template`.
`landing.html` is the marketing front door at `/`; its primary calls-to-action
link to `/plan`, which serves the multi-step intake form (`index.html`). Both
pages are intentionally self-contained — all CSS and JavaScript are inlined, so
there are no separate `styles.css` or `app.js` files to keep in sync. This keeps
them reliable when embedded in Smart 1 Suite. The landing page's "Talk to a
Strategist" links point to `https://smart1marketing.com/free-consultation`.

## Endpoints

- `GET /` — the marketing landing page
- `GET /plan` — the multi-step intake form
- `GET /health` — health check (used by Render)
- `POST /api/analyze` — generates the AI plan for a qualified lead
- `POST /api/lead` — lightweight capture for the LSA conquest (soft-disqualify) branch

## Deploy to GitHub

1. Create a new GitHub repository named `smart1hvac`.
2. Upload every file and folder in this project. Keep the folder structure intact (especially `templates/` and `static/`).
3. Do not upload a real `.env` file or API key.

## Deploy to Render

1. In Render, choose **New + > Blueprint**.
2. Connect the `smart1hvac` GitHub repository.
3. Render will read `render.yaml`.
4. Add the secret environment variable `OPENAI_API_KEY`.
5. Add `GHL_WEBHOOK_URL` — the GoHighLevel (Smart 1 Suite) inbound-webhook trigger URL. Lead data + the report PDF URL are POSTed here.
6. Add `CLOUDINARY_URL` — permanent storage for the generated PDF reports (`cloudinary://<api_key>:<api_secret>@<cloud_name>`). Reports are stored under the `hvac-report` folder.
7. Add `PUBLIC_BASE_URL` = your live Render URL (e.g. `https://smarthvac.onrender.com`). Only used for the local-PDF fallback link when Cloudinary is not configured.
8. Keep `OPENAI_MODEL` at the default or change it to a model available in your OpenAI account.
9. Deploy and test `/health`, open `/` for the landing page, then test the full form at `/plan`.

### Environment variables (Render)

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | yes | Generates the AI plan. If unset/invalid, a labeled baseline plan is served. |
| `GHL_WEBHOOK_URL` | yes | GoHighLevel inbound webhook — receives the lead + `report_pdf_url`. (`SMART1_WEBHOOK_URL` still read as a fallback.) |
| `CLOUDINARY_URL` | yes | Permanent PDF storage. Falls back to local `static/reports/` if unset. |
| `OPENAI_MODEL` | no | Defaults to `gpt-4.1-mini`. |
| `PUBLIC_BASE_URL` | no | Absolute base for the local-PDF fallback URL only. |
| `ENABLE_PDF` | no | `1` (default) builds the PDF; `0` disables it. |
| `PYTHON_VERSION` | no | `3.12.4`. |

## Smart 1 Suite fields

Recommended custom fields:

- Company Name
- Website
- Company ZIP
- Service Radius
- Locations
- Services
- Revenue Focus
- Objectives
- LSA Status
- Lead Type (qualified / conquest_disqualified)
- Notes
- Recommended Package
- Recommended Investment
- Market Type
- Market Summary
- Est Homeowner Households
- Est Replacement Opportunity
- Weather Triggers
- Report PDF URL
- Report JSON (large text field, optional)

The webhook sends human-readable fields plus `report_json`. Map the summary and
opportunity fields first; store the full JSON externally or in a large-text field
if the Suite webhook ignores large data. The `lead_type` field lets you route
conquest (already-on-LSA) leads into a separate pipeline.

## Embed on Smart 1 Suite

```html
<iframe
  src="https://YOUR-RENDER-URL.onrender.com/plan"
  style="width:100%;min-height:1200px;border:0;border-radius:12px;"
  loading="lazy"
  title="HVAC Comfort & Command Market Plan">
</iframe>
```

Using an iframe keeps the JavaScript and API request on the same Render domain and
avoids cross-origin restrictions inside Smart 1 Suite.

## PDF report

Every completed report is rendered to a branded PDF (via `reportlab`, pure
Python — no system libraries needed). When `CLOUDINARY_URL` is set, the PDF is
uploaded to Cloudinary as a `raw` asset under the **`hvac-report`**
folder (permanent storage), and the returned `secure_url` is sent to the GHL
webhook as `report_pdf_url`. If Cloudinary is not configured or the upload
fails, the app falls back to writing the PDF under `static/reports/` and sends a
`PUBLIC_BASE_URL`-based link instead (ephemeral on Render's disk). Set
`ENABLE_PDF=0` to turn PDF generation off entirely.

## Investment tiers

| Tier | Price | Best for |
| --- | --- | --- |
| Comfort Starter | $749/mo | Brand-new / single-truck: LSA + Search foundation |
| Local Comfort Foundation | $1,499/mo | Dominate one city (documented tier) |
| Omnichannel Market Leader | $3,499/mo | Add SmartForecast CTV + New-Mover display (documented tier) |
| Regional Domination | $6,500/mo | Multi-market omnichannel at scale |

> Comfort Starter ($749) and Regional Domination ($6,500) are illustrative
> placeholders — set your real starter and premium prices in `app.py`
> (`generate_report` package menu) and `templates/index.html` (`PACKAGES` array).

## Test locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then add your OPENAI_API_KEY
python app.py
```

Open `http://localhost:5000` for the landing page, or `http://localhost:5000/plan` for the intake form.
