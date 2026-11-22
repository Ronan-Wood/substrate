"""The HTML coverage probe reads a page as Docling's HTML reader parses it, with BeautifulSoup.

The probe's words are what the raw-coverage gate asks the markdown for, so a word it drops is a
loss the gate cannot see, and a word it adds is a correct conversion refused. Most pages below were
read differently by the regex probe this replaced (as of 0c1686c). The rest hold what that probe
already read right, against shapes a hand-written reading of the page got wrong.

The probe needs BeautifulSoup, which the `convert` extra installs. The Docling tests at the end
also need SUBSTRATE_DOCLING_TESTS=1, as in `test_ingest_formats.py`; the `docling` workflow sets
both.
"""

from __future__ import annotations

import importlib.util
import os
import random
import time
from collections import Counter
from pathlib import Path

import pytest

from substrate.extract import convert


def _an_empty_comment_closes_itself() -> bool:
    """Whether this interpreter's `html.parser`, which BeautifulSoup and so Docling parse with, ends
    `<!-->` where it stands, as HTML5 and a browser do.

    CPython 3.14.7 does. 3.14.0 to 3.14.6 open a comment there that runs to the next `-->`, so the
    text between is read as comment by the probe and by Docling alike. Asked of the parser rather
    than of the version, so the expectation is this interpreter's whatever it is.
    """
    from html.parser import HTMLParser

    text: list[str] = []
    parser = HTMLParser()
    parser.handle_data = text.append
    parser.feed("<!-->shown-->")
    parser.close()
    return "".join(text).startswith("shown")


EMPTY_COMMENT_CLOSES = _an_empty_comment_closes_itself()

