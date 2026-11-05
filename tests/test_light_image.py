"""The image: one image with everything, video included (owner, 5 Nov 2026; 1.2 M4.3's -video split is gone), for
amd64 and arm64, rendering videos with the engine's Chromium and ffmpeg; a source install without them says how to add them; upgrade.sh builds the image the install chose;
and the engine loads Playwright and python-pptx only when it renders."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import pack
import video

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / "engine/backend/.venv/bin/python"


def stages() -> dict[str, list[str]]:
    """Dockerfile's stages by name, each with the stages it builds on (FROM <stage>) followed by its own lines."""
    out, name = {}, None
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        if m := re.match(r"FROM\s+(\S+)(?:\s+AS\s+(\S+))?", line, re.I):
            name = m.group(2) or m.group(1)
            out[name] = list(out.get(m.group(1), []))
        elif name and line.strip() and not line.lstrip().startswith("#"):
            out[name].append(line)
    return out


def test_the_one_image_has_video_and_release_publishes_it_for_both_architectures():
    s = stages()
    assert list(s)[-1] == "default" and "video" not in s, "a plain `docker build .` makes the one image"
    img = "\n".join(s["default"])
    assert "playwright install" in img and "headless_shell" in img  # PDF, PNG, PPTX and report emails need it
    assert re.search(r"apt-get install -y --no-install-recommends \\\n.* ffmpeg ", img), "ffmpeg encodes the videos"
    assert "scripts/video" not in img and "npm ci" not in "\n".join(s["base"])  # the video needs no Node packages
    old = "remo" + "tion"  # the renderer 1.2 removed (owner, 5 Nov 2026: its licence), spelled so this file doesn't name it
    tracked = subprocess.run(["git", "grep", "-il", old], cwd=ROOT, capture_output=True, text=True).stdout
    assert tracked == "", f"the old video renderer is still named in: {tracked}"
    rel = (ROOT / ".github/workflows/release.yml").read_text()
    assert rel.count("docker/build-push-action") == 2 and "target: default" in rel and "needs: tests" in rel
    assert "-video" not in rel and rel.count("linux/amd64,linux/arm64") == 1
    # release review: the image is started and checked (tests/image_check.sh) before anything is pushed
    assert rel.index("load: true") < rel.index("bash tests/image_check.sh") < rel.index("push: true")
    assert "bash tests/image_check.sh" in (ROOT / ".github/workflows/image.yml").read_text()


@pytest.mark.parametrize("ref,ok", [("v1.2.0", True), ("release-1.2", False), ("v1.1.0", False)])
def test_release_publishes_only_from_the_tag_of_its_version(tmp_path, ref, ok):
    """Release review: a workflow_dispatch from any branch published :latest and :<pyproject version>. The tag step
    (run as the workflow runs it, in bash) now stops unless the ref is the tag v<version>."""
    rel = (ROOT / ".github/workflows/release.yml").read_text()
    step = re.search(r"- id: tag\n\s+run: \|\n((?:          .*\n)+)", rel)[1].replace("${GITHUB_REPOSITORY_OWNER,,}", "x")  # bash 3.2 here
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.0"\n')
    r = subprocess.run(["bash", "-eo", "pipefail", "-c", step], cwd=tmp_path, capture_output=True, text=True, env={
        **os.environ, "GITHUB_REF_NAME": ref, "GITHUB_REPOSITORY_OWNER": "X", "GITHUB_OUTPUT": str(tmp_path / "out")})
    assert (r.returncode == 0) == ok, r.stderr
    assert ("version=1.2.0" in (tmp_path / "out").read_text()) == ok


