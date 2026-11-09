"""Smoke tests: stdlib only. Run with  python3 -m unittest discover -s tests -v"""
import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import intensify as I  # noqa: E402


def frames_info(path):
    """[(w, h), ...] for every frame of a GIF."""
    out = subprocess.run(I.IDENTIFY + ["-format", "%W %H\n", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return [tuple(map(int, ln.split())) for ln in out.split("\n") if ln.strip()]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if I.IM is None:
            raise unittest.SkipTest("ImageMagick not installed")
        cls.tmp = Path(tempfile.mkdtemp(prefix="intensify-test-"))
        # non-square image on a solid white background - the common "regular image" case
        cls.src = cls.tmp / "face.png"
        subprocess.run(I.IM + ["-size", "300x200", "xc:white", "-fill", "#f5a623",
                               "-draw", "circle 150,100 150,25", "-fill", "#222",
                               "-draw", "circle 125,85 125,74", "-draw", "circle 175,85 175,74",
                               str(cls.src)], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestRender(Base):
    def check_slack(self, r, size=None):
        out = self.tmp / "check.gif"
        out.write_bytes(r["gif"])
        self.assertTrue(out.read_bytes().startswith(b"GIF8"))
        self.assertLessEqual(r["bytes"], r["limit"], "over Slack size limit")
        self.assertTrue(r["transparent"], "no transparent pixels")
        for w, h in frames_info(out):
            self.assertEqual(w, h, "frame not square")
            if size:
                self.assertEqual(w, size)

    def test_every_preset_is_slack_ready(self):
        for name, preset in I.PRESETS.items():
            with self.subTest(preset=name):
                r = I.render(str(self.src), {**preset["p"], "bg": "auto"})
                self.check_slack(r, 128)
                self.assertEqual(r["frames"], I.frame_count(I.clean(preset["p"])))

    def test_sizes(self):
        for size in (32, 64, 256):
            with self.subTest(size=size):
                self.check_slack(I.render(str(self.src), {"bg": "auto", "size": size}), size)

    def test_auto_fit_shrinks_oversized(self):
        r = I.render(str(self.src), {**I.PRESETS["party"]["p"], "bg": "auto", "size": 512,
                                     "dither": True, "max_kb": 24})
        self.assertTrue(r["fits"])
        self.assertTrue(r["notes"], "expected the fitter to report what it changed")
        self.check_slack(r)

    def test_no_bg_removal_reports_opaque(self):
        r = I.render(str(self.src), {"bg": "none", "fit": "cover"})
        self.assertFalse(r["transparent"])

    def test_clean_clamps_garbage(self):
        p = I.clean({"size": 99999, "intensity": "lots", "pattern": "nope", "slack": "false"})
        self.assertEqual(p["size"], 512)
        self.assertEqual(p["intensity"], I.DEFAULTS["intensity"])
        self.assertEqual(p["pattern"], I.DEFAULTS["pattern"])
        self.assertIs(p["slack"], False)


class TestCLI(Base):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "intensify.py"), *args],
                              capture_output=True, text=True)

    def test_cli_writes_and_refuses_to_clobber(self):
        out = self.tmp / "cli"
        r = self.run_cli(str(self.src), "--remove-bg", "-p", "extreme", "-d", str(out))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        gif = out / "face-intensifies.gif"
        self.assertTrue(gif.exists())
        self.assertEqual(self.run_cli(str(self.src), "-d", str(out)).returncode, 1)
        self.assertEqual(self.run_cli(str(self.src), "-d", str(out), "-f").returncode, 0)

    def test_doctor(self):
        self.assertEqual(self.run_cli("--doctor").returncode, 0)


class TestServer(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        I.STORE = I.Store()
        cls.srv = I.ThreadingHTTPServer(("127.0.0.1", 0), I.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        I.STORE.close()
        super().tearDownClass()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()

    def test_ui_and_render_flow(self):
        self.assertEqual(self.req("GET", "/")[0], 200)
        st, body = self.req("POST", "/api/upload", self.src.read_bytes(), {"X-Filename": "My%20Face.png"})
        self.assertEqual(st, 200, body)
        up = json.loads(body)
        self.assertEqual(up["stem"], "my-face")
        st, body = self.req("POST", "/api/render", json.dumps({"id": up["id"], "params": {"bg": "auto"}}))
        self.assertEqual(st, 200, body)
        r = json.loads(body)
        self.assertTrue(r["fits"] and r["transparent"])
        st, gif = self.req("GET", f"/out/{r['id']}.gif")
        self.assertEqual(st, 200)
        self.assertTrue(gif.startswith(b"GIF8"))

    def test_rejects_other_sites(self):
        self.assertEqual(self.req("GET", "/api/info", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/render", "{}", {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/render", "{}", {"Sec-Fetch-Site": "cross-site"})[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