# The `regex probe:` line over each page is what the regex probe read of it: `the same`, `no
# measurement`, or the words it added (+) and dropped (-).
CASES = {
    # An end tag may carry attributes, and `>` inside a quoted value does not end it. The regex
    # probe required `</script>` exactly, so the script ran on to a later one and took the body
    # with it, or never closed and its code was counted.
    # regex probe: - 'after'
    "end-tag-bare-attribute": (
        "<p>kept</p><script>var hidden;</script foo><p>after</p><script>more;</script>",
        ["kept", "after"]),
    # regex probe: + 'var', 'hidden;'
    "end-tag-attribute": (
        '<p>kept</p><script>var hidden;</script foo="bar"><p>after</p>',
        ["kept", "after"]),
    # regex probe: + 'var', 'hidden;'
    "end-tag-quoted-gt": (
        '<p>kept</p><script>var hidden;</script foo="b>ar"><p>after</p>',
        ["kept", "after"]),
    # regex probe: + '.hidden{}'
    "style-end-tag-quoted-gt": (
        "<p>kept</p><style>.hidden{}</style class='b>ar'><p>after</p>",
        ["kept", "after"]),
    # regex probe: + '.hidden{}'
    "end-tag-spaced-attribute": (
        '<p>kept</p><style>.hidden{}</style a = "q>r" ><p>after</p>',
        ["kept", "after"]),
    # regex probe: + 'var', 'hidden;'
    "end-tag-slash": (
        "<p>kept</p><script>var hidden;</script/><p>after</p>",
        ["kept", "after"]),
    # A quote or `=` not after an attribute's name is a character, so these end at the first `>`.
    # regex probe: + 'var', 'hidden;', 'after'; - 'y">after'
    "end-tag-equals-first": (
        '<p>kept</p><script>var hidden;</script ="x>y">after',
        ["kept", 'y">after']),
    # regex probe: + 'var', 'hidden;', 'after'; - 'y">after'
    "end-tag-double-equals": (
        '<p>kept</p><script>var hidden;</script a=="x>y">after',
        ["kept", 'y">after']),
    # An end tag whose quote never closes is not an end tag.
    # regex probe: the same
    "end-tag-unterminated-quote": (
        '<p>kept</p><script>a</script b="open><p>gone</p><script>c</script>',
        ["kept"]),
    # `--!>` closes a comment, and `<!-->` and `<!--->` are whole empty ones. The regex probe ran
    # each to the next `-->` and dropped the text before it. So does `html.parser` before CPython
    # 3.14.7, for the two empty ones: there `shown -->` is comment to the probe and to Docling.
    # regex probe: - 'shown', '-->'
    "comment-bang-close": (
        "<p>kept<!-- note --!> shown --> end</p>",
        ["kept", "shown", "-->", "end"]),
    # regex probe: - 'shown', '-->'
    "comment-empty": (
        "<p>kept<!--> shown --> end</p>",
        ["kept", "shown", "-->", "end"] if EMPTY_COMMENT_CLOSES else ["kept", "end"]),
    # regex probe: - 'shown', '-->'
    "comment-empty-dash": (
        "<p>kept<!---> shown --> end</p>",
        ["kept", "shown", "-->", "end"] if EMPTY_COMMENT_CLOSES else ["kept", "end"]),
    # A comment, script, title or noscript that never closes runs to the end of the file.
    # regex probe: + 'kept<!--', 'never', 'closed'; - 'kept'
    "comment-unclosed": (
        "<p>kept<!-- never closed",
        ["kept"]),
    # regex probe: + 'var', 'hidden;'
    "script-unclosed": (
        "<p>kept</p><script>var hidden;",
        ["kept"]),
    # regex probe: + 'Hidden', 'too'
    "title-unclosed": (
        "<p>kept</p><title>Hidden <b>too</b>",
        ["kept"]),
    # regex probe: + 'never', 'closed', 'words'
    "noscript-unclosed": (
        "<p>kept</p><noscript><p>never closed words",
        ["kept"]),
    # Markup inside a quoted value, a comment or a script is not markup. The regex probe stripped
    # comments, then elements, then tags, each over the whole page, so one could start in another.
    # regex probe: + '<p', 'title="', 'more'; - 'kept-->more'
    "comment-in-attribute": (
        '<p title="<!--">kept-->more</p>',
        ["kept-->more"]),
    # regex probe: + 'a', 'more'; - 'kept-->more'
    "comment-in-script": (
        "<script>a<!-- b</script>kept-->more",
        ["kept-->more"]),
    # regex probe: + '<p', 'title="'; - 'body'
    "script-in-attribute": (
        '<p title="<script>">body</p><script>var hidden;</script>',
        ["body"]),
    # A tag name ends at whitespace, `/` or `>`. The regex probe's `\b` let `<title-card>` open a
    # title that ran to the real one, and the page measured as empty.
    # regex probe: no measurement
    "longer-name-opens-nothing": (
        "<title-card>Card</title-card><p>body</p><title>Hidden</title>",
        ["Card", "body"]),
    # A `<` before a space or a digit is text, and the tag after it still opens.
    # regex probe: no measurement
    "stray-lt-before-space": (
        "<p>1 < 2 <script>var hidden;</script> end</p>",
        ["1", "<", "2", "end"]),
    # regex probe: no measurement
    "stray-lt-before-digit": (
        "<p>x <3 <style>.hidden{}</style> end</p>",
        ["x", "<3", "end"]),
    # Head text is read where BeautifulSoup puts it, as a browser shows it; every title is left
    # out, wherever it is. The regex probe dropped everything from `<head>` to `</head>`.
    # regex probe: - 'more', 'words'
    "head-in-body": (
        "<body><p>kept</p><head>more words</head><p>after</p></body>",
        ["kept", "more", "words", "after"]),
    # regex probe: no measurement
    "body-in-head": (
        "<head><title>Hidden</title><body><p>body words</p></body></head>",
        ["body", "words"]),
    # regex probe: - 'body', 'words'
    "head-closed-after-body-text": (
        "<head><title>Hidden</title><p>body words</p></head><p>after</p>",
        ["body", "words", "after"]),
    # regex probe: the same
    "comment-in-head": (
        "<head><title>Hidden</title><!-- </head> --></head><p>body</p>",
        ["body"]),
    # regex probe: the same
    "title-outside-body": (
        "<title>Quarterly Report</title><body><h1>Quarterly Report</h1><p>Six scopes.</p></body>",
        ["Quarterly", "Report", "Six", "scopes."]),
    # regex probe: the same
    "title-before-heading-without-body": (
        "<!DOCTYPE html><title>Quarterly Report</title><h1>Quarterly Report</h1><p>Six scopes.</p>",
        ["Quarterly", "Report", "Six", "scopes."]),
    # regex probe: the same
    "title-of-an-svg-in-the-body": (
        "<body><h1>Report</h1><svg><title>Download Report</title></svg><p>Six scopes.</p></body>",
        ["Report", "Six", "scopes."]),
    # regex probe: the same
    "title-in-the-body-before-the-heading": (
        "<html><body><title>Boundary Principle</title><h1>Boundary Principle</h1>"
        "<p>A rule that cannot refuse.</p></body></html>",
        ["Boundary", "Principle", "A", "rule", "that", "cannot", "refuse."]),
    # regex probe: the same
    "title-inside-noscript": (
        "<html><noscript><title>x</title></noscript><head><title>Boundary Principle</title></head>"
        "<h1>Boundary Principle</h1><p>A rule that cannot refuse.</p></html>",
        ["Boundary", "Principle", "A", "rule", "that", "cannot", "refuse."]),
    # Docling removes noscript, a template that declares no shadow root, and every element with a
    # `hidden` attribute, as a browser hides them. The regex probe counted them, and asked for words
    # Docling leaves out.
    # regex probe: the same
    "noscript-in-head": (
        "<html><head><title>T</title><noscript>Please enable JavaScript here.</noscript></head>"
        "<body><h1>Guide</h1><p>The engine ingested six scopes.</p></body></html>",
        ["Guide", "The", "engine", "ingested", "six", "scopes."]),
    # regex probe: + 'You', 'need', 'to', 'to', 'enable', 'JavaScript', 'run', 'this', 'app.'
    "noscript-in-body": (
        "<body><noscript>You need to enable JavaScript to run this app.</noscript>"
        "<div id=root><h1>Report</h1><p>The engine ingested six scopes.</p></div></body>",
        ["Report", "The", "engine", "ingested", "six", "scopes."]),
    # regex probe: + 'no', 'script', 'words'
    "noscript-holding-tags": (
        "<body><noscript><div>no <b>script</b> words</div></noscript><p>kept</p></body>",
        ["kept"]),
    # regex probe: + 'row', 'template', 'text'
    "template": (
        "<p>kept</p><template><p>row template text</p></template><p>after</p>",
        ["kept", "after"]),
    # regex probe: the same
    "template-shadow-root": (
        "<div><template shadowrootmode=open><p>shadow words</p></template></div>",
        ["shadow", "words"]),
    # regex probe: + 'inner'
    "noscript-end-closes-template": (
        "<noscript><template>inner</noscript><p>after words</p>",
        ["after", "words"]),
    # An end tag closes the latest open element of its name and everything inside it, as
    # BeautifulSoup builds Docling's tree, so a wrapper's end tag ends a noscript left open in it.
    # regex probe: the same
    "wrapper-end-closes-noscript": (
        "<header><noscript><img src=x></header><main><p>Main body words</p></main>",
        ["Main", "body", "words"]),
    # regex probe: + 'row'
    "wrapper-end-closes-template": (
        "<section><template><tr><td>row</section><p>The engine ingested six scopes.</p>",
        ["The", "engine", "ingested", "six", "scopes."]),
    # regex probe: + 'hid', 'hid', 'still'
    "inner-end-closes-only-the-inner": (
        "<div><noscript><div>hid</div>still hid</noscript><p>after</p></div>",
        ["after"]),
    # regex probe: + 'Enable', 'JS', 'please'
    "void-end-tag-inside-noscript": (
        "<body>Hi there<br><noscript>Enable JS</br>please</noscript><p>Main text</p></body>",
        ["Hi", "there", "Main", "text"]),
    # regex probe: + 'hidden', 'attr', 'words'
    "hidden-attribute": (
        "<p>kept</p><p hidden>hidden attr words</p><p>after</p>",
        ["kept", "after"]),
    # Raw text, which Docling renders as written.
    # regex probe: + 'framed', 'words'; - '<p>framed', 'words</p>'
    "iframe-fallback": (
        "<p>kept</p><iframe><p>framed words</p></iframe><p>after</p>",
        ["kept", "<p>framed", "words</p>", "after"]),
    # regex probe: the same
    "noembed-and-noframes": (
        "<p>kept</p><noembed>alt words</noembed><noframes>more alt</noframes><p>after</p>",
        ["kept", "alt", "words", "more", "alt", "after"]),
    # regex probe: + 'shown', 'words'; - '<b>shown', 'words</b>'
    "xmp-shown-as-written": (
        "<p>kept</p><xmp><b>shown words</b></xmp><p>after</p>",
        ["kept", "<b>shown", "words</b>", "after"]),
    # An entity name BeautifulSoup does not know stays literal; `html.unescape` read the longest
    # known prefix, so the regex probe asked for words no conversion holds.
    # regex probe: + '/api?a=1®ion=eu¤cy=usd¬ify=no'; - '/api?a=1&region=eu&currency=usd&notify=no'
    "entity-unknown-names": (
        "<pre>GET /api?a=1&region=eu&currency=usd&notify=no</pre>",
        ["GET", "/api?a=1&region=eu&currency=usd&notify=no"]),
    # regex probe: + '©right', 'x&y', '¬it;'; - '&copyright', 'x&ampy', '&notit'
    "entity-name-prefixes": (
        "<p>AT&Tcorp &copyright x&ampy &notit; and</p>",
        ["AT&Tcorp", "&copyright", "x&ampy", "&notit", "and"]),
    # regex probe: the same
    "entity-known": (
        "<p>caf&eacute; &copy; &#169; &#x41;BC &amp; done</p>",
        ["café", "©", "©", "ABC", "&", "done"]),
    # A control or noncharacter reference is that character, which splits word tokens; the regex
    # probe's `html.unescape` dropped it and joined them.
    # regex probe: + 'alphabeta', 'gammadelta', 'epszeta'; - 'alpha\x01beta', 'gamma\x7fdelta',
    # 'eps\ufffezeta'
    "charref-control": (
        "<p>alpha&#1;beta gamma&#x7F;delta eps&#xFFFE;zeta</p>",
        ["alpha\x01beta", "gamma\x7fdelta", "eps\ufffezeta"]),
    # Docling takes no comment, CDATA, declaration or processing instruction as text.
    # regex probe: the same
    "cdata-and-processing-instruction": (
        "<p>kept <![CDATA[ inside words ]]> <?pi words?> after</p>",
        ["kept", "after"]),
    # Only ASCII letters spell a tag name, so neither of these opens a raw-text element; a rewrite
    # compiled with `re.I` alone let `scrıpt` open a script that ran to the end of the file.
    # regex probe: the same
    "dotless-i": (
        "<p>keep</p><scrıpt><p>body text</p>",
        ["keep", "body", "text"]),
    # regex probe: - '<ſtyle>'
    "long-s": (
        "<p>keep</p><ſtyle><p>body text</p>",
        ["keep", "<ſtyle>", "body", "text"]),
    # regex probe: the same
    "longer-end-name-closes-nothing": (
        "<p>kept</p><script>a</scripts>b</script><p>after</p>",
        ["kept", "after"]),
    # regex probe: the same
    "dashed-end-name-closes-nothing": (
        "<p>kept</p><script>a</script-x>b</script><p>after</p>",
        ["kept", "after"]),
    # regex probe: the same
    "end-tag-newline": (
        "<p>kept</p><script>var hidden;</script\n><p>after</p>",
        ["kept", "after"]),
    # regex probe: the same
    "upper-case": (
        "<p>kept</p><SCRIPT>var hidden;</script><p>after</p>",
        ["kept", "after"]),
    # regex probe: the same
    "gt-in-comment": (
        "<p>kept<!-- a > b --> end</p>",
        ["kept", "end"]),
    # regex probe: the same
    "self-closing-script-opens-nothing": (
        "<p>kept</p><script/>shown<p>after</p>",
        ["kept", "shown", "after"]),
}

