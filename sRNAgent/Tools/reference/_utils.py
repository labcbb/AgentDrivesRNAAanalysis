"""Compatibility module for the former private reference downloader.

Use :mod:`sRNAgent.Tools.reference.util` for all new reference downloads.
"""

from .util import resumable_download

__all__ = ["resumable_download"]
