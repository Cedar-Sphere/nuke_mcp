"""Knob value and format reporting.

The addon used to describe knobs by whitelisting a handful of knob classes,
which silently dropped every multi-component colour knob -- so a Grade's
``white`` and ``gamma`` never appeared in ``get_node_info``. It also stringified
Nuke's Format object, which has no useful ``__str__``.
"""

import unittest

from tests.test_addon_protocol import FakeNukeModule, load_addon_module


class FakeKnob(object):
    """Stands in for a Nuke knob.

    ``array_size`` of None models a knob with no ``arraySize`` method at all
    (Channel_Knob), which is distinct from one that reports a size of 1.
    """

    def __init__(self, value=None, array_size=None, components=None,
                 value_raises=False, array_raises=False, visible=True):
        self._value = value
        self._components = components or []
        self._array_size = array_size
        self._value_raises = value_raises
        self._array_raises = array_raises
        self._visible = visible
        if array_size is not None:
            self.arraySize = self._array_size_impl

    def _array_size_impl(self):
        if self._array_raises:
            raise RuntimeError("arraySize exploded")
        return self._array_size

    def value(self, index=None):
        if self._value_raises:
            raise RuntimeError("value exploded")
        if index is not None:
            return self._components[index]
        return self._value

    def visible(self):
        return self._visible


class Unserializable(object):
    def __repr__(self):
        return "<Format object at 0xdeadbeef>"


class KnobValueTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addon = load_addon_module()
        FakeNukeModule.reset()

    def test_multi_component_knob_reports_every_component(self):
        """The regression that hid Grade's white/gamma."""
        knob = FakeKnob(value=1.25, array_size=4, components=[1.25, 1.25, 1.25, 1.0])
        ok, value = self.addon.knob_value(knob)
        self.assertTrue(ok)
        self.assertEqual(value, [1.25, 1.25, 1.25, 1.0])

    def test_single_component_knob_reports_scalar(self):
        knob = FakeKnob(value=0.5, array_size=1)
        ok, value = self.addon.knob_value(knob)
        self.assertTrue(ok)
        self.assertEqual(value, 0.5)

    def test_knob_without_array_size_reports_scalar(self):
        knob = FakeKnob(value="rgb")
        self.assertFalse(hasattr(knob, "arraySize"))
        ok, value = self.addon.knob_value(knob)
        self.assertTrue(ok)
        self.assertEqual(value, "rgb")

    def test_booleans_are_preserved_as_booleans(self):
        ok, value = self.addon.knob_value(FakeKnob(value=True))
        self.assertTrue(ok)
        self.assertIs(value, True)

    def test_array_size_failure_falls_back_to_scalar(self):
        knob = FakeKnob(value=7.0, array_size=4, array_raises=True)
        ok, value = self.addon.knob_value(knob)
        self.assertTrue(ok)
        self.assertEqual(value, 7.0)

    def test_value_failure_is_reported_as_unavailable(self):
        ok, value = self.addon.knob_value(FakeKnob(value_raises=True))
        self.assertFalse(ok)
        self.assertIsNone(value)

    def test_unserializable_value_is_skipped_rather_than_repr_dumped(self):
        ok, value = self.addon.knob_value(FakeKnob(value=Unserializable()))
        self.assertFalse(ok)
        self.assertIsNone(value)

    def test_sequence_of_primitives_is_kept(self):
        ok, value = self.addon.knob_value(FakeKnob(value=(1.0, 2.0)))
        self.assertTrue(ok)
        self.assertEqual(value, [1.0, 2.0])

    def test_sequence_containing_unserializable_is_skipped(self):
        knob = FakeKnob(value=[1.0, Unserializable()])
        ok, value = self.addon.knob_value(knob)
        self.assertFalse(ok)
        self.assertIsNone(value)

    def test_reported_values_survive_json_encoding(self):
        """Whatever we accept must not break the response encoding."""
        import json

        knobs = [
            FakeKnob(value=1.25, array_size=4, components=[1.0, 1.0, 1.0, 1.0]),
            FakeKnob(value="rgb"),
            FakeKnob(value=True),
            FakeKnob(value=3),
        ]
        collected = {}
        for index, knob in enumerate(knobs):
            ok, value = self.addon.knob_value(knob)
            if ok:
                collected[str(index)] = value
        self.assertEqual(len(collected), 4)
        json.dumps(collected)


class FakeFormat(object):
    def __init__(self, name=None, width=None, height=None,
                 name_raises=False, dims_raise=False):
        self._name = name
        self._width = width
        self._height = height
        self._name_raises = name_raises
        self._dims_raise = dims_raise

    def name(self):
        if self._name_raises:
            raise RuntimeError("name exploded")
        return self._name

    def width(self):
        if self._dims_raise:
            raise RuntimeError("width exploded")
        return self._width

    def height(self):
        if self._dims_raise:
            raise RuntimeError("height exploded")
        return self._height

    def __str__(self):
        return "<Format object at 0xdeadbeef>"


class FormatDescriptionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addon = load_addon_module()
        FakeNukeModule.reset()

    def test_named_format_includes_name_and_dimensions(self):
        fmt = FakeFormat(name="2K_Super_35(full-ap)", width=2048, height=1556)
        self.assertEqual(
            self.addon.format_description(fmt),
            "2K_Super_35(full-ap) (2048x1556)",
        )

    def test_unnamed_format_falls_back_to_dimensions(self):
        """Custom formats report an empty name."""
        fmt = FakeFormat(name="", width=1920, height=1080)
        self.assertEqual(self.addon.format_description(fmt), "1920x1080")

    def test_none_name_falls_back_to_dimensions(self):
        fmt = FakeFormat(name=None, width=1920, height=1080)
        self.assertEqual(self.addon.format_description(fmt), "1920x1080")

    def test_never_returns_the_bare_object_repr(self):
        fmt = FakeFormat(name="HD_1080", width=1920, height=1080)
        self.assertNotIn("Format object at", self.addon.format_description(fmt))

    def test_dimension_failure_still_yields_the_name(self):
        fmt = FakeFormat(name="HD_1080", dims_raise=True)
        self.assertEqual(self.addon.format_description(fmt), "HD_1080")

    def test_total_failure_degrades_to_a_string_not_an_exception(self):
        fmt = FakeFormat(name_raises=True, dims_raise=True)
        result = self.addon.format_description(fmt)
        self.assertIsInstance(result, str)
        self.assertTrue(result)

    def test_missing_format_is_none(self):
        self.assertIsNone(self.addon.format_description(None))


if __name__ == "__main__":
    unittest.main()
