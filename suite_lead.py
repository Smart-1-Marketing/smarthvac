"""Delivery of one lead into Smart 1 Suite, down the Hub's single write path.

## Why this replaces the webhook

This app used to POST the lead at a GoHighLevel *inbound webhook*. The only
thing a webhook can tell you is that GoHighLevel accepted an HTTP request --
never that a contact exists. A 2xx from it was recorded as "delivered", which
is the weakest possible confirmation, and a mistyped URL answering 404 read
the same as a delivery until `lead_store` started checking the status.

The Hub writes the contact over the GoHighLevel **Contacts API** and gets a
contact id back. That id is the proof, and it is what makes "delivered" mean
something on this app's own health probe as well as on the Hub's leads panel.

## Why through the Hub and not straight at GoHighLevel

`hub/leads.py` exists to be the one place every lead in this company is
written: one token, one sub-account id, one contact write path, one panel
that can answer "how many leads did we get last week, and from which pages?"
Calling the Contacts API from here as well would put the Suite token on five
more Render services -- five more places to rotate it and five more to leak
it -- and would leave this app's leads invisible to that panel, which is
where anybody actually looks for them.

So the delivery this module performs is one POST to the Hub, and the Hub
performs the write. What comes back says which of three things happened, and
they are three because they need three different responses:

  * **delivered** -- a contact id came back. The lead is in Smart 1 Suite.
  * **accepted**  -- the Hub stored the lead and could not reach Suite yet.
    It owns the retry from here. This app must NOT replay it: doing so writes
    a second lead row for one visitor, which is the duplicate the single path
    exists to prevent.
  * **failed**    -- the Hub never got it. Owed, and `replay_failed.py` is
    what re-posts it.

## What is deliberately absent

**There is no fallback to the webhook.** It is tempting -- if the Hub is
unreachable, fire the old URL instead -- and it is wrong for the same reason
`hub/ghl_contacts.py` gives about falling back per lead: the failure that
matters most is a timeout, which is precisely the case where we cannot tell
whether the write landed. Falling back there is how one visitor becomes two
contacts, with nothing to reconcile them against afterwards. One lead goes
down one path; the safety net is the write-ahead record in `lead_store`.

**Nothing here raises.** A delivery fault must never cost the visitor their
report, and the caller has already written the lead down before calling.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

# The Hub's public capture endpoint. Defaulted rather than required: it is a
# fixed address that has not changed, and an app that has to be told where the
# Hub is in order to keep a lead is an app that loses leads on the day nobody
# sets the variable -- which is the failure this whole module exists to end.
DEFAULT_ENDPOINT = "https://smart1.agency/api/leads/capture"

# Sent as a header so the Hub's per-IP rate limit does not apply. That limit is
# three an hour and is written for a browser; every lead this app takes arrives
# at the Hub from one Render egress address, so without the token the fourth
# visitor of a busy hour is refused. Unset here means the lead is still stored
# and still attempted -- it is a throttle, not a gate.
TOKEN_ENV = "LEADS_SOURCE_TOKEN"
TOKEN_HEADER = "X-S1-Lead-Token"

TIMEOUT = int(os.getenv("HUB_LEAD_TIMEOUT") or 12)

STATUS_DELIVERED = "delivered"
STATUS_ACCEPTED = "accepted"
# A lead that cannot be delivered because of what it *is*, rather than because
# of anything that went wrong. Its own state, and not `failed`, because the two
# need opposite handling: a failure is owed and replayed, and this one will be
# refused identically however many times it is offered. Left as a failure it
# would sit in the owed count for ever and be re-posted on every replay run --
# a backlog that can never clear, which is the permanent red this codebase
# keeps having to undo. It is still recorded, still visible, and still counted.
STATUS_UNDELIVERABLE = "undeliverable"
STATUS_FAILED = "failed"


# --- Mapping this app's own field names onto the Hub's ---------------------
#
# The Hub keeps every field it is given, and writes into GoHighLevel only the
# handful the Contacts API has a real home for. So the mapping below is not
# about what survives -- everything survives, in the Hub's own store -- it is
# about which of this app's fields land in a *named* place on the contact
# rather than in the row's spare fields.
#
# One table for all four Python landing apps, so it is identical in each and
# the next correction to it lands once. They ask the same questions in four
# vocabularies: a boat dealership's name is `dealer_name`, a law firm's is
# `firm_name`, a resort's is `resort_name`, an HVAC company's is
# `company_name`. First non-empty wins, in the order written.
#
# `city_state` is deliberately in none of them. Splitting "Columbus, OH" on
# the comma is right until the day it is "Kansas City, KS" or a town with a
# comma in its own name, and a wrong state on a contact is worse than a field
# the Hub is holding for you. It stays in the spare fields, whole.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name":    ("contact_name", "c_name", "full_name", "name"),
    "email":   ("contact_email", "c_email", "email", "proposal_recipient_email"),
    "phone":   ("contact_phone", "c_phone", "phone"),
    "company": ("dealer_name", "firm_name", "resort_name", "company_name",
                "business_name", "company", "business"),
    "website": ("website", "url", "domain"),
    "zip":     ("dealer_zip", "firm_zip", "company_zip", "resort_zip",
                "zip_code", "zip", "postal_code"),
    "city":    ("city",),
    "state":   ("state",),
}

# Kept out of the payload entirely rather than passed and ignored. The report
# JSON is tens of kilobytes, is regenerable, and is already a PDF on
# Cloudinary; the two link fields are passed under their own names.
SKIP_FIELDS = frozenset({"report_json", "report_url", "report_pdf_url"})


def lead_fields(body: dict) -> dict:
    """This app's lead body, with the named fields the Hub can place on a
    contact resolved to the Hub's own names. Everything else rides along
    unchanged -- the Hub stores it, and storing it is the point.
    """
    body = {k: v for k, v in (body or {}).items()
            if k not in SKIP_FIELDS and v not in (None, "")}
    out = dict(body)
    for hub_key, names in FIELD_ALIASES.items():
        for n in names:
            v = body.get(n)
            if v not in (None, ""):
                out[hub_key] = v
                break
    return out


def lead_meta(body: dict, extra_tags=()) -> dict:
    """What travels beside the fields: the report link a Suite workflow would
    email, and any free segmentation tags.

    The tags are what the webhook used to achieve with GoHighLevel "Add Tag"
    workflow actions driven by `market_tag` / `package_tag`. Over the API a
    tag is just a field on the contact, so they are sent as tags and there is
    no workflow in the middle to go missing.
    """
    body = body or {}
    meta: dict = {}
    link = str(body.get("report_url") or "").strip()
    if link:
        meta["report_url"] = link
    tags = [str(t).strip() for t in
            (list(extra_tags) + [body.get("market_tag"), body.get("package_tag")])
            if str(t or "").strip()]
    if tags:
        meta["tags"] = tags[:8]
    return meta


def endpoint() -> str:
    """Where leads are posted. Quotes are stripped because Render stores them
    literally, which has silently broken URL matching in this codebase before.
    """
    v = (os.getenv("HUB_LEAD_ENDPOINT") or "").strip().strip('"').strip("'")
    return v or DEFAULT_ENDPOINT


def token() -> str:
    return (os.getenv(TOKEN_ENV) or "").strip().strip('"').strip("'")


def configured() -> bool:
    """Is there somewhere to deliver to? Always true while the default holds,
    and kept as a function so a deployment that blanks the endpoint on purpose
    reads as unconfigured rather than as broken."""
    return bool(endpoint())


def why_not() -> str:
    """The reason delivery is unavailable, in words. "" when it is available."""
    if not endpoint():
        return ("HUB_LEAD_ENDPOINT is set to an empty value, so there is "
                "nowhere to deliver leads to. Unset it to use the default "
                f"({DEFAULT_ENDPOINT}), or point it at the Hub.")
    return ""


def _headers() -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    tok = token()
    if tok:
        h[TOKEN_HEADER] = tok
    return h


def deliver(source: str, page: str, fields: dict, pdf_url: str = "",
            meta: dict | None = None) -> dict:
    """Post one lead to the Hub. Returns what happened; never raises.

    `status` is one of the three above. `retryable` says whether
    `replay_failed.py` should ever re-post this lead -- false for a delivery,
    false for a lead the Hub has accepted and is retrying itself, and false
    for a refusal the Hub will refuse again however many times it is asked.
    """
    out = {"status": STATUS_FAILED, "ok": False, "contact_id": "",
           "hub_lead_id": "", "http_status": 0, "detail": "", "retryable": True}

    url = endpoint()
    if not url:
        out.update(detail=why_not(), retryable=False)
        return out

    body = {"source": source, "page": page,
            "fields": {k: v for k, v in (fields or {}).items()
                       if v not in (None, "")}}
    if pdf_url:
        body["pdf_url"] = pdf_url
    if meta:
        body["meta"] = meta

    # The Hub refuses a lead with neither, and so should this -- a contactless
    # lead reads as a live prospect on every count that follows it.
    if not (body["fields"].get("email") or body["fields"].get("phone")):
        # Not a fault. An abandoned form that never reached the contact step
        # genuinely has nobody to write down, and the Hub refuses one for the
        # same reason: a contactless lead reads as a live prospect on every
        # count that follows it. Whatever did arrive is kept in this app's own
        # log, which is where somebody would go looking for it.
        out.update(status=STATUS_UNDELIVERABLE,
                   detail="The lead has neither an email nor a phone, so there "
                          "is nobody to create a contact for. Whatever the "
                          "visitor did fill in is kept in this app's lead log.",
                   retryable=False)
        return out

    try:
        import requests
        r = requests.post(url, json=body, headers=_headers(), timeout=TIMEOUT)
    except Exception as exc:                              # noqa: BLE001
        # Includes the timeout, which is the one case where we genuinely do
        # not know whether the Hub wrote the lead. It is retryable because the
        # Hub de-duplicates nothing for us -- but the retry goes back down this
        # same path, never down a second one.
        out["detail"] = f"Couldn't reach the Hub ({type(exc).__name__}). Will retry."
        return out

    out["http_status"] = r.status_code
    try:
        data = r.json() if r.text else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    if r.status_code == 429:
        # Named, because the caller reading this is a server and "wait" is not
        # the fix -- the token is.
        out["detail"] = (f"The Hub rate-limited this lead (HTTP 429). Every lead "
                         f"this app sends arrives from one address, so set "
                         f"{TOKEN_ENV} here and on the Hub to lift the per-IP "
                         f"limit. The lead is stored and replayable.")
        return out

    if not r.ok:
        out["detail"] = (f"The Hub refused the lead: HTTP {r.status_code} "
                         f"{str(data.get('error') or r.text)[:200]}")
        # A 400 is a payload the Hub will refuse identically every time, and a
        # 401/403 is configuration. Retrying either on a timer burns the log
        # and hides the cause.
        if r.status_code in (400, 401, 403, 422):
            out["retryable"] = False
        return out

    out["hub_lead_id"] = str(data.get("lead_id") or "")
    cid = str(data.get("contact_id") or "")
    if data.get("delivered") and cid:
        out.update(status=STATUS_DELIVERED, ok=True, contact_id=cid,
                   retryable=False,
                   detail="Created in Smart 1 Suite.")
        return out

    if data.get("ok"):
        # The Hub has the lead and owns the retry. Not delivered, and not this
        # app's to chase -- replaying it here would write a second lead row for
        # one visitor.
        #
        # A `delivered: true` with no contact id lands here too, and is the
        # one answer worth naming rather than normalising. It is the shape
        # the retired webhook always had -- somebody said yes and there is no
        # id to check it against -- so it is treated as stored-not-delivered,
        # which is the safe reading, and the contradiction is said out loud
        # instead of being quietly rounded down to a state that looks routine.
        note = str(data.get("note") or "").strip()
        if data.get("delivered"):
            detail = ("The Hub reported this lead as delivered but returned no "
                      "contact id, so there is nothing to confirm it against. "
                      "Treating it as stored at the Hub and not yet in Smart 1 "
                      "Suite, which is the safe reading of the two. " + note).strip()
        else:
            detail = ("The Hub stored the lead and will deliver it to "
                      "Smart 1 Suite. " + note).strip()
        out.update(status=STATUS_ACCEPTED, ok=True, retryable=False, detail=detail)
        return out

    out["detail"] = (f"The Hub answered HTTP {r.status_code} without confirming "
                     f"the lead. Body: {json.dumps(data)[:200] or str(r.text)[:200]}")
    return out
