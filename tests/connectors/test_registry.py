"""Tests for the Connector Registry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jobindex.connectors.base import (
    Completeness,
    Connector,
    ListingPage,
    RawPosting,
    Tenant,
)
from jobindex.connectors.registry import (
    ConnectorRegistry,
    get_connector,
    get_registry,
    list_connectors,
    register_connector,
)


class MockConnector:
    """A mock connector that implements the Connector protocol."""

    def __init__(self, context, platform_key: str = "test", transport: str = "API",
                 completeness=Completeness.FULL):
        self._context = context
        self.platform_key = platform_key
        self.transport = transport
        self.completeness = completeness

    async def list_page(self, tenant: Tenant, cursor: str | None = None):
        return ListingPage(postings=())

    async def fetch_detail(self, tenant: Tenant, posting: RawPosting):
        return posting


class InvalidConnector:
    """A mock connector missing required protocol attributes."""

    def __init__(self, context):
        self._context = context
        # Missing platform_key, transport, completeness, list_page, fetch_detail


class TestConnectorRegistry:
    """Tests for ConnectorRegistry."""

    def setup_method(self):
        """Create a fresh registry for each test."""
        self.registry = ConnectorRegistry()

    def test_valid_registration(self):
        """Test registering a valid connector factory."""
        def factory(context):
            return MockConnector(context, "testplatform")

        self.registry.register("testplatform", factory)
        assert "testplatform" in self.registry
        assert len(self.registry) == 1

    def test_duplicate_registration_raises(self):
        """Test that duplicate registration raises ValueError."""
        def factory1(context):
            return MockConnector(context, "testplatform")

        def factory2(context):
            return MockConnector(context, "testplatform")

        self.registry.register("testplatform", factory1)
        with pytest.raises(ValueError, match="already registered"):
            self.registry.register("testplatform", factory2)

    def test_unknown_platform_key_raises(self):
        """Test that getting an unknown platform raises KeyError."""
        with pytest.raises(KeyError, match="No connector registered"):
            self.registry.get("unknown", MagicMock())

    def test_invalid_factory_raises(self):
        """Test that non-callable factory raises TypeError."""
        with pytest.raises(TypeError, match="must be callable"):
            self.registry.register("test", "not_a_callable")

    def test_empty_platform_key_raises(self):
        """Test that empty platform key raises ValueError."""
        def factory(context):
            return MockConnector(context)

        with pytest.raises(ValueError, match="non-empty string"):
            self.registry.register("", factory)

    def test_get_connector_returns_instance(self):
        """Test that get() returns a connector instance."""
        def factory(context):
            return MockConnector(context, "testplatform")

        self.registry.register("testplatform", factory)
        context = MagicMock()
        connector = self.registry.get("testplatform", context)

        assert isinstance(connector, MockConnector)
        assert connector.platform_key == "testplatform"

    def test_connector_missing_platform_key_raises(self):
        """Test that connector missing platform_key raises TypeError."""
        class BadConnector:
            def __init__(self, context):
                self._context = context
                self.transport = "API"
                self.completeness = Completeness.FULL
            async def list_page(self, tenant, cursor=None):
                return ListingPage(postings=())
            async def fetch_detail(self, tenant, posting):
                return posting

        def factory(context):
            return BadConnector(context)

        self.registry.register("bad", factory)
        with pytest.raises(TypeError, match="missing required attribute 'platform_key'"):
            self.registry.get("bad", MagicMock())

    def test_connector_missing_transport_raises(self):
        """Test that connector missing transport raises TypeError."""
        class BadConnector:
            def __init__(self, context):
                self._context = context
                self.platform_key = "bad"
                self.completeness = Completeness.FULL
            async def list_page(self, tenant, cursor=None):
                return ListingPage(postings=())
            async def fetch_detail(self, tenant, posting):
                return posting

        def factory(context):
            return BadConnector(context)

        self.registry.register("bad", factory)
        with pytest.raises(TypeError, match="missing required attribute 'transport'"):
            self.registry.get("bad", MagicMock())

    def test_connector_missing_completeness_raises(self):
        """Test that connector missing completeness raises TypeError."""
        class BadConnector:
            def __init__(self, context):
                self._context = context
                self.platform_key = "bad"
                self.transport = "API"
            async def list_page(self, tenant, cursor=None):
                return ListingPage(postings=())
            async def fetch_detail(self, tenant, posting):
                return posting

        def factory(context):
            return BadConnector(context)

        self.registry.register("bad", factory)
        with pytest.raises(TypeError, match="missing required attribute 'completeness'"):
            self.registry.get("bad", MagicMock())

    def test_connector_missing_list_page_raises(self):
        """Test that connector missing list_page raises TypeError."""
        class BadConnector:
            def __init__(self, context):
                self._context = context
                self.platform_key = "bad"
                self.transport = "API"
                self.completeness = Completeness.FULL
            async def fetch_detail(self, tenant, posting):
                return posting

        def factory(context):
            return BadConnector(context)

        self.registry.register("bad", factory)
        with pytest.raises(TypeError, match="missing required method 'list_page'"):
            self.registry.get("bad", MagicMock())

    def test_connector_missing_fetch_detail_raises(self):
        """Test that connector missing fetch_detail raises TypeError."""
        class BadConnector:
            def __init__(self, context):
                self._context = context
                self.platform_key = "bad"
                self.transport = "API"
                self.completeness = Completeness.FULL
            async def list_page(self, tenant, cursor=None):
                return ListingPage(postings=())

        def factory(context):
            return BadConnector(context)

        self.registry.register("bad", factory)
        with pytest.raises(TypeError, match="missing required method 'fetch_detail'"):
            self.registry.get("bad", MagicMock())

    def test_connector_platform_key_mismatch_raises(self):
        """Test that connector with mismatched platform_key raises ValueError."""
        class MismatchedConnector:
            def __init__(self, context):
                self._context = context
                self.platform_key = "different"
                self.transport = "API"
                self.completeness = Completeness.FULL
            async def list_page(self, tenant, cursor=None):
                return ListingPage(postings=())
            async def fetch_detail(self, tenant, posting):
                return posting

        def factory(context):
            return MismatchedConnector(context)

        self.registry.register("expected", factory)
        with pytest.raises(ValueError, match="mismatched platform_key"):
            self.registry.get("expected", MagicMock())

    def test_list_platforms_deterministic(self):
        """Test that list_platforms returns sorted keys."""
        def factory(context):
            return MockConnector(context)

        self.registry.register("zebra", factory)
        self.registry.register("apple", factory)
        self.registry.register("banana", factory)

        platforms = self.registry.list_platforms()
        assert platforms == ["apple", "banana", "zebra"]

    def test_contains(self):
        """Test __contains__ method."""
        def factory(context):
            return MockConnector(context)

        self.registry.register("test", factory)
        assert "test" in self.registry
        assert "other" not in self.registry

    def test_len(self):
        """Test __len__ method."""
        def factory(context):
            return MockConnector(context)

        assert len(self.registry) == 0
        self.registry.register("a", factory)
        assert len(self.registry) == 1
        self.registry.register("b", factory)
        assert len(self.registry) == 2


class TestDecoratorRegistration:
    """Tests for the register_connector decorator."""

    def setup_method(self):
        """Create a fresh registry for each test."""
        self.registry = ConnectorRegistry()

    def test_decorator_registration(self):
        """Test decorator-based registration."""
        @self.registry.register_connector("decorated")
        def factory(context):
            return MockConnector(context, "decorated")

        assert "decorated" in self.registry
        connector = self.registry.get("decorated", MagicMock())
        assert connector.platform_key == "decorated"


class TestGlobalRegistry:
    """Tests for the global registry functions."""

    def setup_method(self):
        """Clear the global registry before each test."""
        # Get the global registry and clear it
        global_registry = get_registry()
        global_registry._factories.clear()

    def test_global_registry_functions(self):
        """Test global registry helper functions."""
        # Register using global function
        @register_connector("global_test")
        def factory(context):
            return MockConnector(context, "global_test")

        # Test list_connectors
        platforms = list_connectors()
        assert "global_test" in platforms

        # Test get_connector
        context = MagicMock()
        connector = get_connector("global_test", context)
        assert connector.platform_key == "global_test"

        # Test unknown platform
        with pytest.raises(KeyError):
            get_connector("unknown", MagicMock())


class TestRegistryProtocolCompliance:
    """Tests that ensure connectors comply with the Connector protocol."""

    def setup_method(self):
        self.registry = ConnectorRegistry()

    def test_mock_connector_protocol_compliance(self):
        """Test that MockConnector satisfies the Connector protocol."""

        def factory(context):
            return MockConnector(context, "test")

        # This should not raise
        connector = MockConnector(MagicMock(), "test")
        assert isinstance(connector, Connector)


class TestRegistryEdgeCases:
    """Edge case tests for the registry."""

    def setup_method(self):
        self.registry = ConnectorRegistry()

    def test_factory_returns_none_raises(self):
        """Test that factory returning None raises appropriate error."""
        def bad_factory(context):
            return None

        self.registry.register("bad", bad_factory)
        with pytest.raises(TypeError):
            self.registry.get("bad", MagicMock())

    def test_factory_returns_wrong_type_raises(self):
        """Test that factory returning wrong type raises TypeError."""
        def bad_factory(context):
            return "not a connector"

        self.registry.register("bad", bad_factory)
        with pytest.raises(TypeError):
            self.registry.get("bad", MagicMock())

    def test_case_sensitive_keys(self):
        """Test that platform keys are case-sensitive."""
        def factory(context):
            return MockConnector(context, "test")

        self.registry.register("TestPlatform", factory)
        assert "TestPlatform" in self.registry
        assert "testplatform" not in self.registry

        with pytest.raises(KeyError):
            self.registry.get("testplatform", MagicMock())

    def test_special_characters_in_key(self):
        """Test that special characters in key are allowed."""
        def factory(context):
            return MockConnector(context, "test-platform_v2")

        self.registry.register("test-platform_v2", factory)
        assert "test-platform_v2" in self.registry
        connector = self.registry.get("test-platform_v2", MagicMock())
        assert connector.platform_key == "test-platform_v2"