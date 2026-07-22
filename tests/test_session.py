from pathlib import Path
import unittest

from teams_mcp.errors import AuthenticationRequiredError
from teams_mcp.session import TeamsSession


class FakeBrowser:
    pass


class SessionTest(unittest.TestCase):
    def test_detects_sign_in(self) -> None:
        with self.assertRaises(AuthenticationRequiredError):
            TeamsSession.ensure_authenticated('- button "サインイン"')

    def test_lock_is_created(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "teams.lock"
            session = TeamsSession(FakeBrowser(), lock_path=lock_path)  # type: ignore[arg-type]
            with session.acquire():
                self.assertTrue(lock_path.exists())
