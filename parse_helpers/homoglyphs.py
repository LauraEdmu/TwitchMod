import string
import unicodedata
from functools import cache
from typing import Callable
import re

from confusable_homoglyphs import confusables

ZERO_WIDTH_CHARS = (
    "\u200b",  # zero width space
    "\u200c",  # zero width non-joiner
    "\u200d",  # zero width joiner
    "\ufeff",  # zero width no-break space / BOM
    "\u2060",  # word joiner
)

ZERO_WIDTH = set(ZERO_WIDTH_CHARS)


def remove_zero_width(text: str) -> str:
    if not any(ch in text for ch in ZERO_WIDTH_CHARS):
        return text

    return "".join(ch for ch in text if ch not in ZERO_WIDTH)

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
    "🅦": "w",
    "🅞": "o",
    "🅡": "r",
    "🅛": "l",
    "🅓": "d",
    "ᕼ": "h",
    "ᗴ": "e",
    "ᒪ": "l",
    "ᗯ": "w",
    "ᖇ": "r",
    "ᗪ": "d",
    "Ь": "b",
    "ь": "b",
    "Η": "h",
    "η": "h",
    "Ε": "e",
    "ε": "e",
    "Ο": "o",
    "ο": "o",
})

PARENTHESISED_LETTER_RE = re.compile(r"\(([a-z])\)")

def basic_normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = remove_zero_width(text)
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


def advanced_normalise(text: str, remove_parenthesised: bool = True) -> str:
    """
    Aggressive moderation-oriented normalisation.

    Intended for banned-word/spam matching, not for displaying user text.
    """
    text = basic_normalise(text)

    if text.isascii():
        if remove_parenthesised and "(" in text and ")" in text:
            text = PARENTHESISED_LETTER_RE.sub(r"\1", text)
        return text

    text = strip_combining_marks(text)
    text = text.translate(EXTRA_HOMOGLYPH_MAP)
    text = fold_confusables_to_ascii(text)
    text = unicodedata.normalize("NFKC", text)

    if remove_parenthesised:
        # NFKC turns parenthesised letters like "⒜" into "(a)".
        # For moderation matching, collapse those back to plain letters.
        text = PARENTHESISED_LETTER_RE.sub(r"\1", text)

    return text.casefold()


def run_cases(cases: dict[str, str], normaliser: Callable[[str], str]) -> None:
    num_of_cases = len(cases)
    print(f"Running {num_of_cases} test cases for {normaliser.__name__}...")
    successes = 0
    failures = 0
    for test_string, expected in cases.items():
        result = normaliser(test_string)
        if result != expected:
            failures += 1
            print(f"Test failed for input: {test_string}")
            print(f"Expected: {expected}, Got: {result}")
            print("-" * 40)
        else:
            successes += 1
    
    percentage = (successes / num_of_cases * 100) if num_of_cases > 0 else 0
    print(f"\nResults: {successes} passed, {failures} failed ({percentage:.1f}% success rate)")

if __name__ == "__main__":
    test_cases = ({
        # Mathematical / styled Latin
        "𝓱𝓮𝓵𝓵𝓸": "hello",
        "𝔥𝔢𝔩𝔩𝔬": "hello",
        "𝕙𝕖𝕝𝕝𝕠": "hello",
        "𝙝𝙚𝙡𝙡𝙤": "hello",
        "𝚑𝚎𝚕𝚕𝚘": "hello",
        "𝑯𝒆𝒍𝒍𝒐": "hello",
        "𝑤𝑜𝑟𝑙𝑑": "world",
        "𝒘𝒐𝒓𝒍𝒅!": "world!",
        "𝓦𝓸𝓻𝓵𝓭!": "world!",
        "𝔚𝔬𝔯𝔩𝔡!": "world!",
        "𝕎𝕠𝕣𝕝𝕕!": "world!",
        "𝙒𝙤𝙧𝙡𝙙!": "world!",

        # Full-width ASCII
        "Ｈｅｌｌｏ": "hello",
        "Ｗｏｒｌｄ！": "world!",
        "ｓｔｒｅａｍｂｏｏ．ｃｏｍ": "streamboo.com",
        "Ａｉ　ｖｉｅｗｅｒｓ": "ai viewers",

        # Circled / enclosed / parenthesised letters
        "ⓗⓔⓛⓛⓞ": "hello",
        "ⓦⓞⓡⓛⓓ": "world",
        "Ⓗⓔⓛⓛⓞ": "hello",
        "⒜Ⓘ ⓥⓘⓔⓦⓔⓡⓢ": "ai viewers",
        "🄷🄴🄻🄻🄾": "hello",
        "🅦🅞🅡🅛🅓": "world",

        # Modifier / small-cap-ish letters
        "ʰᵉˡˡᵒ": "hello",
        "ʷᵒʳˡᵈ": "world",
        "ᕼᗴᒪᒪO": "hello",
        "ᗯOᖇᒪᗪ": "world",

        # Common Cyrillic homoglyphs
        "hеllo": "hello",          # Cyrillic е
        "hellо": "hello",          # Cyrillic о
        "һеllо": "hello",          # Cyrillic һ + о
        "wоrld": "world",          # Cyrillic о
        "ѕtreamboo": "streamboo",  # Cyrillic ѕ
        "strеamЬоо": "streamboo",  # Cyrillic е, Ь, оо
        "ѕtrеаmbоо": "streamboo",  # Cyrillic ѕ, е, а, оо
        "streambоо.соm": "streamboo.com",
        "ѕtrеаmbоо.соm": "streamboo.com",

        # Common Greek homoglyphs
        "Ηello": "hello",          # Greek capital eta
        "heⅼⅼo": "hello",          # Roman numeral small fifty chars
        "wοrld": "world",          # Greek omicron
        "strεambοο": "streamboo",  # Greek epsilon + omicrons
        "ΑΙ viewers": "ai viewers", # Greek Alpha + Iota
        "Αi viеwеrs": "ai viewers",

        # Mixed scripts and punctuation/casing
        "Ａі ѵіеԝеrѕ ѕtrеаmbоо.соm": "ai viewers streamboo.com",
        "Ai Ⅴiеwеrѕ StreamЬоо.Com": "ai viewers streamboo.com",
        "𝘼𝙞 𝙫𝙞𝙚𝙬𝙚𝙧𝙨 ѕtrеаmbоо.соm": "ai viewers streamboo.com",
        "ʜᴇʟʟᴏ, ᴡᴏʀʟᴅ!": "hello, world!",
        "𝖍𝖊𝖑𝖑𝖔, 𝐰𝐨𝐫𝐥𝐝!": "hello, world!",

        # Zero-width / invisible characters
        "he\u200bllo": "hello",
        "wor\u200cld": "world",
        "stream\u200bboo.com": "streamboo.com",
        "ai\u2060 viewers": "ai viewers",
    })

    # test_cases.update({
    #     # Numbers that commonly appear in leetspeak-style normalisation
    #     # Include these only if your module intentionally maps digits to letters.
    #     "h3llo": "hello",
    #     "w0rld": "world",
    #     "5treamb00": "streamboo",
    #     "a1 viewers": "ai viewers",
    # })

    run_cases(test_cases, advanced_normalise)

    # interactive test
    while True:
        user_input = input("Enter a string to test (or 'exit' to quit): ")
        if user_input.lower() == "exit":
            break
        print(f"Original:   {user_input}")
        print(f"Basic:      {basic_normalise(user_input)}")
        print(f"Advanced:   {advanced_normalise(user_input)}")
        print(f"Suspicious: {suspicious_unicode(user_input)}")