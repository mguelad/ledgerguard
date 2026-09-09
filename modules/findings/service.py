import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.control_plane.errors import Problem
from apps.control_plane.telemetry import metric
from modules.accounts.tenancy import audit
from modules.connectors.models import Installation, ResourceLocator, Store, StoreStripeLink
from modules.findings.models import Finding, FindingEvent, ReconciliationRun
from modules.ingestion.models import Cursor, Projection
from modules.ingestion.service import enqueue
from packages.reconciliation_core import Coverage, Money, Order, Payment, Policy, Refund, Snapshot, evaluate
from packages.reconciliation_core.domain import MATCH_VERSION, RULE_VERSION, Mode
from packages.reconciliation_core.engine import canonical


def money(row: Projection, field: str = "amount_minor") -> Money:
    return Money(getattr(row, field), row.currency, row.exponent)


def build_snapshot(store: Store, now: datetime) -> Snapshot:
    link = StoreStripeLink.objects.filter(store=store).select_related("stripe").first()
    woo = Installation.objects.filter(store=store, kind="woo", status="active").first()
    stripe = link.stripe if link else None
    coverage = []
    for connector in filter(None, [woo, stripe]):
        for c in Cursor.objects.filter(connector=connector):
            if c.covered_from and c.covered_through and c.observed_at:
                coverage.append(
                    Coverage(
                        c.object_class,
                        c.covered_from,
                        c.covered_through,
                        c.observed_at,
                        c.complete and connector.status == "active",
                        int(connector.health.get("clock_offset_seconds", 0)),
                    )
                )
    stripe_rows = list(Projection.objects.filter(connector=stripe).select_related("observation")) if stripe else []
    woo_rows = list(Projection.objects.filter(connector=woo).select_related("observation")) if woo else []
    mode = cast(Mode, store.mode)
    charges: dict[str, set[str]] = {}
    sessions: dict[str, set[str]] = {}
    for row in stripe_rows:
        if row.kind == "charge":
            charges.setdefault(row.parent_id, set()).add(row.source_id)
        if row.kind == "session":
            sessions.setdefault(row.parent_id, set()).add(row.source_id)
    payments = []
    for p in stripe_rows:
        if p.kind != "payment":
            continue
        payments.append(
            Payment(
                p.source_id,
                stripe.account_id if stripe else "",
                mode,
                p.status,
                money(p),
                money(p, "received_minor"),
                p.created_at_source,
                p.revision,
                p.succeeded_at,
                p.method,
                p.capture_method,
                tuple(sorted(charges.get(p.source_id, set()) | ({p.charge_id} if p.charge_id else set()))),
                tuple(sorted(sessions.get(p.source_id, set()))),
                p.store_hint,
                p.order_hint,
                str(p.observation_id),
            )
        )
    orders = tuple(
        Order(
            o.source_id,
            str(store.id),
            mode,
            o.status,
            money(o),
            money(o, "refunded_minor"),
            o.created_at_source,
            o.revision,
            o.paid_at,
            o.method,
            o.transaction_id,
            o.pi_id,
            o.charge_id,
            o.session_id,
            str(o.observation_id),
        )
        for o in woo_rows
        if o.kind == "order"
    )
    refund_inventory = {
        o.source_id: set(o.observation.data.get("refund_ids", [])) for o in woo_rows if o.kind == "order"
    }
    refunds = tuple(
        Refund(
            r.source_id,
            "stripe" if r.kind == "stripe_refund" else "woo",
            r.parent_id,
            money(r),
            r.status,
            r.revision,
            str(r.observation_id),
        )
        for r in (*stripe_rows, *woo_rows)
        if r.kind == "stripe_refund"
        or (r.kind == "woo_refund" and r.source_id in refund_inventory.get(r.parent_id, set()))
    )
    ambiguous = []
    if stripe:
        linked_stores = list(StoreStripeLink.objects.filter(stripe=stripe).values_list("store_id", flat=True))
        from django.db.models import Count

        ambiguous = list(
            Projection.objects.filter(store_id__in=linked_stores, kind="order", mode=store.mode)
            .values("source_id")
            .annotate(stores=Count("store_id", distinct=True))
            .filter(stores__gt=1)
            .values_list("source_id", flat=True)
        )
    policy = Policy(
        store.policy_version,
        timedelta(minutes=store.instant_grace_minutes),
        timedelta(minutes=store.async_grace_minutes),
        timedelta(minutes=store.refund_grace_minutes),
        timedelta(minutes=store.freshness_minutes),
        tuple(store.disabled_rules),
        store.shadow_rules,
    )
    return Snapshot(
        str(store.organization_id),
        str(store.id),
        stripe.account_id if stripe else "",
        mode,
        now,
        now - timedelta(days=30),
        tuple(payments),
        orders,
        refunds,
        tuple(coverage),
        bool(link and link.status == "verified"),
        bool(link and link.status == "conflict"),
        tuple(ambiguous),
        policy,
    )


