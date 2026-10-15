"""Compat shim for InvenTree versions that don't yet provide WellKnownMixin.

WellKnownMixin (letting a plugin advertise entries under /.well-known/) was
added in inventree/InvenTree#12698, merged to InvenTree's master branch but
not yet present in a "stable" release. InvenTreeMCP must still import and
load cleanly on older InvenTree instances, so this falls back to a blank
mixin that simply advertises nothing when the real one isn't available.
"""

try:
    from plugin.mixins import WellKnownMixin

    #: True when core serves /.well-known/<name>[/...] redirects for plugins.
    NATIVE_WELL_KNOWN = True
except ImportError:  # pragma: no cover - only hit on InvenTree < #12698
    NATIVE_WELL_KNOWN = False

    class WellKnownMixin:
        """Blank fallback used when the running InvenTree doesn't provide WellKnownMixin."""

        def get_well_known_urls(self, request=None):
            """No well-known entries to advertise without native support."""
            return []


__all__ = ["NATIVE_WELL_KNOWN", "WellKnownMixin"]
