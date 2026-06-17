import re

LINK_RE = re.compile(
    r"""(?ix)
    (?<![a-z0-9_@./:-])
    (?<!https://\s)
    (?<!http://\s)
    (?<!www\.\s)
    (?:
        https?://[^\s<>"']+
        |
        www\.[^\s<>"']+
        |
        (?:
            [a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.
        )+
        (?:
            [a-z](?:[a-z0-9-]{0,61}[a-z0-9])
            |
            xn--[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])
        )
        (?::\d{1,5})?
        (?:/[^\s<>"']*)?
    )
    """
)
TWITCH_LINK_RE = re.compile(
    r"""(?ix)
    (?<![a-z0-9_@./:-])
    (?<!https://\s)
    (?<!http://\s)
    (?<!www\.\s)

    (?:https?://)?
    (?:www\.)?

    (?:
        twitch\.tv/
        (?:
            videos/\d+
            |
            (?!videos(?:/|$))
            [a-z0-9_]{3,25}
            (?:/clip/[a-z0-9_-]+)?
        )
        |
        clips\.twitch\.tv/[a-z0-9_-]+
    )

    (?:[?#][^\s<>"']*)?

    (?=
        $
        |
        [\s<>"')\]},!?;:]
        |
        \.(?:$|\s)
    )
    """
)

TRAILING_LINK_PUNCT = ".,!?;:)]}>"

def clean_detected_link(link: str) -> str:
    """
    Regex link detection often includes sentence punctuation:
        "https://example.com."
    For classification, strip common trailing punctuation.
    """
    return link.rstrip(TRAILING_LINK_PUNCT)


def iter_links(text: str):
    for match in LINK_RE.finditer(text):
        yield clean_detected_link(match.group(0))


def is_link(text: str) -> bool:
    return bool(LINK_RE.search(text))


def contains_twitch_link(text: str) -> bool:
    return any(TWITCH_LINK_RE.fullmatch(link) for link in iter_links(text))


def contains_non_twitch_link(text: str) -> bool:
    """
    True if the message contains at least one link-shaped thing
    that is not an allowed Twitch link.
    """
    return any(
        not TWITCH_LINK_RE.fullmatch(link)
        for link in iter_links(text)
    )


def only_twitch_links(text: str, *, require_link: bool = False) -> bool:
    """
    True if all links in the message are Twitch links.

    By default, messages with no links return True.
    Set require_link=True if you want "no links" to return False.
    """
    links = list(iter_links(text))

    if require_link and not links:
        return False

    return all(TWITCH_LINK_RE.fullmatch(link) for link in links)

def run_cases(name, regex, cases):
    print(f"\n{name}")
    failed = 0

    for text, expected in cases:
        got = bool(regex.search(text))

        if got != expected:
            failed += 1
            print(f"FAIL: {text!r}")
            print(f"  expected: {expected}")
            print(f"  got:      {got}")
            m = regex.search(text)
            if m:
                print(f"  matched:  {m.group(0)!r}")

    if failed == 0:
        print("All passed")
    else:
        print(f"{failed} failed")