def test_video_without_ffmpeg_or_chromium_says_what_to_add(monkeypatch):
    monkeypatch.setattr(video, "FFMPEG", "abi-no-such-ffmpeg")  # a source install without ffmpeg
    monkeypatch.setattr(video, "chromium", lambda: True)
    assert video.missing() == video.NO_VIDEO.format(what="ffmpeg", how=video.HOW["ffmpeg"]) and "apt install ffmpeg" in video.missing()
    monkeypatch.setattr(video.abi, "call", lambda *a, **k: pytest.fail("no query for a video this install can't render"))
    with pytest.raises(SystemExit, match="brew install ffmpeg"):
        video.build("p", "r")
    monkeypatch.setattr(video, "chromium", lambda: False)
    assert "ffmpeg and the engine's headless Chromium" in video.missing() and "playwright install" in video.missing()
    monkeypatch.setattr(video, "FFMPEG", sys.executable)  # any program on the path stands for ffmpeg here
    monkeypatch.setattr(video, "chromium", lambda: True)
    assert video.missing() == ""


def test_a_board_pack_lists_the_video_it_cant_render_with_the_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "FFMPEG", "abi-no-such-ffmpeg")
    monkeypatch.setattr(pack, "home", lambda project: tmp_path / "packs")
    monkeypatch.setattr(pack.abi, "call", lambda tool, **k: {"reports": [{"id": "r", "pages": []}]} if tool == "list_reports" else {})
    man = pack.make("p", {"id": "board", "items": [{"kind": "video", "report": "r"}]})
    item = man["items"][0]
    assert item["status"] == "failed" and item["error"] == video.missing() and "ffmpeg" in item["error"]


DRAW = r"""
const fs = require("fs"), m = { exports: {} };
new Function("module", fs.readFileSync(process.argv[1], "utf8").split("<script>")[1].split("</script>")[0])(m);
const { frame, FPS } = m.exports, p = JSON.parse(process.argv[2]), at = (n) => frame(p, n / FPS);
console.log(JSON.stringify({ fps: FPS, same: at(40) === at(40) && at(400) === at(400), moves: at(10) !== at(20),
  still: at(170) === at(175), slides: [at(0), at(200), at(380), at(560), at(740)].map((h) => h.slice(0, 1500)) }));
"""


