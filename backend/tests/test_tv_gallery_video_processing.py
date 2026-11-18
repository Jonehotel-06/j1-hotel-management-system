"""Bounded media processing checks using synthetic video, never hotel content."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest import skipUnless
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.tv_rooms_gallery import media, storage, video_processing
from apps.tv_rooms_gallery.video_processing import VideoProcessingError


class VideoProcessingTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gallery-video-tests-')
        self.env = patch.dict(os.environ, {'TV_GALLERY_STORAGE_ROOT': self.tmp.name})
        self.env.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.env.stop)
        self.key, _ = storage.save_chunks([b'private source'], 100)

    def test_missing_binary_fails_closed_without_output(self):
        with patch('apps.tv_rooms_gallery.video_processing.shutil.which', return_value=None):
            with self.assertRaises(VideoProcessingError):
                video_processing.make_video(self.key)
        self.assertEqual(os.listdir(self.tmp.name), [self.key])

    def test_timeout_is_translated_to_processing_error(self):
        with patch('apps.tv_rooms_gallery.video_processing.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('ffmpeg', 1)):
            with self.assertRaisesMessage(VideoProcessingError, 'time limit'):
                video_processing._run(['ffmpeg'], 1)

    @skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe required')
    def test_rejects_invalid_or_oversized_metadata_before_encoding(self):
        inputs = [
            {'format': {'duration': '301'}, 'streams': [{'codec_type': 'video', 'width': 320, 'height': 240}]},
            {'format': {'duration': '10'}, 'streams': [{'codec_type': 'video', 'width': 10000, 'height': 10000}]},
            {'format': {'duration': 'NaN'}, 'streams': [{'codec_type': 'video', 'width': 320, 'height': 240}]},
            {'format': {}, 'streams': []},
        ]
        for data in inputs:
            with self.subTest(data=data), patch('apps.tv_rooms_gallery.video_processing._run', return_value=json.dumps(data).encode()) as run:
                with self.assertRaises(VideoProcessingError):
                    video_processing.make_video(self.key)
                self.assertEqual(run.call_count, 1)
        self.assertEqual(os.listdir(self.tmp.name), [self.key])

    @skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe required')
    def test_h264_mpeg4_hevc_vp8_vp9_inputs_make_h264_and_poster(self):
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        encoders = subprocess.check_output([ffmpeg, '-hide_banner', '-encoders'], stderr=subprocess.DEVNULL, text=True)
        cases = [('libx264', 'h264', 'mp4'), ('mpeg4', 'mpeg4', 'mp4'), ('libx265', 'hevc', 'mp4'),
                 ('libvpx', 'vp8', 'webm'), ('libvpx-vp9', 'vp9', 'webm')]
        for encoder, input_codec, extension in cases:
            with self.subTest(input_codec=input_codec):
                if encoder not in encoders:
                    continue
                path = Path(self.tmp.name) / ('source.' + extension)
                args = [ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10',
                        '-t', '1', '-an', '-c:v', encoder, '-threads', '1']
                if encoder == 'libx265':
                    args += ['-x265-params', 'pools=1:frame-threads=1:log-level=error']
                args += ['-y', str(path)]
                subprocess.run(args, check=True, timeout=45, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                with path.open('rb') as source:
                    original, _ = storage.save_chunks(iter(lambda: source.read(65536), b''), 1024 * 1024)
                path.unlink()
                output = thumb = ''
                try:
                    mime = 'video/mp4' if extension == 'mp4' else 'video/webm'
                    self.assertEqual(media.verify_av(storage.path_for(original), 'video', mime), (True, ''))
                    output, thumb = video_processing.make_video(original)
                    info = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-show_entries',
                        'stream=codec_name,codec_type,width,height:format=duration', '-of', 'json',
                        str(storage.path_for(output))], timeout=15))
                    video = [s for s in info['streams'] if s['codec_type'] == 'video'][0]
                    self.assertEqual(video['codec_name'], 'h264')
                    self.assertLessEqual(video['width'], 1280); self.assertLessEqual(video['height'], 720)
                    self.assertTrue(storage.path_for(thumb).read_bytes().startswith(b'\xff\xd8\xff'))
                    self.assertGreaterEqual(float(info['format']['duration']), .9)
                finally:
                    for key in (original, output, thumb):
                        storage.remove(key)
        self.assertEqual(os.listdir(self.tmp.name), [self.key])
