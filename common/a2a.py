"""A2A-lite: agent discovery cards and the message envelope used across the ecosystem.

This follows the *concept* of the Agent2Agent protocol (agent cards for discovery,
task-oriented messages) plus FIPA-ACL performatives for negotiation. It is a simulation
contract, not a complete implementation of the A2A specification.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from common import multilingual as ml
from common.config import ECOSYSTEM_NAME

A2A_LITE_VERSION = "a2a-lite/0.1"

# FIPA-ACL inspired performatives (Wooldridge 2009, ch. 7)
PERFORMATIVES = {
    "REQUEST": "ask another agent to perform an action",
    "QUERY": "ask for information",
    "INFORM": "provide information / result",
    "CFP": "call for proposals (Contract Net)",
    "PROPOSE": "proposal in reply to a CFP or a counter-offer",
    "REFUSE": "decline to propose / perform",
    "ACCEPT_PROPOSAL": "accept a proposal",
    "REJECT_PROPOSAL": "reject a proposal",
    "COUNTER_PROPOSE": "counter-offer in bilateral bargaining",
    "AGREE": "agree to perform a requested action",
    "CONFIRM": "confirm execution / acknowledgement",
    "FAILURE": "action attempted but failed",
}


class A2AMessage(BaseModel):
    message_id: str
    conversation_id: str
    seq: int
    timestamp: str
    sender: str
    receiver: str
    via: str | None = None
    message_type: str
    protocol: str = A2A_LITE_VERSION
    interaction: str = "request-reply"
    task: str
    payload: dict = Field(default_factory=dict)
    native: dict | None = None
    reply_required: bool = False
    in_reply_to: str | None = None
    status: str = "DELIVERED"


def build_agent_card(profile: dict, base_url: str, skills: list[dict], transport: str) -> dict:
    """A2A-style Agent Card (subset of fields) + an x-enterprise extension for E_i metadata."""
    return {
        "protocolVersion": "0.3.0",
        "name": f"{profile['name']} agent gateway",
        "description": profile["description"],
        "url": base_url,
        "preferredTransport": transport,
        "version": "0.1.0-mvp",
        "provider": {"organization": f"{profile['name']} (fictional enterprise)"},
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": [profile["content_type"], "text/plain"],
        "defaultOutputModes": [profile["content_type"], "text/plain"],
        "additionalInterfaces": [{"url": base_url + "/a2a", "transport": "JSONRPC"}],
        "skills": skills + [{"id": "conversation", "name": "Natural-language conversation & deliberation",
                             "description": f"A2A-style message/send. The agent writes in its working language "
                                            f"({ml.label(profile.get('language', 'en'))}); messages from other agents "
                                            "in any language are interpreted by its own LLM (translation, FIPA-ACL "
                                            "performative, intent). Modes: ask, deliberate, crosstalk, compose, "
                                            "converse, understand.", "tags": ["a2a", "llm", "multilingual"]}],
        "x-enterprise": {
            "ecosystem": ECOSYSTEM_NAME,
            "id": profile["id"],
            "name": profile["name"],
            "role": profile["role"],
            "platform": profile["platform"],
            "database": profile["database"],
            "native_protocol": profile["protocol"],
            "content_type": profile["content_type"],
            "data_semantics": profile["semantics"],
            "agent_paradigm": profile["paradigm"],
            "ai_models": profile["models"],
            "llm_reasoning_model": profile.get("llm_model"),
            "llm": {"model": profile.get("llm_model"), "vendor": profile.get("llm_vendor"),
                    "why": profile.get("llm_why")},
            "working_language": {"code": profile.get("language", "en"), "name": ml.name(profile.get("language", "en")),
                                 "label": ml.label(profile.get("language", "en"))},
            "agents": profile["agents"],
            "governance": profile["governance"],
            "users": [u["role"] for u in profile["users"]],
            "modules": [m["name"] for m in profile["modules"]],
            "dashboard": base_url + "/",
        },
    }
