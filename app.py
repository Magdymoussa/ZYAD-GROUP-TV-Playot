import json, os, sys, time, threading, subprocess, shutil, ipaddress, re, tempfile, bisect
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

APP = 'AMIR SAT TV Playout'
HOME = Path(os.environ.get('LOCALAPPDATA') or str(Path.home())) / 'ZYADGroupPlayout'
HOME.mkdir(parents=True, exist_ok=True)
CONFIG = HOME / 'channels.json'
lock = threading.RLock()
processes, workers, states, errors = {}, {}, {}, {}
# FFmpeg progress files report cumulative bytes emitted by the MPEG-TS muxer.
progress_paths = {}
bitrate_samples = {}
channel_mbps = {}
stop_flags = set()
retry_counts = {}
MAX_RETRIES_PER_VIDEO = 3  # Three total failed starts, then move to the next video
switch_requests = {}  # channel -> requested playlist index
continuous_disabled = set()  # incompatible/broken playlists use safe per-file fallback
last_error_log = HOME / "ffmpeg_errors.log"

def log_channel_error(i, detail):
    try:
        with lock:
            with last_error_log.open("a", encoding="utf-8") as f:
                f.write("%s | CH %02d | %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), i + 1, str(detail).replace("\n", " ")[:1200]))
        if last_error_log.stat().st_size > 2_000_000:
            with lock:
                lines = last_error_log.read_text(encoding="utf-8", errors="replace").splitlines()[-1500:]
                last_error_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass



VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.ts', '.mov', '.avi'}

def natural_key(value):
    """Sort numbered folder/file names as 1, 2, 10 rather than 1, 10, 2."""
    parts = re.split(r'(\d+)', str(value).casefold())
    return tuple((1, int(part), len(part)) if part.isdigit() else (0, part) for part in parts)

def playlist_key(path):
    # Compare path components individually so nested folders retain natural order.
    return tuple(natural_key(part) for part in Path(path).parts)

def add_sorted_files(channel, new_files):
    """Deduplicate and sort; retain the currently selected file when possible."""
    existing = channel['files']
    current = existing[int(channel.get('index', 0)) % len(existing)] if existing else None
    merged = list(dict.fromkeys([*existing, *(str(p) for p in new_files)]))
    merged.sort(key=playlist_key)
    channel['files'] = merged
    if current in merged:
        channel['index'] = merged.index(current)
    else:
        channel['index'] = 0

def defaults(i):
    return dict(name='Channel %02d' % (i+1), files=[], ip='239.1.1.%d' % (i+1), port=1234+i,
                mode='copy', resolution='Original', fps='Original', video_kbps='Original',
                audio_codec='copy', audio_kbps='Original', audio_rate='Original', audio_channels='Original', audio_volume='100', text='', logo='', logo_position='Top Right', logo_width=140, bitrate_mode='Original', loop=True, resume=True,
                index=0, position=0.0, continuous_playback=True)


BACKUP = HOME / 'channels.backup.json'
BACKUP2 = HOME / 'channels.backup2.json'
SETTINGS = HOME / 'settings.json'
SETTINGS_BACKUP = HOME / 'settings.backup.json'

def read_channels(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, list) or len(data) != 32 or not all(isinstance(c, dict) for c in data):
        raise ValueError('Expected exactly 32 channel objects')
    return [{**defaults(i), **data[i]} for i in range(32)]

def atomic_json(path, value):
    tmp = path.with_name(path.name + '.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass

def load():
    for path in (CONFIG, BACKUP, BACKUP2):
        try:
            data = read_channels(path)
            if path != CONFIG:
                atomic_json(CONFIG, data)
            return data
        except (OSError, ValueError, TypeError):
            continue
    # Preserve corrupted files for investigation instead of silently overwriting them.
    if any(p.exists() for p in (CONFIG, BACKUP, BACKUP2)):
        raise RuntimeError('Saved channel data is unreadable. Files preserved in: ' + str(HOME))
    return [defaults(i) for i in range(32)]

channels = load()

def save():
    with lock:
        # Keep a rolling second backup, and write the primary file last.
        if BACKUP.exists():
            try:
                read_channels(BACKUP)
                shutil.copy2(str(BACKUP), str(BACKUP2))
            except (OSError, ValueError, TypeError):
                pass
        atomic_json(BACKUP, channels)
        atomic_json(CONFIG, channels)


def ffmpeg_path():
    candidates = []
    if getattr(sys, 'frozen', False):
        candidates.append(Path(sys.executable).resolve().parent / 'ffmpeg.exe')
        if hasattr(sys, '_MEIPASS'):
            candidates.append(Path(sys._MEIPASS) / 'ffmpeg.exe')
    candidates.append(Path(__file__).resolve().parent / 'ffmpeg.exe')
    stored = settings.get('ffmpeg_path', '')
    if stored:
        candidates.append(Path(stored))
    for p in candidates:
        if p.is_file():
            return str(p)
    return shutil.which('ffmpeg')


def validate(c):
    ipaddress.ip_address(c['ip'])
    if not 1 <= int(c['port']) <= 65535:
        raise ValueError('UDP port must be 1-65535')
    if c['mode'] != 'copy' and str(c['video_kbps']) != 'Original' and int(c['video_kbps']) <= 0:
        raise ValueError('Video bitrate must be positive')
    if str(c.get('audio_volume', '100')) != '100' and not 0 <= float(c['audio_volume']) <= 300:
        raise ValueError('Audio volume must be 0-300%')
    if c['audio_codec'] != 'copy' and str(c['audio_kbps']) != 'Original' and int(c['audio_kbps']) <= 0:
        raise ValueError('Audio bitrate must be positive')


def command(i, video, ff):
    c = channels[i]
    validate(c)
    a = [ff, '-hide_banner', '-loglevel', 'error', '-nostdin', '-re', '-thread_queue_size', '64']
    if c['resume'] and float(c['position']) > 0:
        a += ['-ss', str(c['position'])]
    a += ['-i', video]
    logo = c.get('logo', '').strip()
    if logo:
        if not Path(logo).is_file():
            raise ValueError('Logo image not found: ' + logo)
        a += ['-loop', '1', '-i', logo]
    filters = []
    if c['resolution'] != 'Original':
        filters.append('scale=-2:%s' % c['resolution'].replace('p', ''))
    if c['fps'] != 'Original':
        filters.append('fps=%s' % c['fps'])
    if c['text'].strip():
        txt = HOME / ('overlay_%02d.txt' % (i+1))
        txt.write_text(c['text'], encoding='utf-8')
        escaped = str(txt).replace('\\', '/').replace(':', '\\:').replace("'", "\\'")
        filters.append("drawtext=textfile='%s':fontcolor=white:fontsize=26:x=20:y=20" % escaped)
    transcode = c['mode'] != 'copy' or bool(filters) or bool(logo) or str(c.get('video_kbps', 'Original')) != 'Original'
    if logo:
        width = max(32, min(1024, int(c.get('logo_width', 140))))
        coords = {'Top Left':'20:20', 'Top Right':'W-w-20:20',
                  'Bottom Left':'20:H-h-20', 'Bottom Right':'W-w-20:H-h-20',
                  'Center':'(W-w)/2:(H-h)/2'}
        xy = coords.get(c.get('logo_position'), 'W-w-20:20')
        base = ','.join(filters) if filters else 'null'
        a += ['-filter_complex', '[0:v]%s[base];[1:v]scale=%d:-1[mark];[base][mark]overlay=%s:shortest=1[v]' % (base, width, xy),
              '-map', '[v]', '-map', '0:a:0?']
    else:
        a += ['-map', '0:v:0', '-map', '0:a:0?']
    if transcode:
        codec = 'libx264' if c['mode'] == 'copy' else c['mode']
        a += ['-c:v', codec, '-preset', 'veryfast']
        if str(c['video_kbps']) != 'Original':
            a += ['-b:v', '%sk' % int(c['video_kbps'])]
        if c.get('bitrate_mode', 'Original') == 'CBR' and str(c['video_kbps']) != 'Original':
            kbps = int(c['video_kbps'])
            a += ['-minrate', '%sk' % kbps, '-maxrate', '%sk' % kbps,
                  '-bufsize', '%sk' % (kbps * 2)]
        if filters and not logo:
            a += ['-vf', ','.join(filters)]
    else:
        a += ['-c:v', 'copy']
    audio_changed = str(c.get('audio_rate', 'Original')) != 'Original' or str(c.get('audio_channels', 'Original')) != 'Original' or str(c.get('audio_volume', '100')) != '100'
    if c['audio_codec'] == 'copy' and not audio_changed:
        a += ['-c:a', 'copy']
    else:
        a += ['-c:a', 'aac' if c['audio_codec'] == 'copy' else c['audio_codec']]
        if str(c.get('audio_rate', 'Original')) != 'Original':
            a += ['-ar', str(int(c['audio_rate']))]
        if str(c.get('audio_channels', 'Original')) != 'Original':
            a += ['-ac', str(int(c['audio_channels']))]
        if str(c.get('audio_volume', '100')) != '100':
            a += ['-af', 'volume=%s' % (float(c['audio_volume']) / 100.0)]
        if str(c['audio_kbps']) != 'Original':
            a += ['-b:a', '%sk' % int(c['audio_kbps'])]
    a += ['-flush_packets', '1', '-f', 'mpegts', 'udp://%s:%s?pkt_size=1316&buffer_size=262144' % (c['ip'], int(c['port']))]
    return a


def set_state(i, state, err=''):
    with lock:
        states[i] = state
        errors[i] = err


def wait_for_retry(i, attempts):
    delay = min(2 ** min(attempts, 6), 60)
    # Interruptible sleep: Stop Channel works even during recovery.
    for _ in range(int(delay * 5)):
        if i in stop_flags: return False
        time.sleep(0.2)
    return i not in stop_flags

def recover_or_skip(i, c, video, detail):
    """Retry a failing file three times, then advance to the next playlist item."""
    set_state(i, 'RECOVERING', detail)
    log_channel_error(i, detail)
    attempt = retry_counts.get(i, 0) + 1
    retry_counts[i] = attempt
    if attempt < MAX_RETRIES_PER_VIDEO:
        return wait_for_retry(i, attempt)
    with lock:
        current_files = c['files']
        if not current_files:
            return False
        current_index = current_files.index(video) if video in current_files else int(c['index']) % len(current_files)
        next_index = current_index + 1
        if next_index >= len(current_files):
            if c['loop']:
                next_index = 0
            else:
                c['position'] = 0.0
                save()
                return False
        c['index'] = next_index
        c['position'] = 0.0
        save()
    log_channel_error(i, 'Skipped after 3 failed attempts: ' + video)
    retry_counts[i] = 0
    # If every video fails, keep retrying with a bounded delay rather than spinning.
    if len(current_files) == 1:
        return wait_for_retry(i, 3)
    return wait_for_retry(i, 1)


def ffprobe_path(ff):
    exe = Path(ff)
    probe = exe.with_name('ffprobe.exe' if exe.suffix.lower() == '.exe' else 'ffprobe')
    return str(probe) if probe.is_file() else shutil.which('ffprobe')


def probe_streams(ff, filename):
    probe = ffprobe_path(ff)
    if not probe:
        return None
    try:
        r = subprocess.run([probe, '-v', 'error', '-show_entries',
            'stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,sample_rate,channels,profile,level,extradata_size:format=duration',
            '-of', 'json', filename], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=12, check=True)
        info = json.loads(r.stdout.decode('utf-8', 'replace'))
        duration = float(info['format']['duration'])
        streams = [st for st in info['streams'] if st.get('codec_type') in ('video','audio')]
        if not streams or duration <= 0:
            return None
        # Conservatively require identical codec parameters for safe stream copy.
        signature = tuple(tuple(sorted(st.items())) for st in streams)
        return signature, duration
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def concat_candidate(i, ff, files, c):
    """Only use a persistent FFmpeg process for homogeneous, unfiltered copy streams."""
    if (not c.get('continuous_playback', True) or i in continuous_disabled or
        len(files) < 2 or not c.get('loop') or c.get('mode') != 'copy' or
        c.get('audio_codec') != 'copy' or c.get('logo') or c.get('text') or
        c.get('resolution') != 'Original' or c.get('fps') != 'Original' or
        str(c.get('video_kbps')) != 'Original' or
        str(c.get('audio_rate')) != 'Original' or
        str(c.get('audio_channels')) != 'Original' or
        str(c.get('audio_volume')) != '100' or
        float(c.get('position', 0)) > 0):
        return None
    # A single bad or incompatible source must not stop the channel.
    details = [probe_streams(ff, name) for name in files]
    if any(x is None for x in details):
        return None
    if any(x[0] != details[0][0] for x in details[1:]):
        return None
    return [x[1] for x in details]


def progress_seconds(path):
    try:
        raw = path.read_text(encoding='utf-8', errors='replace')[-2048:]
        m = re.findall(r'^out_time_ms=(\d+)$', raw, flags=re.M)
        if m:
            return int(m[-1]) / 1_000_000.0  # FFmpeg's out_time_ms is microseconds.
        m = re.findall(r'^out_time=(\d+):(\d+):(\d+(?:\.\d+)?)$', raw, flags=re.M)
        if m:
            h, mi, sec = m[-1]
            return int(h)*3600 + int(mi)*60 + float(sec)
    except (OSError, ValueError):
        pass
    return None


def run_continuous(i, ff, files, durations):
    """One long-lived UDP sender; do not launch a second sender for the next file."""
    with lock:
        c = channels[i]
        start_index = int(c['index']) % len(files)
        ordered = files[start_index:] + files[:start_index]
        ordered_durations = durations[start_index:] + durations[:start_index]
        ip, port = c['ip'], int(c['port'])
        validate(c)
    # The concat demuxer handles adjacent files inside the same FFmpeg process.
    with tempfile.TemporaryDirectory(prefix='amir_concat_') as folder:
        concat = Path(folder) / 'playlist.ffconcat'
        def esc(f):
            return str(Path(f).resolve()).replace('\\', '/').replace("'", "'\\''")
        concat.write_text('ffconcat version 1.0\n' + ''.join("file '%s'\n" % esc(f) for f in ordered), encoding='utf-8')
        progress = Path(folder) / 'progress.txt'
        cmd = [ff, '-hide_banner', '-loglevel', 'error', '-nostdin', '-stats_period', '1',
               '-progress', str(progress), '-re', '-stream_loop', '-1', '-safe', '0',
               '-f', 'concat', '-i', str(concat), '-map', '0:v:0', '-map', '0:a:0?',
               '-c:v', 'copy', '-c:a', 'copy', '-flush_packets', '1', '-f', 'mpegts',
               'udp://%s:%s?pkt_size=1316&buffer_size=262144' % (ip, port)]
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        if os.name == 'nt': flags |= getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0)
        boundaries = [0.0]
        for duration in ordered_durations: boundaries.append(boundaries[-1] + duration)
        cycle = boundaries[-1]
        with tempfile.TemporaryFile(mode='w+b') as stderr:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=stderr, creationflags=flags)
            with lock:
                processes[i] = proc
                progress_paths[i] = progress
                channel_mbps[i] = 0.0
                bitrate_samples.pop(i, None)
            set_state(i, 'RUNNING')
            last_saved = 0.0
            while proc.poll() is None and i not in stop_flags:
                with lock:
                    switched = i in switch_requests
                    playlist_changed = channels[i]['files'] != files
                if switched or playlist_changed:
                    proc.terminate()
                    break
                elapsed = progress_seconds(progress)
                if elapsed is not None and cycle > 0:
                    within = elapsed % cycle
                    segment = max(0, min(len(ordered)-1, bisect.bisect_right(boundaries, within)-1))
                    original = (start_index + segment) % len(files)
                    if time.monotonic() - last_saved >= 5:
                        with lock:
                            channels[i]['index'] = original
                            channels[i]['position'] = max(0.0, within - boundaries[segment])
                            save()
                        last_saved = time.monotonic()
                time.sleep(0.25)
            if proc.poll() is None:
                try: proc.terminate(); proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill(); proc.wait()
            stderr.seek(0, os.SEEK_END)
            size = stderr.tell()
            stderr.seek(max(0, size - 2048))
            error = stderr.read().decode('utf-8', 'replace')
        with lock:
            if processes.get(i) is proc: processes.pop(i, None)
            if progress_paths.get(i) == progress:
                progress_paths.pop(i, None)
                bitrate_samples.pop(i, None)
                channel_mbps[i] = 0.0
        if i in stop_flags: return True
        with lock:
            requested = switch_requests.pop(i, None)
            if requested is not None:
                channels[i]['index'] = requested % len(channels[i]['files'])
                channels[i]['position'] = 0.0
                save()
                return True
            if channels[i]['files'] != files:
                channels[i]['position'] = 0.0
                save()
                return True
        log_channel_error(i, 'Continuous playback exited; falling back to per-video recovery: ' + error[-700:])
        continuous_disabled.add(i)
        return False


