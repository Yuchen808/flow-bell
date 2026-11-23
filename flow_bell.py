"""Flow Bell: four pills on the desktop. One click starts a block of focused work.

1 / 2 / 3 / 4 dots = 30 / 60 / 90 / 120 minutes. Click a pill: a short organ piece plays,
and the pill dims from right to left until the time is up. Click the running pill to stop.
Right-drag moves the bar, right-click opens the menu. Windows only.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import time
import tkinter as tk
from pathlib import Path

STATE_DIR = Path.home() / ".flow-bell"
STATE_FILE = STATE_DIR / "state.json"
SOUND_DIR = Path(__file__).parent / "sounds"
BG, RED, TOP_RED = "#0b0b0b", "#e5322d", 0.82
FLOW_DOT = (0.02, 0.02, 0.03)
FLOW_MINUTES = (30, 60, 90, 120)
WIDTH, PAD = 360, 22


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


class Sound:
    """用 Windows 自带的 MCI 放 mp3：不用装别的库，能调音量（淡出用）。所有调用都要在同一个线程。"""
    ALIAS = "flowbell"

    def __init__(self):
        self._mci = ctypes.windll.winmm.mciSendStringW

    def _send(self, cmd):
        return self._mci(cmd, None, 0, None)

    def play(self, path):
        self.stop()
        if path.exists():
            self._send(f'open "{path}" type mpegvideo alias {self.ALIAS}')
            self.volume(1)
            self._send(f"play {self.ALIAS}")
            self._unmute()

    @staticmethod
    def _unmute():
        """The Windows volume mixer remembers a volume per program and can silently leave it at 0. Reset ours on play."""
        try:
            from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
            me = os.getpid()
            for s in AudioUtilities.GetAllSessions():
                if s.Process and s.Process.pid == me:
                    v = s._ctl.QueryInterface(ISimpleAudioVolume)
                    v.SetMasterVolume(1.0, None)
                    v.SetMute(0, None)
        except Exception:
            pass

    def volume(self, v):
        self._send(f"setaudio {self.ALIAS} volume to {int(max(0, min(1, v)) * 1000)}")

    def stop(self):
        self._send(f"stop {self.ALIAS}")
        self._send(f"close {self.ALIAS}")


_u32, _g32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")  # 单独的句柄，给下面几个函数声明 64 位安全的参数类型
_u32.GetDC.restype = _g32.CreateCompatibleDC.restype = _g32.CreateDIBSection.restype = ctypes.c_void_p
_u32.GetParent.restype = _g32.SelectObject.restype = ctypes.c_void_p
_u32.GetParent.argtypes = [ctypes.c_void_p]
_u32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_u32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
_u32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_g32.CreateCompatibleDC.argtypes = _g32.DeleteDC.argtypes = _g32.DeleteObject.argtypes = [ctypes.c_void_p]
_g32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_g32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
_u32.UpdateLayeredWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def ImageFont_cn(size, _cache={}):
    """带中文的小字（提示用）。"""
    from PIL import ImageFont
    if size not in _cache:
        for name in ("msyh.ttc", "simhei.ttf", "segoeui.ttf"):
            try:
                _cache[size] = ImageFont.truetype(name, size)
                break
            except Exception:
                continue
    return _cache[size]


class FlowBar:
    """面板下面单独的一条透明窗口：一串长药丸，细颈相连，每颗上面压着几个正圆的蓝点（= 几个半小时）。
    点一颗 = 开始这么多分钟：放开工声，其他几颗淡下去，这一颗从右往左熄掉；熄完敲一声钟。再点一次 = 停。
    窗口是逐像素透明的，药丸之外能直接点到桌面。

    动效的规矩（出自 vault「Design Engineering · 第一性原理」那张表）：
    - 没有东西瞬间变：悬停、变暗、闪光都是一个 0–1 的量，每帧朝目标靠近一点（先快后慢 = ease-out）。
    - 可以打断：永远从「现在的值」出发，鼠标中途移开不会跳。
    - 进快出慢、都在 300 ms 以内；按下去立刻有反应（先暗一下）。
    - 不动的时候不画：没有量在变，循环就停，不占 CPU。
    静态的质感只算一次（_build），每帧只做几次乘加，所以能跑到 60 帧。"""
    SS = 2            # 画 2 倍再缩小，边缘才不锯齿
    GRID = 3.1                       # 红绿点阵的点距（逻辑像素）。试过 2.3：在 1 倍屏上小到和像素打架，出花纹，改回来了
    PH, NECK, NECK_T = 22, 20, 3.4   # 药丸的高；两颗之间细颈的长和粗（逻辑像素）
    DOT_R, DOT_SP = 3.4, 11.0        # 圆点的半径和圆心距（互不重叠）
    # 每个量靠近目标的快慢（时间常数，秒）：值越小越快。大约 3 倍时间常数走完 95%。
    TAU = {"hover_in": 0.045, "hover_out": 0.090, "dim_in": 0.080, "dim_out": 0.110, "press": 0.050, "flash": 0.130, "run": 0.120}
    HOVER_GAIN, DIM_ALPHA, PRESS_DIP, PULSE = 0.16, 0.12, 0.14, 0.28
    NOSE, NOSE_N, NOSE_FLAT, EDGE_SOFT = 2.0, 2.3, 0.4, 12   # 圆头多长（× 药丸半高）；多方（2 = 椭圆，越大越方）；剩多少时开始压平；交界的模糊有多宽（逻辑像素）
    SPENT, TAIL = 0.38, 0.6          # 计时中：走掉的那段暗到多少；两头伸出去的尾巴固定亮到多少（左右一样，不跟着走）

    def __init__(self, host):
        import numpy as np
        from PIL import ImageFont
        self.np, self.host, self.root, self.scale = np, host, host.root, host.scale
        self.sound = Sound()
        st = host.state.get("flow") or {}
        self.flow = st if st.get("end", 0) > time.time() else None   # 面板重启后接着计时，不重放开工声
        self.hover, self.pos = None, None
        self.speed = host.state.get("flow_speed", 1)  # 试跑倍速：60 = 90 分钟压成 90 秒。右键菜单里切换，只影响之后开始的那一段
        self.margin = int(8 * self.scale)             # 窗口比面板左右各宽出这么多，放光晕和色散的边；药丸本体和面板两边对齐
        self.lead = int(8 * self.scale) + host.line_h   # 顶上多出的一截：红线 + 线和药丸之间的留白
        self.w, self.h = host.w + 2 * self.margin, int((self.PH + 36) * self.scale) + self.lead
        try:
            self.font = ImageFont.truetype("Inter-Medium.ttf", int(11 * self.scale))
        except Exception:
            self.font = ImageFont.truetype("segoeui.ttf", int(11 * self.scale))
        self.top = tk.Toplevel(self.root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", host.topmost)
        self.top.geometry(f"{self.w}x{self.h}+0+0")
        self.top.update_idletasks()
        self.hwnd = _u32.GetParent(self.top.winfo_id())
        _u32.SetWindowLongW(self.hwnd, -20, _u32.GetWindowLongW(self.hwnd, -20) | 0x80000 | 0x80)  # LAYERED | TOOLWINDOW
        self.top.bind("<Button-1>", lambda e: self.click(self._group_at(e.x)))
        self.top.bind("<Motion>", lambda e: self._set_hover(self._group_at(e.x)))
        self.top.bind("<Leave>", lambda e: self._set_hover(None))
        self._layout()
        n = len(self.groups)
        dim0 = [1.0 if self.flow and self.flow["min"] != g[0] else 0.0 for g in self.groups]
        self.v = {"hover": np.zeros(n, np.float32), "dim": np.array(dim0, np.float32),
                  "press": np.zeros(n, np.float32), "flash": np.zeros(n, np.float32),
                  "run": np.array([1.0 if self.flow and self.flow["min"] == g[0] else 0.0 for g in self.groups], np.float32)}
        self.level = np.ones(n, np.float32)           # 每颗还剩多少（0–1）；停下之后留着最后的值，好让它平滑地亮回来
        self._anim, self._last = None, time.perf_counter()
        self._build()
        self.place()
        self.tick()

    # ----- 几何 -----
    def _layout(self):
        """每颗药丸：(分钟, 左端 x, 右端 x)，单位 = 放大后的像素。"""
        k = self.scale * self.SS
        n, neck = len(FLOW_MINUTES), self.NECK * k
        x, self.groups = self.margin * self.SS, []
        self.pl = (self.w * self.SS - 2 * x - (n - 1) * neck) / n     # 一颗的长度
        for m in FLOW_MINUTES:
            self.groups.append((m, x, x + self.pl))
            x += self.pl + neck
        self.cy = (self.PH / 2 + 10) * k + self.lead * self.SS

    def _index(self, m):
        return next((i for i, g in enumerate(self.groups) if g[0] == m), None)

    def _group_at(self, x, y=None):
        xs, half = x * self.SS, self.NECK * self.scale * self.SS / 2
        for m, x0, x1 in self.groups:
            if x0 - half <= xs <= x1 + half:
                return m
        return None

    def _set_hover(self, m):
        if m != self.hover:
            self.hover = m
            self.top.config(cursor="hand2" if m and (not self.flow or self.flow["min"] == m) else "arrow")
            self.kick()

    def place(self):
        r = self.root
        pos = (r.winfo_x() - self.margin, r.winfo_y() + getattr(self.host, "cur_h", r.winfo_height()) - self.host.line_h)
        if pos != self.pos:
            first, self.pos = self.pos is None, pos
            if first:
                self.draw()
            else:                                                    # 只是位置变了：挪窗口就行，不用重画
                _u32.SetWindowPos(self.hwnd, None, pos[0], pos[1], 0, 0, 0x0001 | 0x0004 | 0x0010)   # NOSIZE | NOZORDER | NOACTIVATE

    # ----- 画：静态的部分只算一次 -----
    def _build(self):
        """质感照参考图：红色的底 + 贴在曲面上的绿色点阵（正对你的地方偏绿黄，转到边缘只剩红），左上一片洋红的光；
        每颗的左右两端把红、蓝通道错开一点 = 色散。算出两张图：A = 全亮，B = 熄掉的影子。"""
        np = self.np
        k = self.scale * self.SS
        W, H, cy = self.w * self.SS, self.h * self.SS, self.cy
        rr, pl, neck = self.PH * k / 2, self.pl, self.NECK * k
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        bounds = np.array([g[2] + neck / 2 for g in self.groups[:-1]], np.float32)
        pid = np.searchsorted(bounds, xx[0]).astype(int)[None, :].repeat(H, 0)   # 每一列属于哪颗（分界 = 细颈中点）
        xa = np.array([g[1] for g in self.groups], np.float32)[pid]
        cx = xa + pl / 2
        # 形状：一颗颗胶囊，和贯穿全长的一根细杆做平滑并集 -> 两头收成细颈
        qx = np.abs(xx - cx) - (pl / 2 - rr)
        sd = np.where(qx > 0, np.hypot(np.clip(qx, 0, None), yy - cy), np.abs(yy - cy)) - rr
        x_first, x_last = self.groups[0][1] + rr, self.groups[-1][2] - rr
        bar = np.hypot(np.clip(np.abs(xx - (x_first + x_last) / 2) - (x_last - x_first) / 2, 0, None), yy - cy) - self.NECK_T * k / 2
        kk = 0.75 * rr
        hh = np.clip(0.5 + 0.5 * (bar - sd) / kk, 0, 1)
        sdf = bar * (1 - hh) + sd * hh - kk * hh * (1 - hh)
        e = np.clip(-sdf / rr, 0, 1)
        z = np.sqrt(1 - (1 - e) ** 2)                                # 曲面朝向你的程度：中线 1，边缘 0
        ly = np.clip((yy - cy) / rr, -1, 1)
        fq = 2 * np.pi / (self.GRID * k)                             # 点距（逻辑像素）。参考图量出来是 3–3.7 px，这里按 Yuchen 的要求再小一些
        dots = (0.5 + 0.5 * np.clip(np.cos((xx - cx) * fq) * np.cos(np.arcsin(ly) * rr * fq) * 1.6 + 0.15, 0, 1)) * z ** 0.8
        sheen = np.clip(-((xx - cx) / (pl / 2) * 0.45 + ly * 0.7) + 0.05, 0, 1) * z * z
        col = np.stack([0.70 + 0.22 * (1 - z) - 0.28 * dots + 0.45 * sheen,
                        0.02 + 0.90 * dots - 0.25 * sheen,
                        0.10 + 0.06 * dots + 0.85 * sheen], -1)
        alpha = np.clip(0.5 - sdf / (1.3 * self.SS), 0, 1) + 0.20 * np.exp(-np.clip(sdf, 0, None) / (2.4 * k)) * (sdf > 0)
        sh = max(1, int(round(2.2 * k)))
        wgt = (np.clip(np.abs(xx - cx) / (pl / 2), 0, 1) ** 3)[..., None] * 0.9
        dot_rgb = np.array(FLOW_DOT, np.float32)

        def compose(rgb, a, dot_a):
            g = np.concatenate([np.clip(rgb, 0, 1) * a[..., None], a[..., None]], -1)
            rl = np.roll(g[..., 0:1], -sh, 1)                        # 红：整体往左挪一点
            bl = np.roll(g[..., :2].max(-1, keepdims=True), sh, 1) * 0.9   # 蓝：用亮度往右挪出一条蓝边
            g[..., 0:1] = g[..., 0:1] * (1 - wgt) + rl * wgt
            g[..., 2:3] = g[..., 2:3] * (1 - wgt) + bl * wgt
            g[..., 3:4] = np.maximum(g[..., 3:4], np.maximum(rl, bl) * wgt)
            for m, x0, x1 in self.groups:                            # 圆点：正圆、边缘干净，压在最上面，不参与色散
                cnt = max(1, round(m / 30))
                for jdx in range(cnt):
                    dx = (x0 + x1) / 2 + (jdx - (cnt - 1) / 2) * self.DOT_SP * k
                    da = np.clip(0.5 - (np.hypot(xx - dx, yy - cy) - self.DOT_R * k) / (1.0 * self.SS), 0, 1)[..., None]
                    g[..., :3] = dot_rgb * da * dot_a + g[..., :3] * (1 - da)
                    g[..., 3:4] = np.maximum(g[..., 3:4], da * dot_a)
            return g

        self._A = compose(col, np.clip(alpha, 0, 1), 1.0)
        self._B = compose(col * self.SPENT, np.clip(alpha, 0, 1), 1.0)
        self._pid, self._frac = pid, (xx - xa) / pl
        t = np.maximum(np.clip((self._frac - 0.86) / 0.16, 0, 1), np.clip((0.14 - self._frac) / 0.16, 0, 1))
        self._rr = rr
        ay = np.clip(np.abs(yy - cy) / rr, 0, 1)
        self._cap = (1 - ay ** self.NOSE_N) ** (1 / self.NOSE_N)      # 圆头的轮廓（超椭圆）：中线 1，上下沿 0，在上下沿处和边相切
        self._tail = t * t * (3 - 2 * t)                              # 0 = 药丸身上，1 = 两头的圆头和细颈；中间平滑过渡
        # 每一列对每颗药丸的权重：药丸身上 = 自己 1；相邻两颗之间（这颗的圆头 -> 细颈 -> 下一颗的圆头）平滑地换手
        n, xs = len(self.groups), xx[0]
        wcol = np.zeros((xs.size, n), np.float32)
        for i2, (_, a0, a1) in enumerate(self.groups):
            c = rr * 0.9                                              # 换手的区间以细颈中点为中心，左右各伸进圆头这么多 -> 两边对称
            lo = np.ones_like(xs) if i2 == 0 else np.clip((xs - (self.groups[i2 - 1][2] - c)) / (a0 + c - (self.groups[i2 - 1][2] - c)), 0, 1)
            hi = np.ones_like(xs) if i2 == n - 1 else 1 - np.clip((xs - (a1 - c)) / (self.groups[i2 + 1][1] + c - (a1 - c)), 0, 1)
            wcol[:, i2] = np.minimum(lo * lo * (3 - 2 * lo), hi * hi * (3 - 2 * hi))
        self._wcol = wcol / wcol.sum(1, keepdims=True)
        box = (np.abs(yy - cy) < rr * 1.3) & (xx > self.groups[0][1] - rr * 0.5) & (xx < self.groups[-1][2] + rr * 0.5)
        self._lx = np.clip((xx[0] - self.margin * self.SS) / max(1.0, self.host.w * self.SS), 0, 1)   # 沿着红线从左到右 0–1
        self._lin = ((xx[0] >= self.margin * self.SS) & (xx[0] <= (self.margin + self.host.w) * self.SS)).astype(np.float32)
        self._lred = np.array([int(RED[i:i + 2], 16) / 255 for i in (1, 3, 5)], np.float32)
        self._lbg = np.array([int(BG[i:i + 2], 16) / 255 for i in (1, 3, 5)], np.float32)
        self._floor = box * (2 / 255)                                 # 外接框留一点点不透明，细颈旁边也点得到

    def render(self):
        np = self.np
        from PIL import Image, ImageDraw
        now, f, pid, v = time.time(), self.flow, self._pid, self.v
        n = len(self.groups)
        if f:
            self.level[self._index(f["min"])] = max(0.0, (f["end"] - now) / (f["end"] - f["start"]))
        level = self.level
        # 计时只在药丸身上走（从右往左暗下去）；两头的尾巴不参与，左右固定一样亮。
        # 亮的那一段的前沿是一个朝右的、拉长的半椭圆头：上下两边先贴着药丸的上下沿走（在那里和边相切），
        # 再慢慢收成一个圆头。走到左边最后一段（剩 NOSE_FLAT 以下）时，圆头慢慢压平，最后贴着左端消失。
        lv = level[pid]
        fl = np.clip(lv / self.NOSE_FLAT, 0, 1)
        nose = self.NOSE * self._rr * fl * fl * (3 - 2 * fl)           # 圆头有多长（像素）
        # 圆头整个算在时间里：刚开始时圆头的上下沿正好在药丸右端，一点下去右上、右下角就开始暗，不会先空走一段
        edge = .12 * self.pl + lv * (.74 * self.pl + nose) - nose + nose * self._cap        # 这一行上，亮到离药丸左端多远（中线最远）
        lit = np.clip((edge - self._frac * self.pl) / (self.EDGE_SOFT * self.scale * self.SS) + 0.5, 0, 1)
        lit = lit * lit * (3 - 2 * lit)                                # 交界是一段模糊的过渡，两头缓进缓出
        lit = lit * (1 - self._tail) + self.TAIL * self._tail
        run = v["run"][pid]                                           # 这颗在不在计时（0–1，渐变）；不在计时 = 整颗全亮
        lit = (1 - run + lit * run)[..., None]
        g = self._A * lit + self._B * (1 - lit)
        gain = (1 + self.HOVER_GAIN * v["hover"]) * (1 - self.PRESS_DIP * v["press"])
        amul = 1 - (1 - self.DIM_ALPHA) * v["dim"]
        col = lambda arr: (self._wcol @ arr.astype(np.float32))[None, :, None]   # 每颗一个数 -> 每一列一个数，颗与颗之间平滑过渡
        g[..., :3] *= col(gain)
        g[..., :3] *= 1 + self.PULSE * col(v["flash"])                # 开始 / 结束：亮一下再退回去，不变白
        g *= col(amul)
        g[..., 3] = np.maximum(g[..., 3], self._floor)
        if getattr(self.host, "_collapsed", False):                   # 面板还没显出来：顶上那条两端渐隐的细红线由这里画
            m = getattr(self.host, "line_m", 0.0)
            fade = 0.22 + (1 / self.host.w - 0.22) * m                # 渐隐从 22% 一路收到没有：交接时和面板自己那条整线一模一样
            la = np.clip(np.minimum(self._lx, 1 - self._lx) / fade, 0, 1)
            la = la * la * (3 - 2 * la) * self._lin
            # 变淡 = 往面板的底色（深色）靠，不是变透明：这样交接给面板自己的线时颜色一样，浅色背景上也不会先发白再变深
            lc = self._lbg + (self._lred - self._lbg) * (1 - (1 - TOP_RED) * m)
            g[: self.host.line_h * self.SS] = np.concatenate([lc[None, :] * la[:, None], la[:, None]], -1)[None]
        # 缩回 1 倍：每 SS×SS 个像素取平均。不用 PIL 的 resize —— 它会把 RGBA 当成「没预乘」的再处理一遍，
        # 半透明的边缘（药丸的光晕、红线的两端）会被算得过亮，在浅色背景上是一圈发白的边。
        S = self.SS
        g = g.reshape(g.shape[0] // S, S, g.shape[1] // S, S, 4).mean((1, 3))
        img = Image.fromarray((np.clip(g, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")
        note = None if f or time.time() - getattr(self, "speed_note", 0) > 2.5 else ("Test run x60 on: 1 minute = 1 second" if self.speed > 1 else "Test run off: real time")
        if note:
            mask = Image.new("L", img.size, 0)
            ImageDraw.Draw(mask).text((self.w / 2, self.cy / self.SS + self.PH * self.scale / 2 + 9 * self.scale), note,
                                      font=ImageFont_cn(int(10 * self.scale)), fill=255, anchor="mm")
            mk = np.asarray(mask).astype(np.float32)[..., None] / 255
            arr = np.asarray(img).astype(np.float32)
            arr[..., :3] = 168 * mk + arr[..., :3] * (1 - mk)
            arr[..., 3:4] = np.maximum(arr[..., 3:4], 255 * mk)
            img = Image.fromarray(arr.astype(np.uint8), "RGBA")
            self.root.after(2600, self.draw)
        if f:                                                        # 剩余时间：小字，在正在走的那颗正下方
            left = max(0, int((f["end"] - now) * f.get("speed", 1) + 0.999))
            _, x0, x1 = self.groups[self._index(f["min"])]
            rr = self.PH * self.scale / 2
            mask = Image.new("L", img.size, 0)
            ImageDraw.Draw(mask).text(((x0 + x1) / 2 / self.SS, self.cy / self.SS + rr + 9 * self.scale),
                                      ("×60  " if f.get("speed", 1) > 1 else "") + f"{left // 60:02d}:{left % 60:02d}", font=self.font, fill=255, anchor="mm")
            mk = np.asarray(mask).astype(np.float32)[..., None] / 255
            arr = np.asarray(img).astype(np.float32)
            arr[..., :3] = 168 * mk + arr[..., :3] * (1 - mk)
            arr[..., 3:4] = np.maximum(arr[..., 3:4], 255 * mk)
            img = Image.fromarray(arr.astype(np.uint8), "RGBA")
        return img

    def draw(self):
        if self.pos is None:
            return
        try:
            raw = self.render().tobytes("raw", "BGRA")
        except Exception:
            return
        w, h = self.w, self.h
        screen = _u32.GetDC(None)
        mem = _g32.CreateCompatibleDC(screen)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bmp = _g32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, raw, len(raw))
        old = _g32.SelectObject(mem, bmp)
        blend = (ctypes.c_ubyte * 4)(0, 0, 255, 1)                   # AC_SRC_OVER，整体不透明度 255，用每个像素自己的 alpha
        _u32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wt.POINT(*self.pos)), ctypes.byref(wt.SIZE(w, h)),
                                 mem, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), 2)
        _g32.SelectObject(mem, old)
        _g32.DeleteObject(bmp)
        _g32.DeleteDC(mem)
        _u32.ReleaseDC(None, screen)

    # ----- 动：每个量朝目标靠近 -----
    def _targets(self):
        np, n = self.np, len(self.groups)
        hov, dim = np.zeros(n, np.float32), np.zeros(n, np.float32)
        act = self._index(self.flow["min"]) if self.flow else None
        if self.hover is not None and (act is None or self._index(self.hover) == act):
            hov[self._index(self.hover)] = 1
        if act is not None:
            dim[:] = 1
            dim[act] = 0
        run = np.zeros(n, np.float32)
        if act is not None:
            run[act] = 1
        return {"hover": hov, "dim": dim, "press": np.zeros(n, np.float32), "flash": np.zeros(n, np.float32), "run": run}

    def kick(self):
        """有东西要变了：没在跑就把循环叫起来。"""
        if self._anim is None:
            self._last = time.perf_counter()
            self._anim = self.root.after(0, self._step)

    def _step(self):
        np = self.np
        now = time.perf_counter()
        dt, self._last = min(0.05, now - self._last), now
        moving = False
        for key, tgt in self._targets().items():
            cur = self.v[key]
            up = tgt > cur
            tau = {"hover": np.where(up, self.TAU["hover_in"], self.TAU["hover_out"]),
                   "dim": np.where(up, self.TAU["dim_in"], self.TAU["dim_out"]),
                   "press": self.TAU["press"], "flash": self.TAU["flash"], "run": self.TAU["run"]}[key]
            nxt = cur + (tgt - cur) * (1 - np.exp(-dt / tau))         # 指数靠近：先快后慢，随时能改目标
            nxt = np.where(np.abs(tgt - nxt) < 0.004, tgt, nxt)
            moving |= bool(np.any(nxt != cur))
            self.v[key] = nxt.astype(np.float32)
        self.draw()
        self._anim = self.root.after(8, self._step) if moving else None

    # ----- 行为 -----
    def _save(self):
        if self.flow:
            self.host.state["flow"] = self.flow
        else:
            self.host.state.pop("flow", None)
        save_state(self.host.state)

    def click(self, m):
        if m is None:
            return
        i = self._index(m)
        if self.flow and self.flow["min"] == m:
            self.v["press"][i] = 1                                    # 按下去先暗一下，再弹回来
            self.stop()
        elif not self.flow:
            now = time.time()
            self.flow = {"min": m, "start": now, "end": now + m * 60 / self.speed, "speed": self.speed}
            self.level[:] = 1
            self._save()
            self.sound.play(SOUND_DIR / "open.mp3")
            for ms in (400, 1500):                                    # 这一路声音在系统里出现要一点时间：晚一点再确认两次音量没被调到 0
                self.root.after(ms, self.sound._unmute)
            self.v["flash"][i] = 1                                    # 开始：闪一下白，慢慢退掉
        self.kick()

    def stop(self, fade=15):
        """手动停：声音 1.5 秒淡出。"""
        self.flow = None
        self._save()
        self.kick()

        def step(i):
            if self.flow:                                            # 淡出途中又开了新的一段，别去动它
                return
            if i <= 0:
                self.sound.stop()
            else:
                self.sound.volume(i / fade)
                self.root.after(100, step, i - 1)

        step(fade)

    def tick(self):
        if self.flow and time.time() >= self.flow["end"]:
            i = self._index(self.flow["min"])
            self.flow = None
            self._save()
            self.sound.play(SOUND_DIR / "close.mp3")
            self.v["flash"][i] = 1
            self.kick()
        elif self.flow and self._anim is None:
            self.draw()
        if not self.flow and self._anim is None and time.time() - getattr(self, "_kept", 0) > 5:   # Idle: repaint and re-assert topmost every 5 s. Windows can drop a layered window after sleep or a display change
            self._kept = time.time()
            self.draw()
        if self.host.topmost and time.time() - getattr(self, "_raised", 0) > 5:
            self._raised = time.time()
            _u32.SetWindowPos(self.hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)   # HWND_TOPMOST
        fast = self.flow and self.flow.get("speed", 1) > 1
        self.root.after(33 if fast else 1000, self.tick)   # 试跑时每帧都画，才看得出渐暗是怎么走的

    def toggle_speed(self):
        self.speed = 1 if self.speed > 1 else 60
        self.host.state["flow_speed"] = self.speed
        save_state(self.host.state)
        self.speed_note = time.time()
        self.draw()


class Host:
    """What FlowBar needs from a window: a hidden Tk root, a scale, a position and saved state."""

    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.withdraw()
        self.scale = self.root.winfo_fpixels("1i") / 96
        self.state, self.topmost = load_state(), True
        self.w, self.pad, self.line_h = int(WIDTH * self.scale), int(PAD * self.scale), 1
        self.corner_r, self.expand, self.cur_h = int(8 * self.scale), 1.0, 0
        sw = self.root.winfo_screenwidth()
        self.pos = tuple(self.state.get("pos", (sw - self.w - int(60 * self.scale), int(40 * self.scale))))


class App(FlowBar):
    def __init__(self, host):
        super().__init__(host)
        self.menu = tk.Menu(host.root, tearoff=0)
        self.menu.add_command(label="Test run x60 (90 min = 90 s)", command=self.toggle_speed)
        self.menu.add_command(label="Quit", command=host.root.destroy)
        self.top.bind("<Button-3>", self._grab)
        self.top.bind("<B3-Motion>", self._drag)
        self.top.bind("<ButtonRelease-3>", self._drop)

    def place(self):
        if self.pos != self.host.pos:
            self.pos = self.host.pos
            self.draw()

    def _grab(self, e):
        self._from, self._moved = (e.x_root, e.y_root, *self.host.pos), False

    def _drag(self, e):
        x, y, px, py = self._from
        self._moved = self._moved or abs(e.x_root - x) + abs(e.y_root - y) > 4
        if self._moved:
            self.host.pos = (px + e.x_root - x, py + e.y_root - y)
            self.place()

    def _drop(self, e):
        if self._moved:
            self.host.state["pos"] = list(self.host.pos)
            save_state(self.host.state)
        else:
            self.menu.tk_popup(e.x_root, e.y_root)


if __name__ == "__main__":
    h = Host()
    App(h)
    h.root.mainloop()