# Pages on which Docling drops text a browser shows. The probe asks for it, so the gate refuses each.
REFUSED = {
    # Docling reads only `soup.body` when a page has one; a browser shows head text in the body.
    "text-in-a-head-beside-a-body": (
        "<html><head><title>T</title>Updated nightly</head><body><p>One two three</p></body></html>",
        ["Updated", "nightly", "One", "two", "three"]),
    "a-paragraph-in-a-head-beside-a-body": (
        "<html><head><title>T</title><p>Lead words here</p></head><body><p>One two three</p></body>"
        "</html>",
        ["Lead", "words", "here", "One", "two", "three"]),
    "text-after-body": (
        "<body><p>kept words</p></body><p>trailing words</p>",
        ["kept", "words", "trailing", "words"]),
    "text-after-body-in-a-head-left-open": (
        "<html><head><title>T</title><body><p>kept</p></body><p>trailing words</p></html>",
        ["kept", "trailing", "words"]),
    "an-early-empty-body": (
        "<body>Intro</body><h1>Report</h1><p>The engine ingested six scopes without a refusal.</p>",
        ["Intro", "Report", "The", "engine", "ingested", "six", "scopes", "without", "a",
         "refusal."]),
    "a-second-body": (
        "<html><body><p>Intro</p></body><body><p>The engine ingested six scopes without a refusal."
        "</p></body></html>",
        ["Intro", "The", "engine", "ingested", "six", "scopes", "without", "a", "refusal."]),
    "text-after-html": (
        "<html><body><p>Intro</p></body></html><p>The engine ingested six scopes without a refusal.</p>",
        ["Intro", "The", "engine", "ingested", "six", "scopes", "without", "a", "refusal."]),
    "a-body-inside-an-svg": (
        "<svg><foreignObject><body xmlns='http://www.w3.org/1999/xhtml'><p>Chart</p></body>"
        "</foreignObject></svg><h1>Report</h1><p>The engine ingested six scopes without a refusal.</p>",
        ["Chart", "Report", "The", "engine", "ingested", "six", "scopes", "without", "a", "refusal."]),
    # Docling's reader repairs the tree after its removal step, and moves a row standing outside any
    # table out of the body, with its text.
    "a-row-outside-a-table": (
        "<html><td><body><p>Quarterly review</p><tr>The engine ingested six scopes without a refusal."
        "</tr></body></html>",
        ["Quarterly", "review", "The", "engine", "ingested", "six", "scopes", "without", "a",
         "refusal."]),
}


