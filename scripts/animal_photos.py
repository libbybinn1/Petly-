"""Fetch representative photographs for seeded animals.

Image quality matters here: spec 24 makes photographs a required part of the
experience, not decoration. Generic keyword image services return loosely
tagged pictures - a search for "german shepherd" can return a car park - so
each species uses a source that actually knows what it is serving:

    dogs   dog.ceo, which is organised by breed
    cats   TheCatAPI, a curated cat photo collection
    others Wikimedia Commons search, restricted to photographs

All three are free and need no API key.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import requests
from app.domain.enums import Species

REQUEST_TIMEOUT_SECONDS = 25
USER_AGENT = "PetMatch/0.1 (student project; contact via repository)"

DOG_API_BREEDS = "https://dog.ceo/api/breed/{breed_path}/images/random/{count}"
DOG_API_ANY = "https://dog.ceo/api/breeds/image/random/{count}"
CAT_API = "https://api.thecatapi.com/v1/images/search"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Our breed names mapped onto dog.ceo's taxonomy, verified against its
# /breeds/list/all endpoint. Anything absent falls back to a random dog.
DOG_BREED_PATHS = {
    "Border Collie": "collie/border",
    "Labrador Retriever": "labrador",
    "Jack Russell Terrier": "terrier/russell",
    "German Shepherd": "german/shepherd",
    "Beagle": "beagle",
    "Siberian Husky": "husky",
    "Cavalier King Charles Spaniel": "spaniel/blenheim",
    "Boxer": "boxer",
    "Greyhound": "greyhound/italian",
    "Poodle": "poodle/standard",
    "Australian Shepherd": "australian/shepherd",
    "Bulldog": "bulldog/english",
    "Alaskan Malamute": "malamute",
    "Collie Mix": "collie/border",
    "Mixed Breed": "mix",
}

# Commons *categories*, not free-text search. Search relevance on Commons is
# poor for this purpose - a query for "domestic rabbit pet" returned a 1930s
# photograph of a cat on a radio. Categories are curated by species, so they
# return the right animal essentially every time.
COMMONS_CATEGORIES = {
    Species.RABBIT: [
        "Category:Oryctolagus cuniculus domesticus",
        "Category:Rabbits",
        "Category:Lop rabbits",
    ],
    Species.HAMSTER: [
        "Category:Mesocricetus auratus",
        "Category:Phodopus",
    ],
    Species.GUINEA_PIG: [
        "Category:Cavia porcellus",
    ],
    Species.BIRD: [
        "Category:Nymphicus hollandicus",
        "Category:Melopsittacus undulatus",
        "Category:Psittacus erithacus",
    ],
    Species.OTHER: [
        "Category:Pets",
    ],
}

# Commons categories also hold range maps, diagrams, engravings and museum
# specimens. None of those belong on an adoption listing.
NON_PHOTOGRAPH_TITLE_MARKERS = (
    "range", "map", "diagram", "chart", "distribution", "engraving", "drawing",
    "illustration", "lithograph", "plate", "skeleton", "skull", "anatomy",
    "taxidermy", "specimen", "logo", "icon", "sign", "stamp", "coat of arms",
)

ACCEPTED_MIME_TYPES = ("image/jpeg", "image/png")

MINIMUM_IMAGE_BYTES = 4000
HTTP_OK = 200
DOWNLOAD_ATTEMPTS_PER_ANIMAL = 4


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


@dataclass
class PhotoFetcher:
    """Downloads one photograph per animal, caching candidate URLs per species.

    Candidate lists are fetched once and then drawn from, so seeding forty
    animals does not make forty separate search calls.
    """

    randomizer: random.Random
    _dog_urls: dict[str, list[str]] | None = None
    _cat_urls: list[str] | None = None
    _commons_urls: dict[Species, list[str]] | None = None

    def __post_init__(self) -> None:
        """Initialise the per-species caches."""
        self._dog_urls = {}
        self._cat_urls = []
        self._commons_urls = {}
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})

    def fetch_into(self, species: Species, breed: str | None, destination: Path) -> bool:
        """Download a photograph for one animal.

        Args:
            species: Determines which source is used.
            breed: Used for breed-accurate dog photographs when recognised.
            destination: Where to write the image file.

        These are free public APIs and individual images fail intermittently:
        a dead link, a redirect, an occasional 5xx. Without a retry one bad
        candidate leaves an animal with no picture, so each animal gets a few
        attempts before giving up.

        Returns:
            True when an image was written. False is not fatal - the interface
            falls back to a styled placeholder.
        """
        for _ in range(DOWNLOAD_ATTEMPTS_PER_ANIMAL):
            url = self._choose_url(species, breed)
            if url is None:
                return False
            if self._download(url, destination):
                return True
        return False

    def _choose_url(self, species: Species, breed: str | None) -> str | None:
        """Pick a candidate image URL for a species."""
        if species is Species.DOG:
            return self._next_dog_url(breed)
        if species is Species.CAT:
            return self._next_cat_url()
        return self._next_commons_url(species)

    def _next_dog_url(self, breed: str | None) -> str | None:
        """Return a dog photograph, breed-accurate when the breed is known."""
        breed_path = DOG_BREED_PATHS.get(breed or "", "")
        cache_key = breed_path or "__any__"

        assert self._dog_urls is not None
        if not self._dog_urls.get(cache_key):
            endpoint = (
                DOG_API_BREEDS.format(breed_path=breed_path, count=6)
                if breed_path
                else DOG_API_ANY.format(count=6)
            )
            payload = self._get_json(endpoint)
            urls = payload.get("message", []) if payload else []
            self._dog_urls[cache_key] = list(urls) if isinstance(urls, list) else []

        pool = self._dog_urls.get(cache_key) or []
        return pool.pop() if pool else None

    def _next_cat_url(self) -> str | None:
        """Return a cat photograph."""
        assert self._cat_urls is not None
        if not self._cat_urls:
            payload = self._get_json(CAT_API, params={"limit": 10})
            if isinstance(payload, list):
                self._cat_urls = [item["url"] for item in payload if item.get("url")]

        return self._cat_urls.pop() if self._cat_urls else None

    def _next_commons_url(self, species: Species) -> str | None:
        """Return a photograph from Wikimedia Commons for the smaller species.

        Draws from curated species categories rather than free-text search,
        and discards the maps, diagrams and engravings those categories also
        contain.
        """
        assert self._commons_urls is not None
        if not self._commons_urls.get(species):
            collected: list[str] = []
            for category in COMMONS_CATEGORIES[species]:
                collected.extend(self._photographs_in_category(category))
            self.randomizer.shuffle(collected)
            self._commons_urls[species] = collected

        pool = self._commons_urls.get(species) or []
        return pool.pop() if pool else None

    def _photographs_in_category(self, category: str) -> list[str]:
        """List usable photograph URLs from one Commons category."""
        payload = self._get_json(
            COMMONS_API,
            params={
                "action": "query",
                "format": "json",
                "generator": "categorymembers",
                "gcmtitle": category,
                "gcmtype": "file",
                "gcmlimit": 30,
                "prop": "imageinfo",
                "iiprop": "url|mime",
                "iiurlwidth": 900,
            },
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

    def _get_json(self, url: str, params: dict[str, object] | None = None):  # noqa: ANN202
        """GET a JSON endpoint, returning None on any failure."""
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

    def _download(self, url: str, destination: Path) -> bool:
        """Write an image URL to disk, rejecting non-images and tiny files."""
        try:
            response = self._session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException:
            return False

        is_image = response.headers.get("content-type", "").startswith("image/")
        if response.status_code != HTTP_OK or not is_image:
            return False
        if len(response.content) < MINIMUM_IMAGE_BYTES:
            return False

        destination.write_bytes(response.content)
        return True
