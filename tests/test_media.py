"""Tests for media dimension extraction module."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pyhako.media import get_image_dimensions, get_media_dimensions, get_video_dimensions


class TestGetImageDimensions:
    """Tests for get_image_dimensions function."""

    def test_valid_image(self, tmp_path: Path) -> None:
        """Test extracting dimensions from a valid image."""
        # Create a simple 100x50 red image
        from PIL import Image

        img_path = tmp_path / "test.jpg"
        img = Image.new("RGB", (100, 50), color="red")
        img.save(img_path)

        width, height = get_image_dimensions(img_path)

        assert width == 100
        assert height == 50

    def test_valid_png_image(self, tmp_path: Path) -> None:
        """Test extracting dimensions from a PNG image."""
        from PIL import Image

        img_path = tmp_path / "test.png"
        img = Image.new("RGBA", (200, 150), color=(0, 0, 255, 255))
        img.save(img_path)

        width, height = get_image_dimensions(img_path)

        assert width == 200
        assert height == 150

    def test_invalid_file(self, tmp_path: Path) -> None:
        """Test handling of non-image file."""
        txt_path = tmp_path / "test.txt"
        txt_path.write_text("not an image")

        width, height = get_image_dimensions(txt_path)

        assert width is None
        assert height is None

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        """Test handling of missing file."""
        missing_path = tmp_path / "missing.jpg"

        width, height = get_image_dimensions(missing_path)

        assert width is None
        assert height is None

    def test_corrupted_image(self, tmp_path: Path) -> None:
        """Test handling of corrupted image file."""
        corrupt_path = tmp_path / "corrupt.jpg"
        corrupt_path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")  # Truncated JPEG header

        width, height = get_image_dimensions(corrupt_path)

        assert width is None
        assert height is None


class TestGetVideoDimensions:
    """Tests for get_video_dimensions function."""

    def test_valid_video_mock(self) -> None:
        """Test extracting dimensions from a video using mocked MediaInfo."""
        mock_track = MagicMock()
        mock_track.track_type = "Video"
        mock_track.width = 1920
        mock_track.height = 1080

        mock_media_info = MagicMock()
        mock_media_info.tracks = [mock_track]

        mock_mediainfo_class = MagicMock()
        mock_mediainfo_class.parse.return_value = mock_media_info

        with patch.dict("sys.modules", {"pymediainfo": MagicMock(MediaInfo=mock_mediainfo_class)}):
            # Re-import to pick up the mock
            import importlib
            import pyhako.media
            importlib.reload(pyhako.media)

            width, height = pyhako.media.get_video_dimensions(Path("/fake/video.mp4"))

            assert width == 1920
            assert height == 1080

    def test_video_no_video_track(self) -> None:
        """Test video file with no video track (audio only)."""
        mock_track = MagicMock()
        mock_track.track_type = "Audio"

        mock_media_info = MagicMock()
        mock_media_info.tracks = [mock_track]

        mock_mediainfo_class = MagicMock()
        mock_mediainfo_class.parse.return_value = mock_media_info

        with patch.dict("sys.modules", {"pymediainfo": MagicMock(MediaInfo=mock_mediainfo_class)}):
            import importlib
            import pyhako.media
            importlib.reload(pyhako.media)

            width, height = pyhako.media.get_video_dimensions(Path("/fake/audio.mp3"))

            assert width is None
            assert height is None

    def test_video_parse_error(self) -> None:
        """Test handling of MediaInfo parse error."""
        mock_mediainfo_class = MagicMock()
        mock_mediainfo_class.parse.side_effect = Exception("Parse failed")

        with patch.dict("sys.modules", {"pymediainfo": MagicMock(MediaInfo=mock_mediainfo_class)}):
            import importlib
            import pyhako.media
            importlib.reload(pyhako.media)

            width, height = pyhako.media.get_video_dimensions(Path("/fake/bad.mp4"))

            assert width is None
            assert height is None

    def test_nonexistent_video(self, tmp_path: Path) -> None:
        """Test handling of missing video file."""
        missing_path = tmp_path / "missing.mp4"

        width, height = get_video_dimensions(missing_path)

        assert width is None
        assert height is None


class TestGetMediaDimensions:
    """Tests for get_media_dimensions function."""

    def test_picture_type(self, tmp_path: Path) -> None:
        """Test media_type='picture' routes to image handler."""
        from PIL import Image

        img_path = tmp_path / "photo.jpg"
        img = Image.new("RGB", (640, 480), color="green")
        img.save(img_path)

        width, height = get_media_dimensions(img_path, "picture")

        assert width == 640
        assert height == 480

    def test_video_type_mock(self) -> None:
        """Test media_type='video' routes to video handler."""
        mock_track = MagicMock()
        mock_track.track_type = "Video"
        mock_track.width = 1280
        mock_track.height = 720

        mock_media_info = MagicMock()
        mock_media_info.tracks = [mock_track]

        mock_mediainfo_class = MagicMock()
        mock_mediainfo_class.parse.return_value = mock_media_info

        with patch.dict("sys.modules", {"pymediainfo": MagicMock(MediaInfo=mock_mediainfo_class)}):
            import importlib
            import pyhako.media
            importlib.reload(pyhako.media)

            width, height = pyhako.media.get_media_dimensions(Path("/fake/clip.mp4"), "video")

            assert width == 1280
            assert height == 720

    def test_voice_type(self, tmp_path: Path) -> None:
        """Test media_type='voice' returns None (audio has no dimensions)."""
        audio_path = tmp_path / "voice.m4a"
        audio_path.write_bytes(b"fake audio data")

        width, height = get_media_dimensions(audio_path, "voice")

        assert width is None
        assert height is None

    def test_unknown_type(self, tmp_path: Path) -> None:
        """Test unknown media type returns None."""
        file_path = tmp_path / "file.bin"
        file_path.write_bytes(b"binary data")

        width, height = get_media_dimensions(file_path, "unknown")

        assert width is None
        assert height is None

    def test_text_type(self, tmp_path: Path) -> None:
        """Test text media type returns None."""
        file_path = tmp_path / "message.txt"
        file_path.write_text("Hello world")

        width, height = get_media_dimensions(file_path, "text")

        assert width is None
        assert height is None
