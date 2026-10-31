import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import instaloader

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
    profile_pic_url: Optional[str] = None
    latest_post_url: Optional[str] = None
    latest_post_caption: Optional[str] = None
    latest_post_date: Optional[str] = None

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
        self._lock = asyncio.Lock()
        self._inflight_checks = set()
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
        sessionid = os.getenv("INSTAGRAM_SESSIONID")
        if sessionid:
            try:
                self.loader.context._session.cookies.set("sessionid", sessionid, domain=".instagram.com")
            except Exception:
                pass

    def _check_sync(self, username: str):
        try:
            profile = instaloader.Profile.from_username(self.loader.context, username)
            latest_post_url = None
            latest_post_caption = None
            latest_post_date = None
            try:
                latest_post = next(profile.get_posts())
                latest_post_url = f"https://www.instagram.com/p/{latest_post.shortcode}/"
                latest_post_caption = (latest_post.caption or "").strip() or None
                latest_post_date = latest_post.date_utc.strftime("%b %d, %Y")
            except Exception:
                pass
            data = ProfileData(
                username=profile.username,
                followers=profile.followers,
                following=profile.followees,
                posts=profile.mediacount,
                profile_pic_url=profile.profile_pic_url,
                latest_post_url=latest_post_url,
                latest_post_caption=latest_post_caption,
                latest_post_date=latest_post_date,
            )
            return AccountState.ACTIVE, data
        except instaloader.exceptions.ProfileNotExistsException:
            # This is a strong signal, but the monitor layer confirms it before alerting.
            return AccountState.UNAVAILABLE, None
        except (
            instaloader.exceptions.ConnectionException,
            instaloader.exceptions.QueryReturnedNotFoundException,
            instaloader.exceptions.BadResponseException,
            instaloader.exceptions.LoginRequiredException,
        ):
            return AccountState.UNKNOWN, None
        except Exception:
            return AccountState.UNKNOWN, None

    async def check(self, username: str):
        class Result:
            state = AccountState.UNKNOWN
            profile = None

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
        except asyncio.TimeoutError:
            if not started.is_set() and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            return Result()
        except Exception:
            return Result()
        result = Result()
        result.state = state
        result.profile = profile
        return result
