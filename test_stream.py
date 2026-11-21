import subprocess
import unittest
from unittest.mock import patch

import httpx

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


if __name__ == "__main__":
    unittest.main()
