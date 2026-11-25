import subprocess
import shutil
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import app


class StreamLoadingTests(unittest.TestCase):
    def test_process_error_includes_underlying_hls_diagnostic(self):
        error = (
            b"Master Playlist tag found in a Media Playlist\n"
            b"Error opening input files: Invalid data found when processing input\n"
        )
        with patch(
            "app.subprocess.run",
            return_value=subprocess.CompletedProcess(["ffmpeg"], 1, b"", error),
        ):
            with self.assertRaisesRegex(app.MediaError, "Master Playlist tag"):
                app._run(["ffmpeg"], timeout=1)

    def test_master_retry_preserves_relative_audio_and_video_after_redirect(self):
        url = "http://localhost/master.m3u8"
        base = "http://localhost/asset/hls/master.m3u8"
        response = httpx.Response(
            200,
            request=httpx.Request("GET", base),
            text=(
                "#EXTM3U\n#EXT-X-START:TIME-OFFSET=-18\n"
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",URI="audio/media.m3u8"\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=1000,AUDIO="audio"\n'
                "video/media.m3u8\n"
            ),
        )
        with patch(
            "app._run",
            side_effect=[
                app.MediaError("Master Playlist tag found in a Media Playlist"),
                subprocess.CompletedProcess(["ffmpeg"], 0),
            ],
        ) as run, patch("app.httpx.get", return_value=response):
            app._load_stream(url, "clip.mkv")

        command = run.call_args.args[0]
        playlist = run.call_args.kwargs["input_data"].decode()
        self.assertNotIn("#EXT-X-START:", playlist)
        self.assertIn('URI="http://localhost/asset/hls/audio/media.m3u8"', playlist)
        self.assertIn("http://localhost/asset/hls/video/media.m3u8", playlist)
        self.assertIn('-map', command)
        self.assertIn("0:a:0?", command)
        protocols = command[command.index("-protocol_whitelist") + 1].split(",")
        self.assertNotIn("file", protocols)

    def test_unrelated_failures_do_not_trigger_playlist_retry(self):
        with patch("app._run", side_effect=app.MediaError("Connection refused")), patch(
            "app.httpx.get"
        ) as get:
            with self.assertRaisesRegex(app.MediaError, "Connection refused"):
                app._load_stream("http://localhost/master.m3u8", "clip.mkv")
        get.assert_not_called()

    def test_master_without_start_hint_keeps_original_error(self):
        response = httpx.Response(
            200,
            request=httpx.Request("GET", "http://localhost/master.m3u8"),
            text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nvideo.m3u8\n",
        )
        with patch(
            "app._run",
            side_effect=app.MediaError("Master Playlist tag found in a Media Playlist"),
        ) as run, patch("app.httpx.get", return_value=response):
            with self.assertRaisesRegex(app.MediaError, "Master Playlist tag"):
                app._load_stream("http://localhost/master.m3u8", "clip.mkv")
        self.assertEqual(run.call_count, 1)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class CaptionComparisonTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        (self.path / "captions.srt").write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nHello caption.\n", encoding="utf-8"
        )
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=5:duration=3",
                "-f", "lavfi", "-i", "sine=duration=3",
                "-i", str(self.path / "captions.srt"),
                "-map", "0:v", "-map", "1:a", "-map", "2:s",
                "-c:v", "libx264", "-c:a", "aac", "-c:s", "mov_text",
                str(self.path / "clip.mp4"),
            ],
            check=True,
        )

    def test_uploaded_text_track_and_audio(self):
        with TestClient(app.app) as client, (self.path / "clip.mp4").open("rb") as media:
            response = client.post(
                "/api/demux",
                files={"file": ("clip.mp4", media, "video/mp4")},
                data={"compare_captions": "true"},
            ).json()
        self.assertIn("Hello caption.", response["captions"]["text"])
        self.assertEqual(response["captions"]["source"], "subtitle track")
        self.assertTrue(response["audio"])
        self.assertEqual(response["frames"], [])

    def test_hls_subtitle_rendition_is_retained(self):
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(self.path / "clip.mp4"),
             "-map", "0:v", "-map", "0:a", "-c", "copy", "-f", "mpegts",
             str(self.path / "media.ts")],
            check=True,
        )
        (self.path / "video.m3u8").write_text(
            "#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3,\nmedia.ts\n#EXT-X-ENDLIST\n"
        )
        (self.path / "subtitle.m3u8").write_text(
            "#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3,\nsubtitle.vtt\n#EXT-X-ENDLIST\n"
        )
        (self.path / "subtitle.vtt").write_text(
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:162000\n\n"
            "00:00:02.000 --> 00:00:03.000\nHello HLS caption.\n\n"
            "00:00:03.000 --> 00:00:04.000\nAnother HLS caption.\n\n"
        )
        (self.path / "master.m3u8").write_text(
            '#EXTM3U\n#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",'
            'LANGUAGE="en",DEFAULT=YES,AUTOSELECT=YES,URI="subtitle.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=200000,SUBTITLES="subs"\nvideo.m3u8\n'
        )
        handler = partial(SimpleHTTPRequestHandler, directory=str(self.path))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app.app) as client:
                response = client.post(
                    "/api/demux",
                    data={"url": f"http://127.0.0.1:{server.server_port}/master.m3u8",
                          "compare_captions": "true"},
                ).json()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertNotIn("error", response, response.get("error"))
        self.assertIn("Hello HLS caption.", response["captions"]["text"])
        self.assertTrue(response["audio"])

    def test_missing_audio_reports_error(self):
        no_audio = self.path / "no-audio.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(self.path / "clip.mp4"),
             "-map", "0:v", "-map", "0:s", "-c", "copy", str(no_audio)],
            check=True,
        )
        with TestClient(app.app) as client, no_audio.open("rb") as media:
            response = client.post(
                "/api/demux", files={"file": ("clip.mp4", media)},
                data={"compare_captions": "true"},
            ).json()
        self.assertIn("No audio", response["error"])

    def test_embedded_caption_selection_and_missing_extractor(self):
        info = {"has_video": True, "video_codec": "h264", "subtitles": []}
        no_captions = subprocess.CompletedProcess([], 10, b"", b"No captions")
        caption = subprocess.CompletedProcess([], 0, b"1\ncaption\n", b"")
        with patch("app.shutil.which", return_value="/usr/bin/ccextractor"), \
             patch("app._run"), \
             patch("app.subprocess.run", side_effect=[no_captions, caption]) as run:
            result = app._demux_captions("input.mp4", info, str(self.path))
        self.assertEqual(result["source"], "CEA-608 field 1")
        self.assertIn("--service", run.call_args_list[0].args[0])
        self.assertIn("-1", run.call_args_list[1].args[0])
        with patch("app.shutil.which", return_value=None):
            with self.assertRaisesRegex(app.MediaError, "ccextractor not found"):
                app._demux_captions("input.mp4", info, str(self.path))


if __name__ == "__main__":
    unittest.main()