if __name__ == "__main__":
    LINK_CASES = [
        # Should proc: normal links
        ("example.com", True),
        ("www.example.com", True),
        ("http://example.com", True),
        ("https://example.com", True),
        ("https://www.example.com", True),
        ("sub.example.com", True),
        ("a.b.co.uk", True),
        ("my-server.laura.to", True),
        ("mc01.laura.to", True),
        ("example.io/path", True),
        ("example.dev/path/to/page", True),
        ("example.com:8080", True),
        ("example.com:8080/path", True),
        ("https://example.com:443/path?q=yes", True),
        ("https://example.com/path?q=yes&x=1", True),
        ("https://example.com/path#section", True),
        ("https://example.com/a-b_c~d", True),
        ("go to example.com please", True),
        ("link: example.com/test", True),
        ("(example.com)", True),
        ("example.com.", True),
        ("example.com,", True),
        ("https://example.com/some/path.", True),
        ("www.youtube.com/watch?v=dQw4w9WgXcQ", True),
        ("discord.gg/abcdef", True),
        ("github.com/LauraEdmu/repo", True),
        ("laura.to", True),
        ("status.laura.to", True),
        ("orange.laura.to", True),
        ("xn--d1acufc.xn--p1ai", True),
        ("example.technology", True),
        ("localhost.localdomain", True),
        ("abc.def", True),

        # Should proc: permissive / weird but link-shaped
        ("http://localhost:8000", True),
        ("https://192.168.1.1", True),
        ("http://10.0.0.1:8080/admin", True),
        ("https://example", True),          # because scheme form is permissive
        ("http://x", True),                 # same
        ("www.x", True),                    # because www form is permissive
        ("https://例え.テスト", True),       # scheme branch allows non-space chars
        ("https://example.com/日本語", True),

        # Should not proc
        ("hello world", False),
        ("just some text", False),
        ("example", False),
        ("localhost", False),
        ("com", False),
        ("dot com", False),
        ("example dot com", False),
        ("example .com", False),
        ("example. com", False),
        ("example . com", False),
        ("streamboo. com", False),
        ("http ://example.com", False),
        ("https: //example.com", False),
        ("www .example.com", False),
        ("www. example.com", False),
        ("foo@bar", False),
        ("user@example", False),
        ("not_a_domain", False),
        ("example..com", False),
        (".example.com", False),
        ("example.com-", True),             # catches example.com part
        ("-example.com", False),             # catches xample.com part, due permissive search
        ("exa_mple.com", False),             # catches mple.com part
        ("example.c", False),               # TLD branch requires 2+ chars
        ("example.123", False),             # TLD branch requires letters
        ("192.168.1.1", False),             # no numeric TLD in bare-domain branch
        ("http://", False),
        ("https://", False),
        ("www.", False),
        ("https:// example.com", False),
        ("<https://example.com>", True),
    ]
    TWITCH_CASES = [
        # Should proc: channel links
        ("twitch.tv/laurasmaur", True),
        ("https://twitch.tv/laurasmaur", True),
        ("http://twitch.tv/laurasmaur", True),
        ("www.twitch.tv/laurasmaur", True),
        ("https://www.twitch.tv/laurasmaur", True),
        ("go watch twitch.tv/laurasmaur", True),
        ("(twitch.tv/laurasmaur)", True),
        ("twitch.tv/laurasmaur.", True),
        ("twitch.tv/laurasmaur,", True),
        ("twitch.tv/laura_smaur", True),
        ("twitch.tv/laura123", True),
        ("twitch.tv/abc", True),
        ("twitch.tv/abcdefghijklmnopqrstuvwxy", True),  # 25 chars

        # Should proc: VODs
        ("twitch.tv/videos/1234567890", True),
        ("https://twitch.tv/videos/1234567890", True),
        ("www.twitch.tv/videos/1234567890", True),
        ("twitch.tv/videos/1", True),
        ("twitch.tv/videos/1234567890?t=1h2m3s", True),
        ("https://www.twitch.tv/videos/1234567890?filter=archives&sort=time", True),

        # Should proc: channel clip URLs
        ("twitch.tv/laurasmaur/clip/FunnyClipSlug", True),
        ("https://twitch.tv/laurasmaur/clip/FunnyClipSlug", True),
        ("www.twitch.tv/laurasmaur/clip/FunnyClipSlug", True),
        ("twitch.tv/laurasmaur/clip/funny-clip_slug", True),
        ("twitch.tv/laurasmaur/clip/FunnyClipSlug?filter=clips&range=7d", True),

        # Should proc: old/global clip URLs
        ("clips.twitch.tv/FunnyClipSlug", True),
        ("https://clips.twitch.tv/FunnyClipSlug", True),
        ("http://clips.twitch.tv/FunnyClipSlug", True),
        ("www.clips.twitch.tv/FunnyClipSlug", True),
        ("https://clips.twitch.tv/FunnyClipSlug?filter=clips&range=7d", True),
        ("clips.twitch.tv/funny-clip_slug", True),

        # Should proc: because regex is case-insensitive
        ("TWITCH.TV/laurasmaur", True),
        ("Https://Twitch.TV/laurasmaur", True),
        ("CLIPS.TWITCH.TV/FunnyClipSlug", True),

        # Should not proc: not Twitch
        ("youtube.com/watch?v=abc", False),
        ("kick.com/laurasmaur", False),
        ("discord.gg/abcdef", False),
        ("example.com/twitch.tv/laurasmaur", False),  # may depend on regex boundary/search details
        ("notwitch.tv/laurasmaur", False),
        ("twitch.com/laurasmaur", False),
        ("twitch.tv", False),
        ("twitch.tv/", False),
        ("https://twitch.tv", False),
        ("https://twitch.tv/", False),
        ("www.twitch.tv", False),
        ("clips.twitch.tv", False),
        ("clips.twitch.tv/", False),

        # Should not proc: malformed spacing
        ("twitch .tv/laurasmaur", False),
        ("twitch. tv/laurasmaur", False),
        ("twitch.tv /laurasmaur", False),
        ("twitch.tv/ laurasmaur", False),
        ("https:// twitch.tv/laurasmaur", False),
        ("https ://twitch.tv/laurasmaur", False),
        ("www .twitch.tv/laurasmaur", False),

        # Should not proc: invalid/too-short channels
        ("twitch.tv/a", False),
        ("twitch.tv/ab", False),
        ("twitch.tv/abcdefghijklmnopqrstuvwxyz", False),  # 26 chars
        ("twitch.tv/laura-sm", False),                    # hyphen not valid in Twitch username
        ("twitch.tv/laura.smaur", False),                 # dot not valid in Twitch username

        # Should not proc: malformed VODs/clips
        ("twitch.tv/videos/", False),
        ("twitch.tv/videos/abc", False),
        ("twitch.tv/videos/123abc", False),    # catches twitch.tv/videos/123 part
        ("twitch.tv/laurasmaur/clip/", False), # catches twitch.tv/laurasmaur part
        ("clips.twitch.tv/", False),
        ("clips.twitch.tv/!!!", False),
        ("clips.twitch.tv/Funny Clip Slug", True),  # catches clips.twitch.tv/Funny only
    ]

    # run_cases("LINK_RE", LINK_RE, LINK_CASES)
    # run_cases("TWITCH_LINK_RE", TWITCH_LINK_RE, TWITCH_CASES)
    
    # interactive test:
    # while True:
    #     text = input("\nEnter text to test (or 'exit' to quit): ")
    #     if text.lower() == "exit":
    #         break

    #     link_match = LINK_RE.search(text)
    #     twitch_match = TWITCH_LINK_RE.search(text)

    #     if link_match:
    #         print(f"LINK_RE matched: {link_match.group(0)!r}")
    #     else:
    #         print("LINK_RE did not match.")

    #     if twitch_match:
    #         print(f"TWITCH_LINK_RE matched: {twitch_match.group(0)!r}")
    #     else:
    #         print("TWITCH_LINK_RE did not match.")

    cases = [
        "hello there",
        "watch me at twitch.tv/laurasmaur",
        "clip: https://www.twitch.tv/laurasmaur/clip/GentleRefinedBaboonPunchTrees-Lsf0K_C5m2PVezAO?filter=clips&range=7d",
        "vod https://www.twitch.tv/videos/2797344274",
        "amazon.com",
        "twitch.tv/laurasmaur and amazon.com",
        "https://www.twitch.tv/",
        "help.twitch.tv/",
        "I love this.stuff",
    ]

    for text in cases:
        print()
        print(text)
        print("links:", list(iter_links(text)))
        print("contains_twitch_link:", contains_twitch_link(text))
        print("contains_non_twitch_link:", contains_non_twitch_link(text))
        print("only_twitch_links:", only_twitch_links(text))