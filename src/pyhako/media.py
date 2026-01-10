"""Media utilities for dimension extraction."""

from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()


def get_image_dimensions(filepath: Path) -> tuple[Optional[int], Optional[int]]:
    """
    Extract dimensions from an image file using Pillow.

    Args:
        filepath: Path to the image file.

    Returns:
        Tuple of (width, height) or (None, None) on failure.
    """
    try:
        from PIL import Image
        with Image.open(filepath) as img:
            return img.width, img.height
    except Exception as e:
        logger.warning("Failed to get image dimensions", path=str(filepath), error=str(e))
        return None, None


def get_video_dimensions(filepath: Path) -> tuple[Optional[int], Optional[int]]:
    """
    Extract dimensions from a video file using pymediainfo.

    Args:
        filepath: Path to the video file.

    Returns:
        Tuple of (width, height) or (None, None) on failure.
    """
    try:
        from pymediainfo import MediaInfo
        media_info = MediaInfo.parse(str(filepath))
        for track in media_info.tracks:
            if track.track_type == "Video":
                return track.width, track.height
        return None, None
    except Exception as e:
        logger.warning("Failed to get video dimensions", path=str(filepath), error=str(e))
        return None, None


def get_media_dimensions(filepath: Path, media_type: str) -> tuple[Optional[int], Optional[int]]:
    """
    Extract dimensions from a media file based on its type.

    Args:
        filepath: Path to the media file.
        media_type: Type of media ('picture', 'video', or other).

    Returns:
        Tuple of (width, height) or (None, None) for non-visual media or on failure.
    """
    if media_type == 'picture':
        return get_image_dimensions(filepath)
    elif media_type == 'video':
        return get_video_dimensions(filepath)
    else:
        return None, None
