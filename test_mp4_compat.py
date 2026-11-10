"""
Tests for MP4/Apple-playback normalization.

WebM (VP8/VP9/AV1 video, Opus/Vorbis audio) does not decode on iPhone or macOS,
so downloads are steered towards H.264/AAC at format-selection time and
repaired afterwards when a site only served WebM.
"""

import os
import shutil
import subprocess
import tempfile
from unittest.mock import patch

import pytest

from video_downloader import VideoDownloader


FFMPEG = shutil.which('ffmpeg')
FFPROBE = shutil.which('ffprobe')
needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed"
)


# --- Format selection ---------------------------------------------------------

def test_format_selector_prefers_apple_codecs_first():
    """The avc1/mp4a tiers must come before any codec-agnostic tier."""
    downloader = VideoDownloader(force_mp4=True)
    with patch.object(VideoDownloader, 'has_ffmpeg', True):
        fmt = downloader._format_selector('youtube')

    assert 'vcodec^=avc1' in fmt
    assert 'acodec^=mp4a' in fmt
    # The first tier is the strictest Apple-compatible one.
    assert fmt.split('/')[0].startswith('bv*[vcodec^=avc1]')
    # A codec-agnostic fallback still exists, so nothing becomes undownloadable.
    assert fmt.rstrip().endswith('b')


def test_format_selector_without_force_mp4_has_no_codec_filter():
    downloader = VideoDownloader(force_mp4=False)
    with patch.object(VideoDownloader, 'has_ffmpeg', True):
        fmt = downloader._format_selector('youtube')

    assert 'vcodec^=avc1' not in fmt
    assert 'acodec^=mp4a' not in fmt


def test_format_selector_without_ffmpeg_prefers_progressive_mp4():
    """With no ffmpeg nothing can be merged or repaired, so ask for mp4 up front."""
    downloader = VideoDownloader(force_mp4=True)
    with patch.object(VideoDownloader, 'has_ffmpeg', False):
        fmt = downloader._format_selector('youtube')

    assert fmt.split('/')[0].startswith('b[ext=mp4]')
    # No merge syntax, which would need ffmpeg.
    assert '+ba' not in fmt


def test_format_selectors_are_valid_ytdlp_syntax():
    """A malformed selector would break every download, so parse it for real."""
    from yt_dlp import YoutubeDL

    for force_mp4 in (True, False):
        for has_ffmpeg in (True, False):
            downloader = VideoDownloader(force_mp4=force_mp4)
            with patch.object(VideoDownloader, 'has_ffmpeg', has_ffmpeg):
                fmt = downloader._format_selector('youtube')
            YoutubeDL({'quiet': True}).build_format_selector(fmt)


# --- Post-download normalization (command construction, no ffmpeg needed) ------

def _run_normalize(tmp_path, video_codec, audio_codec, container, pix_fmt='yuv420p'):
    """Drive _ensure_ios_compatible_video with faked probes; return the ffmpeg argv."""
    downloader = VideoDownloader(download_dir=str(tmp_path), force_mp4=True)
    src = os.path.join(str(tmp_path), f'clip.{container}')
    with open(src, 'wb') as handle:
        handle.write(b'not really a video')

    def fake_probe(filepath, entries, select_streams=None):
        del filepath, select_streams
        if 'format_name' in entries:
            fmt = 'mov,mp4,m4a,3gp,3g2,mj2' if container == 'mp4' else 'matroska,webm'
            return {'format_name': fmt}
        if 'pix_fmt' in entries:
            return {} if video_codec is None else {
                'codec_name': video_codec, 'pix_fmt': pix_fmt
            }
        return {} if audio_codec is None else {'codec_name': audio_codec}

    captured = {}

    def fake_run(cmd, **kwargs):
        del kwargs
        captured['cmd'] = cmd
        # Materialize the output file so the success path is taken.
        open(cmd[-1], 'wb').close()
        return subprocess.CompletedProcess(cmd, 0, '', '')

    with patch.object(downloader, '_ffmpeg_binary', return_value='/usr/bin/ffmpeg'), \
         patch.object(downloader, '_ffprobe_binary', return_value='/usr/bin/ffprobe'), \
         patch.object(downloader, '_ffprobe_entries', side_effect=fake_probe), \
         patch('video_downloader.subprocess.run', side_effect=fake_run):
        result = downloader._ensure_ios_compatible_video(src, 'youtube')

    return captured.get('cmd', []), result


def test_webm_vp9_opus_is_transcoded_to_h264_aac(tmp_path):
    cmd, result = _run_normalize(tmp_path, 'vp9', 'opus', 'webm')

    assert '-c:v' in cmd and cmd[cmd.index('-c:v') + 1] == 'libx264'
    assert '-c:a' in cmd and cmd[cmd.index('-c:a') + 1] == 'aac'
    assert result.endswith('.mp4'), "output must be .mp4, never .webm"


def test_h264_with_opus_audio_copies_video_and_only_fixes_audio(tmp_path):
    """Re-encoding video here would be a pointless quality and CPU loss."""
    cmd, result = _run_normalize(tmp_path, 'h264', 'opus', 'mkv')

    assert cmd[cmd.index('-c:v') + 1] == 'copy'
    assert cmd[cmd.index('-c:a') + 1] == 'aac'
    assert result.endswith('.mp4')