def _event(
    finding: Finding, event_type: str, previous: str, actor: str, run: ReconciliationRun | None = None, note: str = ""
) -> FindingEvent:
    event = FindingEvent.objects.create(
        organization_id=finding.organization_id,
        finding=finding,
        event_type=event_type,
        actor_id=actor,
        previous_state=previous,
        new_state=finding.state,
        evidence=finding.evidence,
        run=run,
        note=note,
    )
    if event_type in {"opened", "reopened", "unsuppressed"}:
        enqueue(finding.organization_id, "notify", event.id, f"notify:{event.id}")
    return event


def reconcile(store_id: UUID, now: datetime | None = None) -> ReconciliationRun:
    now = now or timezone.now()
    with transaction.atomic():
        store = Store.objects.select_for_update().get(id=store_id, active=True)
        snapshot = build_snapshot(store, now)
        result = evaluate(snapshot)
        for coverage in snapshot.coverage:
            metric("CoverageAgeSeconds", max(0, (now - coverage.covered_through).total_seconds()))
        run = ReconciliationRun.objects.create(
            organization_id=store.organization_id,
            store=store,
            mode=store.mode,
            rule_version=RULE_VERSION,
            match_version=MATCH_VERSION,
            policy_version=store.policy_version,
            input_digest=result.input_digest,
            evaluated_at=now,
            coverage=json.loads(canonical([asdict(c) for c in snapshot.coverage])),
            warnings=list(result.warnings),
            proposals=json.loads(canonical([asdict(p) for p in result.proposals])),
            shadow=store.shadow_rules,
        )
        if store.shadow_rules:
            return run
        present = set()
        for proposal in result.proposals:
            present.add(proposal.key)
            evidence = json.loads(proposal.evidence_json)
            substantive = {k: v for k, v in evidence.items() if k != "coverage"}
            evidence_hash = hashlib.sha256(canonical(substantive).encode()).hexdigest()
            finding = Finding.objects.select_for_update().filter(finding_key=proposal.key).first()
            created = finding is None
            if finding is None:
                finding = Finding.objects.create(
                    organization_id=store.organization_id,
                    store=store,
                    mode=store.mode,
                    finding_key=proposal.key,
                    rule_code=proposal.rule,
                    rule_version=proposal.rule_version,
                    policy_version=store.policy_version,
                    severity=proposal.severity,
                    identities=list(proposal.identities),
                    risk_amount_minor=proposal.risk.minor if proposal.risk else None,
                    currency=proposal.risk.currency if proposal.risk else "",
                    exponent=proposal.risk.exponent if proposal.risk else 2,
                    match_confidence=proposal.confidence,
                    first_seen=now,
                    last_seen=now,
                    eligible_at=proposal.eligible_at,
                    evidence=evidence,
                    evidence_hash=evidence_hash,
                )
                ResourceLocator.objects.create(id=finding.id, organization_id=store.organization_id, kind="finding")
            previous = finding.state
            changed = finding.evidence_hash != evidence_hash
            finding.evidence, finding.evidence_hash = evidence, evidence_hash
            finding.risk_amount_minor = proposal.risk.minor if proposal.risk else None
            finding.currency = proposal.risk.currency if proposal.risk else ""
            finding.exponent = proposal.risk.exponent if proposal.risk else 2
            finding.match_confidence = proposal.confidence
            finding.last_seen, finding.eligible_at, finding.clean_since = now, proposal.eligible_at, None
            finding.clean_coverage_digest = ""
            event_type = ""
            if proposal.ready:
                if previous == "provisional":
                    finding.state = "open"
                    finding.occurrence_count += 1
                    event_type = "opened"
                elif previous == "resolved":
                    finding.state = "reopened"
                    finding.occurrence_count += 1
                    finding.resolved_at = None
                    event_type = "reopened"
                elif previous == "suppressed" and finding.suppressed_until and finding.suppressed_until <= now:
                    finding.state = "open"
                    finding.suppressed_until = None
                    event_type = "unsuppressed"
            if not event_type and (created or changed):
                event_type = "observed"
            finding.updated_at = now
            finding.lock_version += 1
            finding.save()
            if event_type in {"opened", "reopened"}:
                metric("DetectionDelaySeconds", max(0, (now - finding.eligible_at).total_seconds()))
            if event_type:
                _event(finding, event_type, previous, "system", run)
        coverage_digest = hashlib.sha256(
            canonical(
                [(c.source, c.covered_through) for c in sorted(snapshot.coverage, key=lambda c: c.source)]
            ).encode()
        ).hexdigest()
        for finding in Finding.objects.select_for_update().filter(store=store).exclude(state="resolved"):
            if finding.finding_key in present:
                continue
            if finding.rule_code not in result.evaluated_rules or set(finding.identities) & set(
                result.blocked_identities
            ):
                if finding.clean_since is not None:
                    finding.clean_since = None
                    finding.clean_coverage_digest = ""
                    finding.lock_version += 1
                    finding.updated_at = now
                    finding.save()
                continue
            if finding.clean_since is None:
                finding.clean_since = now
                finding.clean_coverage_digest = coverage_digest
            elif now - finding.clean_since >= timedelta(minutes=5) and (
                finding.rule_code != "PI-009" or finding.clean_coverage_digest != coverage_digest
            ):
                previous = finding.state
                finding.state = "resolved"
                finding.resolved_at = now
                _event(finding, "resolved", previous, "system", run)
            finding.updated_at = now
            finding.lock_version += 1
            finding.save()
        return run


