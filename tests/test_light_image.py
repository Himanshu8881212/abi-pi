"""The image: one image with everything, video included (owner, 5 Nov 2026; 1.2 M4.3's -video split is gone), for
amd64 and arm64; a source install without Remotion says how to add it; upgrade.sh builds the image the install chose;
and the engine loads Playwright and python-pptx only when it renders."""
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
    assert "scripts/video/package.json" in img and "cd scripts/video && npm ci" in img and "REMOTION_BROWSER=" in img
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


def test_video_without_remotion_says_how_to_add_it(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "HERE", tmp_path)  # a source install without scripts/video/node_modules
    assert video.missing() == video.NO_VIDEO and "cd scripts/video && npm ci" in video.NO_VIDEO
    monkeypatch.setattr(video.abi, "call", lambda *a, **k: pytest.fail("no query for a video this install can't render"))
    with pytest.raises(SystemExit, match="npm ci"):
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
