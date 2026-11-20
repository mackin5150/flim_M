"""FFmpeg assembly: exact trims, reorder, mute and consistent export encoding."""
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse
import httpx


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise ValueError('Video processing failed: ' + result.stderr[-800:])
    return result.stdout


def download(url, destination):
    # Only provider-returned HTTPS media; redirects are checked independently.
    for _ in range(5):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or not (parsed.hostname.endswith('.hailuoai.com') or parsed.hostname.endswith('.minimax.io') or parsed.hostname.endswith('.minimaxi.com')):
            raise ValueError('Unrecognized provider media host; download requires an adapter update')
        with httpx.stream('GET', url, timeout=180) as response:
            if response.is_redirect:
                from urllib.parse import urljoin
                url = urljoin(url, response.headers['location'])
                continue
            response.raise_for_status()
            size = 0
            with destination.open('wb') as output:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 300 * 1024 * 1024:
                        raise ValueError('Generated clip exceeds 300 MB')
                    output.write(chunk)
        return
    raise ValueError('Too many media redirects')


def duration(path):
    info = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]))
    if not any(s['codec_type'] == 'video' for s in info['streams']):
        raise ValueError('No video stream')
    return float(info['format']['duration']), any(s['codec_type'] == 'audio' for s in info['streams'])


def assemble(items, output: Path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise ValueError('Install FFmpeg and ffprobe for video export')
    import tempfile
    with tempfile.TemporaryDirectory(dir=output.parent) as folder:
        folder = Path(folder)
        segments = []
        for i, item in enumerate(items):
            path = item['path']
            available, has_audio = duration(path)
            start = float(item.get('start', 0))
            end = float(item.get('end', available))
            if not 0 <= start < end <= available + .1:
                raise ValueError('Trim points exceed the clip duration')
            args = ['ffmpeg', '-v', 'error', '-y', '-ss', str(start), '-i', str(path)]
            if not has_audio or item.get('mute'):
                args += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
            args += ['-map', '0:v:0', '-map', '1:a:0' if not has_audio or item.get('mute') else '0:a:0',
                     '-t', str(end - start), '-vf', 'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1',
                     '-r', '24', '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
                     '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-af', 'apad', '-shortest']
            segment = folder / f'{i}.mp4'
            run(args + [str(segment)])
            segments.append(segment)
        listing = folder / 'concat.txt'
        listing.write_text('\n'.join(f"file '{p.name}'" for p in segments))
        run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '1', '-i', str(listing), '-c', 'copy', '-movflags', '+faststart', str(output)])