def action(
    finding_id: UUID, actor: str, action_name: str, version: int, note: str = "", until: datetime | None = None
) -> Finding:
    with transaction.atomic():
        finding = Finding.objects.select_for_update().get(pk=finding_id)
        if finding.lock_version != version:
            raise Problem("FINDING_VERSION_CONFLICT", 409, "Refresh the finding and retry the action.")
        if action_name not in {"acknowledge", "suppress", "resolve", "note"}:
            raise Problem("INVALID_ACTION", 400)
        previous = finding.state
        if action_name == "acknowledge":
            if previous not in {"open", "reopened", "acknowledged"}:
                raise Problem("INVALID_TRANSITION", 409)
            finding.state = "acknowledged"
        if action_name == "suppress":
            if previous not in {"open", "reopened", "acknowledged", "suppressed"}:
                raise Problem("INVALID_TRANSITION", 409)
            if not note.strip() or not until or not timezone.now() < until <= timezone.now() + timedelta(days=7):
                raise Problem("SUPPRESSION_REASON_AND_EXPIRY_REQUIRED", 422)
            finding.state = "suppressed"
            finding.suppressed_until = until
        if action_name in {"resolve", "note"} and not note.strip():
            raise Problem("NOTE_REQUIRED", 422)
        # External resolution records operator intent. The engine still requires two clean runs.
        if action_name == "resolve":
            finding.clean_since = None
        finding.lock_version += 1
        finding.updated_at = timezone.now()
        finding.save()
        _event(
            finding,
            "external_resolution_requested" if action_name == "resolve" else action_name,
            previous,
            actor,
            note=note[:1000],
        )
        audit(finding.organization_id, actor, "finding." + action_name, finding.id)
        return finding
