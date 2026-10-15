"""Tests for the InvenTreeMCP plugin auth package.

InvenTree 1.6+ (core PR #13007) purges the plugin's modules from ``sys.modules``
on every registry collect, and the test runner collects for every test class.
The test modules (imported once, at discovery) would then hold the old module
objects while the code under test lazily imports the fresh ones: every
``unittest.mock.patch`` target misses, and the re-imported model classes
duplicate. Keep one set of module objects for the whole test run instead;
InvenTree 1.5 has no purge and behaves this way anyway.
"""

from plugin import registry as _registry

if hasattr(_registry, "_purge_plugin_modules"):
    _registry._purge_plugin_modules = lambda entries: None
