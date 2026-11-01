import asyncio
import logging
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from urllib.parse import unquote

import instaloader

log = logging.getLogger("shivannnkk-instagram-monitor")


class AccountState(str, Enum):
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"

@dataclass
class ProfileData:
    username: str
    followers: int = 0
    following: int = 0
    posts: int = 0
    profile_pic_url: str | None = None
    latest_post_url: str | None = None
    latest_post_caption: str | None = None
    latest_post_date: str | None = None

    @property
    def followers_display(self):
        return compact_number(self.followers)

    @property
    def following_display(self):
        return compact_number(self.following)

def compact_number(value: int) -> str:
    value = int(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".rstrip("0").rstrip(".") + "M" if value % 1_000_000 else f"{value // 1_000_000}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K".rstrip("0").rstrip(".") + "K" if value % 1_000 else f"{value // 1_000}K"
    return str(value)

class InstagramChecker:
    def __init__(self):
        self.timeout_seconds = max(5, int(os.getenv("INSTAGRAM_CHECK_TIMEOUT_SECONDS", "25")))
        self.cache_seconds = max(0, int(os.getenv("INSTAGRAM_CACHE_SECONDS", "60")))
        self.retry_count = max(0, int(os.getenv("INSTAGRAM_RETRY_COUNT", "2")))
        self.retry_delay_seconds = max(0.1, float(os.getenv("INSTAGRAM_RETRY_DELAY_SECONDS", "2")))
        self._lock = asyncio.Lock()
        self._inflight_checks = set()
        self._cache = {}
        self.loader = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
        )
        # Optional session support. Set INSTAGRAM_SESSIONID if Instagram requires a logged-in session.
        sessionid = os.getenv("INSTAGRAM_SESSIONID", "").strip()
        if sessionid:
            try:
                self.loader.context._session.cookies.set(
                    "sessionid", unquote(sessionid), domain=".instagram.com", path="/"
                )
            except Exception:
                log.warning("Could not configure the Instagram session cookie")

    def _check_html_sync(self, username: str):
        """Use Instagram's public profile HTML when Instaloader's endpoint is limited."""
        try:
            response = self.loader.context.get(
                f"https://www.instagram.com/{username}/",
                timeout=self.timeout_seconds,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            if response.status_code == 404:
                return AccountState.UNAVAILABLE, None
            if response.status_code != 200:
                log.info(
                    "Instagram returned HTTP %s while checking @%s",
                    response.status_code,
                    username,
                )
                return AccountState.UNKNOWN, None

            body = response.text
            # Instagram frequently changes its HTML. These markers cover the
            # profile JSON and the OpenGraph fallback without treating a login,
            # challenge, or consent page as proof that an account is unavailable.
            profile_marker = (
                '"is_private"' in body
                or 'profilePage_' in body
                or f'"username":"{username}"' in body
            )
            unavailable_marker = any(
                marker in body.lower()
                for marker in (
                    "sorry, this page isn't available",
                    "page isn't available",
                    "the link you followed may be broken",
                )
            )
            if unavailable_marker:
                return AccountState.UNAVAILABLE, None
            login_or_challenge = any(
                marker in body.lower()
                for marker in ("log in", "login", "challenge", "consent")
            )
            if not profile_marker:
                reason = "login/challenge page" if login_or_challenge else "unrecognised HTML"
                log.info("Instagram verification for @%s returned %s", username, reason)
                return AccountState.UNKNOWN, None

            match = re.search(r'"username":"([^"\\]+)"', body)
            profile_username = match.group(1) if match else username
            return AccountState.ACTIVE, ProfileData(username=profile_username)
        except Exception as error:
            log.info(
                "Instagram HTML request failed for @%s: %s",
                username,
                type(error).__name__,
            )
            return AccountState.UNKNOWN, None

    def _check_sync(self, username: str):
        for attempt in range(self.retry_count + 1):
            try:
                profile = instaloader.Profile.from_username(self.loader.context, username)
                data = ProfileData(
                    username=profile.username,
                    followers=profile.followers,
                    following=profile.followees,
                    posts=profile.mediacount,
                    profile_pic_url=profile.profile_pic_url,
                )
                # Fetching posts is optional and creates another rate-limit opportunity.
                if os.getenv("INSTAGRAM_FETCH_LATEST_POST", "0") == "1":
                    try:
                        latest_post = next(profile.get_posts())
                        data.latest_post_url = f"https://www.instagram.com/p/{latest_post.shortcode}/"
                        data.latest_post_caption = (latest_post.caption or "").strip() or None
                        data.latest_post_date = latest_post.date_utc.strftime("%b %d, %Y")
                    except Exception:
                        pass
                return AccountState.ACTIVE, data
            except instaloader.exceptions.ProfileNotExistsException:
                return AccountState.UNAVAILABLE, None
            except instaloader.exceptions.LoginRequiredException:
                # A login wall is not evidence that the account is banned.
                log.info("Instagram requires login while checking @%s", username)
                return self._check_html_sync(username)
            except Exception as error:
                log.info(
                    "Instaloader check failed for @%s (attempt %s/%s): %s",
                    username,
                    attempt + 1,
                    self.retry_count + 1,
                    type(error).__name__,
                )
                if attempt < self.retry_count:
                    time.sleep(self.retry_delay_seconds)
                    continue
                return self._check_html_sync(username)
        return AccountState.UNKNOWN, None

    async def check(self, username: str):
        username = username.strip().lstrip("@").lower()
        class Result:
            state = AccountState.UNKNOWN
            profile = None

        cached = self._cache.get(username)
        if cached and self.cache_seconds and time.monotonic() - cached[0] < self.cache_seconds:
            result = Result()
            result.state, result.profile = cached[1]
            return result

        started = asyncio.Event()

        async def run_check():
            # Keep the lock held until the worker thread finishes. This prevents
            # a timed-out caller from allowing the shared Instaloader session to
            # be used concurrently by the next check.
            async with self._lock:
                started.set()
                return await asyncio.to_thread(self._check_sync, username)

        task = asyncio.create_task(run_check())
        self._inflight_checks.add(task)
        task.add_done_callback(self._inflight_checks.discard)
        try:
            state, profile = await asyncio.wait_for(asyncio.shield(task), timeout=self.timeout_seconds)
        except TimeoutError:
            if not started.is_set() and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            return Result()
        except Exception:
            return Result()
        self._cache[username] = (time.monotonic(), (state, profile))
        result = Result()
        result.state = state
        result.profile = profile
        return result
