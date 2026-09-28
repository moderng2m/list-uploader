"""Admin routes (SPEC §6.8, §21.2.6): settings, lead sources, aliases, audit search.

Every route calls `require_admin` first, which records ACCESS_DENIED and returns
403 for anyone else. Every change writes ADMIN_CONFIG_CHANGED (before/after) in
the same transaction as the new setting, conditioned on the version the admin saw.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import UTC, datetime
from typing import Any

from aws_lambda_powertools.event_handler import Response, content_types
from aws_lambda_powertools.event_handler.exceptions import BadRequestError, NotFoundError

from bff.app import app, correlation_id, deps, json_body, require_admin
from bff.audit_api import reader
from bff.auth import User
from shared import config_defaults, messages
from shared.analysis import lead_source_key
from shared.audit import AuditEvent, EventType, StateConflict, email_sha256
from shared.audit_read import SearchCriteria
from shared.audit_text import event_view
from shared.catalog import BY_KEY, FIELDS, normalize_header
from shared.config_store import (
    ALIASES_PK,
    LEAD_SOURCES_PK,
    THRESHOLDS_PK,
    LeadSourceItem,
)
from shared.ids import new_ulid
from shared.observability import metrics
from shared.processed_file import safe_cell

SEARCH_LIMIT = 500
EXPORT_LIMIT = 10_000
EXPORT_URL_TTL = 300
MAX_LEAD_SOURCE = 255
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _json(status: int, body: Any) -> Response[Any]:
    return Response(status_code=status, content_type=content_types.APPLICATION_JSON, body=body)


def _version(body: dict[str, Any]) -> str:
    version = body.get("version")
    if not isinstance(version, str) or not version:
        raise BadRequestError("Reload the page and try again (missing version).")
    return str(version)


def _save(
    user: User,
    pk: str,
    body: dict[str, Any],
    *,
    expected: str,
    change: str,
    before: Any,
    after: Any,
) -> str | None:
    """Write the setting and its audit event together. None if someone else changed it."""
    d = deps()
    new = new_ulid()
    event = AuditEvent(
        event_type=EventType.ADMIN_CONFIG_CHANGED,
        actor=user.actor(),
        subject={"setting": pk},
        before={pk: before},
        after={pk: after},
        reason="admin",
        details={"setting": pk, "change": change, "from_version": expected, "to_version": new},
        correlation_id=correlation_id(),
    )
    try:
        d.audit.transact([event], [d.config.put_op(pk, body, expected=expected, new=new)])
    except StateConflict:
        return None
    metrics.add_metric(name="AdminConfigChanges", unit="Count", value=1)
    return new


def _conflict() -> Response[Any]:
    return _json(409, {"message": messages.CONFIG_CHANGED})


# --- thresholds -----------------------------------------------------------------------


def _config_view() -> dict[str, Any]:
    version, thresholds = deps().config.threshold_state()
    return {"thresholds": thresholds, "limits": config_defaults.LIMITS, "version": version}


@app.get("/admin/config")
def admin_config() -> dict[str, Any]:
    require_admin("/admin/config")
    return _config_view()


@app.put("/admin/thresholds")
def update_thresholds() -> Response[Any]:
    user = require_admin("/admin/thresholds")
    body = json_body()
    expected = _version(body)
    changes = body.get("values")
    if not isinstance(changes, dict) or not changes:
        raise BadRequestError("Send the thresholds to change.")
    _, current = deps().config.threshold_state()
    new = dict(current)
    for key, value in changes.items():
        if key not in config_defaults.THRESHOLDS:
            raise BadRequestError(messages.THRESHOLD_UNKNOWN.format(key=key))
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
            raise BadRequestError(messages.THRESHOLD_RANGE.format(key=key))
        new[key] = float(value)
    if new["junk_block_threshold"] < new["junk_flag_threshold"]:
        raise BadRequestError(messages.THRESHOLD_JUNK_ORDER)
    changed = {k: v for k, v in new.items() if current.get(k) != v}
    if not changed:
        return _json(200, _config_view())
    saved = _save(
        user,
        THRESHOLDS_PK,
        {"values": new},
        expected=expected,
        change="updated " + ", ".join(sorted(changed)),
        before={k: current.get(k) for k in changed},
        after=changed,
    )
    return _json(200, _config_view()) if saved else _conflict()


# --- lead sources ---------------------------------------------------------------------


def _lead_sources_view() -> dict[str, Any]:
    version, items = deps().config.lead_source_items()
    return {"version": version, "items": [i.as_dict() for i in items]}


def _save_lead_sources(
    user: User,
    expected: str,
    before: list[LeadSourceItem],
    after: list[LeadSourceItem],
    change: str,
) -> Response[Any]:
    after = [
        LeadSourceItem(i.id, i.value, i.active, n) for n, i in enumerate(after)
    ]  # orders are always 0..n-1
    saved = _save(
        user,
        LEAD_SOURCES_PK,
        {"values": [i.as_dict() for i in after]},
        expected=expected,
        change=change,
        before=[i.as_dict() for i in before],
        after=[i.as_dict() for i in after],
    )
    return _json(200, _lead_sources_view()) if saved else _conflict()


def _clean_value(value: Any, items: list[LeadSourceItem], *, skip_id: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError(messages.LEAD_SOURCE_BLANK)
    clean = re.sub(r"\s+", " ", value).strip()
    if len(clean) > MAX_LEAD_SOURCE:
        raise BadRequestError(messages.LEAD_SOURCE_TOO_LONG)
    for i in items:
        if i.id != skip_id and lead_source_key(i.value) == lead_source_key(clean):
            raise BadRequestError(messages.LEAD_SOURCE_DUPLICATE.format(value=i.value))
    return clean


@app.get("/admin/lead-sources")
def list_lead_sources() -> dict[str, Any]:
    require_admin("/admin/lead-sources")
    return _lead_sources_view()


@app.post("/admin/lead-sources")
def add_lead_source() -> Response[Any]:
    user = require_admin("/admin/lead-sources")
    body = json_body()
    expected = _version(body)
    _, items = deps().config.lead_source_items()
    value = _clean_value(body.get("value"), items)
    new = LeadSourceItem(f"ls_{new_ulid()}", value, True, len(items))
    return _save_lead_sources(user, expected, items, [*items, new], f"added '{value}'")


@app.patch("/admin/lead-sources/<item_id>")
def change_lead_source(item_id: str) -> Response[Any]:
    """Rename or (de)activate. There is no delete: history references the values."""
    user = require_admin("/admin/lead-sources/{id}")
    body = json_body()
    expected = _version(body)
    _, items = deps().config.lead_source_items()
    target = next((i for i in items if i.id == item_id), None)
    if target is None:
        raise NotFoundError(messages.LEAD_SOURCE_NOT_FOUND)
    value, active, what = target.value, target.active, []
    if "value" in body:
        value = _clean_value(body["value"], items, skip_id=item_id)
        if value != target.value:
            what.append(f"renamed '{target.value}' to '{value}'")
    if "active" in body:
        if not isinstance(body["active"], bool):
            raise BadRequestError("active must be true or false.")
        active = body["active"]
        if active != target.active:
            what.append(f"{'reactivated' if active else 'deactivated'} '{value}'")
    if not what:
        return _json(200, _lead_sources_view())
    updated = [
        LeadSourceItem(i.id, value, active, i.order) if i.id == item_id else i for i in items
    ]
    return _save_lead_sources(user, expected, items, updated, "; ".join(what))


@app.put("/admin/lead-sources/order")
def reorder_lead_sources() -> Response[Any]:
    user = require_admin("/admin/lead-sources/order")
    body = json_body()
    expected = _version(body)
    _, items = deps().config.lead_source_items()
    ids = body.get("ids")
    by_id = {i.id: i for i in items}
    if not isinstance(ids, list) or sorted(map(str, ids)) != sorted(by_id):
        raise BadRequestError(messages.LEAD_SOURCE_ORDER_MISMATCH)
    return _save_lead_sources(user, expected, items, [by_id[str(i)] for i in ids], "reordered")


# --- field aliases --------------------------------------------------------------------


def _aliases_view() -> dict[str, Any]:
    version, by_field = deps().config.alias_state()
    return {
        "version": version,
        "fields": [
            {"key": f.key, "label": f.label, "aliases": list(by_field.get(f.key, ()))}
            for f in FIELDS
        ],
    }


def _check_aliases(field_key: str, aliases: list[str], by_field: dict[str, list[str]]) -> list[str]:
    taken = {
        normalize_header(a): k for k, values in by_field.items() if k != field_key for a in values
    }
    labels = {normalize_header(f.label): f.label for f in FIELDS}
    labels.update({normalize_header(f.key): f.label for f in FIELDS})
    out: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        if not isinstance(alias, str) or not normalize_header(alias):
            raise BadRequestError(messages.ALIAS_BLANK)
        norm = normalize_header(alias)
        if norm in taken:
            field = BY_KEY[taken[norm]].label
            raise BadRequestError(messages.ALIAS_TAKEN.format(alias=alias.strip(), field=field))
        if norm in labels:
            raise BadRequestError(
                messages.ALIAS_IS_LABEL.format(alias=alias.strip(), field=labels[norm])
            )
        if norm not in seen:
            seen.add(norm)
            out.append(alias.strip())
    return out


def _save_aliases(
    user: User, field_key: str, aliases: list[str], expected: str, change: str
) -> Response[Any]:
    _, by_field = deps().config.alias_state()
    new = {**by_field, field_key: _check_aliases(field_key, aliases, by_field)}
    if new[field_key] == by_field.get(field_key, []):
        return _json(200, _aliases_view())
    saved = _save(
        user,
        ALIASES_PK,
        {"aliases": new},
        expected=expected,
        change=change,
        before={field_key: by_field.get(field_key, [])},
        after={field_key: new[field_key]},
    )
    return _json(200, _aliases_view()) if saved else _conflict()


@app.get("/admin/aliases")
def list_aliases() -> dict[str, Any]:
    require_admin("/admin/aliases")
    return _aliases_view()


@app.put("/admin/aliases/<field_key>")
def replace_aliases(field_key: str) -> Response[Any]:
    user = require_admin("/admin/aliases/{field}")
    if field_key not in BY_KEY:
        raise NotFoundError(messages.ALIAS_FIELD_UNKNOWN.format(field=field_key))
    body = json_body()
    expected = _version(body)
    aliases = body.get("aliases")
    if not isinstance(aliases, list):
        raise BadRequestError("Send the full list of aliases.")
    return _save_aliases(user, field_key, aliases, expected, f"aliases for {field_key}")


def _ai_mappings() -> list[dict[str, Any]]:
    """Columns the AI matched and users kept, not yet covered by an alias."""
    lookup = deps().config.aliases().lookup()
    labels = {normalize_header(f.label) for f in FIELDS} | {normalize_header(f.key) for f in FIELDS}
    found: dict[tuple[str, str], dict[str, Any]] = {}

    def keep(e: dict[str, Any]) -> bool:
        subject = e.get("subject") or {}
        return (
            e.get("event_type") == EventType.SUGGESTION_ACCEPTED
            and subject.get("field") == "column_mapping"
        )

    for e in reader().scan(keep):
        header = str((e.get("subject") or {}).get("source_header") or "")
        key = str((e.get("after") or {}).get("field_key") or "")
        norm = normalize_header(header)
        if not header or key not in BY_KEY or norm in lookup or norm in labels:
            continue
        entry = found.setdefault(
            (norm, key),
            {
                "source_header": header,
                "field_key": key,
                "field_label": BY_KEY[key].label,
                "times": 0,
                "job_ids": [],
                "last_at": "",
            },
        )
        entry["times"] += 1
        if e.get("job_id") and e["job_id"] not in entry["job_ids"]:
            entry["job_ids"].append(e["job_id"])
        entry["last_at"] = max(entry["last_at"], str(e.get("occurred_at", "")))
    return sorted(found.values(), key=lambda m: (-m["times"], m["source_header"]))


@app.get("/admin/ai-mappings")
def list_ai_mappings() -> dict[str, Any]:
    require_admin("/admin/ai-mappings")
    return {"items": _ai_mappings()}


@app.post("/admin/aliases/promote")
def promote_alias() -> Response[Any]:
    """Save an AI column match that users kept as a permanent alias (SPEC §10.2)."""
    user = require_admin("/admin/aliases/promote")
    body = json_body()
    expected = _version(body)
    header, key = body.get("source_header"), body.get("field_key")
    match = next(
        (m for m in _ai_mappings() if m["source_header"] == header and m["field_key"] == key),
        None,
    )
    if match is None:
        raise BadRequestError(messages.PROMOTE_NOT_AI)
    _, by_field = deps().config.alias_state()
    return _save_aliases(
        user,
        str(key),
        [*by_field.get(str(key), []), str(header)],
        expected,
        f"promoted AI match '{header}' -> {key}",
    )


# --- audit search ---------------------------------------------------------------------


def _criteria(params: dict[str, Any]) -> SearchCriteria:
    def get(name: str) -> str | None:
        value = params.get(name)
        return str(value).strip() or None if value is not None else None

    criteria = SearchCriteria(
        email=get("email"),
        job_id=get("job_id"),
        user=get("user"),
        campaign_id=get("campaign_id"),
        event_type=get("event_type"),
        date_from=get("from"),
        date_to=get("to"),
    )
    for d in (criteria.date_from, criteria.date_to):
        if d and not _DATE.match(d):
            raise BadRequestError(messages.AUDIT_DATE_INVALID)
    if criteria.is_empty():
        raise BadRequestError(messages.AUDIT_SEARCH_EMPTY)
    return criteria


def _jobs_with_campaign(campaign_id: str) -> list[str]:
    return [
        j["job_id"]
        for j in deps().jobs.list_all(limit=100_000)
        if campaign_id in ((j.get("analysis_context") or {}).get("campaigns") or {})
    ]


def _search(criteria: SearchCriteria, limit: int) -> tuple[list[dict[str, Any]], bool]:
    return reader().search(criteria, jobs_with_campaign=_jobs_with_campaign, limit=limit)


@app.get("/admin/audit")
def audit_search() -> dict[str, Any]:
    require_admin("/admin/audit")
    criteria = _criteria(app.current_event.query_string_parameters or {})
    events, truncated = _search(criteria, SEARCH_LIMIT)
    jobs: list[dict[str, Any]] = []
    if criteria.email:
        # "Every upload that included this person": all their jobs, not just this page.
        all_events, _ = _search(SearchCriteria(email=criteria.email), EXPORT_LIMIT)
        repo = deps().jobs
        for job_id in dict.fromkeys(e["job_id"] for e in all_events if e.get("job_id")):
            job = repo.get(job_id) or {}
            jobs.append(
                {
                    "job_id": job_id,
                    "filename": job.get("filename"),
                    "owner_email": job.get("owner_email"),
                    "state": job.get("state"),
                    "created_at": job.get("created_at"),
                }
            )
    metrics.add_metric(name="AuditSearches", unit="Count", value=1)
    return {"events": [event_view(e) for e in events], "truncated": truncated, "jobs": jobs}


EXPORT_COLUMNS = (
    "occurred_at",
    "event_type",
    "job_id",
    "row_id",
    "actor_type",
    "actor_email",
    "summary",
    "reason",
    "subject",
    "before",
    "after",
    "details",
)


def _export_csv(events: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(EXPORT_COLUMNS)
    for e in events:
        v = event_view(e)
        row = {
            **v,
            "actor_type": v["actor"]["type"],
            "actor_email": v["actor"]["email"],
            **{
                k: json.dumps(v[k], sort_keys=True) if v[k] is not None else ""
                for k in ("subject", "before", "after", "details")
            },
        }
        writer.writerow([safe_cell(row.get(c)) for c in EXPORT_COLUMNS])
    return buf.getvalue().encode("utf-8-sig")


@app.post("/admin/audit/export")
def audit_export() -> Any:
    user = require_admin("/admin/audit/export")
    criteria = _criteria(json_body())
    events, truncated = _search(criteria, EXPORT_LIMIT)
    d = deps()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    key = f"audit-exports/{new_ulid()}.csv"
    filters = {k: v for k, v in vars(criteria).items() if v}
    if "email" in filters:  # the export's own record holds no plaintext email
        filters["email_sha256"] = email_sha256(filters.pop("email"))
    # A PII export: record it before handing out the link (SPEC §21.2.6).
    d.audit.write(
        AuditEvent(
            event_type=EventType.AUDIT_EXPORTED,
            actor=user.actor(),
            details={
                "filters": filters,
                "events": len(events),
                "truncated": truncated,
                "s3_key": key,
            },
            correlation_id=correlation_id(),
        )
    )
    d.s3.put_object(
        Bucket=d.processed_bucket,
        Key=key,
        Body=_export_csv(events),
        ContentType="text/csv; charset=utf-8",
        ContentDisposition=f'attachment; filename="audit-{stamp}.csv"',
    )
    url = d.s3.generate_presigned_url(
        "get_object", Params={"Bucket": d.processed_bucket, "Key": key}, ExpiresIn=EXPORT_URL_TTL
    )
    metrics.add_metric(name="AuditExports", unit="Count", value=1)
    return {
        "url": url,
        "filename": f"audit-{stamp}.csv",
        "events": len(events),
        "truncated": truncated,
        "expires_in": EXPORT_URL_TTL,
    }
