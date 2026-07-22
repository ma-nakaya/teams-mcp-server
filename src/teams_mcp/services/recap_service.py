from __future__ import annotations

import re
from datetime import date, datetime, timezone

from ..browser import PlaywrightCli
from ..errors import UiChangedError
from ..models import MeetingRecap
from ..parsers.recaps import parse_recap
from ..parsers.refs import find_ref, referenced_lines
from ..session import TeamsSession

RECAP_LABELS = (
    "Recap",
    "AI recap",
    "Meeting recap",
    "リキャップ",
    "会議のまとめ",
)


class RecapService:
    def __init__(self, browser: PlaywrightCli, session: TeamsSession) -> None:
        self.browser = browser
        self.session = session

    def collect(self, target_date: date) -> dict[str, object]:
        with self.session.acquire():
            snapshot = self.session.normalize()
            chat_ref = find_ref(snapshot, "Chat", "チャット")
            if not chat_ref:
                raise UiChangedError("Teamsのチャットナビゲーションが見つかりません。")
            self.browser.click(chat_ref)

            listing = self.browser.snapshot()
            candidates = self._meeting_candidates(listing, target_date)
            recaps: list[MeetingRecap] = []
            for title, time_text in candidates:
                # snapshot更新のたびにrefを再解決する。
                fresh_listing = self.browser.snapshot()
                meeting_ref = find_ref(fresh_listing, title)
                if not meeting_ref:
                    continue
                self.browser.click(meeting_ref)
                meeting_snapshot = self.browser.snapshot()
                recap_ref = find_ref(meeting_snapshot, *RECAP_LABELS)
                if not recap_ref:
                    recaps.append(
                        MeetingRecap(
                            title=title,
                            time_text=time_text,
                            status="no_recap",
                        )
                    )
                    continue
                self.browser.click(recap_ref)
                recap_snapshot = self.browser.snapshot()
                recaps.append(
                    parse_recap(recap_snapshot, title=title, time_text=time_text)
                )

                # 次の候補を探す前にチャット一覧へ戻す。
                normalized = self.session.normalize()
                chat_ref = find_ref(normalized, "Chat", "チャット")
                if not chat_ref:
                    raise UiChangedError("Teamsのチャットナビゲーションが見つかりません。")
                self.browser.click(chat_ref)

            return {
                "status": "ok",
                "date": target_date.isoformat(),
                "meetings": [recap.as_dict() for recap in recaps],
                "count": len(recaps),
                "extractedAt": datetime.now(timezone.utc).isoformat(),
            }

    @staticmethod
    def _meeting_candidates(snapshot: str, target_date: date) -> list[tuple[str, str | None]]:
        date_tokens = {
            target_date.isoformat(),
            target_date.strftime("%Y/%m/%d"),
            target_date.strftime("%m/%d"),
            f"{target_date.month}/{target_date.day}",
            f"{target_date.month}月{target_date.day}日",
        }
        candidates: list[tuple[str, str | None]] = []
        for _, label in referenced_lines(snapshot):
            if not any(token in label for token in date_tokens):
                continue
            time_match = re.search(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", label)
            time_text = time_match.group(0) if time_match else None
            title = label
            for token in date_tokens:
                title = title.replace(token, "")
            if time_text:
                title = title.replace(time_text, "")
            title = re.sub(r"\s+", " ", title).strip(" ,-–—")
            if title:
                candidates.append((title, time_text))
        return list(dict.fromkeys(candidates))
