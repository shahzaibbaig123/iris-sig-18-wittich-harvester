"""HTML parsers for ol.wittich.de pages.

Parsers are pure functions over HTML text. They raise ParseError when a page
does not have the structure they rely on, so the pipeline can log the page
as malformed instead of crashing or silently returning nothing.
"""

from bs4 import BeautifulSoup


class ParseError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def soup(html: str) -> BeautifulSoup:
    if not html or not html.strip():
        raise ParseError("empty_body", "response body is empty")
    return BeautifulSoup(html, "lxml")
