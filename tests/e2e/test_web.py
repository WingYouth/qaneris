import re
from pathlib import Path

from fastapi.testclient import TestClient

from smartdata.interfaces.api.app import create_app


def test_web_workbench_and_static_assets_are_served(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "catalog.db"))
    dist = Path(__file__).resolve().parents[2] / "web" / "frontend" / "dist"

    with TestClient(app) as client:
        page = client.get("/")
        stylesheet = client.get("/static/styles.css")
        script = client.get("/static/app.js")
        if (dist / "index.html").exists():
            assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page.text)
            built_asset = client.get(assets[0]) if assets else None

    assert page.status_code == 200
    assert stylesheet.status_code == 200
    assert script.status_code == 200
    if (dist / "index.html").exists():
        assert 'id="root"' in page.text
        assert assets
        assert built_asset is not None and built_asset.status_code == 200
    else:
        assert 'href="/static/styles.css"' in page.text
        assert 'src="/static/app.js"' in page.text
        assert 'id="question"' in page.text
        assert 'id="source-panel"' in page.text


def test_web_exposes_only_working_product_navigation(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "catalog.db"))

    with TestClient(app) as client:
        page = client.get("/")

    assert 'href="#"' not in page.text
    assert "分析报告" not in page.text
    assert "治理建议" not in page.text
    assert "关系图谱" not in page.text
