import json
import pickle
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://freedictionaryapi.com/api/v1/entries/en"
THESAURUS_URL = "https://api.api-ninjas.com/v1/thesaurus"
URBAN_URL = "https://unofficialurbandictionaryapi.com/api/search"


class Dictionary:
    def __init__(self) -> None:
        self.dict_cache: dict[str, str] = {}
        self.thesaurus_cache: dict[str, tuple[list[str], list[str]]] = {}
        self.urban_cache: dict[tuple[str, bool, int], list[dict[str, str]]] = {}

    def define(self, word: str) -> str | None:
        word = word.strip().casefold()

        if word in self.dict_cache:
            return self.dict_cache[word]

        definition = define(word)

        if definition is not None:
            self.dict_cache[word] = definition

        self.write_cache()

        return definition

    def thesaurus(
        self,
        word: str,
        api_key: str,
        debug: bool = False,
    ) -> tuple[list[str], list[str]] | None:
        word = word.strip().casefold()

        if word in self.thesaurus_cache:
            return self.thesaurus_cache[word]

        result = thesaurus(word, api_key, debug=debug)

        if result is not None:
            self.thesaurus_cache[word] = result

        self.write_cache()

        return result

    def urban_define(
        self,
        word: str,
        strict: bool = True,
        limit: int = 5,
        debug: bool = False,
    ) -> list[dict[str, str]] | None:
        """Return cached Urban Dictionary entries, including meanings and examples."""
        word = word.strip()
        key = (word.casefold(), strict, limit)

        if key in self.urban_cache:
            return self.urban_cache[key]

        result = urban_define(word, strict=strict, limit=limit, debug=debug)

        if result is not None:
            self.urban_cache[key] = result
            self.write_cache()

        return result

    def write_cache(self) -> None:
        with open("dict_cache.pkl", "wb") as f:
            pickle.dump(self.dict_cache, f)

        with open("thesaurus_cache.pkl", "wb") as f:
            pickle.dump(self.thesaurus_cache, f)

        with open("urban_cache.pkl", "wb") as f:
            pickle.dump(self.urban_cache, f)

    def read_cache(self) -> None:

        try:
            with open("dict_cache.pkl", "rb") as f:
                self.dict_cache = pickle.load(f)
        except FileNotFoundError:
            self.dict_cache = {}

        try:
            with open("thesaurus_cache.pkl", "rb") as f:
                self.thesaurus_cache = pickle.load(f)
        except FileNotFoundError:
            self.thesaurus_cache = {}

        try:
            with open("urban_cache.pkl", "rb") as f:
                self.urban_cache = pickle.load(f)
        except FileNotFoundError:
            self.urban_cache = {}


def define(word: str) -> str | None:
    """Return the first definition found for a word."""
    url = f"{BASE_URL}/{quote(word.strip())}"

    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=5) as response:
            data = json.load(response)
    except HTTPError as e:
        print(f"HTTP error: {e.code} {e.reason}")
        return None
    except (URLError, TimeoutError) as e:
        print(f"Request error: {e}")
        return None

    for entry in data.get("entries", []):
        for sense in entry.get("senses", []):
            if definition := sense.get("definition"):
                return definition

    return None


def thesaurus(word: str, api_key: str, debug=False) -> tuple[list[str], list[str]] | None:
    """Return (synonyms, antonyms) for a word."""
    url = f"{THESAURUS_URL}?word={quote(word.strip())}"

    request = Request(
        url,
        headers={
            "X-Api-Key": api_key,
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=5) as response:
            data = json.load(response)
    except (HTTPError, URLError, TimeoutError) as e:
        if debug:
            print(f"Request failed for word: {word}, Error: {e}, Response: {getattr(e, 'read', lambda: None)()}")
        return None

    return (
        data.get("synonyms", []),
        data.get("antonyms", []),
    )


def urban_define(
    word: str,
    strict: bool = True,
    limit: int = 5,
    debug: bool = False,
) -> list[dict[str, str]] | None:
    """Return Urban Dictionary entries, or [] if not found; None on request errors.

    Each entry has: word, meaning, example, contributor, and date.
    """
    word = word.strip()
    if not word:
        return []
    if limit < 1:
        raise ValueError("limit must be a positive integer")

    query = urlencode({
        "term": word,
        "strict": str(strict).lower(),
        "limit": limit,
    })
    request = Request(
        f"{URBAN_URL}?{query}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=5) as response:
            data = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        if debug:
            print(f"Urban Dictionary request failed for {word!r}: {e}")
        return None

    if not isinstance(data, dict) or data.get("statusCode") != 200:
        if debug:
            print(f"Unexpected Urban Dictionary response for {word!r}: {data!r}")
        return None

    if not data.get("found", False):
        return []

    entries = data.get("data", [])
    if not isinstance(entries, list):
        return None

    return [
        {
            "word": str(entry.get("word", "")).strip(),
            "meaning": str(entry.get("meaning", "")).strip(),
            "example": str(entry.get("example", "")).strip(),
            "contributor": str(entry.get("contributor", "")).strip(),
            "date": str(entry.get("date", "")).strip(),
        }
        for entry in entries
        if isinstance(entry, dict)
    ]


if __name__ == "__main__":
    while True:
        word = input("Enter a word to define: ").strip()
        # definition = define(word)

        # if definition:
        #     print(f"Definition of {word!r}: {definition}")
        # else:
        #     print(f"No definition found for {word!r}.")

        synonyms_antonyms = thesaurus(word, "REDACTED")
        if synonyms_antonyms:
            synonyms, antonyms = synonyms_antonyms
            print(
                f"Synonyms of {word!r}: {', '.join(synonyms) if synonyms else 'None'}\n"
                f"Antonyms of {word!r}: {', '.join(antonyms) if antonyms else 'None'}"
            )
        else:
            print(f"No thesaurus entry found for {word!r}.")
