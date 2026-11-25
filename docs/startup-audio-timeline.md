# Align input with process audio

Use [startup_audio_timeline.py](../tools/startup_audio_timeline.py) to compare
controller actions with packet levels from a process-loopback capture. The
script runs offline in Python 3.11 or newer and reads statistics files.

The packet CSV has columns `process_s,frames,rms,peak,flags`. The timestamp is
the start of the packet in seconds relative to process creation. Use linear
full-scale RMS and peak in the range 0 through 1. Frames are audio sample frames
at the capture sample rate. WASAPI flags are decimal integers. The controller
JSONL contains one object per line, such as
`{"action":"down","qpc":104.1}`. The controller timestamp and process origin
must use the same local QueryPerformanceCounter (QPC) clock in seconds.

```powershell
python -B tools/startup_audio_timeline.py packets.csv inputs.jsonl --process-origin-qpc 100 --action down --action confirm --before 0.44 --after 0.30
```

The example window includes 440 ms before each recorded action and 300 ms
after it. Choose the window based on what your controller recorder timestamps
and how long playback may take to reach the endpoint. Use `--sample-rate` if
the capture rate differs from 48,000 Hz.

Coverage is the fraction of the complete window represented by packets with
valid timestamps. The RMS estimate weights each packet's mean squared level
by its overlap with the window. The peak is the largest peak of any included
packet, which can occur outside the window if the packet crosses its edge.
Discontinuities identify included packets that carry the WASAPI discontinuity
flag. A completely uncovered interval produces null audio levels. Compare
covered windows against a known audible interval and an idle baseline.

Run the source-built control without an audio device:

```powershell
python -B tests/startup_audio_timeline_check.py
```

The control checks a nonzero mixed-level window, full-coverage silence, a gap
from an invalid timestamp and a window with zero capture coverage.
