"""Version and constraint algebra."""

from __future__ import annotations

import unittest

from axiom.version import (
    Constraint,
    Version,
    VersionRange,
    parse_constraint,
    parse_version,
)
from tests import *


def v(text: str) -> Version:
    return parse_version(text)


class VersionParsingTests(unittest.TestCase):
    def test_parses_components(self):
        version = v("1.2.3")
        self.assertEqual((version.major, version.minor, version.patch), (1, 2, 3))
        self.assertEqual(str(version), "1.2.3")

    def test_partial_versions_are_normalised(self):
        self.assertEqual(v("1"), Version(1, 0, 0))
        self.assertEqual(v("1.2"), Version(1, 2, 0))

    def test_prerelease_and_build(self):
        version = v("1.2.3-rc.1+build.7")
        self.assertEqual(version.prerelease, ("rc", 1))
        self.assertEqual(version.build, "build.7")
        self.assertTrue(version.is_prerelease)
        self.assertEqual(str(version), "1.2.3-rc.1+build.7")

    def test_rejects_invalid(self):
        for text in ("", "one", "1.2.3.4", "-1.0.0", "1.0.0-", "1.0.0+"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_version(text)

    def test_accepts_leading_v(self):
        self.assertEqual(v("v2.0.0"), Version(2, 0, 0))

    def test_hashable(self):
        self.assertEqual(len({v("1.0.0"), v("1.0.0"), v("1.0.1")}), 2)


class VersionOrderingTests(unittest.TestCase):
    def test_semver_precedence(self):
        ordered = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
            "1.0.1",
            "1.1.0",
            "2.0.0",
        ]
        versions = [v(text) for text in ordered]
        self.assertEqual(sorted(versions), versions)
        self.assertEqual(sorted(versions[::-1]), versions)

    def test_numeric_identifiers_sort_below_text(self):
        self.assertLess(v("1.0.0-1"), v("1.0.0-alpha"))

    def test_build_metadata_does_not_change_precedence(self):
        left = v("1.0.0+aaa")
        right = v("1.0.0+bbb")
        self.assertTrue(left.same_precedence(right))
        self.assertEqual(
            Constraint("==", left).matches(right),
            True,
        )
        self.assertNotEqual(left, right)
        # Total ordering is still consistent even when precedence ties.
        self.assertEqual(left < right, not (right < left))

    def test_equality_is_by_value(self):
        self.assertEqual(v("1.2.3"), Version(1, 2, 3))
        self.assertNotEqual(v("1.2.3"), v("1.2.3-rc.1"))


