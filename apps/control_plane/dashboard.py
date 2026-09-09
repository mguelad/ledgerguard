from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils.dateparse import parse_datetime

from modules.accounts.models import AuditLog, Membership, Organization, SupportGrant
from modules.accounts.tenancy import require_role, tenant_view
from modules.connectors.models import Installation, Store, StoreStripeLink
from modules.findings.models import AlertRoute, EmailDelivery, Finding, FindingEvent, ReconciliationRun
from modules.ingestion.models import Cursor, Scan
from modules.reporting.exports import PLAYBOOKS
from modules.reporting.models import Report
from packages.reconciliation_core.money import Money


@tenant_view
def overview(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    stores = list(Store.objects.filter(active=True).annotate(finding_count=Count("finding", distinct=True)))
    findings = (
        Finding.objects.filter(state__in=["open", "reopened", "acknowledged"])
        .select_related("store")
        .order_by("severity", "first_seen")[:25]
    )
    return render(
        request,
        "overview.html",
        {
            "organization": Organization.objects.get(id=organization_id),
            "stores": stores,
            "findings": findings,
            "counts": {
                "open": Finding.objects.filter(state__in=["open", "reopened"]).count(),
                "acknowledged": Finding.objects.filter(state="acknowledged").count(),
                "suppressed": Finding.objects.filter(state="suppressed").count(),
            },
            "members": Membership.objects.select_related("user"),
            "routes": AlertRoute.objects.all(),
            "reports": Report.objects.order_by("-created_at")[:10],
        },
    )


@tenant_view
def store_detail(request: HttpRequest, store_id: UUID) -> HttpResponse:
    store = get_object_or_404(Store, id=store_id)
    connectors = list(Installation.objects.filter(store=store, kind="woo"))
    link = StoreStripeLink.objects.filter(store=store).select_related("stripe").first()
    if link:
        connectors.append(link.stripe)
    query = Finding.objects.filter(store=store).exclude(state="provisional").order_by("-last_seen", "id")
    state = request.GET.get("state", "")
    if state in {"open", "reopened", "acknowledged", "suppressed", "resolved"}:
        query = query.filter(state=state)
    page = Paginator(query, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "store.html",
        {
            "store": store,
            "public_url": settings.PUBLIC_URL,
            "findings_page": page,
            "selected_state": state,
            "rules": tuple(f"PI-{n:03}" for n in range(1, 11)),
            "link": link,
            "connectors": connectors,
            "coverage": Cursor.objects.filter(connector__in=connectors),
            "scans": Scan.objects.filter(connector__in=connectors, status="pending"),
            "findings": page.object_list,
            "runs": ReconciliationRun.objects.filter(store=store).order_by("-created_at")[:5],
        },
    )


def comparison_rows(evidence: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    sources = [("Woo order", evidence["order"])] if evidence.get("order") else []
    sources += [("Stripe payment", p) for p in evidence.get("payments", [])]
    if evidence.get("payment"):
        sources.append(("Stripe payment", evidence["payment"]))
    sources += [("Stripe refund", r) for r in evidence.get("stripe_refunds", [])]
    sources += [("Woo refund", r) for r in evidence.get("woo_refunds", [])]
    for source, fact in sources:
        amount = fact.get("received") if source == "Stripe payment" else fact.get("amount")
        rows.append(
            {
                "source": source,
                "identity": fact["id"],
                "status": fact["status"].replace("_", " "),
                "amount": f"{amount['currency']} {Money(**amount).decimal()}" if amount else "—",
                "changed_at": fact.get("changed_at", ""),
            }
        )
    return rows


@tenant_view
def finding_detail(request: HttpRequest, finding_id: UUID) -> HttpResponse:
    finding = get_object_or_404(Finding.objects.select_related("store"), id=finding_id)
    title, playbook = PLAYBOOKS[finding.rule_code]
    return render(
        request,
        "finding.html",
        {
            "finding": finding,
            "title": title,
            "playbook": playbook,
            "comparison": comparison_rows(finding.evidence),
            "evidence_coverage": [
                {
                    **c,
                    "label": c["source"].replace("_", " ").title(),
                    "through": parse_datetime(c["covered_through"]),
                    "observed": parse_datetime(c["observed_at"]),
                }
                for c in finding.evidence.get("coverage", [])
            ],
            "events": FindingEvent.objects.filter(finding=finding).order_by("-created_at")[:100],
            "deliveries": EmailDelivery.objects.filter(event__finding=finding),
        },
    )


@tenant_view
def report_detail(request: HttpRequest, report_id: UUID) -> HttpResponse:
    report = get_object_or_404(Report, id=report_id)
    return render(request, "report.html", {"report": report})


@tenant_view
def organization_settings(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    require_role(request, "owner", "admin")
    return render(
        request,
        "settings.html",
        {
            "organization": Organization.objects.get(id=organization_id),
            "members": Membership.objects.select_related("user"),
            "stores": Store.objects.filter(active=True),
            "alert_emails": set(AlertRoute.objects.filter(active=True).values_list("email", flat=True)),
            "grants": SupportGrant.objects.select_related("support_user").order_by("-created_at")[:20],
            "audit_events": AuditLog.objects.order_by("-created_at")[:50],
        },
    )
