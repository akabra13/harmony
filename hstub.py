"""Shared stub answers for quick manual checks."""
from harmony.llm.client import StubClient

def extraction(req):
    p = req.prompt
    if 'PO-77812' in p and 'dock Tuesday' in p:
        return dict(revises_delivery=True, po_id='PO-77812', revised_arrival_date='2026-09-08',
                    confidence=0.95, verbatim_quote='Revised ship date is Monday 9/7, which puts it on your dock Tuesday 9/8.')
    if 'PO-77820' in p and '11th rather than the 5th' in p:
        return dict(revises_delivery=True, po_id='PO-77820', revised_arrival_date='2026-09-11',
                    confidence=0.9, verbatim_quote='We are now looking at the 11th rather than the 5th.')
    return dict(revises_delivery=False, po_id=None, revised_arrival_date=None, confidence=0.0, verbatim_quote=None)

def planner(req):
    p = req.prompt
    if '4812' in p and 'P-4471' in p:
        return dict(summary="Part P-4471 will likely cause production order 4812 to miss its scheduled start; Kestrel says PO-77812 now arrives 2026-09-08. I can reroute to an approved alternate supplier and notify production.",
            reasoning="Stock covers 5 days and 4812 starts in 5 days. PO-77812 was promised 09-04 but Kestrel's email M-001 revises arrival to 09-08, one day after the start.",
            action_kind='workflow', workflow_name='po_reroute',
            workflow_params=dict(at_risk_po_id='PO-77812', part_id='P-4471', production_order_id='4812',
                                 required_on_site_by='2026-09-07', qty=400, supervisor_id='u-301'),
            tool_calls=None, no_action_reason=None,
            evidence=[dict(source='mail', ref='M-001', detail='revised arrival 9/8'),
                      dict(source='erp', ref='PO-77812', detail='open, promised 09-04')],
            alternatives_considered=['Expedite with Kestrel', 'Reschedule 4812'])
    if '4816' in p and 'P-5540' in p:
        return dict(summary="Servo drives for production order 4816 will not arrive in time; PO-77820 has slipped to 2026-09-11.",
            reasoning="4816 starts 09-08 needing 30 of P-5540; only 12 on hand and PO-77820 now lands 09-11.",
            action_kind='workflow', workflow_name='po_reroute',
            workflow_params=dict(at_risk_po_id='PO-77820', part_id='P-5540', production_order_id='4816',
                                 required_on_site_by='2026-09-08', qty=40, supervisor_id='u-301'),
            tool_calls=None, no_action_reason=None, evidence=[], alternatives_considered=[])
    if 'L-2093' in p:
        return dict(summary="Lot L-2093 is on hold and allocated to production order 4820, which starts in 3 days; lot L-2101 can cover it.",
            reasoning="L-2101 is released, unallocated and holds 140 units against the 90 that 4820 needs.",
            action_kind='tools', workflow_name=None, workflow_params=None,
            tool_calls=[
                dict(tool='quality.reallocate_lot', params=dict(production_order_id='4820', from_lot_id='L-2093', to_lot_id='L-2101', reason='L-2093 on quality hold'), rationale='Move 4820 onto good stock'),
                dict(tool='production.notify_supervisor', params=dict(supervisor_id='u-301', subject='Lot change for production order 4820', body='Production order 4820 has been reallocated from lot L-2093 (on quality hold, surface finish) to lot L-2101. No schedule change.', about='4820'), rationale='Tell the supervisor'),
            ],
            no_action_reason=None, evidence=[dict(source='quality', ref='L-2093', detail='on hold')],
            alternatives_considered=['Flag a shortage to purchasing'])
    return dict(summary='No action', reasoning='Nothing warranted', action_kind='none',
                workflow_name=None, workflow_params=None, tool_calls=None,
                no_action_reason='No material risk identified', evidence=[], alternatives_considered=[])

def choose_supplier(req):
    p = req.prompt
    if 'P-5540' in p:
        return dict(supplier_id='S-T', justification='Voss is the only qualified supplier able to deliver before the 8th. Their 91% on-time record supports the date.')
    return dict(supplier_id='S-Z', justification='Meridian delivers in two days against a five-day need and has a 94% on-time record. Halstead is cheaper but its nine-day lead time misses the production start entirely.')

def draft_notification(req):
    import re
    po = re.search(r'Replacement purchase order: (\S+)', req.prompt)
    prod = re.search(r'Production order affected: (\S+)', req.prompt)
    sup = re.search(r'New supplier: (\S+)', req.prompt)
    exp = re.search(r'Expected on site: (\S+)', req.prompt)
    return dict(subject=f"Supply change for production order {prod.group(1)}",
                body=(f"Production order {prod.group(1)} is affected by a supplier delay. "
                      f"The original order has been cancelled and replaced by {po.group(1)} "
                      f"with supplier {sup.group(1)}, expected on site {exp.group(1)}. "
                      f"No change to the scheduled start."))

def build_stub():
    return StubClient({
        'mail.extract_commitment': extraction,
        'planner': planner,
        'workflow.po_reroute.choose_supplier': choose_supplier,
        'workflow.po_reroute.draft_notification': draft_notification,
    })
