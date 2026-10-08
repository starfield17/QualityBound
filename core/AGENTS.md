# Core package guide

The root of `core` is intentionally limited to shared public contracts:
`models.py`, `progress_events.py`, and `i18n.py`.
Implementation belongs to one capability package:

- `config` — application paths, configuration and preset persistence.
- `media` — media-domain paths, files, subtitles, skipped outputs and validation.
- `ffmpeg` — discovery, probing, encoder capabilities and command construction.
- `smart` — sampling, VMAF measurement, cache identity, search and decisions,
  split into the default `smart/v1` path and the experimental `smart/v2` path.
- `encoding` — planning, process execution, analysis and concurrent encode jobs.

CLI and GUI callers use package public APIs (`core.encoding`, `core.ffmpeg`,
`core.smart`, and so on). Core implementation modules import the concrete owner
module and never reach through another package's internals via its facade.

Keep the dependency direction:

```text
models / progress_events / i18n
                ↓
          config + media
                ↓
              ffmpeg
                ↓
               smart
                ↓
             encoding
```

The recursive architecture test enforces the root allowlist, package DAG,
acyclic imports, and public adapter contracts. Package-specific invariants live
in `smart/AGENTS.md` and `encoding/AGENTS.md`. Run targeted checks with:

```text
python -m unittest discover -s test -p "test_architecture.py" -v
```

The retired `core.smart_quality` import path is removed. Application adapters
use `core.smart`; implementation tests import the concrete owner they exercise.