BS4 = pytest.mark.skipif(importlib.util.find_spec("bs4") is None,
                         reason="the HTML probe reads with BeautifulSoup, which the `convert` extra installs")


def _page(tmp: Path, text: str) -> Path:
    src = tmp / "page.html"
    src.write_text(text, "utf-8")
    return src


def _words(src: Path) -> list[str] | None:
    probe = convert.raw_text(src, convert.spec_for(src))
    return probe.text.split() if probe.measured else None


@BS4
def test_the_probe_reads_each_page_as_docling_parses_it(tmp_path: Path) -> None:
    pages = CASES | REFUSED
    got = {case: _words(_page(tmp_path, page)) for case, (page, _) in pages.items()}
    wrong = {case: words for case, words in got.items() if words != pages[case][1]}
    assert not wrong, wrong


@BS4
def test_html_probe_excludes_head_script_and_style(tmp_path: Path) -> None:
    """The regression this probe actually shipped with: `<head><title>X</title>` repeats the
    page's own H1 in a region Docling treats as metadata, so the multiset diff demanded the title
    TWICE and refused a correct conversion of a four-line page at 0.9231."""
    src = tmp_path / "page.html"
    src.write_text(
        "<html><head><title>Boundary Principle</title></head><body>"
        "<h1>Boundary Principle</h1><script>var hidden = 1;</script>"
        "<style>.x{color:red}</style><p>A rule that cannot refuse reads as absent.</p>"
        "</body></html>", "utf-8")
    spec = convert.spec_for(src)
    text = convert.raw_text(src, spec).text
    assert text.count("Boundary") == 1
    assert "hidden" not in text and "color" not in text
    # ... and the correct conversion now clears the gate instead of being refused by it.
    cov, _, _ = convert.verify_conversion(
        src, spec, "# Boundary Principle\n\nA rule that cannot refuse reads as absent.\n")
    assert cov == 1.0


