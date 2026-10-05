"""Complete delivery envelopes and cooperative witnesses for boundary tests."""
from copy import deepcopy
from uuid import UUID

from dream_metadata import canonical_json, content_hash
from test_dream_procedural import NOW, stamp

RID = "proc:" + "a" * 64


def digest(value):
    return content_hash(canonical_json(value))


def case(task="Fix the reproducible parser defect"):
    context = dict(repository="owner/repo", wing="w", session_id=str(UUID(int=901)),
                   actor_id=str(UUID(int=902)), task_id=str(UUID(int=903)), revision=0,
                   task=task, constraints=["Preserve spans"], mode="established")
    item = dict(rule_id=RID, rule_type="rule", statement="Add a parser regression",
                applies_when="Fixing a reproducible parser defect",
                exceptions=["No local reproduction available"], maturity="established",
                effective_score=4.0, relevance=1.0, latest_validation=stamp(),
                evidence=[dict(source_kind="drawer", source_id="source-1")], delivery="guidance")
    guidance = dict(policy_version="procedural-v1", as_of=stamp(), repository="owner/repo",
                    status="ok", item_count=1, omitted_count=0,
                    rules=[item], anti_patterns=[], trials=[])
    packet = dict(schema_version=1, kind="procedural_delivery_packet", status="bound_guidance",
                  request_id=str(UUID(int=904)), context=context, context_digest=digest(context),
                  guidance=guidance, guidance_digest=digest(guidance))
    current = dict(context=deepcopy(context), checked_at=stamp())
    permission = dict(repository="owner/repo", wing="w", session_id=context["session_id"],
                      actor_id=context["actor_id"], checked_at=stamp(), advice="allow",
                      receipts="deny", trials="deny", advice_ref="user:2", receipts_ref=None)
    return packet, current, permission, guidance


def applicability(context, items, *, checked_at=NOW):
    def check(text, verdict):
        return dict(text=text, verdict=verdict,
                    reason="Checked against the independently observed current assignment.")
    return dict(schema_version=1, kind="procedural_applicability_witness", authority="agent_reported",
                context_digest=digest(context), checked_at=stamp(checked_at),
                assessments=[dict(
                    rule_id=item["rule_id"],
                    rule_digest=digest({k: item[k] for k in (
                        "rule_id", "rule_type", "statement", "applies_when", "exceptions", "evidence")}),
                    condition=check(item["applies_when"], "satisfied"),
                    exceptions=[check(text, "absent") for text in item["exceptions"]],
                    constraints=[check(text, "compatible") for text in context["constraints"]],
                    supported_context=dict(verdict="compatible",
                        reason="The reviewed local originals cover this parser and test workflow."),
                ) for item in items])


def wrapped_packet(wrapper="{}", encoded=False):
    from dream_procedural_delivery import build_packet
    packet, current, permission, guidance = case()
    packet = build_packet(current, permission, packet["request_id"],
                          lambda context, now: guidance, NOW)
    text = canonical_json(packet)
    return wrapper.format(canonical_json(text) if encoded else text)
