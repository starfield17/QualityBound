# GUI audit — 2026-10-23

This review covered the comparison window, main-window coordination, queue
model/view and workers, encoding options, settings, presets, decisions, logs,
and window lifetime. The changes stay in the existing GUI owners. No encoding
algorithm, configuration format, dependency rule, or CI route changed.

## Findings and resulting behavior

| Finding | Result |
| --- | --- |
| The A/B button advertised Tab, but only T was registered. | Tab and T switch the visible side in single view. Shift+Tab retains native backward focus traversal. |
| Focusing the timeline disabled Space; clicking the video did not restore focus. | Space controls playback from the timeline, buttons, and canvas. Clicking a decoded image focuses its canvas. Editors, combos, and popups retain their own keys; timeline arrows retain native slider behavior. |
| A pending seek retained a captured resume flag, so pausing could restart playback. | One playback intent survives seeks and scrubbing. Completion reads the latest intent; loading, errors, and closing reset it. Existing generation checks still reject stale results. |
| A normal forward preview inside the final frame's display interval returned no image. | A successful empty decode uses timestamp inspection to decode the covering frame. Process failures still propagate. |
| Closing during encoder detection skipped the unfinished-queue warning. | Confirmation happens before cancellation or the terminal close latch. Declining leaves the queue and detection intact. |
| Temporary menus and dialogs remained children of the main window. | Menus and dialogs schedule deletion after consuming their return values, including cancellation and execution errors. Decision-owned comparison windows keep their existing close relationship. |
| Comparison and log control rows exceeded narrow screens with larger fonts or Japanese labels. | Controls reflow by their size hints. Comparison help is collapsible and vertically scrollable. |

The keyboard policy is intentionally different from the former blanket ban on
comparison shortcuts while the timeline was focused. The existing editor test
now checks that Tab is registered but disabled during editing. Transport
toggles do not auto-repeat.

## Regression and review points

`test/test_gui_audit.py` covers focused key delivery, native editor and popup
behavior, playback intent changes during delayed seeks and scrubbing, EOF
previews, tool failures, close confirmation, repeated dialog/menu disposal, and
three-language layouts at 760 × 520 with 14-point fonts. Existing comparison,
queue, presentation, lifecycle, and architecture tests remain enabled.

For review, check these boundaries:

- Comparison shortcuts retain window context; they must not control another
  comparison window or intercept parameter editing.
- Worker callbacks may apply only the current generation. Changing transport
  intent during a seek must not launch a second decoder session.
- Deferred deletion occurs after reading dialog values and menu choices.
  Tests flush deferred deletion before counting retained objects.
- Every codec operation still uses the resolved external FFmpeg/FFprobe pair
  (`SPEC.md` N11). EOF recovery neither substitutes tools nor hides a failed
  process. The existing file-publication rules are unchanged.

## Validation evidence

The local runtime was Python 3.13.14, PySide6/Qt 6.11.2, and FFmpeg 9.0.2 on
macOS arm64.

- Full asynchronous-exception-gated suite: 784 tests, no failures, one existing
  skip for the unconfigured `VMAF_INTEGRATION_FFMPEG` integration test.
- Ruff and all three Pyright scopes passed. Compilation and generated-icon
  checks passed. The strict-debt measurement remained at 559 total / 89 GUI
  diagnostics, matching the recorded baselines; the probe explicitly selected
  the Qt-enabled test interpreter with `--pythonpath`.
- Native Cocoa checks verified the comparison keyboard interactions and narrow
  layouts. A separate two-window check verified that Tab and Space affect only
  the active comparison window.
- Real FFmpeg previews used generated 64 × 64 clips at 25 and 30 fps. Positions
  0.96, 0.97, 0.99, and 1.00 seconds, with B offsets of 0 and 0.3 seconds, all
  returned frames. These fixtures verify timeline behavior, not media quality.
- Japanese narrow-window screenshots were inspected in light and dark themes,
  including expanded help.

Windows and Linux native verification remains with the existing three-platform
CI route. No packaged release was built or claimed by this audit.