@BS4
def test_html_probe_excludes_the_title_when_head_is_never_closed(tmp_path: Path) -> None:
    """HTML5 makes `</head>` OPTIONAL, and on a page that omits it the regex probe's `head` branch
    could not match, so the title walked back into the probe text — the 0.9231 false refusal,
    reached by a page that is perfectly legal. The probe now leaves out the title Docling takes."""
    src = tmp_path / "nohead.html"
    src.write_text(
        "<!DOCTYPE html><html><head><title>Boundary Principle</title>"
        "<body><h1>Boundary Principle</h1>"
        "<p>A rule that cannot refuse reads as absent.</p></body></html>", "utf-8")
    spec = convert.spec_for(src)
    assert convert.raw_text(src, spec).text.count("Boundary") == 1
    cov, _, _ = convert.verify_conversion(
        src, spec, "# Boundary Principle\n\nA rule that cannot refuse reads as absent.\n")
    assert cov == 1.0


@BS4
def test_html_probe_leaves_no_markup_fragment_behind_a_nested_gt(tmp_path: Path) -> None:
    """`<[^>]*>` stops at the FIRST `>`, so a comment or an attribute value containing one — both
    legal, both ordinary — left the tail of the markup in the probe as "words". `always` and `here`
    below are markup no faithful conversion contains, and they took this page to 0.7143 against the
    0.95 gate: the measuring instrument refusing a valid file, the failure this section has already
    shipped once."""
    src = tmp_path / "attrs.html"
    src.write_text(
        "<html><body><!-- editor: 3 > 2 always -->"
        "<p>See <img src=\"chart.png\" alt=\"ratio a > b here\"> the ruling for detail.</p>"
        "</body></html>", "utf-8")
    spec = convert.spec_for(src)
    text = convert.raw_text(src, spec).text
    assert set(text.split()) == {"See", "the", "ruling", "for", "detail."}, text
    cov, missing, _ = convert.verify_conversion(src, spec, "See the ruling for detail.\n")
    assert cov == 1.0, missing


