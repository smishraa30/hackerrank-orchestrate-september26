"""Per-request orchestration: ledger -> forecast -> candidates -> decision."""
from __future__ import annotations

from typing import Optional

from .config import EngineConfig
from .explain import explain
from .images import ImageExtractor, UsageMeter
from .ingest import Dataset
from .ledger import Ledger, LedgerBuilder
from .models import Decision, Request
from .planner import Analysis, analyse, status_for
from .render import render_plan


class Engine:
    def __init__(self, ds: Dataset, cfg: Optional[EngineConfig] = None, extractor: Optional[ImageExtractor] = None):
        self.ds = ds
        self.cfg = cfg or EngineConfig()
        self.meter = UsageMeter()
        self.extractor = extractor or ImageExtractor(meter=self.meter)
        self.builder = LedgerBuilder(ds, self.extractor, self.cfg)

    def decide(self, req: Request, with_trace: bool = False) -> tuple[Decision, Ledger, Analysis]:
        ledger = self.builder.build(req)
        options = self.ds.options_by_request.get(req.request_id, [])
        an = analyse(req, ledger, options, self.cfg)
        status, method = status_for(an.chosen)
        plan = an.chosen
        payment_plan = render_plan(plan.payments) if plan is not None else "none"
        changes = "|".join(c.render() for c in sorted(plan.changes, key=lambda c: _event_num(c.event_id))) if plan and plan.changes else "none"
        earliest = an.earliest
        if status == "affordable_now":
            earliest = req.request_date
        dec = Decision(
            request_id=req.request_id,
            amount_safe_to_pay=an.amount_safe,
            affordability_status=status,
            recommended_payment_method=method,
            payment_plan=payment_plan,
            earliest_date_for_full_payment=earliest,
            spending_changes_needed=changes,
            decision_explanation=explain(req, ledger, an, self.cfg),
        )
        if with_trace:
            dec.trace = trace_of(req, ledger, an)
        return dec, ledger, an


def _event_num(event_id: str) -> int:
    try:
        return int(event_id.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return 0


def trace_of(req: Request, ledger: Ledger, an: Analysis) -> dict:
    return {
        "request": {"id": req.request_id, "user": req.user_id, "date": str(req.request_date), "requested": str(req.requested_amount),
                    "deadline": str(req.desired_completion_date), "allows_partial": req.allows_partial_payment},
        "profile": {"balance": str(ledger.balance), "minimum": str(ledger.minimum), "methods": list(ledger.profile.methods),
                    "max_installment_months": ledger.profile.max_installment_months},
        "baseline": {"amount_safe": str(an.amount_safe), "earliest": str(an.earliest) if an.earliest else None,
                     "low": str(an.baseline_low), "low_date": str(an.baseline_low_date)},
        "series": [
            {"key": s.key, "cadence": s.cadence, "dom": s.day_of_month, "interval": s.interval_days, "amount": str(s.amount),
             "currency": s.currency, "active": s.active, "last": str(s.last_date), "last_event": s.last_event_id,
             "flex": s.flexibility, "min_allowed": str(s.minimum_allowed_amount) if s.minimum_allowed_amount is not None else None,
             "amount_from": {str(k): str(v) for k, v in s.amount_from.items()}, "date_override": str(s.date_override) if s.date_override else None,
             "notes": s.notes}
            for s in ledger.series
        ],
        "one_offs": [{"day": str(c.day), "amount": str(c.amount), "kind": c.kind, "label": c.label, "event": c.event_id} for c in ledger.one_offs],
        "notes": ledger.notes,
        "provenance": ledger.provenance,
        "candidates": [
            {"method": p.method, "payments": [(str(d), str(a)) for d, a in p.payments], "changes": [c.render() for c in p.changes],
             "option": p.option_id, "by_deadline": p.completes_by_deadline, "total": str(p.total_paid)}
            for p in an.candidates
        ],
        "rejected": an.rejected,
    }