def work(i):
    while i not in stop_flags:
        with lock:
            c = channels[i]
            files = list(c['files'])
            if not files:
                break
            idx = int(c['index']) % len(files)
            video = files[idx]
        if not Path(video).is_file():
            detail = 'Missing video: ' + video
            if not recover_or_skip(i, c, video, detail): break
            continue
        ff = ffmpeg_path()
        if not ff:
            detail = 'ffmpeg.exe not found. Open Global Settings.'
            set_state(i, 'RECOVERING', detail)
            log_channel_error(i, detail)
            retry_counts[i] = retry_counts.get(i, 0) + 1
            if not wait_for_retry(i, retry_counts[i]): break
            continue
        # Prefer one persistent FFmpeg sender when all files are stream-copy compatible.
        # A failure falls back to the existing 3-attempts-then-next recovery path.
        if i not in continuous_disabled:
            durations = concat_candidate(i, ff, files, c)
            if durations is not None:
                try:
                    run_continuous(i, ff, files, durations)
                    if i in stop_flags: break
                    continue
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    log_channel_error(i, 'Continuous mode fallback: ' + str(exc))
                    continuous_disabled.add(i)
        try:
            cmd = command(i, video, ff)
            # A separate small progress file per active FFmpeg process.
            # No extra video decoding, transcoding, or UDP receiving socket.
            progress_path = HOME / ('ffmpeg_progress_%02d_%d.txt' % (i+1, time.monotonic_ns()))
            cmd[1:1] = ['-stats_period', '1', '-progress', str(progress_path)]
            with lock:
                progress_paths[i] = progress_path
                bitrate_samples.pop(i, None)
                channel_mbps[i] = 0.0
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            if os.name == 'nt':
                flags |= getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0)
            # A temporary disk-backed stderr stream avoids keeping hours of FFmpeg
            # output in RAM (important when 24-32 channels run concurrently).
            with tempfile.TemporaryFile(mode='w+b') as ffmpeg_stderr:
                p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=ffmpeg_stderr, creationflags=flags)
                with lock:
                    processes[i] = p
                    run_base[i] = float(c.get('position', 0)) if c['resume'] else 0.0
                    run_start[i] = time.monotonic()
                set_state(i, 'RUNNING')
                start_time = time.monotonic()
                p.wait()
                elapsed = time.monotonic() - start_time
                ffmpeg_stderr.seek(0, os.SEEK_END)
                size = ffmpeg_stderr.tell()
                ffmpeg_stderr.seek(max(0, size - 2048))
                err = ffmpeg_stderr.read()
            with lock:
                processes.pop(i, None)
                if progress_paths.get(i) == progress_path:
                    progress_paths.pop(i, None)
                    bitrate_samples.pop(i, None)
                    channel_mbps[i] = 0.0
            try: progress_path.unlink(missing_ok=True)
            except OSError: pass
            if i in stop_flags:
                break
            # A manual switch deliberately terminates the old FFmpeg process.
            # Consume it before treating the exit code as an error.
            with lock:
                requested = switch_requests.pop(i, None)
                if requested is not None:
                    c['index'] = requested % len(c['files']) if c['files'] else 0
                    c['position'] = 0.0
                    save()
            if requested is not None:
                continue
            if p.returncode != 0:
                detail = err.decode('utf-8', 'replace')[-700:] if err else 'Unknown FFmpeg error'
                if not recover_or_skip(i, c, video, detail): break
                continue
            retry_counts[i] = 0
            with lock:
                # The playlist may have changed while this video was playing.
                # Find the actual video in the current sorted playlist.
                current_files = c['files']
                c['index'] = (current_files.index(video) + 1) if video in current_files else min(idx, len(current_files))
                c['position'] = 0.0
                if c['index'] >= len(current_files):
                    if c['loop']:
                        c['index'] = 0
                    else:
                        save()
                        break
                save()
        except (OSError, ValueError, TypeError) as exc:
            if not recover_or_skip(i, c, video, str(exc)): break
    with lock:
        processes.pop(i, None)
        progress_paths.pop(i, None)
        bitrate_samples.pop(i, None)
        channel_mbps[i] = 0.0
        run_start.pop(i, None)
        run_base.pop(i, None)
        workers.pop(i, None)
        stop_flags.discard(i)
        continuous_disabled.discard(i)
        retry_counts.pop(i, None)
        switch_requests.pop(i, None)
        if states.get(i) != 'ERROR':
            states[i] = 'STOPPED'
    save()


