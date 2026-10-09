r"""Escaping for FFmpeg filter option values.

FFmpeg's filtergraph parser reads ``\``, ``:`` and ``'`` inside an option value,
so a value that contains one has to be escaped before it is quoted. This is the
single owner of that rule: the Scout metadata file, the libvmaf model
configuration and the VMAF log path all build filtergraphs, and they previously
carried their own copies of the replacement chain.

Measured against FFmpeg 9.0.2 with ``metadata=mode=print:file=<value>`` (the raw
transcript is in docs/development.md, "A filter option value cannot carry a colon
or a quote"):

- value ``c\d.txt`` wrote ``cd.txt``; escaped ``c\\d.txt`` wrote ``c\d.txt``.
  Doubling the backslash is what makes a backslash survive, so this rule works.
- value ``a:b.txt`` failed with ``Invalid argument``; escaped ``a\:b.txt`` failed
  with ``Protocol not found``. Escaping a colon does not make such a path usable,
  it only changes the error, so no caller may pass one.
- values ``e'f.txt`` and ``e\'f.txt`` both wrote ``ef.txt``. The quote is consumed
  by the filtergraph parser either way, so no caller may pass one.

The colon and quote replacements stay because the emitted value is still parsed
as a filtergraph and dropping them would be a silent change; the callers pass
bare file names or model configurations, neither of which contains either
character.
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
