import tempfile
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

def _avatar(url, unavailable=False):
    size = 220
    if unavailable or not url:
        im = Image.new("RGB", (size, size), (205, 205, 205))
        d = ImageDraw.Draw(im)
        d.ellipse((70, 45, 150, 125), fill=(145, 145, 145))
        d.rounded_rectangle((45, 125, 175, 205), radius=40, fill=(145, 145, 145))
        d.line((20, 20, 200, 200), fill=(225, 35, 40), width=18)
        d.line((200, 20, 20, 200), fill=(225, 35, 40), width=18)
        return im
    try:
        with urllib.request.urlopen(url, timeout=12) as r:
            data = r.read()
        from io import BytesIO
        im = Image.open(BytesIO(data)).convert("RGB")
        return im.resize((size, size))
    except Exception:
        return _avatar(None, unavailable=True)

def create_profile_card(profile, username, unavailable=False):
    # Visual design is inspired by the attached Discord screenshot:
    # dark Instagram-style panel, avatar, Follow button, stats.
    W, H = 980, 430
    bg = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(bg)

    avatar = _avatar(getattr(profile, "profile_pic_url", None), unavailable)
    mask = Image.new("L", (220, 220), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 220, 220), fill=255)
    bg.paste(avatar, (65, 105), mask)

    white = (245, 245, 245)
    blue = (20, 145, 245)
    gray = (180, 180, 180)

    draw.text((345, 112), username, fill=white, font=_font(36, True))
    draw.rounded_rectangle((500, 105, 650, 155), radius=12, fill=blue)
    draw.text((545, 116), "Follow", fill=white, font=_font(23, True))
    draw.text((675, 112), "•••", fill=white, font=_font(30, True))

    posts = getattr(profile, "posts", 0) if profile else 0
    followers = getattr(profile, "followers", 0) if profile else 0
    following = getattr(profile, "following", 0) if profile else 0

    def num(v):
        if v >= 1_000_000:
            return f"{v:,}"
        return f"{v:,}"

    draw.text((345, 190), f"{num(posts)} posts", fill=white, font=_font(27))
    draw.text((545, 190), f"{num(followers)} followers", fill=white, font=_font(27))
    draw.text((780, 190), f"{num(following)} following", fill=white, font=_font(27))

    # Visible ownership watermark on every generated profile card.
    draw.text((W - 210, H - 38), "shivannnkk", fill=(145, 145, 145), font=_font(20, True))

    if unavailable:
        draw.text((345, 250), "UserNotFound", fill=gray, font=_font(22))
    else:
        draw.text((345, 250), f"@{username}", fill=gray, font=_font(22))
        caption = getattr(profile, "latest_post_caption", None)
        post_date = getattr(profile, "latest_post_date", None)
        if caption or post_date:
            draw.text((345, 300), "Latest post", fill=white, font=_font(20, True))
            post_text = (caption or "No caption").replace("\n", " ")[:58]
            draw.text((345, 330), post_text, fill=gray, font=_font(18))

    out = Path(tempfile.gettempdir()) / f"shivannnkk_{username}.png"
    bg.save(out, "PNG")
    return str(out)
