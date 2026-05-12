"""Integration tests for the V4 stack.

Tests in this package wire multiple ``quanta_core`` modules together with
mocks/fakes for venues, ledgers, and any wave-2 modules not yet landed.

The goal is **shape-correctness**: prove that the typed events flow end to
end through the system in the right order, that ids round-trip, and that
the lifecycle hooks fire as documented. Coverage is not the metric here.
"""