def test_compatible_mp4_is_only_remuxed(tmp_path):
    cmd, result = _run_normalize(tmp_path, 'h264', 'aac', 'mp4')

    assert cmd[cmd.index('-c:v') + 1] == 'copy'
    assert cmd[cmd.index('-c:a') + 1] == 'copy'
    assert '+faststart' in cmd
    assert result.endswith('.mp4')


def test_hevc_is_retagged_hvc1_for_apple(tmp_path):
    """Apple refuses HEVC tagged hev1; hvc1 is the same bitstream."""
    cmd, _ = _run_normalize(tmp_path, 'hevc', 'aac', 'mp4')

    assert cmd[cmd.index('-c:v') + 1] == 'copy'
    assert '-tag:v' in cmd and cmd[cmd.index('-tag:v') + 1] == 'hvc1'


def test_ten_bit_h264_is_transcoded(tmp_path):
    """yuv420p10le decodes nowhere on iOS even though the codec is H.264."""
    cmd, _ = _run_normalize(tmp_path, 'h264', 'aac', 'mp4', pix_fmt='yuv420p10le')

    assert cmd[cmd.index('-c:v') + 1] == 'libx264'
    assert cmd[cmd.index('-pix_fmt') + 1] == 'yuv420p'


def test_video_without_audio_track_is_handled(tmp_path):
    cmd, result = _run_normalize(tmp_path, 'vp9', None, 'webm')

    assert cmd[cmd.index('-c:v') + 1] == 'libx264'
    assert result.endswith('.mp4')


# --- Safety: normalization must never lose the download -----------------------

def test_force_mp4_disabled_is_a_no_op(tmp_path):
    downloader = VideoDownloader(download_dir=str(tmp_path), force_mp4=False)
    src = os.path.join(str(tmp_path), 'clip.webm')
    open(src, 'wb').close()

    assert downloader._ensure_ios_compatible_video(src, 'youtube') == src


def test_missing_ffmpeg_returns_original_file(tmp_path):
    downloader = VideoDownloader(download_dir=str(tmp_path), force_mp4=True)
    src = os.path.join(str(tmp_path), 'clip.webm')
    open(src, 'wb').close()

    with patch.object(downloader, '_ffmpeg_binary', return_value=None):
        assert downloader._ensure_ios_compatible_video(src, 'youtube') == src


def test_ffprobe_failure_returns_original_file(tmp_path):
    downloader = VideoDownloader(download_dir=str(tmp_path), force_mp4=True)
    src = os.path.join(str(tmp_path), 'clip.webm')
    open(src, 'wb').close()

    with patch.object(downloader, '_ffmpeg_binary', return_value='/usr/bin/ffmpeg'), \
         patch.object(downloader, '_ffprobe_binary', return_value='/usr/bin/ffprobe'), \
         patch.object(downloader, '_ffprobe_entries', return_value=None):
        assert downloader._ensure_ios_compatible_video(src, 'youtube') == src


def test_ffmpeg_failure_keeps_the_original_download(tmp_path):
    """A failed conversion must fall back, not leave the user with nothing."""
    downloader = VideoDownloader(download_dir=str(tmp_path), force_mp4=True)
    src = os.path.join(str(tmp_path), 'clip.webm')
    with open(src, 'wb') as handle:
        handle.write(b'payload')

    def failing_run(cmd, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(cmd, 1, '', 'ffmpeg exploded')

    with patch.object(downloader, '_ffmpeg_binary', return_value='/usr/bin/ffmpeg'), \
         patch.object(downloader, '_ffprobe_binary', return_value='/usr/bin/ffprobe'), \
         patch.object(downloader, '_ffprobe_entries',
                      side_effect=lambda p, e, s=None: (
                          {'format_name': 'matroska,webm'} if 'format_name' in e
                          else {'codec_name': 'vp9', 'pix_fmt': 'yuv420p'} if 'pix_fmt' in e
                          else {'codec_name': 'opus'})), \
         patch('video_downloader.subprocess.run', side_effect=failing_run):
        result = downloader._ensure_ios_compatible_video(src, 'youtube')

    assert result == src
    assert os.path.exists(src), "the original download must survive a failed convert"


# --- End-to-end with real media ----------------------------------------------

@needs_ffmpeg
def test_real_webm_becomes_playable_mp4(tmp_path):
    """Build an actual VP9/Opus WebM and confirm it comes out as H.264/AAC MP4."""
    src = os.path.join(str(tmp_path), 'real.webm')
    subprocess.run(
        [FFMPEG, '-y', '-loglevel', 'error',
         '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=15:duration=1',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
         '-c:v', 'libvpx-vp9', '-b:v', '150k', '-c:a', 'libopus', src],
        check=True, capture_output=True, timeout=180,
    )

    downloader = VideoDownloader(download_dir=str(tmp_path), force_mp4=True)
    out = downloader._ensure_ios_compatible_video(src, 'youtube')

    assert out.endswith('.mp4')
    assert os.path.exists(out)
    assert not os.path.exists(src), "the WebM original should be cleaned up"

    probe = subprocess.run(
        [FFPROBE, '-v', 'error', '-show_entries',
         'stream=codec_name:format=format_name', '-of', 'default=nw=1:nk=1', out],
        capture_output=True, text=True, timeout=60,
    ).stdout
    assert 'h264' in probe
    assert 'aac' in probe
    assert 'mp4' in probe
    assert 'vp9' not in probe and 'opus' not in probe

    # moov atom at the front so playback can start before the file finishes.
    with open(out, 'rb') as handle:
        assert b'moov' in handle.read(2048)
