"""The single owner of FFmpeg filter option escaping.

Scout metadata files, the libvmaf model configuration and the VMAF log path all
interpolate a value into a filtergraph. These tests pin the escaping rule itself
and the call sites that used to carry their own copies of it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core.ffmpeg.filters import escape_filter_value, quote_filter_value
from core.smart.v1.sampling.complexity import build_scout_command
from core.smart.v1.vmaf import (
    VMAF_STANDARD_MODEL,
    VmafEncodeMetadata,
    build_cpu_vmaf_filter_graph,
)


class EscapeFilterValueTestCase(unittest.TestCase):
    def test_each_metacharacter_is_escaped(self) -> None:
        self.assertEqual(escape_filter_value("a\\b"), "a\\\\b")
        self.assertEqual(escape_filter_value("a:b"), "a\\:b")
        self.assertEqual(escape_filter_value("a'b"), "a\\'b")

    def test_backslash_is_doubled_before_the_other_escapes_are_added(self) -> None:
        # A literal backslash followed by a colon must not become "\\\:" ...
        self.assertEqual(escape_filter_value("\\:"), "\\\\\\:")

    def test_ordinary_text_is_unchanged(self) -> None:
        self.assertEqual(escape_filter_value("metrics.txt"), "metrics.txt")

    def test_quoting_wraps_the_escaped_value(self) -> None:
        self.assertEqual(quote_filter_value("a:b"), "'a\\:b'")
        self.assertEqual(quote_filter_value("plain"), "'plain'")


class ScoutMetadataFilterTestCase(unittest.TestCase):
    def test_metadata_path_is_escaped_not_rewritten(self) -> None:
        command = build_scout_command(
            Path("ffmpeg"),
            Path("source.mp4"),
            start_sec=0.0,
            duration_sec=1.0,
            metadata_path=Path("odd:name.txt"),
        )
        graph = command[command.index("-vf") + 1]
        self.assertIn("metadata=mode=print:file='odd\\:name.txt'", graph)


class VmafLogPathTestCase(unittest.TestCase):
    def test_log_path_is_escaped_once_by_the_builder(self) -> None:
        graph = build_cpu_vmaf_filter_graph(
            model_spec=VMAF_STANDARD_MODEL,
            encode_metadata=VmafEncodeMetadata(1920, 1080, 8),
            log_path="C:\\work\\vmaf:out.json",
            n_threads=2,
            n_subsample=1,
            distorted_input="0:v",
            reference_input="1:v",
        )
        self.assertIn("log_path='C\\:\\\\work\\\\vmaf\\:out.json'", graph)

    def test_missing_log_path_omits_the_option(self) -> None:
        graph = build_cpu_vmaf_filter_graph(
            model_spec=VMAF_STANDARD_MODEL,
            encode_metadata=VmafEncodeMetadata(1920, 1080, 8),
            log_path=None,
            n_threads=2,
            n_subsample=1,
        )
        self.assertNotIn("log_path", graph)


if __name__ == "__main__":
    unittest.main()
