import unittest

from teams_mcp.parsers.refs import find_ref, referenced_lines


SNAPSHOT = """
- navigation:
  - button "チャット" [ref=e12] [pressed]
  - listitem "2026/07/22 14:00 新人研修会議" [ref=e99]
"""


class RefsParserTest(unittest.TestCase):
    def test_find_ref(self) -> None:
        self.assertEqual(find_ref(SNAPSHOT, "Chat", "チャット"), "e12")

    def test_referenced_lines(self) -> None:
        self.assertIn(("e99", "2026/07/22 14:00 新人研修会議"), referenced_lines(SNAPSHOT))
