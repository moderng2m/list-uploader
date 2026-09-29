"""Synthetic Salesforce campaigns for the fake Workato client (INTEGRATIONS=fake).

Not real campaigns. IDs are valid 18-character Salesforce IDs with the 701
campaign prefix, so they exercise the checksum rules.
"""

from __future__ import annotations

from shared.workato_client import Campaign, MemberStatus

_EVENT_STATUSES = (
    MemberStatus("Registered", is_default=True, has_responded=False, id="01Y000000000001AAA"),
    MemberStatus("Attended", is_default=False, has_responded=True, id="01Y000000000002AAA"),
    MemberStatus("No Show", is_default=False, has_responded=False, id="01Y000000000003AAA"),
)
_WEBINAR_STATUSES = (
    MemberStatus("Registered", is_default=True, has_responded=False, id="01Y000000000004AAA"),
    MemberStatus("Attended", is_default=False, has_responded=True, id="01Y000000000005AAA"),
    MemberStatus(
        "Watched On Demand", is_default=False, has_responded=True, id="01Y000000000006AAA"
    ),
)

CAMPAIGNS: dict[str, Campaign] = {
    c.id: c
    for c in (
        Campaign(
            id="701000000000001AAA",
            found=True,
            name="Demo Conference 2026",
            type="Marketing: Events",
            is_active=True,
            status="In Progress",
            member_statuses=_EVENT_STATUSES,
        ),
        Campaign(
            id="701000000000002AAA",
            found=True,
            name="Demo Webinar Series",
            type="Marketing: Webinar",
            is_active=True,
            status="In Progress",
            member_statuses=_WEBINAR_STATUSES,
        ),
        Campaign(
            id="701000000000003AAA",
            found=True,
            name="Demo Roadshow 2025",
            type="Marketing: Events",
            is_active=False,
            status="Completed",
            member_statuses=_EVENT_STATUSES,
        ),
    )
}
