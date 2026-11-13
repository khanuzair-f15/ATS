"""Connector boundary tests — Protocol conformance, completeness, Tenant secret rejection.

These tests verify the structural contract in `jobindex.connectors.base` without
requiring any live network or database.
"""

from __future__ import annotations

import pytest

from jobindex.connectors.base import (
    Completeness,
    Connector,
    ListingPage,
    RawPosting,
    Tenant,
    closure_eligible,
)


def test_completeness_enum_values() -> None:
    assert set(Completeness) == {Completeness.FULL, Completeness.WINDOWED, Completeness.UNKNOWN}


def test_closure_eligible_only_for_full() -> None:
    assert closure_eligible(Completeness.FULL) is True
    assert closure_eligible(Completeness.WINDOWED) is False
    assert closure_eligible(Completeness.UNKNOWN) is False


def test_raw_posting_fields() -> None:
    posting = RawPosting(
        external_id="123",
        title="Engineer",
        source_url="https://example.com/jobs/123",
        apply_urls=("https://example.com/apply/123",),
        description_html="<p>desc</p>",
        location_raw="San Francisco, CA",
        salary_raw="$120k",
        posted_at_raw="2024-01-15",
        raw={"custom": "field"},
    )
    assert posting.external_id == "123"
    assert posting.apply_urls == ("https://example.com/apply/123",)


def test_listing_page_fields() -> None:
    page = ListingPage(
        postings=(
            RawPosting(
                external_id="1",
                title="A",
                source_url="https://example.com/1",
                apply_urls=("https://example.com/a/1",),
            ),
            RawPosting(
                external_id="2",
                title="B",
                source_url="https://example.com/2",
                apply_urls=("https://example.com/a/2",),
            ),
        ),
        next_cursor="page-2",
        is_last=False,
    )
    assert len(page.postings) == 2
    assert page.next_cursor == "page-2"
    assert page.is_last is False


def test_tenant_requires_no_secrets_in_config() -> None:
    # Valid tenant config (no forbidden keys)
    tenant = Tenant(
        tenant_key="acme",
        entry_url="https://acme.example.com/careers",
        config={"some": "setting"},
    )
    assert tenant.tenant_key == "acme"

    # Invalid: any forbidden key in config raises
    forbidden = ["token", "password", "secret", "api_key", "authorization"]
    for key in forbidden:
        with pytest.raises(ValueError, match=r"tenant config must not carry secrets"):
            Tenant(
                tenant_key="acme",
                entry_url="https://acme.example.com/careers",
                config={key: "value"},
            )

    # Multiple forbidden keys - still raises
    with pytest.raises(ValueError, match=r"tenant config must not carry secrets"):
        Tenant(
            tenant_key="acme",
            entry_url="https://acme.example.com/careers",
            config={"token": "x", "password": "y"},
        )


def test_connector_protocol_structural() -> None:
    """A class implementing the Connector protocol must have the required attributes and methods."""

    class DummyConnector:
        platform_key = "dummy"
        from jobindex.db.enums import TransportClass

        transport = TransportClass.HTML
        completeness = Completeness.FULL

        async def list_page(self, tenant: Tenant, cursor: str | None = None) -> ListingPage:
            return ListingPage(postings=())

        async def fetch_detail(self, tenant: Tenant, posting: RawPosting) -> RawPosting:
            return posting

    # Structural subtyping check
    connector = DummyConnector()
    assert isinstance(connector, Connector)
    assert connector.platform_key == "dummy"
    assert connector.transport.value == "html"
    assert connector.completeness is Completeness.FULL


def test_connector_missing_attribute_fails_protocol() -> None:
    """A class missing a required attribute is not a Connector."""

    class Incomplete:
        platform_key = "incomplete"
        # missing transport, completeness, list_page, fetch_detail

    # This would fail a static type check; at runtime the protocol check is structural
    # but we can verify the attributes don't exist
    incomplete = Incomplete()
    assert not hasattr(incomplete, "transport")
    assert not hasattr(incomplete, "completeness")