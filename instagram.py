import asyncio
import os
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
            data = ProfileData(
                username=profile.username,
                followers=profile.followers,
                following=profile.followees,
                posts=profile.mediacount,
                profile_pic_url=profile.profile_pic_url,
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
        state, profile = await asyncio.to_thread(self._check_sync, username)
        class Result:
            pass
        result = Result()
        result.state = state
        result.profile = profile
        return result
