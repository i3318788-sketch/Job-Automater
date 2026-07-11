"""Apify integration for fetching job listings.

Wraps the Apify "UK Jobs Aggregator" actor. The actor id can be overridden via
the ``APIFY_JOBS_ACTOR`` setting/env var; it defaults to a popular UK jobs
aggregator. Returns a list of normalized job dicts so callers don't depend on
the actor's raw field names.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Default actor. Override with APIFY_JOBS_ACTOR in .env if you use a different one.
DEFAULT_ACTOR_ID = getattr(
    settings, 'APIFY_JOBS_ACTOR', None
) or 'memo23/apify-uk-jobs-aggregator'


class ApifyConfigError(RuntimeError):
    """Raised when Apify is not configured (missing token)."""


class ApifySearchError(RuntimeError):
    """Raised when the Apify actor run or dataset fetch fails."""


def _first(mapping, *keys, default=''):
    """Return the first present, non-empty value among ``keys`` in ``mapping``."""
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ''):
            return value
    return default


def _as_dict(obj):
    """Coerce an Apify SDK item/model into a plain dict.

    Newer apify-client versions may return pydantic models instead of dicts.
    """
    if isinstance(obj, dict):
        return obj
    for attr in ('model_dump', 'dict'):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return method()
            except Exception:  # pragma: no cover - defensive
                pass
    return getattr(obj, '__dict__', {}) or {}


def _run_dataset_id(run):
    """Extract the default dataset id from a Run dict or a typed Run object."""
    if not run:
        return None
    if isinstance(run, dict):
        return run.get('defaultDatasetId')
    return (
        getattr(run, 'default_dataset_id', None)
        or getattr(run, 'defaultDatasetId', None)
    )


def normalize_job(raw):
    """Map a raw Apify dataset item to our internal job dict.

    Handles the field-name variance between actors gracefully; anything missing
    falls back to an empty string.
    """
    return {
        'title': str(_first(raw, 'title', 'jobTitle', 'position')),
        'company': str(_first(raw, 'company', 'companyName', 'employer')),
        'location': str(_first(raw, 'location', 'jobLocation', 'city')),
        'datePosted': str(_first(raw, 'datePosted', 'date_posted', 'postedAt', 'posted_at', 'date', 'publishedAt')),
        'employmentType': str(_first(raw, 'employmentType', 'employment_type', 'jobType', 'job_type', 'contractType')),
        'seniorityLevel': str(_first(raw, 'seniorityLevel', 'seniority', 'experienceLevel')),
        'salary': str(_first(raw, 'salary', 'salary_raw', 'salaryRange', 'salaryText', 'compensation')) or _build_salary(raw),
        'description': str(_first(raw, 'description', 'jobDescription', 'descriptionText', 'summary', 'snippet')),
        'applyLink': str(_first(raw, 'applyLink', 'direct_apply_url', 'applyUrl', 'applicationLink', 'url', 'jobUrl', 'redirectUrl', 'link')),
    }


def _build_salary(raw):
    """Compose a salary string from structured salary fields when no raw text exists."""
    currency = str(raw.get('salary_currency') or '')
    symbol = {'GBP': '£', 'USD': '$', 'EUR': '€'}.get(currency.upper(), currency)

    def _num(*keys):
        for k in keys:
            v = raw.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return None

    lo = _num('salary_annual_min', 'salary_min')
    hi = _num('salary_annual_max', 'salary_max')
    if lo and hi and lo != hi:
        return f'{symbol}{lo:,} - {symbol}{hi:,}'
    value = lo or hi
    if value:
        return f'{symbol}{value:,}'
    hourly = _num('salary_hourly')
    if hourly:
        return f'{symbol}{hourly}/hr'
    return ''


# Maps country/region names to the actor's `country` enum (uk/us/de/fr/nl/au/remote).
COUNTRY_CODES = {
    'united kingdom': 'uk', 'uk': 'uk', 'england': 'uk', 'scotland': 'uk',
    'wales': 'uk', 'northern ireland': 'uk', 'britain': 'uk', 'great britain': 'uk',
    'united states': 'us', 'usa': 'us', 'us': 'us', 'america': 'us',
    'germany': 'de', 'deutschland': 'de',
    'france': 'fr',
    'netherlands': 'nl', 'holland': 'nl',
    'australia': 'au',
    'remote': 'remote',
}


def _build_actor_input(location, min_salary, limit):
    """Build input for the ``doggo/uk-jobs-board-scraper`` actor.

    The actor requires ``keyword`` and ``location`` (enums, overridable by the
    ``custom_*`` fields) and expects ``searchTerms`` to be an array. See the
    actor's input schema for the full field list.
    """
    country_code = COUNTRY_CODES.get((location or '').strip().lower(), 'uk')
    keyword = getattr(settings, 'APIFY_SEARCH_KEYWORD', '') or 'software engineer'

    actor_input = {
        # Required enum preset; custom_keyword overrides it with our real term.
        'keyword': 'software engineer',
        'custom_keyword': keyword,
        # MUST be an array for this actor (empty = rely on keyword only).
        'searchTerms': [],
        # Required enum preset; custom_location overrides it with the requested area.
        'location': 'London',
        'custom_location': location or 'United Kingdom',
        'country': country_code,
        # Actor enforces a minimum of 100.
        'max_results': max(100, int(limit)),
        'deduplicate': True,
        'descriptionFormat': 'plaintext',
    }
    if min_salary is not None:
        actor_input['salary_min'] = int(min_salary)
    return actor_input


def search_jobs(country_list, min_salary=None, limit=200):
    """Run the Apify jobs actor and return a list of normalized job dicts.

    Args:
        country_list: list of country/location strings; the first is used as the
            primary location for the actor input.
        min_salary: optional int minimum salary passed to the actor if supported.
        limit: maximum number of jobs to return (default 200).

    Raises:
        ApifyConfigError: if APIFY_API_TOKEN is not configured.
        ApifySearchError: if the actor run or dataset fetch fails.
    """
    token = getattr(settings, 'APIFY_API_TOKEN', '')
    if not token:
        raise ApifyConfigError('APIFY_API_TOKEN is not configured in .env')

    location = (country_list or ['United Kingdom'])[0]

    try:
        from apify_client import ApifyClient
    except ImportError as exc:  # pragma: no cover - dependency guaranteed present
        raise ApifySearchError('apify-client is not installed') from exc

    client = ApifyClient(token)
    actor_input = _build_actor_input(location, min_salary, limit)

    logger.info('Starting Apify actor %s for location=%s', DEFAULT_ACTOR_ID, location)
    try:
        run = client.actor(DEFAULT_ACTOR_ID).call(run_input=actor_input)
    except Exception as exc:
        logger.exception('Apify actor run failed')
        raise ApifySearchError(f'Apify actor run failed: {exc}') from exc

    dataset_id = _run_dataset_id(run)
    if not dataset_id:
        raise ApifySearchError('Apify run returned no dataset')

    jobs = []
    try:
        for item in client.dataset(dataset_id).iterate_items():
            jobs.append(normalize_job(_as_dict(item)))
            if len(jobs) >= limit:
                break
    except Exception as exc:
        logger.exception('Fetching Apify dataset failed')
        raise ApifySearchError(f'Fetching Apify dataset failed: {exc}') from exc

    # Post-fetch location filter (some actors ignore the location input).
    filtered = _filter_by_location(jobs, country_list)
    logger.info('Apify returned %d jobs (%d after location filter)', len(jobs), len(filtered))
    return filtered


def _filter_by_location(jobs, country_list):
    """Keep jobs whose location mentions any requested country.

    If no job matches (e.g. the actor returns short/blank locations), fall back
    to returning everything rather than silently dropping all results.
    """
    if not country_list:
        return jobs
    needles = [c.lower() for c in country_list if c]
    # "United Kingdom" jobs are often labelled by city/region, so also accept
    # common UK synonyms when the UK is requested.
    if any('united kingdom' in n for n in needles):
        needles += ['uk', 'england', 'scotland', 'wales', 'northern ireland']

    matched = [
        job for job in jobs
        if any(n in job['location'].lower() for n in needles)
    ]
    return matched or jobs
