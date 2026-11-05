"""1.2 M4.3 the light image: the default image has no Remotion (video is the -video image, Dockerfile's video
target), video says plainly what to switch, upgrade.sh keeps an install on the image it chose, and the engine loads
Playwright and python-pptx only when it renders."""
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


def test_the_default_image_has_no_remotion_and_the_video_image_adds_it():
    s = stages()
    assert list(s)[-1] == "default", "a plain `docker build .` must make the light image"
    default, vid = "\n".join(s["default"]), "\n".join(s["video"])
    assert "scripts/video" not in default and "REMOTION" not in default and "npm ci" not in default.split("FROM node")[0]
    assert "playwright install" in default and "headless_shell" in default  # PDF, PNG, PPTX and report emails need it
    assert "scripts/video/package.json" in vid and "cd scripts/video && npm ci" in vid and "REMOTION_BROWSER=" in vid
    rel = (ROOT / ".github/workflows/release.yml").read_text()
    assert rel.count("target: default") == 1 and rel.count("target: video") == 1 and "needs: tests" in rel
    assert ":latest-video" in rel and "version }}-video" in rel and rel.count("linux/amd64,linux/arm64") == 2


def test_video_without_remotion_says_how_to_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "HERE", tmp_path)  # an image without scripts/video/node_modules
    assert video.missing() == video.NO_VIDEO and "ABI_IMAGE=" in video.NO_VIDEO and "-video" in video.NO_VIDEO
    monkeypatch.setattr(video.abi, "call", lambda *a, **k: pytest.fail("no query for a video this image can't render"))
    with pytest.raises(SystemExit, match="ABI_IMAGE=ghcr.io/himanshu8881212/abi:latest-video"):
        video.build("p", "r")
    (tmp_path / "video/node_modules/remotion").mkdir(parents=True)
    assert video.missing() == ""


def test_a_board_pack_lists_the_video_it_cant_render_with_the_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "HERE", tmp_path)
    monkeypatch.setattr(pack, "home", lambda project: tmp_path / "packs")
    monkeypatch.setattr(pack.abi, "call", lambda tool, **k: {"reports": [{"id": "r", "pages": []}]} if tool == "list_reports" else {})
    man = pack.make("p", {"id": "board", "items": [{"kind": "video", "report": "r"}]})
    item = man["items"][0]
    assert item["status"] == "failed" and item["error"] == video.NO_VIDEO


@pytest.mark.parametrize("env,target,image", [
    ("ABI_IMAGE=ghcr.io/x/abi:latest-video\n", "video", "ghcr.io/x/abi:latest-video"),
    ("ABI_IMAGE='ghcr.io/x/abi:1.2.0-video'\n", "video", "ghcr.io/x/abi:1.2.0-video"),
    ("ABI_DOMAIN=abi.example.com\n", "default", "ghcr.io/himanshu8881212/abi:latest"),
])
def test_upgrade_builds_the_image_the_install_chose(tmp_path, env, target, image):
    """No registry (the pull fails): upgrade.sh builds here, the -video target for an install on the -video image."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/upgrade.sh").write_bytes((ROOT / "scripts/upgrade.sh").read_bytes())
    (tmp_path / ".env").write_text(env)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/docker").write_text(f"""#!/bin/sh
echo "$*" >> {tmp_path}/calls
case "$*" in
  "compose pull") exit 1 ;;
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
    assert f"build --target {target} -t {image} ." in calls and "compose up -d" in calls, calls


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
  "compose ps -q abi") echo new ;;
  inspect*) echo healthy ;;
  "exec new cat /data/VERSION") echo 1.2.0 ;;
esac
""")
    (tmp_path / "bin/docker").chmod(0o755)
    r = subprocess.run(["sh", "-c", "umask 022 && sh scripts/upgrade.sh"], cwd=tmp_path, capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"})
    [f] = (tmp_path / "backups").glob("abi-*.tar")
    assert r.returncode == 0 and f.read_text() == "the dump\n", r.stdout + r.stderr
    assert f.stat().st_mode & 0o077 == 0, oct(f.stat().st_mode)


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
