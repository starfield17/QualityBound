"""Escaping for FFmpeg filter option values.

FFmpeg's filtergraph parser reads ``\\``, ``:`` and ``'`` inside an option value,
so a path or expression that contains one has to be escaped before the value is
quoted. This is the single owner of that rule: the Scout metadata file, the
libvmaf model configuration and the VMAF log path all build filtergraphs, and
they previously carried their own copies of the replacement chain.
"""

from __future__ import annotations


def escape_filter_value(value: str) -> str:
    """Escape the filtergraph metacharacters that can appear in an option value.

    The backslash is doubled first so that the escapes added for ``:`` and ``'``
    are not themselves escaped.
    """

    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def quote_filter_value(value: str) -> str:
    """Escape and single-quote a value for use inside an FFmpeg filtergraph."""

    return f"'{escape_filter_value(value)}'"
