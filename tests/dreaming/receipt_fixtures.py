"""Complete historical receipt fixtures; no consent or outcome authority."""
from copy import deepcopy
from uuid import UUID

from delivery_fixtures import RID, case, digest
from test_dream_procedural import stamp


def receipts():
    packet, current, permission, _ = case()
    permission.update(receipts="allow", receipts_ref="user:3")
    delivery = dict(schema_version=1, kind="procedural_receipt",
                    receipt_id="delivery:" + packet["request_id"], record_type="delivery",
                    recorded_at=stamp(), authority="agent_reported",
                    context_digest=packet["context_digest"], receipt_permission_ref="user:3",
                    payload=dict(packet=packet, acknowledgment="read_full_packet"))
    delivery["digest"] = digest(delivery)
    application = dict(schema_version=1, kind="procedural_receipt",
                       receipt_id="application:" + str(UUID(int=905)), record_type="application",
                       recorded_at=stamp(), authority="agent_reported",
                       context_digest=packet["context_digest"], receipt_permission_ref="user:3",
                       payload=dict(delivery_receipt_id=delivery["receipt_id"], rule_id=RID,
                                    disposition="applied", action="Added parser regression",
                                    action_reference="commit:example"))
    application["digest"] = digest(application)
    return delivery, application, permission


def resign(record):
    result = deepcopy(record)
    result["digest"] = digest({k: v for k, v in result.items() if k != "digest"})
    return result