def start_channel(i):
    with lock:
        if i in workers:
            return
        if not channels[i]['files']:
            set_state(i, 'ERROR', 'Add a video or folder first')
            return
        stop_flags.discard(i)
        continuous_disabled.discard(i)
        retry_counts.pop(i, None)
        switch_requests.pop(i, None)
        set_state(i, 'STARTING')
        t = threading.Thread(target=work, args=(i,), daemon=True)
        workers[i] = t
        t.start()


def stop_channel(i):
    with lock:
        stop_flags.add(i)
        switch_requests.pop(i, None)
        p = processes.get(i)
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
        states[i] = 'STOPPED'


def switch_video(i, index):
    """Switch a single running channel without disturbing other channels."""
    with lock:
        c = channels[i]
        if not c['files']:
            return
        target = int(index) % len(c['files'])
        c['index'] = target
        c['position'] = 0.0
        if i in workers:
            switch_requests[i] = target
            p = processes.get(i)
            if p and p.poll() is None:
                try:
                    p.terminate()
                except OSError:
                    pass
        else:
            start_channel(i)
        save()


def checkpoint():
    # Save elapsed time based on each process's own start marker, not wall clock downtime.
    with lock:
        for i, p in list(processes.items()):
            if p.poll() is None and i in run_start and i not in switch_requests:
                elapsed = max(0, time.monotonic() - run_start[i])
                channels[i]['position'] = run_base.get(i, 0.0) + elapsed
        atomic_json(CONFIG, channels)
    root.after(3000, checkpoint)


