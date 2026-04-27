"""Test config.

Registers `pytest.mark.timeout` so tests that use it don't trip
PytestUnknownMarkWarning when `pytest-timeout` isn't installed (the marker
is a no-op without the plugin, but registration silences the warning and
documents intent).

Install pytest-timeout to actually enforce wall-clock bounds:
    pip install pytest-timeout
"""
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "timeout(seconds): per-test wall-clock bound (no-op without pytest-timeout)",
    )