def test_the_video_page_draws_each_frame_from_the_time_alone(tmp_path):
    """scripts/video.html's frame(P, t) (draw(t) puts it on the page) is the same for the same t, moves while a slide
    animates in and stands still after; the title card, a slide per view and the end card follow each other."""
    props = {"title": "T <b>", "subtitle": "s", "footer": "the end", "theme": video.THEMES["paper"], "seconds": 6, "width": 1920, "height": 1080,
             "slides": [{"title": "Bars", "kind": "bar", "caption": "c", "labels": ["a", "b"], "series": [{"name": "x", "values": [1, 2]}]},
                        {"title": "KPIs", "kind": "kpi", "caption": "k", "kpis": [{"label": "Rev", "value": 37330985.06, "text": "37.3m"}]},
                        {"title": "Rows", "kind": "table", "caption": "t", "dims": 1, "columns": ["r", "v"], "rows": [["N", 5]]}]}
    r = subprocess.run(["node", "-e", DRAW, str(ROOT / "scripts/video.html"), json.dumps(props)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["fps"] == video.FPS and out["same"] and out["moves"] and out["still"], out
    title, bars, kpi, rows, end = out["slides"]
    assert "T &lt;b&gt;" in title and ">01<" in bars and ">02<" in kpi and ">03<" in rows and "the end" in end


def test_render_pipes_the_frames_to_ffmpeg_as_an_h264_mp4(tmp_path, monkeypatch):
    """render() without a browser: a stand-in for the screenshots (one 'PNG' per frame) and for ffmpeg (keeps its
    arguments and what it was given). 2 s a slide at 30 fps, one slide: (1 + 2) x 60 frames, even sides."""
    (tmp_path / "ffmpeg").write_text(f"#!{sys.executable}\nimport json, sys\n"
                                     f"open({str(tmp_path / 'ff.json')!r}, 'w').write(json.dumps([sys.argv[1:], len(sys.stdin.buffer.read())]))\n"
                                     "open(sys.argv[-1], 'wb').write(b'mp4')\n")
    (tmp_path / "ffmpeg").chmod(0o755)
    monkeypatch.setattr(video, "FFMPEG", str(tmp_path / "ffmpeg"))
    assert "draw({i} / {fps})" in video.SHOOT[2] and video.SHOOT[0] == str(video.PY)  # the real one: the page in the engine's Chromium
    monkeypatch.setattr(video, "SHOOT", [sys.executable, "-c", f"import shutil, sys; shutil.copy(sys.argv[1], {str(tmp_path / 'page.html')!r});"
                                         f"open({str(tmp_path / 'shoot')!r}, 'w').write(' '.join(sys.argv[2:]));"
                                         "sys.stdout.buffer.write(b'PNG.' * int(sys.argv[3]))"])
    props = {"title": "T", "subtitle": "", "footer": "f </script>", "theme": video.THEMES["ink"], "seconds": 2, "width": 321, "height": 181,
             "slides": [{"title": "K", "kind": "kpi", "caption": "c", "kpis": [{"label": "a", "value": 1, "text": "1"}]}]}
    out = video.render(props, tmp_path / "out/deck.mp4")
    args, got = json.loads((tmp_path / "ff.json").read_text())
    assert out.read_bytes() == b"mp4" and got == 4 * 180
    assert args == ["-y", "-loglevel", "error", "-f", "image2pipe", "-c:v", "png", "-framerate", "30", "-i", "-", "-c:v", "libx264",
                    "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out.resolve())]
    assert (tmp_path / "shoot").read_text() == "30 180 320 180"
    page = (tmp_path / "page.html").read_text()
    assert "const P = {" in page and "f <\\/script>" in page and "/*PROPS*/" not in page  # the props in, the page not broken by them


@pytest.mark.parametrize("env,image", [
    ("ABI_IMAGE='ghcr.io/x/abi:1.2.0'\n", "ghcr.io/x/abi:1.2.0"),
    ("ABI_DOMAIN=abi.example.com\n", "ghcr.io/himanshu8881212/abi:latest"),
])
def test_upgrade_builds_the_image_the_install_chose(tmp_path, env, image):
    """No registry (the pull fails): upgrade.sh builds here, under the image name the install chose."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/upgrade.sh").write_bytes((ROOT / "scripts/upgrade.sh").read_bytes())
    (tmp_path / ".env").write_text(env)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/docker").write_text(f"""#!/bin/sh
echo "$*" >> {tmp_path}/calls
case "$*" in
  "compose pull") exit 1 ;;
  "volume inspect"*) exit 1 ;;
  "compose ps -q abi") echo new ;;
  inspect*) echo healthy ;;
  "exec new cat /data/VERSION") echo 1.2.0 ;;
