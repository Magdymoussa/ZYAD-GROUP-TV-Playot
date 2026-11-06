AMIR SAT v3.1 - Reduced black gaps (experimental)

Compatible Original/Copy playlists now use FFmpeg concat demuxer inside ONE
long-running FFmpeg process and ONE UDP sender, avoiding a process restart at
every video boundary. All files are checked using ffprobe before enabling it.
Different codecs/resolutions/audio parameters or custom processing fall back
to the prior per-file mode with 3 failed attempts then skip.

Important limitations:
- FFmpeg concat stream copy requires genuinely compatible source streams.
- The compatibility check cannot guarantee decoder continuity for all MP4s.
- Video transitions are not guaranteed to be seamless at the receiving modulator.
- If continuous mode fails, the app falls back to the old playback engine.
- No Windows 8 + SATLINK hardware validation has been performed.
- ffprobe.exe should be next to ffmpeg.exe for automatic compatibility checks.
- Manual next/previous still restarts the sender, by design.
- Source files are not copied or re-encoded.

Run: python app.py
Tests: python test_gap_reduction.py
