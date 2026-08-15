"""Catalog grouping. Pure functions, no proxy needed."""

import pytest

import catalog


@pytest.mark.parametrize("path,expected", [
    ("/api/v1/orders/123", "Orders"),
    ("/api/v2/users/me", "Users"),
    # Regression: `startswith("v")` dropped any segment beginning with v, so these lost their
    # meaningful segment and were grouped under the id or the verb that followed.
    ("/api/v1/vouchers/123", "Vouchers"),
    ("/api/v1/validate/token", "Validate"),
    ("/v1/vouchers", "Vouchers"),
    ("/api/v1/order-history/last", "Order History"),
    ("/", "Other"),
    ("/v1", "Other"),
])
def test_domain_from_path(path, expected):
    assert catalog._domain_from_path(path) == expected
