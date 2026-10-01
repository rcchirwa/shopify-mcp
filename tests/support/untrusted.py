"""The untrusted-data fence as tests spell it (Story 10.69, SEC-04-errors).

Spelled here independently of `tools/_untrusted.py::wrap()`, so a change to
that helper cannot move both sides of an assertion at once. Only valid for a
value `wrap()` returns byte-for-byte: one with no forged closing delimiter.
"""


def fenced(text: str) -> str:
    """`text` inside the `<UNTRUSTED-DATA>` delimiters."""
    return "<UNTRUSTED-DATA>" + text + "</UNTRUSTED-DATA>"
