`app.svg` is the canonical project icon: a white rounded tile with a navy video
frame, a teal play mark, and a Q-shaped tail. Regenerate the committed platform
assets after editing it:

```bash
python scripts/build_icons.py
```

- `app.ico`: Windows executable and Setup installer metadata.
- `app.icns`: macOS application bundle icon.
- `app.png`: high-resolution preview and fallback raster.
- `app.svg`: runtime window icon on every platform.
- `app-assets.json`: cross-platform SHA-256 manifest for the committed assets.

Use `python scripts/build_icons.py --check` in CI to detect stale generated
assets.
