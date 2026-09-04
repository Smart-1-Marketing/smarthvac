"""Write-ahead record of every lead this app takes.

The webhook POST used to be the only record that a lead ever existed. Three
ways that lost one, all of them silent and all of them behind a success
response already on its way to the visitor:

  * `if not WEBHOOK_URL: return` -- an unset or misnamed environment variable
    threw every lead away, for the life of the deploy, with nothing logged.
  * a GoHighLevel timeout was caught and logged, and the lead was still gone,
    because there was nowhere else it had been written down.
  * `requests.post(...)` was never checked for its status, so a wrong webhook
    URL answered 404 and the lead read as delivered.

So the lead is written down BEFORE the webhook fires and updated after it, and
every failure is loud. Two places, because neither on its own is enough:

  * `leads.jsonl` on local disk -- instant, and what `replay_failed.py` reads.
    This service has no Render disk mounted, so that file dies with the
    instance; it is the working copy, not the durable one.
  * Cloudinary -- one small raw object per lead, overwritten in place, which
    is what actually survives a redeploy. The app already holds a Cloudinary
    client for the report PDFs, so this costs no new dependency and no new
    credential.

Append-only, one line per event. An update appends rather than rewriting,
because gunicorn runs two workers here and rewriting a file both of them hold
open is how one worker's lead disappears. `rows()` reduces the log by taking
the last event per lead. Each line is one `write()` of a deliberately small
row -- the lead's own fields, never the generated report -- which is what keeps
two workers from interleaving halves of a line; `rows()` skips and counts an
unparseable line anyway, because "small enough in practice" is not a guarantee
and a torn tail must not hide the leads above it.

**Nothing in here may raise.** A lead store that can break the tool it exists
to protect is worse than no lead store, so every entry point returns rather
than throwing, and a failure to record costs the record and never the
visitor's report.
"""
import datetime
import io
import json
import logging
import os
import re
import tempfile
import uuid

log = logging.getLogger(__name__)

# The app that owns these rows. Used in the Cloudinary path so several tools
# can mirror into one account without colliding.
SOURCE_SLUG = "hvac"

# Per-value cap. The report itself is deliberately not recorded here: it is
# regenerable, it is already on Cloudinary as a PDF, and it is what would push
# a line past the atomic-append size.
MAX_VALUE = 1500

_STATUS_SENT = "sent"
# The Hub answers three ways, and only one of them means this app still owes
# the lead to anybody. "accepted" is the Hub having stored it and taken over
# the retry itself: replaying one of those from here writes a second lead row
# for a single visitor, which is the duplicate the single write path exists to
# prevent. So it is done as far as this app is concerned, and still not a
# contact -- which is why the two are counted apart rather than merged.
_STATUS_ACCEPTED = "accepted"
# A lead with nobody to contact on it -- an abandoned form that never reached
# the contact step. Not owed, because no number of retries will make one
# deliverable; kept and counted, because it is still a real visitor.
_STATUS_UNDELIVERABLE = "undeliverable"
_DONE_STATUSES = (_STATUS_SENT, _STATUS_ACCEPTED, _STATUS_UNDELIVERABLE)


def _data_dir() -> str:
    """Prefer a mounted Render disk, fall back to the system temp directory.

    Overridable with LEADS_DIR. Never raises: a directory that cannot be made
    falls through to the temp directory, which always exists.
    """
    for candidate in (os.getenv("LEADS_DIR", "").strip(), "/var/data"):
        if candidate and os.path.isdir(candidate):
            return candidate
    path = os.path.join(tempfile.gettempdir(), f"s1-{SOURCE_SLUG}-leads")
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return tempfile.gettempdir()


def leads_path() -> str:
    override = os.getenv("LEADS_FILE", "").strip()
    return override or os.path.join(_data_dir(), "leads.jsonl")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip().lower()).strip("-") or "unknown"


