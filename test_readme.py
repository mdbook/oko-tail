"""Keep the short setup guide and its container defaults consistent, without hermes or Docker."""
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parent
GUIDES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def test_readme_setup_order():
    readme = (ROOT / "README.md").read_text()
    opening = readme[:readme.index("## Quick start: Docker")]
    assert "hermes" in opening and "same machine" in opening and "API server enabled" in opening
    assert readme.index("## Quick start: Docker") < readme.index("## Configuration")
    assert readme.index("docker compose up -d") < readme.index("## Python instead of Docker")
    assert readme.index("## Python instead of Docker") < readme.index("## Features")
    assert "docker compose exec oko-tail python3 /app/oko-tail.py --check" in readme
    assert "unpublished" in readme and "not included here" in readme


def test_compose_setup_contract():
    # Static contract check, not a YAML parser or a Docker runtime test. Keep host paths unchanged
    # inside the container so images and plugin sockets work with only two user edits.
    compose = (ROOT / "compose.yaml").read_text()
    home = re.search(r'^x-hermes-home: &hermes-home "([^"]+)"', compose, re.M).group(1)
    assert PurePosixPath(home).is_absolute() and ".." not in PurePosixPath(home).parts
    assert len(re.findall(r"# EDIT [12]:", compose)) == 2
    assert re.search(r'^    user: "\d+:\d+"', compose, re.M)
    for setting in ("HERMES_HOME", "source", "target"):
        assert re.search(rf"^\s+{setting}: \*hermes-home$", compose, re.M), setting
    for line in ("image: ghcr.io/mdbook/oko-tail:release", 'command: ["--serve", "9120"]',
                 "network_mode: host", "read_only: true", "create_host_path: false"):
        assert line in compose
    assert "API_SERVER_KEY" not in compose and "OKO_TAIL_HOST:" not in compose
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert 'ENTRYPOINT ["python3", "/app/oko-tail.py"]' in dockerfile


def headings(text):
    return {re.sub(r"[^\w -]", "", line.lstrip("# ").lower()).replace(" ", "-")
            for line in text.splitlines() if line.startswith("#")}


@pytest.mark.parametrize("guide", GUIDES, ids=lambda path: str(path.relative_to(ROOT)))
def test_local_guide_links(guide):
    text = guide.read_text()
    links = re.findall(r"\[[^\]]*\]\(([^)]+)\)", text)
    links += re.findall(r'(?:src|href)="([^"]+)"', text)
    for link in links:
        url = urlsplit(link)
        if url.scheme or url.netloc:
            continue
        target = (guide.parent / unquote(url.path)).resolve() if url.path else guide
        assert target.is_relative_to(ROOT), (guide, link)
        assert target.is_file(), (guide, link)
        if url.fragment and target.suffix == ".md":
            assert unquote(url.fragment) in headings(target.read_text()), (guide, link)
