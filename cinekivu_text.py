"""Bounded, plain metadata text shared by provider adapters."""
from html import unescape
import re


def synopsis(*values):
    for value in values:
        if not isinstance(value, str):
            continue
        text = unescape(re.sub(r'</?[A-Za-z][^>]*>', ' ', value)).strip()
        if text:
            return text[:8000]
    return ''