@BS4
def test_the_gate_asks_for_the_body_and_not_the_script(tmp_path: Path) -> None:
    """The fault this replaces, both ways. `</script foo>` did not close the script for the regex
    probe, so the body between two scripts was never asked for and a conversion that lost it passed
    at 1.0; `</script foo="bar">` closed nothing, so it asked for the code and refused a correct
    conversion."""
    src = _page(tmp_path, "<p>Quarterly review</p><script>var a;</script foo>"
                "<p>The engine ingested six scopes without a refusal.</p><script>var b;</script>")
    with pytest.raises(convert.ConversionRefused, match="six"):
        convert.verify_conversion(src, convert.spec_for(src), "Quarterly review\n")
    src = _page(tmp_path, '<p>Quarterly review</p><script>var hidden = 1;</script foo="bar">'
                "<p>The engine ingested six scopes.</p>")
    cov, missing, _ = convert.verify_conversion(
        src, convert.spec_for(src), "Quarterly review\n\nThe engine ingested six scopes.\n")
    assert cov == 1.0, missing


@BS4
def test_title_text_is_never_checked(tmp_path: Path) -> None:
    """The probe's one exclusion, and its exposure: Docling renders an SVG's title, and a conversion
    that lost it still clears the gate, because no title is ever asked for."""
    src = _page(tmp_path, "<body><h1>Report</h1><svg><title>Download Report</title></svg>"
                "<p>The engine ingested six scopes.</p></body>")
    cov, missing, _ = convert.verify_conversion(
        src, convert.spec_for(src), "# Report\n\nThe engine ingested six scopes.\n")
    assert (cov, missing) == (1.0, [])


@BS4
def test_known_blind_spot_a_word_split_into_one_character_pieces_is_not_checked(tmp_path: Path) -> None:
    """KNOWN BLIND SPOT, pinned so the fix has something to flip. Strings are joined with a space,
    so `<span>4</span><span>2</span>` reads as `4 2`, two pieces under the two-character token
    floor, while Docling renders `42`: the probe asks for neither, and a conversion that lost the
    number clears the gate. The regex probe read it the same way. The same join refuses a drop cap
    (`<span>T</span>he` asks for `he`); both are one change to how the probe joins strings."""
    src = _page(tmp_path, "<p>Revenue <span>4</span><span>2</span> million</p>")
    assert _words(src) == ["Revenue", "4", "2", "million"]
    cov, missing, _ = convert.verify_conversion(src, convert.spec_for(src), "Revenue million\n")
    assert (cov, missing) == (1.0, [])


