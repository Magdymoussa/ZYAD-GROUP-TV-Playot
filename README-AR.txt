AMIR SAT v3.1 - tested source update

This package is SOURCE CODE, not a built Windows EXE.
Run on Windows 8 64-bit with Python 3, Pillow, psutil and FFmpeg.
Build workflow is included for Windows x64 via GitHub Actions.

Verified in Linux:
- Python compilation
- Tkinter GUI startup in Xvfb for 7 seconds (no traceback)
- Unit tests: natural sorting, deduplication, current item retention, 3 retries then next item

NOT VERIFIED:
- Windows 8 execution, 24/32 live channels, modulator compatibility
- Pixel-exact UI match with the supplied mockup
- Gapless transitions, true per-channel bitrate, embedded video preview

Critical behavior: three retries of a failed video, then next video; recovery continues indefinitely if all fail.
FFmpeg video stream copy remains default when no filters/bitrate changes are requested.
