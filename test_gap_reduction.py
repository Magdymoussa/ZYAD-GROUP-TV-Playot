"""Small FFmpeg concat smoke test, independent of Tkinter/Windows hardware."""
import shutil, subprocess, tempfile
from pathlib import Path

def main():
    ffmpeg = shutil.which('ffmpeg')
    ffprobe = shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        print('SKIP: ffmpeg/ffprobe unavailable')
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for n in (1, 2):
            subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=size=160x90:rate=10', '-t', '1', '-c:v',
                            'mpeg4', '-q:v', '5', '-y', str(root / ('%d.mp4' % n))],
                           check=True, timeout=20)
        concat = root / 'playlist.ffconcat'
        concat.write_text("ffconcat version 1.0\nfile '1.mp4'\nfile '2.mp4'\n")
        output = root / 'result.ts'
        subprocess.run([ffmpeg, '-v', 'error', '-safe', '0', '-f', 'concat', '-i',
                        str(concat), '-map', '0:v:0', '-c', 'copy', '-f', 'mpegts',
                        '-y', str(output)], check=True, timeout=20)
        result = subprocess.run([ffprobe, '-v', 'error', '-show_entries',
                                 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
                                 str(output)], capture_output=True, text=True, check=True)
        duration = float(result.stdout.strip())
        assert 1.8 <= duration <= 2.5, duration
        print('PASS: compatible files concat in a single FFmpeg process; output %.2fs' % duration)

if __name__ == '__main__':
    main()
