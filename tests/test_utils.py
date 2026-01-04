from pyhako.utils import get_media_extension, normalize_message


def test_get_media_extension():
    # Test valid extension extraction
    assert get_media_extension("http://example.com/file.jpg", "image") == "jpg"
    assert get_media_extension("http://example.com/file.mp4?query=1", "video") == "mp4"

    # Test backup map
    assert get_media_extension("http://example.com/file", "image") == "jpg"
    assert get_media_extension(None, "voice") == "m4a"

def test_normalize_message():
    raw_msg = {
        "id": 123,
        "published_at": "2023-01-01T00:00:00Z",
        "text": "Hello",
        "type": "image",
        "file": "http://img.com/a.jpg"
    }

    normalized = normalize_message(raw_msg)

    assert normalized['id'] == 123
    assert normalized['type'] == 'picture'  # Mapping check
    assert normalized['content'] == 'Hello'
    assert normalized['is_favorite'] is False
    assert '_raw_type' in normalized # Internal field presence check

def test_normalize_message_types():
    assert normalize_message({"id":1, "type": "movie"})['type'] == 'video'
    assert normalize_message({"id":1, "type": "voice"})['type'] == 'voice'
    assert normalize_message({"id":1, "type": "text"})['type'] == 'text'
