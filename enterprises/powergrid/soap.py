"""Minimal SOAP 1.1 envelope helpers for PowerGrid's legacy EMS web service."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
PG_NS = "urn:powergrid:ems:2009"
NS = {"soap": SOAP_NS, "pg": PG_NS}


def parse_request(body: bytes) -> tuple[str, ET.Element]:
    root = ET.fromstring(body)
    soap_body = root.find("soap:Body", NS)
    if soap_body is None or len(soap_body) == 0:
        raise ValueError("SOAP Body missing")
    op = soap_body[0]
    name = op.tag.split("}", 1)[1] if "}" in op.tag else op.tag
    return name, op


def text(elem: ET.Element, tag: str, default: str | None = None) -> str | None:
    child = elem.find(f"pg:{tag}", NS)
    return child.text.strip() if child is not None and child.text else default


def envelope(inner: str) -> str:
    return (f'<?xml version="1.0" encoding="utf-8"?>\n'
            f'<soap:Envelope xmlns:soap="{SOAP_NS}" xmlns:pg="{PG_NS}">\n'
            f'  <soap:Body>\n{inner}\n  </soap:Body>\n</soap:Envelope>')


def fault(code: str, message: str) -> str:
    return envelope(f"    <soap:Fault><faultcode>soap:{escape(code)}</faultcode>"
                    f"<faultstring>{escape(message)}</faultstring></soap:Fault>")


def attr(value) -> str:
    return quoteattr(str(value))


def esc(value) -> str:
    return escape(str(value))