def _append(row: dict) -> None:
    """One line, one event. Never raises."""
    try:
        line = json.dumps(row, separators=(",", ":"), default=str) + "\n"
        with open(leads_path(), "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:                       # noqa: BLE001 -- never cost a lead
        log.exception("Could not append to the lead log at %s", leads_path())


def _mirror(row: dict) -> None:
    """Overwrite this lead's object in Cloudinary. Never raises.

    One object per lead rather than a copy of the whole log: the log grows
    without bound and two workers uploading it would race, where a per-lead
    object is bounded, idempotent and safe to overwrite from either worker.
    """
    try:
        import cloudinary.uploader                       # local: optional dep
        if not os.getenv("CLOUDINARY_URL", "").strip():
            return
        body = json.dumps(row, separators=(",", ":"), default=str).encode("utf-8")
        cloudinary.uploader.upload(
            io.BytesIO(body),
            public_id=f"leads/{SOURCE_SLUG}/{row.get('lead_id')}",
            resource_type="raw",
            overwrite=True,
            use_filename=False,
            unique_filename=False,
            timeout=10,
        )
    except Exception:                       # noqa: BLE001 -- never cost a lead
        log.exception("Could not mirror lead %s to Cloudinary", row.get("lead_id"))


def record(fields: dict, kind: str = "lead", **extra) -> dict:
    """Write the lead down before anything is attempted with it.

    Returns the row. Hand that same row to `mark()` afterwards rather than
    looking it up again -- both calls happen inside one request, so passing it
    along is race-free and saves re-reading a file the other worker is also
    appending to.
    """
    row = {
        "lead_id": str(extra.pop("lead_id", "") or uuid.uuid4().hex),
        "source": SOURCE_SLUG,
        "kind": kind,
        "created": _now(),
        "updated": _now(),
        "status": "pending",
        "fields": {k: str(v)[:MAX_VALUE] for k, v in (fields or {}).items() if v not in (None, "")},
    }
    row.update({k: v for k, v in extra.items() if v not in (None, "")})
    _append(row)
    _mirror(row)
    return row


def mark(row: dict, status: str, **extra) -> dict:
    """Record what happened to a lead. `status` is "sent" or "failed: <why>".

    Anything that is not exactly "sent" is logged at error level with the
    lead's own fields, because that is the message somebody needs in order to
    know a lead is owed -- the whole failure this module exists to end.
    """
    if not isinstance(row, dict):
        return {}
    row = dict(row)
    row["status"] = status
    row["updated"] = _now()
    row.update({k: v for k, v in extra.items() if v not in (None, "")})
    _append(row)
    _mirror(row)
    if str(status).startswith(_STATUS_UNDELIVERABLE):
        log.info("Lead has nobody to contact, so no contact was created (%s) "
                 "id=%s", SOURCE_SLUG, row.get("lead_id"))
    elif status == _STATUS_ACCEPTED:
        # Not an error: the lead is stored at the Hub and being retried there.
        # Said out loud anyway, because "stored somewhere else" and "in the
        # CRM" are different answers and only the second is finished.
        log.warning("Lead accepted by the Hub, not yet in Smart 1 Suite (%s) "
                    "id=%s detail=%s", SOURCE_SLUG, row.get("lead_id"),
                    row.get("detail", ""))
    elif status != _STATUS_SENT:
        log.error("LEAD NOT DELIVERED (%s) id=%s status=%s fields=%s",
                  SOURCE_SLUG, row.get("lead_id"), status,
                  json.dumps(row.get("fields") or {}, default=str))
    return row


def rows() -> list:
    """The log reduced to one entry per lead, latest event winning.

    A line that will not parse is skipped and counted in the log rather than
    taking the whole read down -- a half-written line at the tail is exactly
    what a crash mid-append leaves, and it must not hide the leads above it.
    """
    latest: dict = {}
    bad = 0
    path = leads_path()
    try:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(row, dict) and row.get("lead_id"):
                    latest[row["lead_id"]] = row
    except Exception:                       # noqa: BLE001
        log.exception("Could not read the lead log at %s", path)
    if bad:
        log.warning("Skipped %d unreadable line(s) in %s", bad, path)
    return sorted(latest.values(), key=lambda r: r.get("created", ""))


def unsent() -> list:
    """Leads still owed by this app -- what `replay_failed.py` re-posts.

    A lead the Hub accepted is not in here: the Hub holds it and retries it,
    and re-posting one would create a second lead row for one visitor.
    `accepted()` is how many are in that state, counted apart because it is
    not the same claim as being in the CRM.
    """
    return [r for r in rows()
            if not str(r.get("status", "")).startswith(_DONE_STATUSES)]


def undeliverable() -> list:
    """Leads with nobody to contact on them. Kept, never replayed."""
    return [r for r in rows()
            if str(r.get("status", "")).startswith(_STATUS_UNDELIVERABLE)]


def accepted() -> list:
    """Leads the Hub has, that are not confirmed in Smart 1 Suite yet."""
    return [r for r in rows() if r.get("status") == _STATUS_ACCEPTED]