settings = {}
for settings_path in (SETTINGS, SETTINGS_BACKUP):
    try:
        candidate = json.loads(settings_path.read_text(encoding='utf-8'))
        if isinstance(candidate, dict):
            settings = candidate
            break
    except (OSError, ValueError):
        continue

def save_settings():
    atomic_json(SETTINGS_BACKUP, settings)
    atomic_json(SETTINGS, settings)

run_start, run_base = {}, {}

# Windows per-user startup: no Administrator rights required.
def configure_windows_startup(enabled):
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return
    try:
        import winreg
        key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            if enabled:
                winreg.SetValueEx(key, 'AMIR_SAT_TV_Playout', 0, winreg.REG_SZ, '"%s"' % sys.executable)
            else:
                try: winreg.DeleteValue(key, 'AMIR_SAT_TV_Playout')
                except FileNotFoundError: pass
    except OSError:
        pass

settings.setdefault('backup_dir', str(HOME / 'Exports'))
settings.setdefault('auto_start_channels', True)
settings.setdefault('start_with_windows', True)


# AMIR SAT desktop interface (Tkinter / Windows 8 compatible)
root = tk.Tk()
root.title('AMIR SAT - TV Playout (UDP) - 32 Channels')
root.geometry('1536x900')
# Use explicit widget fonts for cross-platform compatibility.
root.minsize(1050, 690)
BG, NAVY, BLUE = '#030c1b', '#06172e', '#087bce'
root.configure(bg=BG)
style = ttk.Style()
try: style.theme_use('clam')
except tk.TclError: pass
style.configure('Treeview', rowheight=34, borderwidth=0, font=('Segoe UI', 9), background='#102943', fieldbackground='#102943', foreground='#f0f6ff')
style.map('Treeview', background=[('selected', '#087bce')], foreground=[('selected', '#ffffff')])
style.configure('Treeview.Heading', relief='flat', padding=(8, 10), font=('Segoe UI', 9, 'bold'), background='#123b64', foreground='#ffffff')
style.configure('TButton', font=('Segoe UI', 9, 'bold'), padding=(10, 8), background='#134d7e', foreground='#ffffff')
style.map('TButton', background=[('active', '#087bce')])
style.configure('TNotebook', background=BG)
style.configure('TNotebook.Tab', background='#143c61', foreground='#ffffff')
style.map('TNotebook.Tab', background=[('selected', '#087bce')])
style.configure('TFrame', background=BG)
style.configure('TLabel', background=BG, foreground='#e8f3ff')
style.configure('TCheckbutton', background=BG, foreground='#e8f3ff')
style.configure('TNotebook.Tab', padding=(15, 10))
style.configure('TCombobox', padding=(6, 5))
style.configure('TEntry', padding=(6, 5))
# Icons are bundled with the source and with the frozen executable.
UI_ICONS = {}
try:
    from PIL import Image, ImageTk
    icon_root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'ui_icons'
    for icon_file in icon_root.glob('*.png'):
        UI_ICONS[icon_file.stem] = ImageTk.PhotoImage(Image.open(icon_file).convert('RGBA').resize((25, 25), Image.Resampling.LANCZOS))
except Exception:
    pass

def icon_button(parent, label, icon, callback, *, color='#092f5c', side='left', padx=3, pady=3, **kwargs):
    options = dict(text=label, command=callback, bg=color, fg='white',
                   activebackground='#095fc1', activeforeground='white',
                   font=('Segoe UI', 9, 'bold'), relief='flat', bd=0,
                   highlightthickness=2, highlightbackground='#08bafa',
                   padx=12, pady=10, cursor='hand2')
    if icon in UI_ICONS:
        options.update(image=UI_ICONS[icon], compound='left')
    pack_options = {k:kwargs.pop(k) for k in ('fill','expand','anchor') if k in kwargs}
    options.update(kwargs)
    btn = tk.Button(parent, **options)
    btn.pack(side=side, padx=padx, pady=pady, **pack_options)
    return btn

selected = 0
banner = tk.Frame(root, bg=NAVY, height=84)
banner.pack(fill='x'); banner.pack_propagate(False)
try:
    from PIL import Image, ImageTk
    image_file = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'amir_sat_banner.png'
    banner_img = Image.open(str(image_file))
    banner_photo = None
    banner_label = tk.Label(banner, bg=NAVY)
    banner_label.pack(side='left', fill='y')
    def resize_banner(event):
        global banner_photo
        if event.width > 10 and event.height > 10:
            banner_photo = ImageTk.PhotoImage(banner_img.resize((min(345, max(100, event.width)), event.height), Image.LANCZOS))
            banner_label.configure(image=banner_photo)
    banner.bind('<Configure>', resize_banner)
except Exception:
    tk.Label(banner, text='◉ AMIR SAT  |  DIGITAL TV PLAYOUT  |  Support: 01008566858',
             font=('Segoe UI', 20, 'bold'), fg='#ffcc55', bg=NAVY).pack(expand=True)

top = tk.Frame(root, bg='#123451', pady=1)
top.pack_forget()
tk.Label(top, text='AMIR SAT  |  32 CHANNELS UDP MPEG-TS PLAYOUT', bg='#123451',
         fg='#e8f3ff', font=('Segoe UI', 11, 'bold')).pack(side='left', padx=14)
status_var = tk.StringVar(value='Ready')
tk.Label(top, textvariable=status_var, bg='#123451', fg='#155c91', font=('Segoe UI', 9)).pack(side='right', padx=12)

# Navigation and performance summary, matching the approved AMIR SAT dashboard.
nav = tk.Frame(banner, bg='#08182d', pady=8)
nav.pack(side='right', fill='both', expand=True)
def nav_action(title):
    if title in ('Settings', 'Tools'):
        global_settings()
    elif title == 'Playlists':
        notebook.select(playlist_tab)
    elif title in ('Channels', 'Home'):
        notebook.select(settings_tab)
    elif title == 'Monitor':
        show_error_log()
    else:
        messagebox.showinfo('AMIR SAT v3.1', '32-channel UDP MPEG-TS playout')
for title, symbol in [('Home','home'),('Channels','channels'),('Playlists','playlist'),
                      ('Settings','settings'),('Tools','tools'),('Monitor','monitor'),('About','about')]:
    icon_button(nav, title, symbol, lambda t=title: nav_action(t),
                padx=5, pady=4, fill='both', expand=True, width=100)
metrics = tk.Frame(root, bg=BG, pady=8)
metrics.pack(fill='x')
metric_vars = {}
for key, title in [('cpu','CPU'),('ram','RAM'),('disk','HDD'),('bitrate','Total Output Bitrate'),('clock','Time')]:
    cell = tk.Frame(metrics, bg='#102d4c', padx=14, pady=9,
                    highlightbackground='#087bce', highlightthickness=2)
    cell.pack(side='left', expand=True, fill='x', padx=6)
    tk.Label(cell, text=title, bg='#102d4c', fg='#c5e9ff',
             font=('Segoe UI', 9, 'bold')).pack()
    metric_vars[key] = tk.StringVar(value='--')
    tk.Label(cell, textvariable=metric_vars[key], bg='#102d4c', fg='#32ec72',
             font=('Segoe UI', 13, 'bold')).pack()

pane = ttk.PanedWindow(root, orient='horizontal'); pane.pack(fill='both', expand=True, padx=12, pady=7)
left = ttk.Frame(pane); right = ttk.Frame(pane)
# The settings frame is placed first (physical left); the channel workspace is second (right).
# Variable names remain unchanged to preserve existing callbacks and bindings.
pane.add(right, weight=1); pane.add(left, weight=4)
# Keep the settings panel around 24% of the available width.
def position_settings_sash():
    width = pane.winfo_width()
    if width > 200:
        pane.sashpos(0, int(width * 0.24))
root.after(350, position_settings_sash)

