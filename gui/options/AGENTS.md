# Encoding options and configuration owner

`encode_options_panel.EncodeOptionsPanel(QWidget)` owns the Basic / Video /
Audio-Subtitles tabs and all internal wiring: codec/backend
filtering from runtime capabilities, encoder-preset refresh, analysis-profile state,
and Smart/Fixed control syncing.

- `MainWindow` interacts only through the public contract:
  `read_options()`, `apply_options()`,
  `apply_analysis_profile_settings()`, `current_analysis_profile_name()`,
  `sync_dependent_controls()`,
  `set_runtime_capabilities()`, `notify_capability_detection_failed()`,
  `begin_capability_detection()`, `set_translator()`, `set_busy()`,
  `set_visible()`, `summary_text()`.
- Semantic signals: `codec_changed`, `compression_mode_changed`,
  `analysis_profile_changed`, `options_changed`, `smart_unavailable`.
- Display labels for codec, container, backend, audio mode and log level live in
  `EncodeOptionsPanel`/`SettingsDialog`; the combo item data keeps the raw enum
  value. Do not read enum choices from `currentText()`.
- `MainWindow` must not reach into the panel's raw widgets; tests that need widget
  state access them via `window.options_panel.<widget>`.
- The panel renders the capability snapshot produced by `EncoderCapabilityDetectWorker`.
  Encoder entries include `preset_choices`; never run FFmpeg discovery or encoder
  probing synchronously from a widget refresh path.

`capability_worker.EncoderCapabilityDetectWorker` owns encoder and VMAF probing.
`settings_dialog` owns application settings; `preset_manager_dialog` owns preset
editing. Queue intake/preparation workers live in `gui.queue.workers`.

```text
python -m unittest discover -s test -p "test_smart_gui.py" -v
python -m unittest discover -s test -p "test_gui_workers.py" -v
python -m unittest discover -s test -p "test_gui_presentation.py" -v
python -m unittest discover -s test -p "test_architecture.py" -v
```
