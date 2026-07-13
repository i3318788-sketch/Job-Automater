"""Static city lists per supported country.

The Apify actor scopes searches by a country enum plus a free-text location, so
the city is what actually narrows a search from "United Kingdom" to "Bristol".
Kept as a static table rather than an API call: the list changes rarely, and a
lookup that can fail at request time would be a poor trade for data this stable.

Keys are the actor's country codes (see ``apify_service.COUNTRY_CODES``).
"""

COUNTRY_CITIES = {
    'uk': [
        'London', 'Manchester', 'Birmingham', 'Leeds', 'Glasgow', 'Edinburgh',
        'Liverpool', 'Bristol', 'Sheffield', 'Cardiff', 'Belfast', 'Nottingham',
        'Newcastle upon Tyne', 'Leicester', 'Cambridge', 'Oxford', 'Reading',
        'Brighton', 'Southampton', 'Coventry', 'Milton Keynes', 'Aberdeen',
        'Bradford', 'Derby', 'Plymouth', 'Stoke-on-Trent', 'Wolverhampton',
        'Portsmouth', 'York', 'Swansea',
    ],
    'us': [
        'New York', 'San Francisco', 'Los Angeles', 'Seattle', 'Austin',
        'Boston', 'Chicago', 'Denver', 'Atlanta', 'Dallas', 'Houston',
        'Washington DC', 'Philadelphia', 'San Diego', 'Phoenix', 'Portland',
        'Miami', 'Minneapolis', 'Detroit', 'San Jose',
    ],
    'de': [
        'Berlin', 'Munich', 'Hamburg', 'Frankfurt', 'Cologne', 'Stuttgart',
        'Düsseldorf', 'Leipzig', 'Dortmund', 'Essen', 'Bremen', 'Dresden',
        'Hanover', 'Nuremberg',
    ],
    'fr': [
        'Paris', 'Lyon', 'Marseille', 'Toulouse', 'Nice', 'Nantes',
        'Strasbourg', 'Montpellier', 'Bordeaux', 'Lille', 'Rennes',
    ],
    'nl': [
        'Amsterdam', 'Rotterdam', 'The Hague', 'Utrecht', 'Eindhoven',
        'Groningen', 'Tilburg', 'Almere', 'Breda', 'Nijmegen',
    ],
    'au': [
        'Sydney', 'Melbourne', 'Brisbane', 'Perth', 'Adelaide', 'Canberra',
        'Gold Coast', 'Newcastle', 'Hobart', 'Darwin',
    ],
}


def cities_for_country(country):
    """Cities for a country name or code. Empty list when unsupported."""
    from .apify_service import COUNTRY_CODES

    if not country:
        return []
    key = str(country).strip().lower()
    code = COUNTRY_CODES.get(key, key)
    return COUNTRY_CITIES.get(code, [])


def cities_by_country_name():
    """Map every country *name* the preferences form offers to its cities.

    The form stores country names ("United Kingdom"), while this module is keyed
    by actor codes ("uk"), so the template needs the name-keyed view to drive its
    dropdown without a round trip.
    """
    from .apify_service import COUNTRY_CODES
    from ..forms import COUNTRY_CHOICES

    mapping = {}
    for name, _label in COUNTRY_CHOICES:
        code = COUNTRY_CODES.get(name.strip().lower())
        cities = COUNTRY_CITIES.get(code, []) if code else []
        if cities:
            mapping[name] = cities
    return mapping


def is_valid_city(city, countries=None):
    """Is ``city`` a known city, optionally within one of ``countries``?"""
    if not city:
        return True  # blank = no city filter, always valid
    target = str(city).strip().lower()
    if countries:
        pools = [c for country in countries for c in cities_for_country(country)]
    else:
        pools = [c for cities in COUNTRY_CITIES.values() for c in cities]
    return any(target == c.lower() for c in pools)
