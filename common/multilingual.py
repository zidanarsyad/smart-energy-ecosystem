"""Multilingual agent communication: language names and a deterministic check on LLM translations.

Language is one more facet of semantic heterogeneity. Every enterprise agent writes its natural-language messages
in its own working language (SolarGreen in Mandarin, PowerGrid and IndustriPrima in Bahasa Indonesia, ...). The
receiving agent's own LLM interprets each message into the receiver's language: a faithful translation plus the
FIPA-ACL performative and the intent. The LLM output is then checked deterministically: every number of the
original (kWh, Rp, %, hours) must survive the translation, so a hallucinated or dropped quantity is visible.
"""
from __future__ import annotations

import re

LANGUAGES: dict[str, tuple[str, str]] = {   # code -> (English name, name in the language itself)
    "en": ("English", "English"),
    "id": ("Indonesian", "Bahasa Indonesia"),
    "zh": ("Mandarin Chinese", "中文"),
    "ja": ("Japanese", "日本語"),
    "jv": ("Javanese", "Basa Jawa"),
    "ms": ("Malay", "Bahasa Melayu"),
    "ar": ("Arabic", "العربية"),
    "de": ("German", "Deutsch"),
    "fr": ("French", "Français"),
    "es": ("Spanish", "Español"),
}


def name(code: str | None) -> str:
    """English name of a language code ('zh' -> 'Mandarin Chinese'); unknown codes are returned as given."""
    return LANGUAGES.get(code or "", (code or "unknown", ""))[0]


def label(code: str | None) -> str:
    """UI label: the language's own name plus its English name, e.g. '中文 (Mandarin Chinese)'."""
    english, own = LANGUAGES.get(code or "", (code or "unknown", code or "unknown"))
    return own if own == english else f"{own} ({english})"


_NUM = re.compile(r"\d+(?:[.,  ]\d+)*")
_GROUPED = re.compile(r"\d{1,3}([.,  ])\d{3}(?:\1\d{3})*")
_SEPS = re.compile(r"[.,  ]")


def _value(token: str) -> float:
    if _GROUPED.fullmatch(token):                       # 1.500 (id) = 1,500 (en) = 1500; 4.684.100
        return float(_SEPS.sub("", token))
    seps = _SEPS.findall(token)
    if not seps:
        return float(token)
    last = max(token.rfind("."), token.rfind(","))     # 1.500,75 / 1,500.75 / 23,08 / 37.5
    return float(_SEPS.sub("", token[:last]) + "." + token[last + 1:])


def numbers_in(text: str) -> set[float]:
    """Numbers in a text, normalised across locales (Indonesian 1.741 = English 1,741). Zeros are ignored."""
    out = set()
    for token in _NUM.findall(text or ""):
        try:
            value = _value(token)
        except ValueError:
            continue
        if value:
            out.add(round(value, 4))
    return out


_CJK_CHARS = "぀-ヿ㐀-䶿一-鿿가-힯"
_CJK = re.compile(f"[{_CJK_CHARS}]")
_CJK_RUN = re.compile(f"[{_CJK_CHARS}]{{4,}}")


def looks_untranslated(source: str, translation: str | None, src: str | None, dst: str | None) -> bool:
    """Deterministic guard: the 'translation' is still the original text, echoes the original inside it, or is in
    the wrong script (a run of Chinese characters left in a Bahasa Indonesia or English rendering, or no Chinese
    characters in a Mandarin one). Same-script language pairs can only be checked for a copied original."""
    if not translation or not src or not dst or src == dst:
        return False
    text, original = translation.strip(), (source or "").strip()
    if text == original or (len(original) > 10 and original in text):
        return True
    if dst in ("zh", "ja"):
        return len(_CJK.findall(text)) / max(1, len(text)) < 0.1
    return dst != "ar" and _CJK_RUN.search(text) is not None


def number_fidelity(source: str, translation: str | None) -> dict:
    """Did every number of the source survive the translation? (deterministic guard on the LLM interpreter)"""
    src, dst = numbers_in(source), numbers_in(translation or "")
    missing, added = sorted(src - dst), sorted(dst - src)
    show = lambda xs: [int(x) if x == int(x) else x for x in xs]  # noqa: E731
    return {"kept": len(src) - len(missing), "total": len(src), "ok": not missing,
            "missing": show(missing), "added": show(added)}
