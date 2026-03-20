"""Configuration constants for pysaka sync operations.

Centralized location for tunable parameters to make it easy
to adjust sync behavior without modifying code.
"""

# =============================================================================
# Message Sync Configuration
# =============================================================================

# Legacy concurrency constants — kept for backward compatibility.
# When using AdaptivePool (SakaDesk 0.2.0+), these are ignored; the pool
# manages concurrency dynamically based on network conditions.
MEDIA_DOWNLOAD_CONCURRENCY_INITIAL = 20  # First sync: aggressive
MEDIA_DOWNLOAD_CONCURRENCY_INCREMENTAL = 5  # Incremental: gentle on server


# =============================================================================
# Blog Sync Configuration (re-exported from blog.config for convenience)
# =============================================================================

# Blog config is in pysaka.blog.config - import from there for blog-specific settings
# This file focuses on message/media sync settings
