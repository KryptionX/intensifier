#!/usr/bin/env python3
"""
intensifier - turn any image into a Slack-ready "intensifies" emoji GIF.

    ./intensify.py                    open the web UI (default)
    ./intensify.py --ui               same, explicitly
    ./intensify.py cat.png            CLI: writes cat-intensifies.gif
    ./intensify.py -p extreme *.png   CLI with a preset, many files

Needs: Python 3.8+ (stdlib only) and ImageMagick (`magick` or `convert`).
"""
import argparse
import copy
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
COMMENT = "Made with intensifier"

# --------------------------------------------------------------------------
# ImageMagick discovery (IM7 `magick`, or IM6 `convert`/`identify`)
# --------------------------------------------------------------------------
if shutil.which("magick"):
    IM = ["magick"]
    IDENTIFY = ["magick", "identify"]
elif shutil.which("convert"):
    IM = ["convert"]
    IDENTIFY = ["identify"]
else:
    IM = IDENTIFY = None


GIFSICLE = shutil.which("gifsicle")

BREW_HINT = "brew bundle --file=" + str(HERE / "Brewfile")


def need_im():
    if IM is None:
        raise RuntimeError(
            "ImageMagick not found. Install deps:  " + BREW_HINT + "   |  "
            "sudo apt install imagemagick  |  sudo dnf install ImageMagick"
        )


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "ImageMagick failed").strip()[:600])
    return r.stdout


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------
DEFAULTS = {
    # --- source prep
    "bg": "none",          # none | auto  (auto = flood-fill the background from the corners)
    "bg_fuzz": 12,         # % colour tolerance for background removal
    "trim": True,          # crop empty/transparent border before fitting
    # --- output geometry
    "size": 128,           # square edge in px
    "fit": "contain",      # contain (pad) | cover (crop) | stretch
    "safe": False,         # shrink art so the shake never clips its edges
    # --- motion
    "pattern": "classic",  # classic random circle sway-x sway-y bounce vibrate none
    "intensity": 5.0,      # shake distance, % of output size (5% of 128 = 6.4px)
    "rotate": 0.0,         # wobble amplitude in degrees
    "spin": "none",        # none | cw | ccw  (full 360 over the loop)
    "pulse": 0.0,          # scale pulse amplitude, %
    "hue": 0,              # hue rotation over the loop, degrees (360 = full rainbow)
    "frames": 0,           # 0 = auto (9 for classic, 8 otherwise)
    "delay": 30,           # ms per frame (GIF resolution is 10ms, browsers clamp < 20ms)
    "seed": 1,             # random pattern seed
    # --- GIF encoding
    "colors": 256,
    "dither": False,
    "alpha_cut": 50,       # % alpha above which a pixel becomes opaque (GIF is 1-bit alpha)
    # --- Slack
    "slack": True,         # auto-shrink until under max_kb
    "max_kb": 128,
    "min_size": 64,        # never shrink below this px while fitting
}

PATTERNS = ["classic", "random", "circle", "sway-x", "sway-y", "bounce", "vibrate", "none"]

PRESETS = {
    "classic":    {"label": "Classic",    "p": {}},
    "mild":       {"label": "Mild",       "p": {"intensity": 2.5, "delay": 40}},
    "intense":    {"label": "Intense",    "p": {"intensity": 9, "delay": 25}},
    "extreme":    {"label": "EXTREME",    "p": {"pattern": "random", "intensity": 14, "rotate": 8, "frames": 10, "delay": 20}},
    "earthquake": {"label": "Earthquake", "p": {"pattern": "vibrate", "intensity": 8, "rotate": 4, "frames": 6, "delay": 20}},
    "wobble":     {"label": "Wobble",     "p": {"pattern": "none", "rotate": 14, "frames": 12, "delay": 40}},
    "pulse":      {"label": "Pulse",      "p": {"pattern": "none", "pulse": 12, "frames": 12, "delay": 40}},
    "bounce":     {"label": "Bounce",     "p": {"pattern": "bounce", "intensity": 10, "frames": 10, "delay": 30}},
    "spin":       {"label": "Spin",       "p": {"pattern": "none", "spin": "cw", "frames": 16, "delay": 40, "safe": True}},
    "party":      {"label": "Party",      "p": {"pattern": "circle", "intensity": 4, "hue": 360, "pulse": 6, "frames": 12, "delay": 40}},
}

