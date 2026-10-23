## 2026-10-23 · Qt splitter ownership during GUI maintenance

constraint: widgets moved into splitters must retain safe native ownership through Python garbage collection.
happened: constructing a QScrollArea before its eventual QSplitter parent passed ordinary interaction tests but crashed during repeated MainWindow destruction under PySide6 6.11.2. Holding Qt-created toolbar-button wrappers also exposed teardown failures.
workaround: construct parent splitters before child scroll areas, and explicitly construct detached-queue toolbar buttons. The regression suite exercises repeated window disposal; a standalone parent-first reproduction also survived 1,000 iterations.
proposal: preserve parent-first construction when reorganizing Qt containers; keep native teardown coverage alongside interaction tests.
