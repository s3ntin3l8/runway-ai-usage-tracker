import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from app.core.browser_cookies import get_all_browser_cookies_paths
from app.core.config import settings

def test_browser_preference_ordering():
    # Mock Path.exists to return True for some paths
    with patch.object(Path, "exists", return_value=True):
        with patch.object(Path, "glob", return_value=[Path("/home/user/.mozilla/firefox/profile.default")]):
            with patch.object(Path, "home", return_value=Path("/home/user")):
                with patch("platform.system", return_value="Linux"):
                    # Test default order
                    with patch("app.core.config.settings.BROWSER_PREFERENCE", ""):
                        paths = get_all_browser_cookies_paths()
                        browsers = [p["browser"] for p in paths]
                        assert "Chrome" in browsers
                        assert "Firefox" in browsers
                        
                    # Test explicit preference: Firefox first
                    with patch("app.core.config.settings.BROWSER_PREFERENCE", "firefox,chrome"):
                        paths = get_all_browser_cookies_paths()
                        browsers = [p["browser"] for p in paths]
                        first_browser = next(b for b in browsers if b in ["Firefox", "Chrome"])
                        assert first_browser == "Firefox"

                    # Test unknown browser in preference (should be ignored)
                    with patch("app.core.config.settings.BROWSER_PREFERENCE", "opera,firefox"):
                        paths = get_all_browser_cookies_paths()
                        browsers = [p["browser"] for p in paths]
                        assert browsers[0] == "Firefox"
