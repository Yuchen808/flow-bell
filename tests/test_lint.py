"""Every correction becomes a check that fails by itself. Each test names the mistake it prevents."""
import pathlib
import re
import subprocess
import sys
import time
import types

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import flow_bell as fb  # noqa: E402

FILES = [f for f in subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout.split("\0") if f]


def bar(minutes=90, left=1.0):
    k = 2
    host = types.SimpleNamespace(w=fb.WIDTH * k, pad=fb.PAD * k, scale=k, line_h=k, state={}, topmost=True, expand=1.0, corner_r=8 * k, root=None)
    b = fb.App.__new__(fb.App)
    b.np, b.host, b.root, b.scale, b.hover, b.pos, b.speed, b.flow = np, host, None, k, None, None, 1, None
    b.margin, b.lead = 8 * k, 8 * k + host.line_h
    b.w, b.h = host.w + 2 * b.margin, (b.PH + 36) * k + b.lead
    from PIL import ImageFont
    b.font = ImageFont.load_default()
    b._layout()
    n = len(b.groups)
    b.v = {q: np.zeros(n, np.float32) for q in ("hover", "dim", "press", "flash", "run")}
    b.level = np.ones(n, np.float32)
    b._build()
    i = b._index(minutes)
    now = time.time()
    b.flow = {"min": minutes, "start": now - minutes * 60 * (1 - left), "end": now + minutes * 60 * left}
    b.v["dim"][:] = 1
    b.v["dim"][i] = 0
    b.v["run"][i] = 1
    return b, i


def shot(b):
    a = np.asarray(b.render()).astype(float)
    return a[: int(b.cy / b.SS + b.PH * b.scale / 2)]


def test_no_jump_after_start():
    """The pill froze, then jumped, at 0.1 % of the session."""
    f = [shot(bar(left=x)[0]) for x in (.9992, .9988)]
    assert np.abs(f[1] - f[0]).sum() < 400


def test_starts_dimming_at_once():
    """The lit edge ran outside the pill for the first quarter, so nothing seemed to happen."""
    assert np.abs(shot(bar(left=.95)[0]) - shot(bar(left=1.0)[0])).sum() > 2000


def test_both_tails_stay_lit():
    """The right neck went dark the moment the timer started, the left one only at the end."""
    tail = []
    for left in (.99, .5, .05):
        b, i = bar(minutes=60, left=left)
        a = shot(b)
        _, x0, x1 = b.groups[i]
        d = b.NECK * b.scale * b.SS / 4
        tail.append([a[:, int(round(x / b.SS)) - 1:int(round(x / b.SS)) + 2, :3].sum() for x in (x0 - d, x1 + d)])
    tail = np.array(tail)
    assert (tail.max(0) / tail.min(0) < 1.15).all(), tail


def test_no_white_flash():
    """Starting must brighten the pill, not bleach it."""
    b, i = bar(left=1.0)
    b.v["flash"][i] = 1
    _, x0, x1 = b.groups[i]
    a = shot(b)[:, int(x0 / b.SS) + 10:int(x1 / b.SS) - 10]
    assert (a[..., :3].min(-1) > 235).mean() < .02


def test_dots_stay_dark():
    b, i = bar(left=.5)
    _, x0, x1 = b.groups[i]
    assert shot(b)[int(b.cy / b.SS), int((x0 + x1) / 2 / b.SS), :3].max() < 40


def decode(path):
    import imageio_ffmpeg
    raw = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", "22050", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).astype(float)


SOUNDS = [f for f in FILES if f.endswith(".mp3")]


@pytest.mark.parametrize("f", SOUNDS)
def test_sound_is_clean(f):
    """No dead time before the sound, no clipping, no cut-off ending, no hour-long cue."""
    x = decode(ROOT / f)
    sr = 22050
    assert len(x) / sr < 60
    assert np.abs(x).max() < 1.0
    assert np.argmax(np.abs(x) > .01 * np.abs(x).max()) / sr < .7
    assert np.abs(x[-int(.1 * sr):]).max() < .05


def test_bells_after_board():
    """The bells ring only after the board has stopped."""
    x = decode(ROOT / "sounds/open_board.mp3")
    e = np.array([np.sqrt((x[i:i + 441] ** 2).mean()) for i in range(0, len(x) - 441, 441)])
    t = np.arange(len(e)) * .02
    assert e[t < 18.8].max() < .7 * e[(t > 18.9) & (t < 19.9)].max()


def test_bells_start_without_a_click():
    """The bell recording begins mid-wave; every strike popped until each one got a fade-in."""
    x = decode(ROOT / "sounds/open_board.mp3")
    sr = 22050
    for t in (23.89, 28.89):
        assert np.abs(np.diff(x[int((t - .05) * sr):int((t + .15) * sr)])).max() < .1


@pytest.mark.parametrize("f", [f for f in SOUNDS if "close" in f])
def test_ending_starts_from_silence(f):
    """The ending began at full level on the first sample and crackled."""
    x = decode(ROOT / f)
    assert np.abs(x[:int(.005 * 22050)]).max() < .02 * np.abs(x).max()


def test_every_sound_is_credited():
    credits = (ROOT / "CREDITS.md").read_text(encoding="utf-8")
    assert [f for f in SOUNDS if f"`{f}`" not in credits] == []


def test_no_superseded_files():
    """Old versions carry _SS and stay out of the repo."""
    assert [f for f in FILES if re.search(r"_SS\b|_SS\.", f)] == []


def test_nothing_private():
    bs = chr(92) * 2
    bad = re.compile("[A-Z]:" + bs + "Users" + bs + "|[A-Z]:" + bs + r"\[0|ghp_|api[_-]?key *=|@gmail|todoist", re.I)
    hits = []
    for f in FILES:
        if f.endswith((".py", ".md", ".yml", ".txt")) and not f.startswith("tests/") and bad.search((ROOT / f).read_text(encoding="utf-8")):
            hits.append(f)
    assert hits == []


@pytest.mark.parametrize("bg, dark_text", [(255, True), (0, False), (128, True), (110, False)])
def test_countdown_readable_and_stable_on_any_background(bg, dark_text):
    """Grey digits vanished on mid backgrounds. The bar also reads back its own digits, which must not make it flicker."""
    from PIL import Image, ImageDraw, ImageFont
    b, _ = bar()
    b.pos = (0, 0)
    screen = np.full((b.h, b.w, 3), bg, np.float32)
    b._read_screen = lambda x, y, w, h: screen[y:y + h, x:x + w].copy()
    seen = []
    for text in ("26:36", "26:35", "26:34"):
        b._inv_at = 0
        mask = Image.new("L", (b.w, b.h), 0)
        ImageDraw.Draw(mask).text((b.w // 2, b.h - 30), text, font=ImageFont.load_default(), fill=255)
        inv, a = b._invert(mask), np.asarray(mask)[..., None] / 255
        screen = np.full((b.h, b.w, 3), bg, np.float32) * (1 - a) + inv * a
        seen.append(inv[np.asarray(mask) > 128].mean())
    assert all((x < bg) == dark_text and abs(x - bg) > 60 for x in seen), seen
    assert max(seen) - min(seen) < 3, seen


def test_screen_reader_does_not_shadow_the_drag_handler():
    """App already had a _grab (right-drag), which would have replaced a screen reader of the same name."""
    assert not set(vars(fb.App)) & {"_read_screen", "_invert"}