LIMITS = {
    "bg_fuzz": (0, 60), "size": (16, 512), "intensity": (0, 40), "rotate": (0, 90),
    "pulse": (0, 60), "hue": (0, 360), "frames": (0, 60), "delay": (20, 1000),
    "seed": (0, 999999), "colors": (2, 256), "alpha_cut": (1, 99), "max_kb": (8, 4096),
    "min_size": (16, 512),
}
BOOLS = {"trim", "safe", "dither", "slack"}
CHOICES = {"bg": ["none", "auto"], "fit": ["contain", "cover", "stretch"],
           "spin": ["none", "cw", "ccw"], "pattern": PATTERNS}


def clean(raw):
    """Merge user params over defaults, coerce types, clamp to sane ranges."""
    p = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k not in DEFAULTS or v is None:
            continue
        d = DEFAULTS[k]
        try:
            if k in BOOLS:
                p[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif k in CHOICES:
                p[k] = v if v in CHOICES[k] else d
            elif isinstance(d, float):
                p[k] = float(v)
            else:
                p[k] = int(round(float(v)))
        except (TypeError, ValueError):
            p[k] = d
        if k in LIMITS:
            lo, hi = LIMITS[k]
            p[k] = max(lo, min(hi, p[k]))
    return p


def frame_count(p):
    if p["frames"]:
        return max(2, p["frames"])
    return 9 if p["pattern"] == "classic" else 8


# --------------------------------------------------------------------------
# Motion
# --------------------------------------------------------------------------
CLASSIC = [(0, 0), (-1, -1), (1, 1), (0, 0), (-1, -2), (2, 1), (1, -1), (-1, 1), (0, 0)]


def motion(p, n, size):
    """Yield (dx, dy, rot_deg, scale, hue_deg) for each of n frames."""
    rng = random.Random(p["seed"])
    amp = size * p["intensity"] / 100.0
    out = []
    for i in range(n):
        t = i / n
        w = 2 * math.pi * t
        pat = p["pattern"]
        if pat == "classic":
            dx, dy = (v * amp for v in CLASSIC[i % len(CLASSIC)])
        elif pat == "random":
            r, a = amp * math.sqrt(rng.random()), rng.random() * 2 * math.pi
            dx, dy = r * math.cos(a), r * math.sin(a)
        elif pat == "circle":
            dx, dy = amp * math.cos(w), amp * math.sin(w)
        elif pat == "sway-x":
            dx, dy = amp * math.sin(w), 0
        elif pat == "sway-y":
            dx, dy = 0, amp * math.sin(w)
        elif pat == "bounce":
            dx, dy = 0, -amp * abs(math.sin(w / 2))
        elif pat == "vibrate":
            dx, dy = amp * (1 if i % 2 == 0 else -1), amp * 0.4 * (1 if (i // 2) % 2 == 0 else -1)
        else:
            dx = dy = 0
        rot = p["rotate"] * math.sin(w)
        if p["spin"] != "none":
            rot += 360 * t * (1 if p["spin"] == "cw" else -1)
        scale = 1 + p["pulse"] / 100.0 * math.sin(w)
        out.append((dx, dy, rot, scale, p["hue"] * t))
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def prepare(src, workdir, p):
    """Normalise the source: first frame, orient, optional bg removal + trim -> pre.png."""
    need_im()
    pre = str(Path(workdir) / "pre.png")
    cmd = IM + [f"{src}[0]", "-background", "none", "-auto-orient", "-alpha", "set"]
    if p["bg"] == "auto":
        w, h = map(int, run(IDENTIFY + ["-format", "%w %h", f"{src}[0]"]).split()[:2])
        cmd += ["-fuzz", f"{p['bg_fuzz']}%", "-fill", "none"]
        for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            cmd += ["-draw", f"color {x},{y} floodfill"]
    if p["trim"]:
        cmd += ["-fuzz", "1%", "-trim", "+repage"]
    run(cmd + ["PNG32:" + pre])
    return pre


def animate(pre, out, p, size, colors, n):
    box = size
    if p["safe"]:
        # leave room for the largest excursion so nothing clips
        amp = size * p["intensity"] / 100.0
        reach = amp * (2 if p["pattern"] == "classic" else 1)
        grow = 1 + p["pulse"] / 100.0
        box = max(8, int(size - 2 * math.ceil(reach) - (size * (grow - 1) if grow > 1 else 0)))
        if p["spin"] != "none" or p["rotate"] > 0:
            box = max(8, int(box * 0.72)) if p["spin"] != "none" else max(8, int(box * 0.9))
    cmd = IM + [pre, "-background", "none", "-alpha", "set"]
    if p["fit"] == "cover":
        cmd += ["-resize", f"{box}x{box}^", "-gravity", "center", "-extent", f"{box}x{box}"]
    elif p["fit"] == "stretch":
        cmd += ["-resize", f"{box}x{box}!"]
    else:
        cmd += ["-resize", f"{box}x{box}"]
    cmd += ["-gravity", "center", "-extent", f"{size}x{size}", "+repage", "-virtual-pixel", "transparent"]
    c = (size - 1) / 2.0
    for dx, dy, rot, sc, hue in motion(p, n, size):
        cmd += ["(", "-clone", "0"]
        if abs(hue) > 0.01:
            cmd += ["-modulate", f"100,100,{(100 + hue / 360.0 * 200) % 200:.2f}"]
        cmd += ["-distort", "SRT", f"{c:.2f},{c:.2f} {sc:.4f} {rot:.3f} {c + dx:.3f},{c + dy:.3f}", ")"]
    cmd += ["-delete", "0",
            "-channel", "A", "-threshold", f"{p['alpha_cut']}%", "+channel"]
    cmd += ["-dither", "FloydSteinberg"] if p["dither"] else ["+dither"]
    cmd += ["-colors", str(colors), "+remap",
            "-set", "delay", str(max(2, round(p["delay"] / 10))),
            "-set", "dispose", "background", "-loop", "0",
            "-layers", "OptimizeTransparency", "-comment", COMMENT, out]
    run(cmd)
    if GIFSICLE:   # optional second pass: much better LZW/frame optimisation than ImageMagick
        try:
            run([GIFSICLE, "-O3", "--no-comments", "-b", out])
        except Exception:
            pass


def ladder(p):
    """Progressively harsher (size, colors, frames) tries; index 0 is exactly what was asked."""
    size0, col0, n0 = p["size"], p["colors"], frame_count(p)
    steps = [(1.0, col0, 1.0)]
    for c in (192, 128, 96, 64, 48, 32):
        if c < col0:
            steps.append((1.0, c, 1.0))
    base = min(col0, 32)
    steps += [(1.0, base, 0.75), (0.9, base, 0.75), (0.8, base, 0.75), (0.7, base, 0.6),
              (0.6, min(base, 24), 0.6), (0.5, 16, 0.5)]
    out = []
    for s, c, f in steps:
        size = max(min(p["min_size"], size0), int(round(size0 * s)))
        n = max(min(4, n0), int(round(n0 * f)))
        out.append((size, c, n))
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def describe(p, size, colors, n):
    notes = []
    if size != p["size"]:
        notes.append(f"shrunk {p['size']}px → {size}px")
    if colors != p["colors"]:
        notes.append(f"{p['colors']} → {colors} colours")
    if n != frame_count(p):
        notes.append(f"{frame_count(p)} → {n} frames")
    return notes


class Cancelled(Exception):
    pass


def render(src, params, cancel=None):
    """Render src (path to any image) -> dict(gif=bytes, ...). Fits under max_kb when slack=True.
    `cancel()` returning True aborts between ImageMagick runs (a newer request superseded this one)."""
    p = clean(params)
    limit = p["max_kb"] * 1024
    with tempfile.TemporaryDirectory(prefix="intensify-") as wd:
        if cancel and cancel():
            raise Cancelled()
        pre = prepare(src, wd, p)
        steps = ladder(p) if p["slack"] else ladder(p)[:1]
        cache = {}

        def attempt(i):
            if cancel and cancel():
                raise Cancelled()
            if i not in cache:
                size, colors, n = steps[i]
                out = str(Path(wd) / f"try{i}.gif")
                animate(pre, out, p, size, colors, n)
                cache[i] = (Path(out).stat().st_size, out)
            return cache[i][0]

        lo, hi = 0, len(steps) - 1
        if attempt(0) <= limit or len(steps) == 1:
            pick = 0
        elif attempt(hi) > limit:
            pick = hi          # nothing fits; hand back the smallest we can make
        else:                  # binary search for the gentlest step that fits
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if attempt(mid) <= limit:
                    hi = mid
                else:
                    lo = mid
            pick = hi
        size, colors, n = steps[pick]
        data = Path(cache[pick][1]).read_bytes()
        try:
            amin = float(run(IM + [f"{cache[pick][1]}[0]", "-alpha", "set", "-format",
                                   "%[fx:minima.a]", "info:"]).strip())
        except Exception:
            amin = 1.0
    notes = describe(p, size, colors, n)
    return {
        "gif": data, "bytes": len(data), "size": size, "colors": colors, "frames": n,
        "delay": max(2, round(p["delay"] / 10)) * 10,
        "fits": len(data) <= limit, "limit": limit, "transparent": amin < 1.0,
        "square": True, "notes": notes,
    }


def safe_stem(name):
    stem = Path(name).stem
    stem = re.sub(r"[^a-z0-9_-]+", "-", stem.lower()).strip("-_")
    return stem or "emoji"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def doctor():
    def ver(cmd):
        try:
            return run(cmd).splitlines()[0].strip()
        except Exception:
            return None
    rows = [
        ("python3", True, sys.version.split()[0], "required"),
        ("imagemagick", IM is not None, ver(IM + ["-version"]) if IM else None, "required"),
        ("gifsicle", bool(GIFSICLE), ver([GIFSICLE, "--version"]) if GIFSICLE else None,
         "recommended - smaller GIFs (extra optimisation pass)"),
        ("librsvg", bool(shutil.which("rsvg-convert")), None, "optional - better SVG input"),
        ("webp", bool(shutil.which("cwebp")), None, "optional - WebP input"),
    ]
    for name, ok, v, note in rows:
        print(f"{'✓' if ok else '✗'} {name:12} {v or ''}{'' if ok else '  ← missing: ' + note}")
    if not all(r[1] for r in rows):
        print("\nInstall everything with:\n  " + BREW_HINT)
        print("(Linux: sudo apt install imagemagick gifsicle librsvg2-bin webp)")
    return 0 if IM else 1


def build_parser():
    ap = argparse.ArgumentParser(
        prog="intensify",
        description="Make Slack-ready 'intensifies' emoji GIFs. With no files, opens the web UI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="presets: " + ", ".join(PRESETS) + "\npatterns: " + ", ".join(PATTERNS),
    )
    ap.add_argument("files", nargs="*", help="input images (png, jpg, webp, gif, svg, ...)")
    ap.add_argument("--ui", action="store_true", help="launch the web UI")
    ap.add_argument("--doctor", action="store_true", help="check dependencies and exit")
    ap.add_argument("--port", type=int, default=8765, help="UI port (default 8765)")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    ap.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    ap.add_argument("-p", "--preset", choices=list(PRESETS), help="start from a preset")
    ap.add_argument("-o", "--output", help="output file (single input) ")
    ap.add_argument("-d", "--out-dir", help="output directory (default: next to each input)")
    ap.add_argument("--suffix", default="-intensifies", help="output name suffix")
    ap.add_argument("-f", "--force", action="store_true", help="overwrite existing outputs")
    ap.add_argument("-s", "--size", type=int, help="square size in px (default 128)")
    ap.add_argument("--fit", choices=CHOICES["fit"])
    ap.add_argument("--safe", action="store_true", default=None, help="shrink art so shake never clips")
    ap.add_argument("--no-trim", action="store_true", help="keep empty borders")
    ap.add_argument("--remove-bg", action="store_true", help="flood-fill background from corners")
    ap.add_argument("--bg-fuzz", type=int, help="background tolerance %% (default 12)")
    ap.add_argument("--pattern", choices=PATTERNS)
    ap.add_argument("-i", "--intensity", type=float, help="shake distance %% of size (default 5)")
    ap.add_argument("--rotate", type=float, help="wobble degrees")
    ap.add_argument("--spin", choices=CHOICES["spin"])
    ap.add_argument("--pulse", type=float, help="scale pulse %%")
    ap.add_argument("--hue", type=int, help="hue sweep degrees (360 = rainbow)")
    ap.add_argument("--frames", type=int, help="frame count (0 = auto)")
    ap.add_argument("--delay", type=int, help="ms per frame (default 30)")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--colors", type=int, help="max palette size (default 256)")
    ap.add_argument("--dither", action="store_true", default=None)
    ap.add_argument("--alpha-cut", type=int, help="alpha %% threshold (default 50)")
    ap.add_argument("--max-kb", type=int, help="target file size (default 128)")
    ap.add_argument("--no-slack-fit", action="store_true", help="don't auto-shrink to fit max-kb")
    return ap


def args_to_params(a):
    p = {}
    if a.preset:
        p.update(PRESETS[a.preset]["p"])
    m = {"size": a.size, "fit": a.fit, "safe": a.safe, "bg_fuzz": a.bg_fuzz, "pattern": a.pattern,
         "intensity": a.intensity, "rotate": a.rotate, "spin": a.spin, "pulse": a.pulse, "hue": a.hue,
         "frames": a.frames, "delay": a.delay, "seed": a.seed, "colors": a.colors, "dither": a.dither,
         "alpha_cut": a.alpha_cut, "max_kb": a.max_kb}
    p.update({k: v for k, v in m.items() if v is not None})
    if a.no_trim:
        p["trim"] = False
    if a.remove_bg:
        p["bg"] = "auto"
    if a.no_slack_fit:
        p["slack"] = False
    return p


def cli(a):
    params = args_to_params(a)
    if a.output and len(a.files) != 1:
        sys.exit("-o/--output needs exactly one input file")
    failures = 0
    for f in a.files:
        if not os.path.exists(f):
            print(f"✗ {f}: not found")
            failures += 1
            continue
        if a.output:
            dest = Path(a.output)
        else:
            d = Path(a.out_dir) if a.out_dir else Path(f).resolve().parent
            dest = d / (Path(f).stem + a.suffix + ".gif")
        if dest.exists() and not a.force:
            print(f"✗ {dest} already exists (use -f to overwrite)")
            failures += 1
            continue
        try:
            t0 = time.time()
            r = render(f, params)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r["gif"])
        except Exception as e:
            print(f"✗ {f}: {e}")
            failures += 1
            continue
        ok = "✓" if r["fits"] else "!"
        extra = f"  ({'; '.join(r['notes'])})" if r["notes"] else ""
        warn = "" if r["transparent"] else "  [no transparency]"
        warn += "" if r["fits"] else f"  [over {r['limit'] // 1024}KB limit]"
        print(f"{ok} {dest}  {r['size']}x{r['size']}  {r['bytes'] / 1024:.1f}KB  "
              f"{r['frames']}f{extra}{warn}  {time.time() - t0:.1f}s")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# Web UI server
# --------------------------------------------------------------------------
class Store:
    """Uploaded sources + rendered outputs live in a temp dir for the session."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="intensifier-ui-"))
        self.sources = {}   # id -> {path, name, w, h}
        self.outputs = {}   # id -> path
        self.latest = {}    # source id -> newest output id
        self.prev = {}      # source id -> previous output id
        self.gen = {}       # source id -> render generation (newer request cancels older)
        self.lock = threading.Lock()

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


STORE = None
MAX_UPLOAD = 40 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "intensifier"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": str(msg)})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ValueError("file too large (40MB max)")
        return self.rfile.read(n)

    def _trusted(self):
        """Only answer our own page: blocks other websites and DNS-rebinding from driving the server."""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in allowed}:
            return False
        site = self.headers.get("Sec-Fetch-Site")
        return site in (None, "same-origin", "none")

    def do_GET(self):
        if not self._trusted():
            return self._err(403, "forbidden")
        path = urlparse(self.path).path
        try:
            if path == "/":
                return self._send(200, (HERE / "ui.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/info":
                return self._send(200, {
                    "defaults": DEFAULTS, "limits": LIMITS, "presets": PRESETS, "patterns": PATTERNS,
                    "magick": " ".join(IM or []) or None, "gifsicle": bool(GIFSICLE),
                })
            m = re.fullmatch(r"/(src|out)/([0-9a-f]{12})(?:\.\w+)?", path)
            if m:
                kind, ident = m.groups()
                if kind == "src":
                    s = STORE.sources.get(ident)
                    return self._send(200, Path(s["path"]).read_bytes(), "image/png") if s else self._err(404, "gone")
                p = STORE.outputs.get(ident)
                return self._send(200, Path(p).read_bytes(), "image/gif") if p else self._err(404, "gone")
            self._err(404, "not found")
        except Exception as e:
            self._err(500, e)

    def do_POST(self):
        if not self._trusted():
            return self._err(403, "forbidden")
        path = urlparse(self.path).path
        try:
            if path == "/api/upload":
                return self._upload()
            data = json.loads(self._body() or b"{}")
            if path == "/api/render":
                return self._render(data)
            if path == "/api/zip":
                return self._zip(data)
            self._err(404, "not found")
        except Cancelled:
            try:
                self._err(409, "superseded")
            except OSError:
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass               # browser aborted the request (newer render started)
        except Exception as e:
            try:
                self._err(400, e)
            except OSError:
                pass

    def _upload(self):
        need_im()
        raw = self._body()
        name = self.headers.get("X-Filename", "image.png")
        from urllib.parse import unquote
        name = unquote(name)
        ident = uuid.uuid4().hex[:12]
        raw_path = STORE.dir / f"{ident}.upload"
        png = STORE.dir / f"{ident}.png"
        raw_path.write_bytes(raw)
        try:
            # normalise to PNG once (first frame, oriented); the UI shows this as the "original"
            run(IM + [f"{raw_path}[0]", "-background", "none", "-auto-orient", "-alpha", "set", "PNG32:" + str(png)])
            w, h = map(int, run(IDENTIFY + ["-format", "%w %h", str(png)]).split()[:2])
        except Exception as e:
            raise ValueError(f"couldn't read that image: {e}")
        finally:
            raw_path.unlink(missing_ok=True)
        STORE.sources[ident] = {"path": str(png), "name": name, "w": w, "h": h}
        self._send(200, {"id": ident, "name": name, "w": w, "h": h, "stem": safe_stem(name)})

    def _render(self, data):
        src_id = data.get("id")
        s = STORE.sources.get(src_id)
        if not s:
            raise ValueError("unknown image - re-upload it")
        with STORE.lock:
            gen = STORE.gen[src_id] = STORE.gen.get(src_id, 0) + 1
        r = render(s["path"], data.get("params"), cancel=lambda: STORE.gen.get(src_id) != gen)
        ident = uuid.uuid4().hex[:12]
        out = STORE.dir / f"{ident}.gif"
        out.write_bytes(r.pop("gif"))
        with STORE.lock:
            STORE.outputs[ident] = str(out)
            old = STORE.latest.get(src_id)
            STORE.latest[src_id] = ident
            # keep the one before too, so a download link that's mid-click still works
            older = STORE.prev.get(src_id)
            STORE.prev[src_id] = old
            if older and older in STORE.outputs:
                Path(STORE.outputs.pop(older)).unlink(missing_ok=True)
        r["id"] = ident
        self._send(200, r)

    def _zip(self, data):
        import io
        buf, used = io.BytesIO(), set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for item in data.get("items", []):
                s = STORE.sources.get(item.get("id"))
                if not s:
                    continue
                r = render(s["path"], data.get("params"))
                stem = safe_stem(item.get("name") or s["name"])
                n, k = stem, 2
                while n in used:
                    n, k = f"{stem}-{k}", k + 1
                used.add(n)
                z.writestr(n + ".gif", r["gif"])
        self._send(200, buf.getvalue(), "application/zip",
                   {"Content-Disposition": 'attachment; filename="intensified-emoji.zip"'})


def serve(a):
    global STORE
    need_im()
    STORE = Store()
    srv = None
    for port in range(a.port, a.port + 20):
        try:
            srv = ThreadingHTTPServer((a.host, port), Handler)
            break
        except OSError:
            continue
    if not srv:
        sys.exit(f"no free port near {a.port}")
    url = f"http://{a.host}:{srv.server_address[1]}/"
    print(f"intensifier UI → {url}   (Ctrl-C to quit)")
    if not a.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    def stop(*_):
        raise KeyboardInterrupt
    import signal
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig:
            signal.signal(sig, stop)   # closing the terminal still cleans up temp files
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STORE.close()


def main():
    a = build_parser().parse_args()
    if a.doctor:
        return doctor()
    if a.ui or not a.files:
        return serve(a)
    need_im()
    return cli(a)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except RuntimeError as e:
        sys.exit(str(e))