header = tk.Frame(left, bg=BG); header.pack(fill='x')
tk.Label(header, text='▣  CHANNELS (32)  |  UDP MPEG-TS', bg=BG, fg='#e8f3ff', font=('Segoe UI', 11, 'bold')).pack(side='left', pady=5)
cols = ('number','name','source','udp','bitrate','status','playing','elapsed','retries')
tree_frame = tk.Frame(left, bg='#087bce', highlightthickness=2, highlightbackground='#087bce'); tree_frame.pack(fill='both', expand=True, pady=(3, 7))
tree = ttk.Treeview(tree_frame, columns=cols, show='headings', selectmode='browse')
for key,title,width in [('number','#',40),('name','Channel Name',132),('source','Source / Folder',180),
                        ('udp','UDP Destination',165),('bitrate','Bitrate Mb/s',110),('status','Status',78),('playing','Current Video',125),('elapsed','Time',75),('retries','Retries',68)]:
    tree.heading(key,text=title); tree.column(key,width=width,minwidth=width if key in ('number','status','retries') else 60,stretch=(key in ('name','source','udp','playing')))
for name,color in [('running','#2ee77e'),('error','#ff5b67'),('stopped','#9fb4c9'),('starting','#ffcb55'),('recovering','#ffcb55')]:
    tree.tag_configure(name,foreground=color)
tree.pack(side='left',fill='both',expand=True)
scroll = ttk.Scrollbar(tree_frame,orient='vertical',command=tree.yview)
scroll.pack(side='right',fill='y'); tree.configure(yscrollcommand=scroll.set)
for i in range(32): tree.insert('', 'end', iid=str(i), values=(i+1,'','','','','',''))

workspace_actions=tk.Frame(left,bg='#0a2342',pady=7);workspace_actions.pack(fill='x')
# Buttons invoke the same playback and file actions used by the original controls.
for title, symbol, fn in [('Play','play',lambda: start_selected()),('Stop','stop',lambda: stop_selected()),
                          ('Add Folder','folder',lambda: add_folder()),('Add Videos','videos',lambda: add_videos()),
                          ('Refresh','refresh',lambda: refresh_playlist())]:
    icon_button(workspace_actions, title, symbol, fn, padx=5, pady=4)
right_header = tk.Frame(left, bg=BG); right_header.pack(fill='x')
tk.Label(right_header,text='VIDEO PREVIEW',bg=BG,fg='#e8f3ff',font=('Segoe UI',10,'bold')).pack(anchor='w',pady=4)
preview_area = tk.Frame(left, bg=BG); preview_area.pack(fill='both', expand=False, padx=5, pady=3)
preview = tk.Frame(preview_area,bg='#09182b',height=205, width=340,highlightthickness=2,highlightbackground='#087bce'); preview.pack(side='left',fill='both',expand=False, padx=(0,5))
preview.pack_propagate(False)
preview_title=tk.StringVar(value='CH 01   •   No video selected')
tk.Label(preview,text='▶',fg='#6aaee6',bg='#09182b',font=('Segoe UI',25)).pack(pady=(4,0))
tk.Label(preview,textvariable=preview_title,fg='white',bg='#09182b',font=('Segoe UI',10)).pack()
tk.Label(preview,text='Preview image (not a second UDP sender)',fg='#9db1c8',bg='#09182b',font=('Segoe UI',8)).pack()
preview_image_label = tk.Label(preview, bg='#09182b')
preview_image_label.pack(expand=True, fill='both')
# Playlist is visible beside the preview, rather than hidden behind a tab.
playlist_dock = tk.Frame(preview_area, bg='#0b2440', highlightbackground='#078bd5', highlightthickness=2)
playlist_dock.pack(side='left',fill='both',expand=True)
tk.Label(playlist_dock, text='PLAYLIST', bg='#123b64', fg='#ffffff', font=('Segoe UI',10,'bold')).pack(fill='x')
quick_playlist = tk.Listbox(playlist_dock, bg='#0a1c32', fg='#e8f3ff', selectbackground='#087bce', font=('Segoe UI',9), height=5, borderwidth=0, exportselection=False)
quick_playlist.pack(fill='both',expand=True,padx=4,pady=3)
quick_playlist_actions=tk.Frame(playlist_dock,bg='#0b2440'); quick_playlist_actions.pack(fill='x')
preview_photo = None
preview_generation = 0

