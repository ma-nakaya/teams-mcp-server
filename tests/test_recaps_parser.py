from pathlib import Path
import unittest

from teams_mcp.parsers.recaps import parse_recap

FIXTURES = Path(__file__).parent / "fixtures"


class RecapParserTest(unittest.TestCase):
    def test_parse_japanese_recap(self) -> None:
        snapshot = (FIXTURES / "recap_ja.txt").read_text(encoding="utf-8")

        result = parse_recap(snapshot, title="新人研修定例", time_text="14:00")

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.title, "新人研修定例")
        self.assertEqual(
            [item.text for item in result.notes],
            [
                "AI によるメモ",
                "新人研修環境は Codespaces を使用する",
                "8月のお盆前までに資料を更新する",
                "担当者",
                "中谷さんがレビュー観点を整理する",
            ],
        )

    def test_no_access_has_priority(self) -> None:
        result = parse_recap(
            'paragraph "このリキャップへのアクセス権がありません"',
            title="private meeting",
        )
        self.assertEqual(result.status, "no_access")
        self.assertEqual(result.notes, [])

    def test_no_recap(self) -> None:
        result = parse_recap('text "リキャップはありません"', title="meeting")
        self.assertEqual(result.status, "no_recap")
