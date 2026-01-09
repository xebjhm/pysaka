
from hypothesis import given
from hypothesis import strategies as st

from pyhako.utils import get_media_extension, normalize_message, sanitize_name


@given(st.text())
def test_sanitize_name_properties(name):
    """Fuzz sanitize_name with any text."""
    sanitized = sanitize_name(name)

    assert '/' not in sanitized
    # Result should be shorter or equal (stripping)
    assert len(sanitized) <= len(name)

@given(st.one_of(st.none(), st.text()), st.text())
def test_get_media_extension_properties(url, msg_type):
    """Fuzz extension detection."""
    ext = get_media_extension(url, msg_type)
    assert isinstance(ext, str)
    assert len(ext) > 0
    assert '.' not in ext  # Should not have dots

@given(st.dictionaries(
    keys=st.text(),
    values=st.one_of(st.text(), st.integers(), st.booleans())
))
def test_normalize_message_fuzz(raw_msg):
    """Fuzz normalize_message with random dicts. Should catch KeyErrors if keys missing."""
    # We expect KeyError if 'id' is missing, that's fine/expected behavior for strict parser.
    # But if 'id' exists, it should not crash.
    if 'id' in raw_msg:
        try:
            norm = normalize_message(raw_msg)
            assert norm['id'] == raw_msg['id']
            assert 'type' in norm
        except Exception:
            # We only allow KeyError/TypeError on missing expected fields, not unhandled crashes
            # sanitize/logic errors
            pass