class ConstraintTests(unittest.TestCase):
    def assertMatches(self, expression, version, expected=True):
        result = parse_constraint(expression).matches(v(version))
        self.assertIs(result, expected, f"{expression!r} vs {version!r}")

    def test_any(self):
        for expression in ("", "*", None):
            self.assertMatches(expression, "0.0.1")
            self.assertMatches(expression, "99.0.0")

    def test_exact(self):
        self.assertMatches("1.2.3", "1.2.3")
        self.assertMatches("=1.2.3", "1.2.3")
        self.assertMatches("==1.2.3", "1.2.3")
        self.assertMatches("1.2.3", "1.2.4", False)
        self.assertMatches("v1.2.3", "1.2.3")

    def test_comparisons(self):
        self.assertMatches(">1.2.3", "1.2.4")
        self.assertMatches(">1.2.3", "1.2.3", False)
        self.assertMatches(">=1.2.3", "1.2.3")
        self.assertMatches("<1.2.3", "1.2.2")
        self.assertMatches("<=1.2.3", "1.2.3")
        self.assertMatches("!=1.2.3", "1.2.4")
        self.assertMatches("!=1.2.3", "1.2.3", False)

    def test_caret(self):
        self.assertMatches("^1.2.3", "1.2.3")
        self.assertMatches("^1.2.3", "1.99.0")
        self.assertMatches("^1.2.3", "2.0.0", False)
        self.assertMatches("^1.2.3", "1.2.2", False)
        self.assertMatches("^0.2.3", "0.2.9")
        self.assertMatches("^0.2.3", "0.3.0", False)
        self.assertMatches("^0.0.3", "0.0.3")
        self.assertMatches("^0.0.3", "0.0.4", False)
        self.assertMatches("^0", "0.9.9")
        self.assertMatches("^0", "1.0.0", False)

    def test_tilde(self):
        self.assertMatches("~1.2.3", "1.2.9")
        self.assertMatches("~1.2.3", "1.3.0", False)
        self.assertMatches("~1.2", "1.2.0")
        self.assertMatches("~1.2", "1.3.0", False)
        self.assertMatches("~1", "1.9.0")
        self.assertMatches("~1", "2.0.0", False)

    def test_wildcards_and_partials(self):
        self.assertMatches("1.2.x", "1.2.7")
        self.assertMatches("1.2.x", "1.3.0", False)
        self.assertMatches("1.x", "1.9.9")
        self.assertMatches("1.x", "2.0.0", False)
        self.assertMatches("1.2", "1.2.4")
        self.assertMatches("1.2", "1.3.0", False)
        self.assertMatches("1", "1.4.0")
        self.assertMatches("1", "2.0.0", False)
        self.assertMatches("1.2.*", "1.2.0")

    def test_hyphen_range(self):
        self.assertMatches("1.2.3 - 2.0.0", "1.2.3")
        self.assertMatches("1.2.3 - 2.0.0", "2.0.0")
        self.assertMatches("1.2.3 - 2.0.0", "2.0.1", False)
        self.assertMatches("1.2 - 2", "2.9.9")
        self.assertMatches("1.2 - 2", "3.0.0", False)

    def test_conjunction(self):
        expression = ">=1.0.0, <2.0.0, !=1.5.0"
        self.assertMatches(expression, "1.4.0")
        self.assertMatches(expression, "1.5.0", False)
        self.assertMatches(expression, "2.0.0", False)
        self.assertMatches(expression, "0.9.0", False)

    def test_prerelease_gating(self):
        self.assertMatches("^1.2.0", "1.3.0-rc.1", False)
        self.assertMatches(">=1.0.0", "2.0.0-beta.1", False)
        self.assertMatches(">=1.3.0-rc.1", "1.3.0-rc.2")
        self.assertMatches("1.3.0-rc.1", "1.3.0-rc.1")
        self.assertMatches("1.3.0-rc.1", "1.3.0-rc.2", False)
        self.assertMatches("*", "1.0.0-rc.1", False)
        self.assertMatches(">=1.0.0", "2.0.0-beta.1", False)

    def test_explicit_prerelease_permission(self):
        version_range = parse_constraint("^1.2.0")
        self.assertTrue(
            version_range.matches(v("1.3.0-rc.1"), allow_prerelease=True)
        )
        self.assertFalse(
            parse_constraint("*").matches(v("1.0.0-rc.1"))
        )
        self.assertTrue(
            parse_constraint("*").matches(
                v("1.0.0-rc.1"), allow_prerelease=True
            )
        )

    def test_range_api(self):
        left = parse_constraint(">=1.0.0,<2.0.0")
        right = parse_constraint("!=1.5.0")
        combined = left.intersect(right)
        self.assertTrue(combined.matches(v("1.4.0")))
        self.assertFalse(combined.matches(v("1.5.0")))
        self.assertEqual(str(combined), ">=1.0.0,<2.0.0,!=1.5.0")
        self.assertTrue(VersionRange((Constraint("*"),)).is_any)
        self.assertEqual(left.exact, None)
        self.assertEqual(parse_constraint("1.2.3").exact, v("1.2.3"))

    def test_invalid_constraints(self):
        for expression in ("abc", ">=1.0.0 || <2.0.0", "~", ">>1.0.0", "1.2.3 - "):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                parse_constraint(expression)

    def test_roundtrip(self):
        for expression in (
            "^1.2.3",
            "~0.4.0",
            ">=1.0.0,<2.0.0",
            "1.2.x",
            "!=1.2.3",
        ):
            parsed = parse_constraint(expression)
            reparsed = parse_constraint(str(parsed))
            for candidate in ("0.4.9", "1.2.3", "1.2.4", "1.9.9", "2.0.0"):
                self.assertEqual(
                    parsed.matches(v(candidate)),
                    reparsed.matches(v(candidate)),
                    f"{expression} -> {parsed} vs {candidate}",
                )


if __name__ == "__main__":
    unittest.main()
