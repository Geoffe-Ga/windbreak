"""Tests for hedgekit.main module."""

from hedgekit.main import main


def test_main_runs() -> None:
    """Test that main() runs without error."""
    main()  # Should print "Hello from hedgekit!"