@BS4
def test_unclosed_markup_is_read_in_one_pass(tmp_path: Path) -> None:
    """Markup that never closes was rescanned to the end of the page from every later candidate:
    16,000 repeats (80 to 340 KB) took 5 to 123 seconds on the regex probe, and at most 0.12 now."""
    slow = {}
    for lead, unclosed in [
        ("", "<script> "), ("", "<!-- "), ("", "<head> "), ("", "<title> "), ("", "<p a=' "),
        ("<script>", '</script a="" b=""'), ("<script>", '=" </script "'),
        ("<script>", '=" <script </script "'), ("<script>", '<script </script "'),
        ("", "<noscript> "),
    ]:
        src = _page(tmp_path, lead + unclosed * 16000)
        t0 = time.perf_counter()
        convert.raw_text(src, convert.spec_for(src))
        if (took := time.perf_counter() - t0) >= 1.0:
            slow[lead + unclosed] = round(took, 2)
    assert not slow, slow


@BS4
def test_the_page_is_decoded_as_docling_decodes_it(tmp_path: Path) -> None:
    """Docling hands BeautifulSoup the bytes, which reads a BOM and `<meta charset>`. The regex
    probe read UTF-8 whatever the page said: a cp1252 page lost every accented word, a UTF-16 one
    measured as empty, and a declaration that misnames UTF-8 bytes read as UTF-8 where Docling
    follows the declaration."""
    for encoding, meta, words in (
        ("cp1252", "<meta charset='windows-1252'>", ["Résumé", "of", "the", "café", "façade"]),
        ("utf-16", "", ["Résumé", "of", "the", "café", "façade"]),
        ("utf-8", "<meta charset='iso-8859-1'>", ["RÃ©sumÃ©", "of", "the", "cafÃ©", "faÃ§ade"]),
    ):
        src = tmp_path / f"{encoding}.html"
        src.write_bytes(f"<html><head>{meta}</head><body><p>Résumé of the café façade</p></body>"
                        "</html>".encode(encoding))
        assert _words(src) == words, encoding


def test_a_fault_in_the_probe_is_a_named_non_measurement(tmp_path: Path, monkeypatch) -> None:
    """A probe that raises does not refuse the file and does not report a coverage: it is
    `html-failed` with its error. `raw_text` holds that rule for every probe, and the CLI's
    UNMEASURED warning keys on the null coverage this returns
    (`test_ingest_formats.py::test_the_unmeasured_warning_fires_for_a_broken_probe_not_just_an_absent_one`)."""
    def broken(src: Path) -> str:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(convert, "_html_text", broken)
    src = _page(tmp_path, "<p>The engine ingested six scopes.</p>")
    cov, missing, probe = convert.verify_conversion(src, convert.spec_for(src), "Anything at all.\n")
    assert (cov, missing, probe.label) == (None, [], "html-failed")
    assert probe.error == "RecursionError: maximum recursion depth exceeded"


DOCLING = pytest.mark.skipif(
    os.environ.get("SUBSTRATE_DOCLING_TESTS") != "1" or importlib.util.find_spec("docling") is None,
    reason="needs SUBSTRATE_DOCLING_TESTS=1 and Docling")


@DOCLING
def test_docling_roundtrip_each_page_clears_the_gate(tmp_path: Path) -> None:
    """The probe asks only for words Docling renders: each page above converts at coverage 1.0."""
    below = {}
    for case, (page, _) in CASES.items():
        src = _page(tmp_path, page)
        cov = convert.to_markdown(src, convert.spec_for(src), log=lambda *_: None).stats["raw_coverage"]
        if cov != 1.0:
            below[case] = cov
    assert not below, below


@DOCLING
def test_docling_roundtrip_refuses_each_page_it_loses_text_from(tmp_path: Path) -> None:
    passed = {}
    for case, (page, _) in REFUSED.items():
        src = _page(tmp_path, page)
        try:
            passed[case] = convert.to_markdown(src, convert.spec_for(src),
                                               log=lambda *_: None).stats["raw_coverage"]
        except convert.ConversionRefused:
            pass
    assert not passed, passed


# The two steps of Docling's reader that repair the tree after its removal step. Private names, held
# by the `<2.149` pin; the differential below fails, naming what moved, if either is gone.
_RESTRUCTURING = ("_close_implied_end_tags", "_fix_invalid_paragraph_structure")


