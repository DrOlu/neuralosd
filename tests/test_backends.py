"""Tests for the sandbox backends."""
import pytest
from neuralosd.backends import get_backend, available_backends


class TestBackendFactory:
    def test_get_boxlite(self):
        backend = get_backend("boxlite")
        assert backend.name() == "boxlite"

    def test_get_msb(self):
        backend = get_backend("msb")
        assert backend.name() == "msb"

    def test_unknown_backend(self):
        with pytest.raises(ValueError):
            get_backend("nonexistent")

    def test_available_backends(self):
        backends = available_backends()
        assert isinstance(backends, dict)
        assert "boxlite" in backends
        assert "msb" in backends
