"""Fetch representative photographs for seeded animals.

Image quality matters here: spec 24 makes photographs a required part of the
experience, not decoration. Generic keyword image services return loosely
tagged pictures - a search for "german shepherd" can return a car park - so
each kind of animal uses a source that actually knows what it is serving:

    dogs     dog.ceo, which is organised by breed
    cats     TheCatAPI, which filters by breed identifier
    the rest Wikimedia Commons *categories*, curated per species

All three are free and need no API key.

Two things this module has to get right, because the roster is no longer six
familiar species:

- **Exotic kinds need their own source.** A bearded dragon, an axolotl and a
  miniature donkey share the `Species.OTHER` enum value and nothing else, so
  categories are keyed on the breed name first and fall back to the species
  and then to a generic pet category. That layering is what stops a tortoise
  listing from showing somebody's cat.
- **Commons rate-limits anonymous callers.** It answers a burst of roughly
  thirty requests and then returns 429 with a `Retry-After` header for the
  rest of the minute. Seeding asks for one category per kind of animal, which
  is easily enough to hit that ceiling, so calls here are paced and a refusal
  is waited out rather than mistaken for "this animal has no photographs".
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from app.domain.enums import Species

REQUEST_TIMEOUT_SECONDS = 25
USER_AGENT = "PetMatch/0.1 (student project; contact via repository)"

DOG_API_BREEDS = "https://dog.ceo/api/breed/{breed_path}/images/random/{count}"
DOG_API_ANY = "https://dog.ceo/api/breeds/image/random/{count}"
CAT_API = "https://api.thecatapi.com/v1/images/search"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Cache key for "any animal of this kind will do", used when a breed is not
# recognised and when a breed-specific search comes back empty.
ANY_BREED_KEY = "__any__"

IMAGES_PER_DOG_CALL = 8
IMAGES_PER_CAT_CALL = 10

# Our breed names mapped onto dog.ceo's taxonomy, verified against its
# /breeds/list/all endpoint. Anything absent falls back to a random dog.
#
# Two deliberate approximations: dog.ceo holds no plain greyhound, only the
# Italian variety, and it has no Canaan Dog - the Indian pariah dog is the
# same landrace type and photographs as one.
DOG_BREED_PATHS = {
    "Akita": "akita",
    "Alaskan Malamute": "malamute",
    "Australian Cattle Dog": "cattledog/australian",
    "Australian Shepherd": "australian/shepherd",
    "Basset Hound": "hound/basset",
    "Beagle": "beagle",
    "Belgian Malinois": "malinois",
    "Bernese Mountain Dog": "mountain/bernese",
    "Border Collie": "collie/border",
    "Border Terrier": "terrier/border",
    "Boston Terrier": "bulldog/boston",
    "Boxer": "boxer",
    "Bulldog": "bulldog/english",
    "Canaan Dog": "pariah/indian",
    "Canaan Dog Mix": "pariah/indian",
    "Cavalier King Charles Spaniel": "spaniel/blenheim",
    "Chihuahua": "chihuahua",
    "Cockapoo": "cockapoo",
    "Collie Mix": "collie/border",
    "Dachshund": "dachshund",
    "Dalmatian": "dalmatian",
    "French Bulldog": "bulldog/french",
    "German Shepherd": "german/shepherd",
    "Golden Retriever": "retriever/golden",
    "Great Dane": "dane/great",
    "Greyhound": "greyhound/italian",
    "Irish Setter": "setter/irish",
    "Jack Russell Terrier": "terrier/russell",
    "Labrador Retriever": "labrador",
    "Labradoodle": "labradoodle",
    "Maltese": "maltese",
    "Miniature Schnauzer": "schnauzer/miniature",
    "Mixed Breed": "mix",
    "Papillon": "papillon",
    "Pembroke Welsh Corgi": "pembroke",
    "Pitbull Terrier": "pitbull",
    "Poodle": "poodle/standard",
    "Pug": "pug",
    "Rottweiler": "rottweiler",
    "Saluki": "saluki",
    "Samoyed": "samoyed",
    "Shetland Sheepdog": "sheepdog/shetland",
    "Shiba Inu": "shiba",
    "Shih Tzu": "shihtzu",
    "Siberian Husky": "husky",
    "Staffordshire Bull Terrier": "bullterrier/staffordshire",
    "Weimaraner": "weimaraner",
    "Whippet": "whippet",
}

# TheCatAPI breed identifiers. /v1/breeds now needs a key, but the image
# search accepts breed_ids without one, so these were verified by asking for
# images of each and checking that the list came back non-empty. Mixed-breed
# descriptions such as "Tabby" have no identifier and get a random cat.
CAT_BREED_IDS = {
    "Abyssinian": "abys",
    "Bengal": "beng",
    "Bombay": "bomb",
    "British Shorthair": "bsho",
    "Burmese": "bure",
    "Chartreux": "char",
    "Devon Rex": "drex",
    "Egyptian Mau": "emau",
    "Himalayan": "hima",
    "Japanese Bobtail": "jbob",
    "Maine Coon": "mcoo",
    "Munchkin": "munc",
    "Nebelung": "nebe",
    "Norwegian Forest Cat": "norw",
    "Oriental Shorthair": "orie",
    "Persian": "pers",
    "Ragdoll": "ragd",
    "Russian Blue": "rblu",
    "Scottish Fold": "sfol",
    "Selkirk Rex": "srex",
    "Singapura": "sing",
    "Somali": "soma",
    "Sphynx": "sphy",
    "Tonkinese": "tonk",
    "Turkish Angora": "tang",
}

# Commons *categories*, not free-text search. Search relevance on Commons is
# poor for this purpose - a query for "domestic rabbit pet" returned a 1930s
# photograph of a cat on a radio. Categories are curated by species, so they
# return the right animal essentially every time.
#
# Every category below was checked against the live API for how many usable
# photographs it actually holds, because many plausible-looking Commons
# categories contain only subcategories and no files at all
# ("Category:Fancy rats" and "Category:Alpacas" are both empty in that sense).
COMMONS_CATEGORIES_BY_BREED: dict[str, tuple[str, ...]] = {
    # Birds
    "African Grey Parrot": ("Category:Psittacus erithacus",),
    "Budgerigar": ("Category:Melopsittacus undulatus",),
    "Canary": ("Category:Serinus canaria", "Category:Aviculture"),
    "Cockatiel": ("Category:Nymphicus hollandicus",),
    "Diamond Dove": ("Category:Geopelia cuneata",),
    "Eclectus Parrot": ("Category:Eclectus roratus", "Category:Aviculture"),
    "Galah Cockatoo": ("Category:Eolophus roseicapilla",),
    "Gouldian Finch": ("Category:Chloebia gouldiae",),
    "Green-cheeked Conure": ("Category:Pyrrhura molinae",),
    "Indian Ringneck Parakeet": ("Category:Psittacula krameri",),
    "Lovebird": ("Category:Agapornis", "Category:Aviculture"),
    "Quaker Parrot": ("Category:Myiopsitta monachus",),
    "Ringneck Dove": ("Category:Streptopelia roseogrisea",),
    "Society Finch": ("Category:Lonchura striata",),
    "Sulphur-crested Cockatoo": ("Category:Cacatua galerita",),
    "Sun Conure": ("Category:Aratinga solstitialis", "Category:Aviculture"),
    "Zebra Finch": ("Category:Taeniopygia guttata",),
    # Reptiles and amphibians
    "Axolotl": ("Category:Ambystoma mexicanum",),
    "Ball Python": ("Category:Python regius",),
    "Bearded Dragon": ("Category:Pogona vitticeps",),
    "Corn Snake": ("Category:Pantherophis guttatus",),
    "Greek Tortoise": ("Category:Testudo graeca",),
    "Hermann's Tortoise": ("Category:Testudo hermanni",),
    "Leopard Gecko": ("Category:Eublepharis macularius",),
    "Red-eared Slider": ("Category:Trachemys scripta elegans",),
    "Russian Tortoise": ("Category:Testudo horsfieldii",),
    # Exotic mammals
    "African Pygmy Hedgehog": ("Category:Atelerix albiventris",),
    "Chinchilla": ("Category:Chinchilla lanigera",),
    "Degu": ("Category:Octodon degus",),
    "Fancy Mouse": ("Category:Pet mice", "Category:Mus musculus"),
    "Fancy Rat": ("Category:Pet rats", "Category:Rattus norvegicus"),
    "Ferret": ("Category:Mustela putorius furo",),
    "Mongolian Gerbil": ("Category:Meriones unguiculatus",),
    "Sugar Glider": ("Category:Petaurus breviceps",),
    # Farm animals and poultry
    "Alpaca": ("Category:Vicugna pacos",),
    "Bantam Chicken": ("Category:Chickens",),
    "Domestic Duck": ("Category:Domestic ducks", "Category:Anas platyrhynchos domesticus"),
    "Miniature Donkey": ("Category:Equus asinus",),
    "Miniature Horse": ("Category:Miniature horses", "Category:Falabella"),
    "Nigerian Dwarf Goat": ("Category:Goats", "Category:Goat kids"),
    "Pot-bellied Pig": ("Category:Sus scrofa domesticus",),
}

COMMONS_CATEGORIES_BY_SPECIES: dict[Species, tuple[str, ...]] = {
    Species.RABBIT: (
        "Category:Lop rabbits",
        "Category:Rabbits",
        "Category:Cuniculture",
    ),
    Species.HAMSTER: (
        "Category:Mesocricetus auratus",
        "Category:Phodopus",
    ),
    Species.GUINEA_PIG: ("Category:Cavia porcellus",),
    Species.BIRD: (
        "Category:Nymphicus hollandicus",
        "Category:Melopsittacus undulatus",
        "Category:Aviculture",
    ),
    Species.OTHER: ("Category:Pets",),
}

# Last resort, so an animal whose own categories came back empty still gets a
# photograph of an animal rather than nothing at all.
GENERIC_PET_CATEGORIES: tuple[str, ...] = ("Category:Pets",)

# Commons categories also hold range maps, diagrams, engravings and museum
# specimens. None of those belong on an adoption listing.
NON_PHOTOGRAPH_TITLE_MARKERS = (
    "range", "map", "diagram", "chart", "distribution", "engraving", "drawing",
    "illustration", "lithograph", "plate", "skeleton", "skull", "anatomy",
    "taxidermy", "specimen", "logo", "icon", "sign", "stamp", "coat of arms",
)

ACCEPTED_MIME_TYPES = ("image/jpeg", "image/png")

# The saved file takes its extension from what the server actually sent. A PNG
# written as `.jpg` is then served with the wrong Content-Type; browsers sniff
# around that today, but it breaks the moment anybody adds an
# X-Content-Type-Options header, and animated GIFs do not belong on a listing.
EXTENSION_BY_CONTENT_TYPE = {"image/jpeg": ".jpg", "image/png": ".png"}

MINIMUM_IMAGE_BYTES = 4000
HTTP_OK = 200
HTTP_TOO_MANY_REQUESTS = 429
DOWNLOAD_ATTEMPTS_PER_ANIMAL = 4

# Stop walking a kind's category list once this many candidates are in hand.
# Enough to give every animal of that kind a different picture, without
# spending an API call on a category nobody will draw from.
CANDIDATES_WANTED_PER_KIND = 10
COMMONS_FILES_PER_CALL = 30

COMMONS_SECONDS_BETWEEN_CALLS = 2.5
COMMONS_ATTEMPTS_WHEN_RATE_LIMITED = 3
DEFAULT_RETRY_WAIT_SECONDS = 10.0
MAXIMUM_RETRY_WAIT_SECONDS = 45.0


def _looks_like_an_animal_photograph(file_title: str) -> bool:
    """Reject Commons files whose titles mark them as not a live animal photo.

    Args:
        file_title: The Commons file title, for example "File:Hamster.jpg".

    Returns:
        False when the title contains a marker such as "range map" or
        "engraving", True otherwise.
    """
    lowered = file_title.lower()
    return not any(marker in lowered for marker in NON_PHOTOGRAPH_TITLE_MARKERS)


def _retry_wait_seconds(response: requests.Response) -> float:
    """How long to wait before retrying a rate-limited Commons request.

    Args:
        response: The 429 response, whose `Retry-After` header Commons sets to
            the number of seconds left in the current window.

    Returns:
        A bounded number of seconds. Capped so a badly behaved header cannot
        stall a seed run indefinitely.
    """
    try:
        requested = float(response.headers.get("Retry-After", ""))
    except ValueError:
        requested = DEFAULT_RETRY_WAIT_SECONDS
    return min(max(requested, 1.0), MAXIMUM_RETRY_WAIT_SECONDS)


@dataclass
class PhotoFetcher:
    """Downloads one photograph per animal, caching candidate URLs per kind.

    Candidate lists are fetched once per breed - or per species where the
    breed has no source of its own - and then drawn from, so seeding a hundred
    and fifty animals does not make a hundred and fifty search calls.
    """

    randomizer: random.Random
    _dog_urls: dict[str, list[str]] = field(default_factory=dict)
    _cat_urls: dict[str, list[str]] = field(default_factory=dict)
    _commons_urls: dict[str, list[str]] = field(default_factory=dict)
    # Cache keys whose source came back with nothing. A dry pool is refilled
    # on demand, which is right when the source is alive and wrong when it is
    # not: a category that has been renamed, or an API that is down, was
    # asked again for every remaining animal of that kind - a hundred and
    # fifty round trips, each timing out, to learn the same thing.
    _exhausted_sources: set[str] = field(default_factory=set)
    _last_commons_call_at: float = 0.0

    def __post_init__(self) -> None:
        """Open the shared HTTP session."""
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})

    def close(self) -> None:
        """Release the HTTP session.

        A seed run opens one session and holds it across a hundred and fifty
        downloads, which is the point of it - but nothing closed it, so the
        connection pool was left to the garbage collector and Python warned
        about an unclosed socket on exit.
        """
        self._session.close()

    def _refilled(self, cache_key: str, urls: list[str]) -> list[str]:
        """Record a refill, remembering a source that answered with nothing.

        Args:
            cache_key: Which pool was refilled.
            urls: What the source returned.

        Returns:
            The same list, so a caller can return it directly.
        """
        if not urls:
            self._exhausted_sources.add(cache_key)
        return urls

    def fetch_into(
        self, species: Species, breed: str | None, destination: Path
    ) -> Path | None:
        """Download a photograph for one animal.

        Args:
            species: Determines which source is used.
            breed: Used for breed-accurate photographs when recognised, and
                for exotic animals it is the only usable key - every reptile,
                goat and ferret shares `Species.OTHER`.
            destination: Where to write the image file. Its extension is
                replaced by one matching what the server actually sent.

        These are free public APIs and individual images fail intermittently:
        a dead link, a redirect, an occasional 5xx. Without a retry one bad
        candidate leaves an animal with no picture, so each animal gets a few
        attempts before giving up.

        Returns:
            The path written, or None. None is not fatal - the seed script
            falls back to a shared photograph, and the interface falls back to
            a styled placeholder.
        """
        for _ in range(DOWNLOAD_ATTEMPTS_PER_ANIMAL):
            url = self._choose_url(species, breed)
            if url is None:
                return None
            written = self._download(url, destination)
            if written is not None:
                return written
        return None

    def _choose_url(self, species: Species, breed: str | None) -> str | None:
        """Pick a candidate image URL for one animal."""
        if species is Species.DOG:
            return self._next_dog_url(breed)
        if species is Species.CAT:
            return self._next_cat_url(breed)
        return self._next_commons_url(species, breed)

    # ----------------------------------------------------------------------
    # Dogs
    # ----------------------------------------------------------------------

    def _next_dog_url(self, breed: str | None) -> str | None:
        """Return a dog photograph, breed-accurate when the breed is known."""
        breed_path = DOG_BREED_PATHS.get(breed or "")
        if breed_path:
            breed_pool = self._dog_pool(breed_path, breed_path)
            if breed_pool:
                return breed_pool.pop()

        any_dog_pool = self._dog_pool(ANY_BREED_KEY, None)
        return any_dog_pool.pop() if any_dog_pool else None

    def _dog_pool(self, cache_key: str, breed_path: str | None) -> list[str]:
        """Candidate dog photograph URLs for one breed, fetched on demand.

        A dry pool is refetched rather than cached empty. Several animals draw
        from the same pool and each of them may burn more than one candidate
        on a dead link, so caching emptiness means the last few animals of a
        popular breed silently get no photograph at all.
        """
        pool = self._dog_urls.get(cache_key)
        if pool or cache_key in self._exhausted_sources:
            return pool or []

        endpoint = (
            DOG_API_BREEDS.format(breed_path=breed_path, count=IMAGES_PER_DOG_CALL)
            if breed_path
            else DOG_API_ANY.format(count=IMAGES_PER_DOG_CALL)
        )
        payload = self._get_json(endpoint)
        urls = (payload or {}).get("message", [])
        refilled = list(urls) if isinstance(urls, list) else []
        self._dog_urls[cache_key] = refilled
        return self._refilled(cache_key, refilled)

    # ----------------------------------------------------------------------
    # Cats
    # ----------------------------------------------------------------------

    def _next_cat_url(self, breed: str | None) -> str | None:
        """Return a cat photograph, breed-accurate when the breed is known."""
        breed_id = CAT_BREED_IDS.get(breed or "")
        if breed_id:
            breed_pool = self._cat_pool(breed_id, breed_id)
            if breed_pool:
                return breed_pool.pop()

        any_cat_pool = self._cat_pool(ANY_BREED_KEY, None)
        return any_cat_pool.pop() if any_cat_pool else None

    def _cat_pool(self, cache_key: str, breed_id: str | None) -> list[str]:
        """Candidate cat photograph URLs for one breed, fetched on demand.

        Refilled when dry, for the same reason as the dog pool and with more
        force: every cat whose description is not a recognised breed - the
        domestic shorthairs, the tabby, the calico - draws from one shared
        pool of ten, which nine cats will exhaust.
        """
        pool = self._cat_urls.get(cache_key)
        if pool or cache_key in self._exhausted_sources:
            return pool or []

        parameters: dict[str, str | int] = {"limit": IMAGES_PER_CAT_CALL}
        if breed_id:
            parameters["breed_ids"] = breed_id
        payload = self._get_json(CAT_API, params=parameters)
        refilled = (
            [item["url"] for item in payload if item.get("url")]
            if isinstance(payload, list)
            else []
        )
        self._cat_urls[cache_key] = refilled
        return self._refilled(cache_key, refilled)

    # ----------------------------------------------------------------------
    # Everything else, via Wikimedia Commons
    # ----------------------------------------------------------------------

    def _next_commons_url(self, species: Species, breed: str | None) -> str | None:
        """Return a photograph for one of the non-dog, non-cat kinds.

        Draws from curated categories rather than free-text search, and
        discards the maps, diagrams and engravings those categories also
        contain.
        """
        breed_name = breed or ""
        cache_key = (
            breed_name if breed_name in COMMONS_CATEGORIES_BY_BREED else species.value
        )
        pool = self._commons_urls.get(cache_key)
        if not pool:
            if cache_key in self._exhausted_sources:
                return None
            pool = self._refilled(
                cache_key, self._collect_commons_urls(species, breed_name)
            )
            self._commons_urls[cache_key] = pool

        return pool.pop() if pool else None

    def _collect_commons_urls(self, species: Species, breed_name: str) -> list[str]:
        """Gather candidate photographs for one kind of animal.

        Categories are tried in order - this breed, then this species, then
        pets in general - and the walk stops as soon as there are enough
        candidates, so a well-covered kind costs one API call.
        """
        collected: list[str] = []
        for category in _categories_for(species, breed_name):
            collected.extend(self._photographs_in_category(category))
            if len(collected) >= CANDIDATES_WANTED_PER_KIND:
                break

        self.randomizer.shuffle(collected)
        return collected

    def _photographs_in_category(self, category: str) -> list[str]:
        """List usable photograph URLs from one Commons category."""
        payload = self._get_commons_json(
            {
                "action": "query",
                "format": "json",
                "generator": "categorymembers",
                "gcmtitle": category,
                "gcmtype": "file",
                "gcmlimit": COMMONS_FILES_PER_CALL,
                "prop": "imageinfo",
                "iiprop": "url|mime",
                "iiurlwidth": 900,
            }
        )
        pages = (payload or {}).get("query", {}).get("pages", {})

        urls: list[str] = []
        for page in pages.values():
            image_info_list = page.get("imageinfo") or []
            if not image_info_list:
                continue
            image_info = image_info_list[0]
            if image_info.get("mime") not in ACCEPTED_MIME_TYPES:
                continue
            if not _looks_like_an_animal_photograph(page.get("title", "")):
                continue
            thumbnail_url = image_info.get("thumburl")
            if thumbnail_url:
                urls.append(thumbnail_url)

        return urls

    def _get_commons_json(
        self, params: dict[str, str | int]
    ) -> Any | None:  # noqa: ANN401 - provider-specific JSON, see Returns
        """GET the Commons API, pacing calls and honouring its rate limit.

        Commons allows anonymous callers a short burst and then answers 429
        until the window rolls over. A 429 treated as a failure would silently
        leave a whole kind of animal without photographs, so it is waited out
        instead.

        Args:
            params: Query-string parameters for the MediaWiki API.

        Returns:
            The decoded body, or None if the request failed or stayed
            rate-limited. Untyped because the MediaWiki response shape varies
            with the query; the caller picks the fields it needs.
        """
        for _ in range(COMMONS_ATTEMPTS_WHEN_RATE_LIMITED):
            self._pace_commons_calls()
            try:
                response = self._session.get(
                    COMMONS_API, params=params, timeout=REQUEST_TIMEOUT_SECONDS
                )
            except requests.RequestException:
                return None

            if response.status_code == HTTP_TOO_MANY_REQUESTS:
                time.sleep(_retry_wait_seconds(response))
                continue
            if response.status_code != HTTP_OK:
                return None
            try:
                return response.json()
            except ValueError:
                return None
        return None

    def _pace_commons_calls(self) -> None:
        """Leave a gap between Commons API calls to stay inside its limit."""
        elapsed = time.monotonic() - self._last_commons_call_at
        if elapsed < COMMONS_SECONDS_BETWEEN_CALLS:
            time.sleep(COMMONS_SECONDS_BETWEEN_CALLS - elapsed)
        self._last_commons_call_at = time.monotonic()

    # ----------------------------------------------------------------------
    # Shared HTTP plumbing
    # ----------------------------------------------------------------------

    def _get_json(
        self, url: str, params: dict[str, str | int] | None = None
    ) -> Any | None:  # noqa: ANN401 - provider-specific JSON, see Returns
        """GET a JSON endpoint, returning None on any failure.

        Args:
            url: The endpoint to fetch.
            params: Query-string parameters.

        Returns:
            The decoded body, or None if the request or the decode failed.
            Untyped because each photo provider answers with a different
            shape; the callers pick the fields they need.
        """
        try:
            response = self._session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException:
            return None
        if response.status_code != HTTP_OK:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def _download(self, url: str, destination: Path) -> Path | None:
        """Write an image URL to disk, rejecting the wrong sort of file.

        Args:
            url: The image to fetch.
            destination: The intended path. Its extension is replaced by one
                matching the served content type, so the file on disk and the
                name it is served under always agree.

        Returns:
            The path written, or None when the response was not a usable
            photograph.
        """
        try:
            response = self._session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException:
            return None

        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        extension = EXTENSION_BY_CONTENT_TYPE.get(content_type)
        if response.status_code != HTTP_OK or extension is None:
            return None
        if len(response.content) < MINIMUM_IMAGE_BYTES:
            return None

        written = destination.with_suffix(extension)
        written.write_bytes(response.content)
        return written


def _categories_for(species: Species, breed_name: str) -> tuple[str, ...]:
    """Order the Commons categories to try for one kind of animal.

    Most specific first: the breed's own category, then the species-wide ones,
    then a generic pet category. Duplicates are removed so a category shared
    between two levels is not fetched twice.

    Args:
        species: The animal's species.
        breed_name: The breed as written in the roster, or an empty string.

    Returns:
        Category titles in the order they should be tried.
    """
    ordered: list[str] = [
        *COMMONS_CATEGORIES_BY_BREED.get(breed_name, ()),
        *COMMONS_CATEGORIES_BY_SPECIES.get(species, ()),
        *GENERIC_PET_CATEGORIES,
    ]
    return tuple(dict.fromkeys(ordered))
