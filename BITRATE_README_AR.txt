AMIR SAT v3.1 - Sender Bitrate Monitor

- Added measured MPEG-TS output bitrate (Mb/s) to every channel row.
- Added total output bitrate (Mb/s) at the top of the dashboard.
- FFmpeg writes cumulative muxed output bytes to its own progress file.
- The UI computes a 3-second delta in bytes divided by actual elapsed seconds.
- Each stopped channel displays 0.00 Mb/s. The first sample may show 0.00 while measurement initializes.
- These are actual FFmpeg sender-side MPEG-TS output byte rates, NOT source-file metadata or a configured target bitrate.
- UDP has no delivery acknowledgment; this does NOT guarantee the receiver actually received all bytes.
- Measurement excludes Ethernet/IP/UDP packet header overhead.
- No additional encoding or decoding required.
- Tested: Python syntax, existing logic test, and FFmpeg UDP progress file creation on Linux.
- Not tested: 24 channels on Windows 8 or ST-8631 receiver.