def refresh_preview_frame():
    global preview_generation
    preview_generation += 1
    generation = preview_generation
    with lock:
        c = channels[selected]
        file = c['files'][int(c['index']) % len(c['files'])] if c['files'] else None
    if not file or not Path(file).is_file():
        preview_image_label.configure(image='', text='No video', fg='white')
        return
    ff = ffmpeg_path()
    if not ff:
        preview_image_label.configure(image='', text='FFmpeg not found', fg='white')
        return
    def fetch():
        try:
            with tempfile.TemporaryDirectory() as folder:
                out = str(Path(folder) / 'frame.jpg')
                result = subprocess.run([ff, '-hide_banner', '-loglevel', 'error', '-ss', '1', '-i', file,
                                         '-frames:v', '1', '-vf', 'scale=320:-2', '-y', out],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=12, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if result.returncode != 0 or not Path(out).exists(): return
                data = Path(out).read_bytes()
            def show():
                global preview_photo
                if generation != preview_generation or not preview_image_label.winfo_exists(): return
                try:
                    from PIL import Image, ImageTk
                    import io
                    im = Image.open(io.BytesIO(data)); im.thumbnail((320, 105))
                    preview_photo = ImageTk.PhotoImage(im)
                    preview_image_label.configure(image=preview_photo, text='')
                except (ImportError, OSError, tk.TclError): pass
            root.after(0, show)
        except (OSError, subprocess.TimeoutExpired): pass
    threading.Thread(target=fetch, daemon=True).start()


tk.Label(right, text='SELECTED CHANNEL SETTINGS', bg='#123b64', fg='white', font=('Segoe UI',11,'bold'), pady=8).pack(fill='x')
notebook=ttk.Notebook(right); notebook.pack(fill='both',expand=True,padx=5,pady=7)
settings_tab=ttk.Frame(notebook,padding=10); playlist_tab=ttk.Frame(notebook,padding=10)
logo_tab=ttk.Frame(notebook,padding=10); advanced_tab=ttk.Frame(notebook,padding=10)
notebook.add(settings_tab,text='Channel Settings'); notebook.add(playlist_tab,text='Playlist')
notebook.add(logo_tab,text='Logo & Text'); notebook.add(advanced_tab,text='Channel Options')
fields={}
entries=[('name','Channel Name'),('ip','UDP multicast IP'),('port','UDP Port'),
         ('mode','Video Encoding'),('resolution','Resolution'),('fps','Frame Rate'),
         ('video_kbps','Video Bitrate (Kbps)'),('bitrate_mode','Bitrate Mode'),
         ('audio_codec','Audio Encoding'),('audio_kbps','Audio Bitrate (Kbps)'),
         ('audio_rate','Audio Sample Rate'),('audio_channels','Audio Channels'),('audio_volume','Audio Volume %')]
options={'mode':['copy','libx264','libx265'],'resolution':['Original','1080p','720p','480p'],
         'fps':['Original','24','25','30','50','60'],
         'video_kbps':['Original','500','800','1000','1500','2000','2500','3000','4000','5000','6000','8000'],
         'bitrate_mode':['Original','VBR','CBR'],'audio_codec':['copy','aac','mp2'],'audio_kbps':['Original','64','96','128','160','192','256','320'],
         'audio_rate':['Original','32000','44100','48000'],'audio_channels':['Original','1','2'],
         'audio_volume':['100','50','75','125','150','200']}
for n,(key,label) in enumerate(entries):
    ttk.Label(settings_tab,text=label).grid(row=n,column=0,sticky='w',pady=4)
    v=tk.StringVar(); fields[key]=v
    if key in options:
        widget=ttk.Combobox(settings_tab,textvariable=v,values=options[key],
                            state='normal' if key in ('video_kbps','audio_kbps') else 'readonly')
    else: widget=ttk.Entry(settings_tab,textvariable=v)
    widget.grid(row=n,column=1,sticky='ew',padx=7,pady=3)
settings_tab.columnconfigure(1,weight=1)
ttk.Label(settings_tab,text='Bitrate: select a preset or type any positive Kbps value.',
          foreground='#a7c1d9',wraplength=350).grid(row=len(entries),column=0,columnspan=2,sticky='w',pady=6)

playlist=tk.Listbox(playlist_tab,selectmode='extended',height=13,font=('Segoe UI',9))
playlist.pack(fill='both',expand=True)
logo_fields={}
for n,(key,label) in enumerate([('logo','Logo Image (PNG/JPG)'),('logo_position','Logo Position'),
                                 ('logo_width','Logo Width (pixels)'),('text','Overlay Text')]):
    ttk.Label(logo_tab,text=label).grid(row=n,column=0,sticky='w',pady=8)
    v=tk.StringVar();logo_fields[key]=v
    if key=='logo_position':
        w=ttk.Combobox(logo_tab,textvariable=v,state='readonly',values=['Top Left','Top Right','Bottom Left','Bottom Right','Center'])
    else: w=ttk.Entry(logo_tab,textvariable=v)
    w.grid(row=n,column=1,sticky='ew',padx=7,pady=5)
logo_tab.columnconfigure(1,weight=1)
def browse_logo():
    path=filedialog.askopenfilename(title='Choose channel logo',filetypes=[('Images','*.png *.jpg *.jpeg'),('All','*.*')])
    if path:logo_fields['logo'].set(path)
ttk.Button(logo_tab,text='Browse Logo Image',command=browse_logo).grid(row=4,column=1,sticky='w',pady=8)
ttk.Label(logo_tab,text='Logo and text overlays require video re-encoding.\nUse PNG with transparency for best results.',
          wraplength=380,foreground='#a7c1d9').grid(row=5,column=0,columnspan=2,sticky='w',pady=12)
loop=tk.BooleanVar();resume=tk.BooleanVar()
ttk.Checkbutton(advanced_tab,text='Loop playlist automatically',variable=loop).pack(anchor='w',pady=10)
ttk.Checkbutton(advanced_tab,text='Resume saved file and playback position after power loss',variable=resume).pack(anchor='w',pady=10)
ttk.Label(advanced_tab,text='Each video gets 3 attempts. If it still fails, playback skips to the next video automatically.',wraplength=390,foreground='#8af2a2').pack(anchor='w',pady=8)
ttk.Label(advanced_tab,text='Stream copy uses minimal CPU. Changing resolution, FPS, bitrate or adding a logo/text will re-encode the video.',
          wraplength=390,foreground='#a7c1d9').pack(anchor='w',pady=12)

def apply():
    c=channels[selected]
    proposal=dict(c)
    for k,v in list(fields.items())+list(logo_fields.items()):proposal[k]=v.get().strip()
    proposal['loop']=loop.get();proposal['resume']=resume.get()
    try:
        validate(proposal)
        if proposal['logo_width'] and int(proposal['logo_width']) <= 0:raise ValueError('Logo width must be positive')
    except (ValueError,TypeError) as e:
        messagebox.showerror('Invalid channel settings',str(e));return False
    with lock:c.update(proposal)
    save();return True

def populate():
    c=channels[selected]
    for k,v in list(fields.items())+list(logo_fields.items()):v.set(str(c.get(k,'')))
    loop.set(bool(c['loop']));resume.set(bool(c['resume']))
    playlist.delete(0,'end')
    quick_playlist.delete(0,'end')
    for p in c['files']:
        playlist.insert('end',Path(p).name)
        quick_playlist.insert('end',Path(p).name)
    preview_title.set('CH %02d   •   %s'%(selected+1,Path(c['files'][int(c['index'])%len(c['files'])]).name if c['files'] else 'No video selected'))
    refresh_preview_frame()

def choose(event=None):
    global selected
    sel=tree.selection()
    if not sel:return
    n=int(sel[0])
    if n!=selected:
        if not apply():tree.selection_set(str(selected));return
        selected=n;populate()
tree.bind('<<TreeviewSelect>>',choose)

def add_videos():
    if not apply():return
    paths=filedialog.askopenfilenames(title='Select video files',filetypes=[('Video files','*.mp4 *.mkv *.ts *.mov *.avi'),('All','*.*')])
    if paths:
        with lock:add_sorted_files(channels[selected], paths)
        save();populate()

def add_folder():
    if not apply():return
    folder=filedialog.askdirectory(title='Choose video folder')
    if not folder:return
    paths=sorted((p for p in Path(folder).rglob('*') if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),key=playlist_key)
    with lock:
        add_sorted_files(channels[selected], paths)
    save();populate()
    if not paths:messagebox.showinfo('Folder','No supported videos found')

def remove_videos():
    if selected in workers:messagebox.showwarning('Channel running','Stop channel before editing playlist');return
    with lock:
        for n in reversed(playlist.curselection()):channels[selected]['files'].pop(n)
        channels[selected]['index']=0;channels[selected]['position']=0
    save();populate()

def move_video(delta):
    if selected in workers:return
    idx=playlist.curselection()
    if len(idx)!=1:return
    a=idx[0];b=a+delta;c=channels[selected]
    if not 0<=b<len(c['files']):return
    c['files'][a],c['files'][b]=c['files'][b],c['files'][a]
    save();populate();playlist.selection_set(b)
pbuttons=ttk.Frame(playlist_tab);pbuttons.pack(fill='x',pady=7)
for label,symbol,fn in [('Add Files','videos',add_videos),('Add Folder','folder',add_folder),('Remove','stop',remove_videos),
                 ('Move Up','previous',lambda:move_video(-1)),('Move Down','next',lambda:move_video(1))]:
    icon_button(pbuttons,label,symbol,fn,padx=3,pady=3)

def switch_relative(delta):
    with lock:
        c = channels[selected]
        if not c['files']:
            return
        current = int(switch_requests.get(selected, c['index']))
    switch_video(selected, current + delta)
    populate()


def play_playlist_selection(event=None):
    chosen = quick_playlist.curselection() if event is not None and event.widget is quick_playlist else playlist.curselection()
    if not chosen:
        messagebox.showinfo('Select Video', 'Choose a video from the playlist first.')
        return
    switch_video(selected, chosen[0])
    populate()


navigation = ttk.Frame(playlist_tab)
navigation.pack(fill='x', pady=5)
icon_button(navigation, 'Previous', 'previous', lambda: switch_relative(-1))
icon_button(navigation, 'Play Selected', 'play', play_playlist_selection)
icon_button(navigation, 'Next', 'next', lambda: switch_relative(1))
playlist.bind('<Double-Button-1>', play_playlist_selection)
quick_playlist.bind('<Double-Button-1>', play_playlist_selection)
for label, fn in [('◀◀', lambda: switch_relative(-1)), ('▶ Play Selected', lambda: play_quick_selected()), ('▶▶', lambda: switch_relative(1))]:
    icon_button(quick_playlist_actions,label, 'previous' if label=='◀◀' else ('next' if label=='▶▶' else 'play'),fn,padx=3,pady=3)
def play_quick_selected():
    chosen=quick_playlist.curselection()
    if chosen:
        switch_video(selected,chosen[0]);populate()
    else: messagebox.showinfo('Playlist','Select a video first')

def start_selected():
    if apply():start_channel(selected)
def stop_selected():stop_channel(selected);save()
def start_all():
    if not apply():return
    for i in range(32):
        if channels[i]['files']:start_channel(i)
def stop_all():
    for i in range(32):stop_channel(i)
    save()

def preview_selected():
    if not apply():return
    c=channels[selected]
    if not c['files']:messagebox.showinfo('Preview','Add a video first');return
    file=c['files'][int(c['index'])%len(c['files'])]
    if not Path(file).is_file():messagebox.showerror('Preview','Video not found: '+file);return
    try:os.startfile(file)
    except (AttributeError,OSError) as exc:messagebox.showerror('Preview',str(exc))

def global_settings():
    w=tk.Toplevel(root);w.title('Global Settings / FFmpeg');w.geometry('720x560');w.transient(root)
    ttk.Label(w,text='FFmpeg executable path (optional when bundled):').pack(anchor='w',padx=12,pady=8)
    v=tk.StringVar(value=settings.get('ffmpeg_path',''))
    line=ttk.Frame(w);line.pack(fill='x',padx=12)
    ttk.Entry(line,textvariable=v).pack(side='left',fill='x',expand=True)
    def browse():
        path=filedialog.askopenfilename(filetypes=[('FFmpeg','ffmpeg.exe'),('Executable','*.exe')])
        if path:v.set(path)
    ttk.Button(line,text='Browse',command=browse).pack(side='left',padx=5)
    auto_channels=tk.BooleanVar(value=settings.get('auto_start_channels',True))
    auto_windows=tk.BooleanVar(value=settings.get('start_with_windows',True))
    ttk.Checkbutton(w,text='Automatically start configured channels when the app opens',variable=auto_channels).pack(anchor='w',padx=12,pady=5)
    ttk.Checkbutton(w,text='Start AMIR SAT automatically when Windows starts',variable=auto_windows).pack(anchor='w',padx=12,pady=5)
    def accept():
        settings['auto_start_channels']=auto_channels.get()
        settings['start_with_windows']=auto_windows.get()
        configure_windows_startup(auto_windows.get())
        settings['ffmpeg_path']=v.get().strip();save_settings()
        w.destroy();messagebox.showinfo('FFmpeg','Detected: '+str(ffmpeg_path() or 'NOT FOUND'))
    ttk.Button(w,text='Save / Test FFmpeg',command=accept).pack(pady=8)
    ttk.Separator(w,orient='horizontal').pack(fill='x',padx=12,pady=7)
    ttk.Label(w,text='Channel Settings Backup & Restore',font=('Segoe UI',11,'bold')).pack(anchor='w',padx=12)
    ttk.Label(w,text='Auto-save and two local backup copies are enabled. Export a separate copy too.').pack(anchor='w',padx=12,pady=5)
    backup_dir_var = tk.StringVar(value=settings.get('backup_dir', str(HOME / 'Exports')))
    location = ttk.Frame(w); location.pack(fill='x',padx=12,pady=5)
    ttk.Label(location, text='Backup location:').pack(side='left')
    ttk.Entry(location, textvariable=backup_dir_var).pack(side='left',fill='x',expand=True,padx=6)
    def choose_backup_dir():
        chosen = filedialog.askdirectory(parent=w, title='Choose backup folder')
        if chosen:
            backup_dir_var.set(chosen)
            settings['backup_dir'] = chosen
            save_settings()
    ttk.Button(location,text='Browse',command=choose_backup_dir).pack(side='left')
    actions=ttk.Frame(w);actions.pack(fill='x',padx=12,pady=8)
    def manual_save():
        if apply():
            save_settings()
            messagebox.showinfo('Saved','All 32 channels and global settings saved.',parent=w)
    def export_backup():
        if not apply():return
        target_dir = Path(backup_dir_var.get()).expanduser()
        target_dir.mkdir(parents=True,exist_ok=True)
        path=filedialog.asksaveasfilename(parent=w,initialdir=str(target_dir),defaultextension='.json',initialfile='AMIR_SAT_backup.json',filetypes=[('JSON backup','*.json')])
        if not path:return
        try:
            atomic_json(Path(path),{'format':'AMIR_SAT_BACKUP_V1','channels':channels,'settings':settings})
            messagebox.showinfo('Backup','Backup exported successfully.',parent=w)
        except OSError as exc:messagebox.showerror('Backup failed',str(exc),parent=w)
    def import_backup():
        path=filedialog.askopenfilename(parent=w,initialdir=backup_dir_var.get(),filetypes=[('JSON backup','*.json')])
        if not path:return
        try:
            data=json.loads(Path(path).read_text(encoding='utf-8'))
            if not isinstance(data,dict) or data.get('format')!='AMIR_SAT_BACKUP_V1':raise ValueError('Not an AMIR SAT backup file')
            incoming=data.get('channels')
            if not isinstance(incoming,list) or len(incoming)!=32 or not all(isinstance(x,dict) for x in incoming):raise ValueError('Invalid channel data')
            incoming_settings=data.get('settings')
            if not isinstance(incoming_settings,dict):raise ValueError('Invalid global settings')
            if not messagebox.askyesno('Restore','Replace current channel settings with this backup? Running channels will be stopped.',parent=w):return
            stop_all()
            with lock:
                channels[:]=[{**defaults(i),**incoming[i]} for i in range(32)]
                settings.clear();settings.update(incoming_settings)
                save();save_settings()
            populate();refresh()
            messagebox.showinfo('Restored','Settings restored. Restart channels when ready.',parent=w)
        except (OSError,ValueError,TypeError) as exc:messagebox.showerror('Restore failed',str(exc),parent=w)
    for title,action in [('Save Settings',manual_save),('Backup Settings',export_backup),('Restore Settings',import_backup)]:
        ttk.Button(actions,text=title,command=action).pack(side='left',padx=5)
    ttk.Label(w,text='Automatic rolling backups: '+str(HOME),wraplength=660).pack(anchor='w',padx=12,pady=8)

def show_error_log():
    if not last_error_log.exists():
        messagebox.showinfo('FFmpeg Errors', 'No errors logged yet.')
        return
    w = tk.Toplevel(root)
    w.title('FFmpeg Error Log - AMIR SAT')
    w.geometry('850x480')
    area = tk.Text(w, wrap='word', bg='#071627', fg='#e8f3ff')
    area.pack(fill='both', expand=True)
    area.insert('1.0', last_error_log.read_text(encoding='utf-8', errors='replace')[-30000:])
    area.configure(state='disabled')

def save_all():
    if apply():messagebox.showinfo('Saved','All channel settings saved successfully')

controls=tk.Frame(root,bg='#123451',pady=4);controls.pack(fill='x')
for label,symbol,fn in [('Add Folder','folder',add_folder),('Add Files','videos',add_videos),
                        ('Preview','preview',preview_selected),('Start Channel','play',start_selected),
                        ('Stop Channel','stop',stop_selected),('Start All','all',start_all),
                        ('Stop All','stop',stop_all),('Save All','save',save_all),
                        ('Settings','settings',global_settings),('Error Log','errors',show_error_log),
                        ('Exit','exit',lambda:close())]:
    icon_button(controls,label,symbol,fn,padx=2,pady=2, font=('Segoe UI',8,'bold'))
def quick_backup():
    if not apply(): return
    directory=Path(settings.get('backup_dir', str(HOME/'Exports')))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target=directory/('AMIR_SAT_%s.json' % time.strftime('%Y%m%d_%H%M%S'))
        with lock:
            atomic_json(target, {'format':'AMIR_SAT_BACKUP_V1','channels':channels,'settings':settings})
        messagebox.showinfo('Backup created',str(target))
    except (OSError,ValueError) as exc:
        messagebox.showerror('Backup failed',str(exc))

def quick_restore():
    path = filedialog.askopenfilename(title='Restore AMIR SAT backup',
                                      initialdir=settings.get('backup_dir',str(HOME/'Exports')),
                                      filetypes=[('JSON backup','*.json')])
    if not path: return
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(data,dict) or data.get('format') != 'AMIR_SAT_BACKUP_V1':
            raise ValueError('Not an AMIR SAT backup')
        incoming = data.get('channels')
        if not isinstance(incoming,list) or len(incoming) != 32 or not all(isinstance(x,dict) for x in incoming):
            raise ValueError('Backup must contain 32 channels')
        if not isinstance(data.get('settings'),dict):
            raise ValueError('Invalid backup settings')
        if not messagebox.askyesno('Restore backup','Stop all channels and replace current settings?'): return
        stop_all()
        # Wait for workers to finish so old workers cannot overwrite restored settings.
        for worker in list(workers.values()): worker.join(timeout=3)
        with lock:
            channels[:] = [{**defaults(i), **incoming[i]} for i in range(32)]
            settings.clear(); settings.update(data['settings'])
            save(); save_settings()
        populate()
        messagebox.showinfo('Backup restored','Settings restored. Start channels when ready.')
    except (OSError,ValueError,TypeError) as exc:
        messagebox.showerror('Restore failed',str(exc))

def quick_backup_location():
    folder = filedialog.askdirectory(title='Backup location', initialdir=settings.get('backup_dir',str(HOME)))
    if folder:
        settings['backup_dir'] = folder
        save_settings()


def refresh_playlist():
    if not apply(): return
    with lock:
        c=channels[selected]
        # Re-scan known source directories and include new videos in subfolders.
        roots={str(Path(f).parent) for f in c['files']}
        new=[]
        for folder in roots:
            path=Path(folder)
            if path.is_dir():
                new.extend(p for p in path.rglob('*') if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)
        add_sorted_files(c,new)
    save();populate()

# Reserve bottom space before the expanding channel pane so backup controls stay visible.
pane.pack_forget()
footer=tk.Frame(root,bg=NAVY,pady=7,highlightbackground='#f2bd32',highlightthickness=2)
footer.pack(side='bottom',fill='x')
pane.pack(fill='both',expand=True,padx=12,pady=7)
tk.Label(footer,text='◈ BACKUP & RESTORE',fg='#ffd45a',bg=NAVY,font=('Segoe UI',11,'bold')).pack(side='left',padx=9)
for label, symbol, action in [('Backup History','history',lambda: global_settings()),
                              ('Backup Location','location',quick_backup_location),
                              ('Restore','restore',quick_restore),('Backup Now','backup',quick_backup),
                              ('Auto Backup','auto_backup',lambda: global_settings())]:
    icon_button(footer,label,symbol,action,side='right',padx=3,pady=2)
footer_var=tk.StringVar(value='Running: 0     Stopped: 32     Idle: 32')
resource_var=tk.StringVar(value='CPU Load: --%    Memory: --%')
tk.Label(footer,textvariable=resource_var,fg='#8af2a2',bg=NAVY,font=('Segoe UI',9,'bold')).pack(side='left',padx=12)
try:
    import psutil
    psutil.cpu_percent(interval=None)
except ImportError:
    psutil=None
tk.Label(footer,textvariable=footer_var,fg='white',bg=NAVY,font=('Segoe UI',9,'bold')).pack(side='left',padx=15)
tk.Label(footer,text='AMIR SAT   •   Technical Support: 01008566858',fg='#c8def4',bg=NAVY).pack(side='right',padx=15)

def read_progress_bytes(path):
    """Read the last complete FFmpeg progress snapshot without blocking UI."""
    try:
        # Progress files are small and truncated on each new process launch.
        raw = path.read_bytes()[-8192:].decode('ascii', 'replace')
    except (OSError, ValueError):
        return None
    values = re.findall(r'^total_size=(\d+)\s*$', raw, flags=re.MULTILINE)
    return int(values[-1]) if values else None


def update_bitrate_measurements():
    """Measure actual muxed output bytes over elapsed wall-clock time.

    This is sender-side output, not a receiver-confirmed network delivery metric.
    """
    now = time.monotonic()
    with lock:
        active = dict(progress_paths)
    for i, path in active.items():
        total = read_progress_bytes(path)
        if total is None:
            continue
        with lock:
            if progress_paths.get(i) != path:
                continue
            previous = bitrate_samples.get(i)
            if previous is not None:
                old_path, old_bytes, old_time = previous
                elapsed = now - old_time
                if old_path == path and elapsed > 0.5 and total >= old_bytes:
                    channel_mbps[i] = (total - old_bytes) * 8 / elapsed / 1_000_000
            bitrate_samples[i] = (path, total, now)
    with lock:
        for i in list(channel_mbps):
            if i not in active:
                channel_mbps[i] = 0.0
        return dict(channel_mbps)


def refresh():
    measured = update_bitrate_measurements()
    with lock:
        running=0;stopped=0
        for i,c in enumerate(channels):
            st=states.get(i,'STOPPED')
            if st=='RUNNING':running+=1
            if st=='STOPPED':stopped+=1
            icon={'RUNNING':'● RUNNING','ERROR':'● ERROR','STARTING':'● STARTING','STOPPED':'● STOPPED','RECOVERING':'● RETRY'}.get(st,st)
            idx=int(c.get('index',0))
            playing=Path(c['files'][idx%len(c['files'])]).name if c['files'] else '-'
            source=str(Path(c['files'][0]).parent) if c['files'] else '-'
            elapsed=int(float(c.get('position',0)))
            clock='%02d:%02d:%02d'%(elapsed//3600,(elapsed//60)%60,elapsed%60)
            tree.item(str(i),values=(i+1,c['name'],source,'%s:%s'%(c['ip'],c['port']),('%.2f' % measured.get(i, 0.0) if st=='RUNNING' else '0.00'),icon,playing,clock,retry_counts.get(i,0)),tags=(st.lower(),))
        status='Running: %d / 32   |   Selected: CH %02d'%(running,selected+1)
        if states.get(selected)=='ERROR':status+='  |  ERROR: '+errors.get(selected,'')[:130]
        status_var.set(status)
        if psutil is not None:
            resource_var.set('CPU Load: %.0f%%    Memory: %.0f%%' % (psutil.cpu_percent(interval=None),psutil.virtual_memory().percent))
        footer_var.set('Running: %d     Stopped: %d     Configured: %d / 32'%(running,stopped,sum(bool(c['files']) for c in channels)))
    metric_vars['clock'].set(time.strftime('%H:%M:%S'))
    if psutil is not None:
        metric_vars['cpu'].set('%.0f%%' % psutil.cpu_percent(interval=None))
        metric_vars['ram'].set('%.0f%%' % psutil.virtual_memory().percent)
        try: metric_vars['disk'].set('%.0f%%' % psutil.disk_usage(str(HOME.anchor)).percent)
        except (OSError, ValueError): pass
    metric_vars['bitrate'].set('%.2f Mb/s' % sum(measured.get(i, 0.0) for i in range(32) if states.get(i)=='RUNNING'))
    root.after(3000,refresh)

def close():
    if not apply():return
    stop_all();save();root.destroy()
root.protocol('WM_DELETE_WINDOW',close)
tree.selection_set('0');populate();refresh();root.after(3000,checkpoint)
# Register startup on the installed executable, and begin only configured channels.
configure_windows_startup(settings.get('start_with_windows', True))
def auto_start_configured_channels():
    if settings.get('auto_start_channels', True):
        for i, channel in enumerate(channels):
            if channel.get('files'):
                start_channel(i)
root.after(1200, auto_start_configured_channels)
root.mainloop()
