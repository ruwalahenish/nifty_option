"""PLAN.md section 6, guard 1: a data provider cannot send an order, now or through a future provider."""
import inspect
import unittest

from nifty_core.data import fixture_provider, provider
from nifty_core.guards import forbidden
from tests._base import NoNetworkTestCase

EXPECTED_INTERFACE = {"get_clock", "get_underlying_bars", "get_option_chain", "get_quote",
                      "get_expiries", "get_event_calendar"}
ALLOWED_EXTRAS = {"from_file", "scenario"}                     # construction and inspection helpers


def public_members(cls):
    return {name for name, _ in inspect.getmembers(cls) if not name.startswith("_")}


class ReadOnlyInterfaceTests(NoNetworkTestCase):
    def test_the_abstract_interface_is_exactly_the_six_getters(self):
        self.assertEqual(public_members(provider.MarketDataProvider) - {"register"}, EXPECTED_INTERFACE)
        self.assertEqual(set(provider.MarketDataProvider.__abstractmethods__), EXPECTED_INTERFACE)

    def test_the_fixture_provider_adds_nothing_order_like(self):
        members = public_members(fixture_provider.FixtureProvider)
        self.assertEqual(members - EXPECTED_INTERFACE - {"register"}, ALLOWED_EXTRAS)

    def test_no_public_member_name_contains_an_order_word(self):
        for cls in (provider.MarketDataProvider, fixture_provider.FixtureProvider):
            for name in public_members(cls):
                self.assertFalse(forbidden.method_name_is_forbidden(name), "%s.%s" % (cls.__name__, name))

    def test_every_interface_method_is_a_getter(self):
        for name in EXPECTED_INTERFACE:
            self.assertTrue(name.startswith("get_"))

    def test_interface_methods_take_no_order_like_parameters(self):
        for name in EXPECTED_INTERFACE:
            for param in inspect.signature(getattr(provider.MarketDataProvider, name)).parameters:
                self.assertFalse(forbidden.method_name_is_forbidden(param), "%s(%s)" % (name, param))

    def test_a_subclass_that_adds_an_order_method_is_caught_by_the_same_check(self):
        class Bad(provider.MarketDataProvider):
            def get_clock(self): pass
            def get_underlying_bars(self, *a, **k): pass
            def get_option_chain(self, *a, **k): pass
            def get_quote(self, *a, **k): pass
            def get_expiries(self, *a, **k): pass
            def get_event_calendar(self, *a, **k): pass
            def place_order(self, *a): pass
        bad = [n for n in public_members(Bad) if forbidden.method_name_is_forbidden(n)]
        self.assertEqual(bad, ["place_order"])

    def test_the_interface_cannot_be_instantiated_without_all_getters(self):
        with self.assertRaises(TypeError):
            provider.MarketDataProvider()

    def test_data_package_has_no_network_or_process_imports(self):
        import os
        folder = os.path.dirname(provider.__file__)
        for path in forbidden.python_files(folder):
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(forbidden.forbidden_imports(fh.read()), [], path)


if __name__ == "__main__":
    unittest.main()
