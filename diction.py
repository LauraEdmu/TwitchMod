import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_URL = "https://freedictionaryapi.com/api/v1/entries/en"
THESAURUS_URL = "https://api.api-ninjas.com/v1/thesaurus"


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


def thesaurus(word: str, api_key: str) -> tuple[list[str], list[str]] | None:
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
    except (HTTPError, URLError, TimeoutError):
        return None

    return (
        data.get("synonyms", []),
        data.get("antonyms", []),
    )


if __name__ == "__main__":
    while True:
        word = input("Enter a word to define: ").strip()
        # definition = define(word)

        # if definition:
        #     print(f"Definition of {word!r}: {definition}")
        # else:
        #     print(f"No definition found for {word!r}.")

        synonyms_antonyms = thesaurus(word, "sZaGWiD5pUtrxxb4B13aaX09imxkpnanJb40cbtw")
        if synonyms_antonyms:
            synonyms, antonyms = synonyms_antonyms
            print(
                f"Synonyms of {word!r}: {', '.join(synonyms) if synonyms else 'None'}\n"
                f"Antonyms of {word!r}: {', '.join(antonyms) if antonyms else 'None'}"
            )
        else:
            print(f"No thesaurus entry found for {word!r}.")
