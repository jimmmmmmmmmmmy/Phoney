"""The public team page serves only its checked-in logo and resume downloads."""

import base64
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re
from xml.etree import ElementTree

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from config import Settings
from dashboard import register_dashboard


ROOT = Path(__file__).resolve().parents[1]
MEMBERS = (
    ("James Liu", "james-liu.pdf"),
    ("Gerry Jones", "gerry-jones.pdf"),
    ("Muhammed Altindal", "muhammed-altindal.pdf"),
    ("Shane McCarthy", "shane-mccarthy.pdf"),
)


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.headings = []
        self.images = []
        self.current_link = None
        self.current_heading = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.current_link = {"attrs": attrs, "text": ""}
            self.links.append(self.current_link)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.current_heading = {"tag": tag, "text": ""}
            self.headings.append(self.current_heading)
        if tag == "img":
            self.images.append(attrs)

    def handle_data(self, data):
        if self.current_link is not None:
            self.current_link["text"] += data
        if self.current_heading is not None:
            self.current_heading["text"] += data

    def handle_endtag(self, tag):
        if tag == "a":
            self.current_link = None
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.current_heading = None


@pytest.fixture
def client():
    class EmptyManager:
        def snapshot(self):
            return {"enabled": False, "revision": 0, "sessions": []}

    settings = Settings("AC" + "1" * 32, "unused-test-auth", "https://operator.example")
    app = FastAPI()
    register_dashboard(app, settings, EmptyManager())
    with TestClient(app, base_url="https://operator.example") as value:
        yield value


def test_team_url_redirects_to_the_internal_dashboard_view(client):
    response = client.get("/team", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard#team"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_internal_team_view_is_public_with_the_requested_member_and_download_order(client):
    response = client.get("/team")
    assert response.status_code == 200
    assert response.url.path == "/dashboard" and response.url.fragment == "team"
    assert response.headers["content-type"].startswith("text/html")
    assert "set-cookie" not in response.headers and not client.cookies
    assert '<section id="team-view"' in response.text
    document = Document(response.text)
    headings = [" ".join(heading["text"].split()) for heading in document.headings]
    assert [name for name in headings if name in dict(MEMBERS)] == [name for name, _ in MEMBERS]
    assert "New College" in response.text and "The Hacking Banyons" in response.text
    downloads = [link for link in document.links if link["attrs"].get("href", "").startswith("/resumes/")]
    assert [link["attrs"]["href"] for link in downloads] == [f"/resumes/{filename}" for _, filename in MEMBERS]
    assert [" ".join(link["text"].split()) for link in downloads] == [name for name, _ in MEMBERS]
    assert [link["attrs"].get("download") for link in downloads] == [filename for _, filename in MEMBERS]
    assert 'class="team-heading"' not in response.text


def test_team_page_has_security_headers_and_csp_allows_its_inline_styles(client):
    response = client.get("/team")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "img-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    styles = re.findall(r"<style>(.*?)</style>", response.text, re.S)
    assert styles
    for block in styles:
        digest = base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in csp


@pytest.mark.parametrize("name,filename", MEMBERS)
def test_resume_downloads_return_exact_checked_in_pdf_bytes(client, name, filename):
    original = (ROOT / "public" / "resumes" / filename).read_bytes()
    response = client.get(f"/resumes/{filename}")
    assert response.status_code == 200
    assert original.startswith(b"%PDF-") and response.content == original
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == f'attachment; filename="{filename}"'
    assert int(response.headers["content-length"]) == len(original)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in response.headers
    head = client.head(f"/resumes/{filename}")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-type"] == response.headers["content-type"]
    assert head.headers["content-length"] == response.headers["content-length"]


@pytest.mark.parametrize("path", [
    "/resumes/unknown.pdf", "/resumes/james-liu.txt", "/resumes/.env",
    "/resumes/%2e%2e%2f.env", "/resumes/%252e%252e%252f.env",
    "/resumes/..%5c.env", "/assets/unknown.svg", "/assets/%2e%2e%2f.env",
])
def test_asset_routes_do_not_expose_unlisted_files_or_traversal_paths(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert "unused-test-auth" not in response.text
    assert str(ROOT) not in response.text


def test_logo_is_local_svg_without_executable_or_external_resources(client):
    response = client.get("/assets/hacking-banyons.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";", 1)[0] == "image/svg+xml"
    assert response.content == (ROOT / "public" / "branding" / "hacking-banyons.svg").read_bytes()
    assert response.headers["x-content-type-options"] == "nosniff"
    svg = ElementTree.fromstring(response.content)
    assert svg.tag == "{http://www.w3.org/2000/svg}svg"
    for element in svg.iter():
        assert element.tag.rsplit("}", 1)[-1].lower() not in {"script", "foreignobject"}
        for key, value in element.attrib.items():
            local_name = key.rsplit("}", 1)[-1].lower()
            assert not local_name.startswith("on")
            if local_name in {"href", "src"}:
                assert value.startswith("#")
    assert not re.search(r"(?:url\(|@import)[^;}]*https?://", response.text, re.I)


def test_team_background_serves_exact_png_bytes_with_get_and_head(client):
    original = (ROOT / "public" / "branding" / "shellhacks-2026.png").read_bytes()
    response = client.get("/assets/shellhacks-2026.png")
    assert response.status_code == 200
    assert original.startswith(b"\x89PNG\r\n\x1a\n") and response.content == original
    assert response.headers["content-type"] == "image/png"
    assert int(response.headers["content-length"]) == len(original)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in response.headers
    head = client.head("/assets/shellhacks-2026.png")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-type"] == response.headers["content-type"]
    assert head.headers["content-length"] == response.headers["content-length"]


def test_dashboard_brand_and_footer_use_internal_team_links(client):
    document = Document(client.get("/dashboard").text)
    footer = next(link for link in document.links if link["attrs"].get("id") == "team-footer")
    assert " ".join(footer["text"].split()) == "The Hacking Banyons"
    for identifier in ("team-brand", "team-brand-mobile", "team-footer"):
        link = next(link for link in document.links if link["attrs"].get("id") == identifier)
        assert link["attrs"]["href"] == "#team"
        assert link["attrs"].get("target") in (None, "_self")
    assert any(image.get("src") == "/assets/hacking-banyons.svg" for image in document.images)
