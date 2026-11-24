"""Message bus of the interoperability layer.

Every exchange is recorded twice: as an A2A-lite envelope with a *canonical* payload, and with
a snapshot of the *native* wire format (SOAP XML, JSON-RPC, Bahasa JSON, ...) that the adapter
actually sent/received. This is what the Control Center shows as the agent communication log.
"""
from __future__ import annotations

import json
import threading
import time

import httpx

from common.a2a import A2A_LITE_VERSION, A2AMessage
from common.timeutil import now_wib

VIA = "orchestrator.interop_gateway"
RETRYABLE = (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)


class EnterpriseUnavailable(Exception):
    def __init__(self, enterprise: str, detail: str):
        super().__init__(f"{enterprise}: {detail}")
        self.enterprise, self.detail = enterprise, detail


def snippet(text: str | bytes | None, limit: int = 4000) -> str | None:
    if text is None:
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


class MessageBus:
    def __init__(self, store, conversation_id: str, http: httpx.Client):
        self.store, self.conv, self.http = store, conversation_id, http
        self.seq = 0
        self.stats = {"native_calls": 0, "native_ok": 0, "native_failed": 0, "messages": 0}
        self._lock = threading.Lock()

    def post(self, sender: str, receiver: str, message_type: str, task: str, payload: dict | None = None, *,
             native: dict | None = None, interaction: str = "request-reply", in_reply_to: str | None = None,
             reply_required: bool = False, via: str | None = VIA, status: str = "DELIVERED",
             protocol: str = A2A_LITE_VERSION) -> A2AMessage:
        with self._lock:  # agents may talk in parallel (deliberation round)
            self.seq += 1
            self.stats["messages"] += 1
            msg = A2AMessage(message_id=f"{self.conv}-M{self.seq:03d}", conversation_id=self.conv, seq=self.seq,
                             timestamp=now_wib().isoformat(timespec="milliseconds"), sender=sender, receiver=receiver,
                             via=via, message_type=message_type, protocol=protocol, interaction=interaction, task=task,
                             payload=payload or {}, native=native, reply_required=reply_required,
                             in_reply_to=in_reply_to, status=status)
            self.store.save_message(msg)
            return msg

    def internal(self, sender: str, receiver: str, message_type: str, task: str, payload: dict) -> A2AMessage:
        """Message between agents *inside* the orchestrator (no native protocol involved)."""
        return self.post(sender, receiver, message_type, task, payload, via=None, protocol="internal")

    def exchange(self, *, sender: str, receiver: str, enterprise: str, message_type: str, task: str,
                 payload: dict, protocol_label: str, method: str, url: str, json_body: dict | None = None,
                 content: str | None = None, headers: dict | None = None, params: dict | None = None,
                 interaction: str = "request-reply") -> tuple[httpx.Response, A2AMessage]:
        native_req = json_body if json_body is not None else content
        if isinstance(native_req, dict):
            native_req = json.dumps(native_req, ensure_ascii=False, indent=1)
        native = {"protocol": protocol_label, "method": method, "url": url, "params": params,
                  "request": snippet(native_req)}
        req = self.post(sender, receiver, message_type, task, payload, native=native, interaction=interaction,
                        reply_required=True)
        with self._lock:
            self.stats["native_calls"] += 1
        t0 = time.perf_counter()
        resp = None
        for attempt in (1, 2):
            try:
                resp = self.http.request(method, url, json=json_body, content=content, headers=headers, params=params)
                break
            except RETRYABLE as exc:
                # a pooled keep-alive connection can be reset (e.g. after the host slept): retry once
                if attempt == 1:
                    self.stats["retries"] = self.stats.get("retries", 0) + 1
                    continue
                self._fail(req, task, f"{type(exc).__name__}: {exc}", protocol_label)
                raise EnterpriseUnavailable(enterprise, f"{type(exc).__name__}: {exc}") from exc
            except httpx.HTTPError as exc:
                self._fail(req, task, f"{type(exc).__name__}: {exc}", protocol_label)
                raise EnterpriseUnavailable(enterprise, f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            self._fail(req, task, f"HTTP {resp.status_code}: {resp.text[:300]}", protocol_label, resp)
            raise EnterpriseUnavailable(enterprise, f"HTTP {resp.status_code}: {resp.text[:300]}")
        with self._lock:
            self.stats["native_ok"] += 1
        resp.extensions["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return resp, req

    def reply(self, req: A2AMessage, message_type: str, payload: dict, resp: httpx.Response, protocol_label: str,
              interaction: str | None = None) -> A2AMessage:
        native = {"protocol": protocol_label, "status": resp.status_code,
                  "content_type": resp.headers.get("content-type"),
                  "latency_ms": resp.extensions.get("latency_ms"), "response": snippet(resp.text)}
        return self.post(req.receiver, req.sender, message_type, req.task, payload, native=native,
                         in_reply_to=req.message_id, interaction=interaction or req.interaction)

    def _fail(self, req: A2AMessage, task: str, error: str, protocol_label: str,
              resp: httpx.Response | None = None) -> None:
        with self._lock:
            self.stats["native_failed"] += 1
        self.post(req.receiver, req.sender, "FAILURE", task, {"error": error},
                  native={"protocol": protocol_label, "status": resp.status_code if resp is not None else None,
                          "response": snippet(resp.text) if resp is not None else None},
                  in_reply_to=req.message_id, interaction=req.interaction, status="FAILED")
