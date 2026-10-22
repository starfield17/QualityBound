# Video comparison owner

- `gui.comparison.window` owns independent A/B playback windows,
  scrubbing, audio selection, and cancellable background inspection workers.
- `gui.comparison.state` owns Qt-free timeline/offset and frame-selection rules;
  `gui.comparison.decode` owns read-only FFmpeg/FFprobe inspection and static-frame
  decoding. `gui.comparison.playback` owns the shared clock, external FFmpeg
  video/PCM audio pipes, and native Qt PCM output. Workers never mutate queue
  records or publish media. The codec ownership boundary is `SPEC.md` N11.
- MainWindow supplies paths, bound tools and translations. Comparison modules
  never import MainWindow or the queue. A generation token rejects stale decode
  results after seeks, file replacement, playback or closing.

```text
python -m unittest discover -s test -p "test_video_comparison.py" -v
python -m unittest discover -s test -p "test_architecture.py" -v
```