def _generated_pages() -> list[str]:
    pages = [page for page, _ in (CASES | REFUSED).values()]
    for seed in range(4):
        rng = random.Random(seed)
        pages += ["".join(rng.choice(_FRAGMENTS) if rng.random() < 0.5 else f" {rng.choice(_WORDS)} "
                          for _ in range(rng.randint(1, 40))) for _ in range(250)]
    return pages


def _docling_html():
    """Docling's HTML reader module, failing loudly if a private name the tests below use moved."""
    from docling.backend import html_backend

    moved = [name for name in _RESTRUCTURING if not hasattr(html_backend.HTMLDocumentBackend, name)]
    moved += [] if hasattr(html_backend, "_BR_SENTINEL") else ["_BR_SENTINEL"]
    assert not moved, (f"Docling's HTML reader no longer has {moved}: find what replaced them in "
                       "docling/backend/html_backend.py before trusting these comparisons")
    return html_backend


def _converted_reader(path: Path):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.document import InputDocument

    backend = _docling_html().HTMLDocumentBackend
    reader = backend(InputDocument(path, InputFormat.HTML, backend), path)
    reader.convert()
    return reader


@DOCLING
def test_docling_reader_removes_exactly_what_the_probe_removes(tmp_path: Path, monkeypatch) -> None:
    """Docling's soup after its removal step, read whole as the probe reads its own, gives the
    probe's words, page for page: the parser and the removal are Docling's own.

    Read off the backend's `soup` after `convert()`, with the steps that repair the tree after the
    removal patched out. The `<br>` placeholder its walk writes in reads as the space a `<br>` is."""
    html_backend = _docling_html()
    for name in _RESTRUCTURING:
        monkeypatch.setattr(html_backend.HTMLDocumentBackend, name, staticmethod(lambda soup: None))
    differ = []
    for page in _generated_pages():
        src = _page(tmp_path, page)
        held = convert._html_reading(_converted_reader(src).soup).replace(html_backend._BR_SENTINEL, " ")
        if Counter(convert._html_text(src).split()) != Counter(held.split()):
            differ.append(page)
    assert not differ, differ[:5]


@DOCLING
def test_docling_walks_no_word_the_probe_lacks(tmp_path: Path) -> None:
    """Wherever probe and reader differ on anything but `<title>` text, the probe holds more, so the
    difference refuses and never passes. Docling walks `soup.body or soup` of its repaired tree;
    every string there outside a title, split on whitespace, is in the probe's."""
    from bs4.element import NavigableString, PreformattedString

    html_backend = _docling_html()
    more = []
    for page in _generated_pages():
        src = _page(tmp_path, page)
        soup = _converted_reader(src).soup
        titles = {id(t) for t in soup("title")}
        walked = Counter(" ".join(
            str(s).replace(html_backend._BR_SENTINEL, " ") for s in (soup.body or soup).descendants
            if isinstance(s, NavigableString) and not isinstance(s, PreformattedString)
            and not any(id(p) in titles for p in s.parents)).split())
        if extra := walked - Counter(convert._html_text(src).split()):
            more.append((page, dict(extra)))
    assert not more, more[:5]


_FRAGMENTS = [
    "<p>", "</p>", "<div>", "</div>", "<b>", "</b>", "<br>", "</br>", "<img src=x>", "</img>",
    "<noscript>", "</noscript>", "<template>", "<template shadowrootmode=open>", "</template>",
    "<script>", "</script>", "<script foo>", "</script foo>", "<style>", "</style>", "<title>",
    "</title>", "<head>", "</head>", "<body>", "</body>", "<p hidden>", "<span hidden>", "</span>",
    "<!--", "-->", "--!>", "<![CDATA[", "]]>", "<!DOCTYPE html>", "<?pi?>", "&amp;", "&region",
    "&#1;", "&copy;", "<", ">", '"', "'", "<table>", "<tr>", "<td>", "</td>", "</tr>", "</table>",
    "<ul>", "<li>", "</ul>", "<iframe>", "</iframe>", "<textarea>", "</textarea>", "<header>",
    "</header>", "<h1>", "</h1>", "<a href='x>y'>", "</a>",
]
_WORDS = ["alpha", "beta", "gamma", "delta", "kept", "words", "after", "café", "engine"]
