"""Fetch a job's real advert text when the board only gave us a teaser.

Most UK job boards return a one-line teaser in their listing feed and keep the
advert itself on the job's own page. Measured across four real Apify runs (320
jobs), only themuse.com returned a usable description; reed, totaljobs, indeed,
cwjobs and cv-library all returned a median of 60-154 characters. That left 94%
of every search unscoreable: there is nothing in a 127-character teaser to match
a CV against, so the score was low or absent no matter how good the candidate.

This module recovers the advert from the job's own page. It reads the schema.org
``JobPosting`` block that boards embed for Google Jobs — structured, stable, and
far more reliable than scraping page furniture.

Best-effort throughout: a fetch that fails, times out or is blocked leaves the
original teaser untouched and the job is still shown. Nothing here can fail a
search.

Only boards that actually serve the page to us are attempted (see FETCHABLE_HOSTS).
cv-library, totaljobs/cwjobs and indeed answer with 403 to any non-browser client,
and hammering them on every run would earn nothing but a rate-limit.
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# Below this, a "description" is a teaser: too thin to mine requirements from,
# so the job is worth a fetch. The full adverts we do get run to thousands of
# characters, so this threshold is nowhere near them.
MIN_USEFUL_DESCRIPTION = 400

# Boards that serve their job pages to a plain HTTP client. The others answer
# 403 (they front their pages with bot protection), so we do not waste a request
# on them. Add a host here once it is confirmed fetchable.
FETCHABLE_HOSTS = (
    'reed.co.uk',
    'themuse.com',
)

FETCH_TIMEOUT = 15
MAX_WORKERS = 6

BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/122.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

_JSON_LD_RE = re.compile(
    r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
)


def _strip_html(text):
    """Turn the advert's HTML into the plain text the keyword miner expects."""
    text = re.sub(r'(?i)<br\s*/?>|</p>|</li>|</div>', '\n', text)
    text = re.sub(r'(?s)<[^>]+>', '', text)
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&')
    text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&#39;', "'")
    text = re.sub(r'&[a-z]+;', ' ', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _job_posting_nodes(payload):
    """Yield every JobPosting object in a parsed JSON-LD payload.

    A page may carry several blocks (JobPosting, BreadcrumbList, Organization),
    a block may be a list, and it may nest its content under @graph.
    """
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            if str(node.get('@type')) == 'JobPosting':
                yield node
            if '@graph' in node:
                stack.append(node['@graph'])


def extract_job_posting_description(html):
    """The advert text from a page's schema.org JobPosting block, or ''."""
    for block in _JSON_LD_RE.findall(html or ''):
        try:
            payload = json.loads(block.strip())
        except (json.JSONDecodeError, TypeError):
            continue  # a malformed block is not a reason to give up on the page
        for node in _job_posting_nodes(payload):
            description = _strip_html(str(node.get('description') or ''))
            if description:
                return description
    return ''


def is_fetchable(url):
    """Is this a board we know serves its pages to us?"""
    host = (urlparse(url or '').hostname or '').lower()
    if not host:
        return False
    return any(host == h or host.endswith('.' + h) for h in FETCHABLE_HOSTS)


def fetch_description(url, session=None):
    """The full advert at ``url``, or '' if it cannot be had. Never raises."""
    if not is_fetchable(url):
        return ''
    try:
        getter = session or requests
        response = getter.get(url, headers=BROWSER_HEADERS, timeout=FETCH_TIMEOUT)
        if response.status_code != 200:
            logger.info('Advert fetch got HTTP %s for %s', response.status_code, url)
            return ''
        return extract_job_posting_description(response.text)
    except Exception as exc:
        logger.info('Advert fetch failed for %s: %s', url, exc)
        return ''


def needs_description(job):
    """Is this job's description too thin to score, and worth fetching?"""
    if len((job.get('description') or '').strip()) >= MIN_USEFUL_DESCRIPTION:
        return False
    return is_fetchable(job.get('applyLink') or '')


def enrich_descriptions(jobs, max_workers=MAX_WORKERS):
    """Replace teaser descriptions with the real advert, in place.

    Returns the number of jobs actually improved. The fetches are network-bound
    and independent, so they run concurrently; each one is best-effort, and a job
    whose advert cannot be fetched simply keeps the teaser it arrived with.
    """
    targets = [job for job in jobs if needs_description(job)]
    if not targets:
        return 0

    improved = 0
    with requests.Session() as session:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(fetch_description, job['applyLink'], session): job
                for job in targets
            }
            for future in as_completed(futures):
                job = futures[future]
                try:
                    description = future.result()
                except Exception:  # pragma: no cover - fetch_description swallows
                    continue
                # Only ever an improvement: never overwrite what we have with less.
                if len(description) > len(job.get('description') or ''):
                    job['description'] = description
                    improved += 1

    logger.info(
        'Fetched the full advert for %d of %d thin descriptions (%d jobs total)',
        improved, len(targets), len(jobs),
    )
    return improved