esac
""")
    (tmp_path / "bin/docker").chmod(0o755)
    r = subprocess.run(["sh", tmp_path / "scripts/upgrade.sh"], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"})
    calls = (tmp_path / "calls").read_text().splitlines()
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"build -t {image} ." in calls and "compose up -d" in calls, calls


def test_upgrades_backup_is_readable_by_its_owner_only(tmp_path):
    """The backup upgrade.sh takes first is the whole database (logins, every table): with the usual umask it was
    world-readable (-rw-r--r--, M5 upgrade check on pve1); now 0600 whatever the umask."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/upgrade.sh").write_bytes((ROOT / "scripts/upgrade.sh").read_bytes())
    (tmp_path / ".env").write_text("ABI_USER=a\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/docker").write_text("""#!/bin/sh
case "$*" in
  ps*service=abi) echo old ;;
  "exec old scripts/backup.sh backup") echo the dump ;;
  "exec old scripts/backup.sh keys") echo the keys ;;
  "compose ps -q abi") echo new ;;
  inspect*) echo healthy ;;
  "exec new cat /data/VERSION") echo 1.2.0 ;;
esac
""")
    (tmp_path / "bin/docker").chmod(0o755)
    r = subprocess.run(["sh", "-c", "umask 022 && sh scripts/upgrade.sh"], cwd=tmp_path, capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"})
    [k] = (tmp_path / "backups").glob("abi-*-keys.tar")  # release review: without the keys a restore loses every stored secret
    [f] = set((tmp_path / "backups").glob("abi-*.tar")) - {k}
    assert r.returncode == 0 and f.read_text() == "the dump\n" and k.read_text() == "the keys\n", r.stdout + r.stderr
    assert f.stat().st_mode & 0o077 == 0 and k.stat().st_mode & 0o077 == 0, oct(f.stat().st_mode)


@pytest.mark.parametrize("skip", ["", "1"])
def test_upgrade_refuses_a_stopped_install_it_cant_back_up(tmp_path, skip):
    """Release review: with no container running (ABI stopped, Docker not up yet after a reboot) upgrade.sh skipped the
    backup without a word and migrated the volume for good. Now it stops while the volume exists, unless ABI_NO_BACKUP=1."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/upgrade.sh").write_bytes((ROOT / "scripts/upgrade.sh").read_bytes())
    (tmp_path / ".env").write_text("ABI_VOLUME=abi-pi_abi-pi-data\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/docker").write_text(f"""#!/bin/sh
echo "$*" >> {tmp_path}/calls
case "$*" in
  "compose ps -q abi") echo new ;;
  inspect*) echo healthy ;;
  "exec new cat /data/VERSION") echo 1.2.0 ;;
esac
""")
    (tmp_path / "bin/docker").chmod(0o755)
    r = subprocess.run(["sh", tmp_path / "scripts/upgrade.sh"], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "ABI_NO_BACKUP": skip, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"})
    calls = (tmp_path / "calls").read_text().splitlines()
    if skip:
        assert r.returncode == 0 and "compose up -d" in calls, r.stdout + r.stderr
    else:
        assert r.returncode == 1 and "isn't running" in r.stderr and "volume inspect abi-pi_abi-pi-data" in calls, r.stderr
        assert "compose up -d" not in calls and "compose pull" not in calls, calls


def test_the_engine_and_the_worker_start_without_playwright_or_pptx(tmp_path):
    """1.1 imported Playwright and python-pptx when the engine and every worker started (an idle engine measured
    228 MB before, 211 MB after; the worker 225 MB, 208 MB); now only a render loads them. With both made unimportable the engine and the worker still load."""
    for name in ("playwright", "pptx"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text(f"raise ImportError('{name} loaded at start')\n")
    r = subprocess.run([PY, "-c", "import sys; import abi.app, abi.worker, abi.tools.builtin as b;"
                        "print(sorted(m for m in sys.modules if m.split('.')[0] in ('playwright', 'pptx')))"],
                       cwd=ROOT / "engine/backend", capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PYTHONPATH": f"{tmp_path}{os.pathsep}{ROOT / 'engine/backend'}"})
    assert r.returncode == 0 and r.stdout.strip() == "[]", r.stdout + r.stderr[-2000:]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_abi_names_no_model_of_its_own():
    """The model comes from Settings or the deployment's ABI_LLM_MODEL, never from ABI itself."""
    pi = json.loads((ROOT / ".pi/settings.json").read_text())
    assert not {"defaultModel", "defaultProvider", "enabledModels"} & set(pi)
    ext = (ROOT / ".pi/extensions/abi/index.ts").read_text()
    assert "gpt-6.1-sol" not in ext and "const model = process.env.ABI_PI_LLM_MODEL;" in ext
    for f in ("scripts/settings.py", "scripts/abi.py", "scripts/upgrade.sh", "scripts/app.py", ".pi/extensions/subagent/index.ts"):
        assert "gpt-6.1-sol" not in (ROOT / f).read_text(), f
    assert "NO_MODEL" in (ROOT / "scripts/app.py").read_text()
    assert not any("model:" in f.read_text().split("---")[1] for f in (ROOT / ".pi/agents").glob("*.md"))
