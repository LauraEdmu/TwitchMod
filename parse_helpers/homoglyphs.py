import string
import unicodedata
from functools import cache

from confusable_homoglyphs import confusables

ZERO_WIDTH = {
    "\u200b",  # zero width space
    "\u200c",  # zero width non-joiner
    "\u200d",  # zero width joiner
    "\ufeff",  # zero width no-break space / BOM
}

ASCII_TARGETS = set(string.ascii_lowercase + string.digits)

# Extra folds for common "small caps" / modifier-letter spam that NFKC often
# does not reduce to plain ASCII.
EXTRA_HOMOGLYPH_MAP = str.maketrans({
    "ʙ": "b",
    "ᴄ": "c",
    "ᴅ": "d",
    "ᴇ": "e",
    "ғ": "f",
    "ɢ": "g",
    "ʜ": "h",
    "ɪ": "i",
    "ᴊ": "j",
    "ᴋ": "k",
    "ʟ": "l",
    "ᴍ": "m",
    "ɴ": "n",
    "ᴏ": "o",
    "ᴘ": "p",
    "ʀ": "r",
    "ᴛ": "t",
    "ᴜ": "u",
    "ᴠ": "v",
    "ᴡ": "w",
    "ʏ": "y",
    "ᴢ": "z",
})


def basic_normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch not in ZERO_WIDTH)
    return text.casefold()


def suspicious_unicode(text: str) -> bool:
    norm = basic_normalise(text)
    return confusables.is_dangerous(norm, preferred_aliases=["latin"])


def strip_combining_marks(text: str) -> str:
    """
    Turns things like:
        he̷l̸l̵o -> hello
        café      -> cafe

    This is useful for moderation matching, but destructive for real text.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        ch for ch in text
        if not unicodedata.category(ch).startswith("M")
    )
    return unicodedata.normalize("NFKC", text)


@cache
def _confusable_char_to_ascii(ch: str) -> str:
    """
    Try to map one non-ASCII character to a simple ASCII lookalike.

    This deliberately only maps to single ASCII letters/digits. That keeps it
    much less chaotic than blindly accepting every Unicode confusable mapping.
    """
    if ch.isascii():
        return ch

    # Manual high-value mappings first.
    mapped = ch.translate(EXTRA_HOMOGLYPH_MAP)
    if mapped != ch:
        return mapped

    # confusable_homoglyphs exposes Unicode confusable data here.
    for candidate in confusables.confusables_data.get(ch, []):
        replacement = candidate.get("c", "")
        replacement = unicodedata.normalize("NFKC", replacement).casefold()

        if len(replacement) == 1 and replacement in ASCII_TARGETS:
            return replacement

    return ch


def fold_confusables_to_ascii(text: str) -> str:
    return "".join(_confusable_char_to_ascii(ch) for ch in text)


def advanced_normalise(text: str) -> str:
    """
    Aggressive moderation-oriented normalisation.

    Intended for banned-word/spam matching, not for displaying user text.
    """
    text = basic_normalise(text)
    text = strip_combining_marks(text)
    text = text.translate(EXTRA_HOMOGLYPH_MAP)
    text = fold_confusables_to_ascii(text)
    text = unicodedata.normalize("NFKC", text)
    return text.casefold()


if __name__ == "__main__":
    test_string = (
        "This is a test string with some homoglyphs: "
        "𝖍𝖊𝖑𝖑𝖔, "
        "𝖜𝖔𝖗𝖑𝖉! "
        "𝐇𝐞𝐥𝐥𝐨, "
        "𝐰𝐨𝐫𝐥𝐝! "
        "𝗛𝗲𝗹𝗹𝗼, "
        "𝗪𝗼𝗿𝗹𝗱! "
        "𝘏𝘦𝘭𝘭𝘰, "
        "𝘞𝘰𝘳𝘭𝘥! "
        "ʜᴇʟʟᴏ, "
        "ᴡᴏʀʟᴅ! "
        "ѕtrеambоо"
        "Ai viewers streamboo. Com"
    )

    print(f"Original:   {test_string}")
    print(f"Basic:      {basic_normalise(test_string)}")
    print(f"Advanced:   {advanced_normalise(test_string)}")
    print(f"Suspicious: {suspicious_unicode(test_string)}")
