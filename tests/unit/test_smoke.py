"""Smoke tests for the installed TechPulse-AI package."""

import techpulse


def test_package_version_exists() -> None:
    """The package exposes a non-empty version string."""
    assert isinstance(techpulse.__version__, str)
    assert techpulse.__version__
