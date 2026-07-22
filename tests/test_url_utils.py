import pytest

from adp.utils.url_utils import is_probably_url, looks_like_download_url, extract_urls_from_mime_text


@pytest.mark.parametrize("text,expected", [
    ("https://example.com/file.zip", True),
    ("http://example.com", True),
    ("not a url", False),
    ("ftp://example.com/file.zip", False),
    ("", False),
    ("hello world http://example.com", False),
])
def test_is_probably_url(text, expected):
    assert is_probably_url(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("https://example.com/movie.mp4", True),
    ("https://example.com/archive.tar.gz", True),
    ("https://example.com/", False),
    ("https://example.com/page.html", False),
    ("not a url", False),
])
def test_looks_like_download_url(text, expected):
    assert looks_like_download_url(text) == expected


def test_extract_urls_from_mime_text_dedupes_and_filters():
    text = "https://a.com/one.zip\nnot a url\nhttps://a.com/one.zip\nhttps://b.com/two.iso"
    assert extract_urls_from_mime_text(text) == [
        "https://a.com/one.zip",
        "https://b.com/two.iso",
    ]


def test_extract_urls_from_mime_text_empty():
    assert extract_urls_from_mime_text("") == []
    assert extract_urls_from_mime_text(None) == []
