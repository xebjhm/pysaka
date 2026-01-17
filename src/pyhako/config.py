"""Configuration constants for PyHako sync operations.

Centralized location for tunable parameters to make it easy
to adjust sync behavior without modifying code.
"""

# =============================================================================
# Message Sync Configuration
# =============================================================================

# Concurrency for media downloads
# Higher values = faster sync but more server load
MEDIA_DOWNLOAD_CONCURRENCY_INITIAL = 20  # First sync: aggressive
MEDIA_DOWNLOAD_CONCURRENCY_INCREMENTAL = 5  # Incremental: gentle on server


# =============================================================================
# Blog Sync Configuration (re-exported from blog.config for convenience)
# =============================================================================

# Blog config is in pyhako.blog.config - import from there for blog-specific settings
# This file focuses on message/media sync settings
