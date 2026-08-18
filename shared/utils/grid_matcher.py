"""
Grid Name Fuzzy Matcher

Provides fuzzy matching for grid names to handle typos and misspellings.
Uses rapidfuzz for high-performance string matching.

Usage:
    from shared.utils.grid_matcher import find_best_grid_match

    # Given a list of valid grid names and user input
    valid_names = ["ExampleGrid", "SiteAlpha", "Site Beta"]
    result = find_best_grid_match("SiteAlph", valid_names)
    # Returns: ("ExampleGrid", True, 89)  # (matched_name, was_fuzzy, score)
"""

from typing import List, Optional, Tuple

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Minimum similarity score (0-100) for a fuzzy match to be accepted
DEFAULT_FUZZY_THRESHOLD = 80

# find_grid_mention searches free text (a whole chat message, not a single
# extracted value), which carries far more incidental partial-match risk
# than find_best_grid_match's structured-field use case -- hence a higher bar.
TEXT_MENTION_FUZZY_THRESHOLD = 90


def find_best_grid_match(
    input_name: str,
    valid_names: List[str],
    threshold: int = DEFAULT_FUZZY_THRESHOLD,
) -> Tuple[Optional[str], bool, int]:
    """
    Find the best matching grid name from a list of valid names.

    First tries exact case-insensitive match, then falls back to fuzzy matching
    if no exact match is found.

    Args:
        input_name: User-provided grid name (possibly misspelled)
        valid_names: List of valid grid names to match against
        threshold: Minimum similarity score (0-100) for fuzzy match acceptance

    Returns:
        Tuple of (matched_name, was_fuzzy_match, similarity_score)
        - If exact match: (name, False, 100)
        - If fuzzy match above threshold: (name, True, score)
        - If no match: (None, False, 0)
    """
    if not input_name or not valid_names:
        return (None, False, 0)

    input_lower = input_name.lower().strip()

    # Build case-insensitive lookup map
    name_map = {name.lower().strip(): name for name in valid_names if name}

    # First try exact case-insensitive match
    exact_match = name_map.get(input_lower)
    if exact_match:
        return (exact_match, False, 100)

    # Fall back to fuzzy matching using rapidfuzz
    try:
        from rapidfuzz import fuzz, process

        # Use token_sort_ratio: sorts tokens alphabetically then compares.
        # This correctly distinguishes "Test GridA" from "Test GridB"
        # because ALL tokens must match, not just the shared ones.
        # (token_set_ratio gave 100% to any name sharing a prefix like "Test",
        # causing ambiguous matches where list ordering determined the winner.)
        top_matches = process.extract(
            input_name,
            valid_names,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=threshold,
            limit=2,
        )

        if not top_matches:
            LOGGER.warning(
                f"No fuzzy match found for grid name '{input_name}' "
                f"(best match below {threshold}% threshold)"
            )
            return (None, False, 0)

        best_name, best_score, _ = top_matches[0]

        # Ambiguity guard: if 2+ candidates score within 10 points of each other,
        # the input is too vague (e.g. "Grid" matching both "GridA" and
        # "GridB"). Reject so the user is asked to be more specific.
        if len(top_matches) >= 2:
            _, second_score, _ = top_matches[1]
            if best_score - second_score < 10:
                ambiguous_names = [m[0] for m in top_matches]
                LOGGER.warning(
                    f"Ambiguous fuzzy match for '{input_name}': "
                    f"{ambiguous_names} (scores: {best_score:.0f}%, {second_score:.0f}%). "
                    f"Rejecting — user should be more specific."
                )
                return (None, False, 0)

        LOGGER.info(
            f"Fuzzy matched grid name '{input_name}' -> '{best_name}' (score: {best_score:.0f}%)"
        )
        return (best_name, True, int(best_score))

    except ImportError:
        LOGGER.warning("rapidfuzz not installed - fuzzy matching unavailable")
        return (None, False, 0)


def find_grid_mention(
    text: str,
    valid_names: List[str],
    threshold: int = TEXT_MENTION_FUZZY_THRESHOLD,
) -> Optional[str]:
    """
    Search free-form text for a mention of one of the given grid names.

    ``find_best_grid_match`` uses ``token_sort_ratio``, built for comparing
    two short, comparable-length strings (e.g. correcting a provided grid
    name against a list of options) -- not for finding a name's approximate
    presence inside a much longer block of text. This uses
    ``fuzz.partial_ratio`` instead, which scores the best-matching substring
    of the longer text against each candidate name.

    Args:
        text: Free-form text to search (e.g. a customer's chat message).
        valid_names: Grid names to search for.
        threshold: Minimum similarity score (0-100) to accept a match.

    Returns:
        The matched name, or None if no candidate reaches the threshold, or
        if the top two candidates are too close to call (ambiguous).
    """
    if not text or not valid_names:
        return None

    try:
        from rapidfuzz import fuzz
    except ImportError:
        LOGGER.warning("rapidfuzz not installed - grid mention search unavailable")
        return None

    text_lower = text.lower()
    scored = sorted(
        (
            (name, fuzz.partial_ratio(name.lower(), text_lower))
            for name in valid_names
            if name
        ),
        key=lambda pair: pair[1],
        reverse=True,
    )
    if not scored:
        return None

    best_name, best_score = scored[0]
    if best_score < threshold:
        return None

    if len(scored) >= 2:
        _, second_score = scored[1]
        if best_score - second_score < 10:
            LOGGER.warning(
                f"Ambiguous grid mention in text: top candidates "
                f"{[name for name, _ in scored[:2]]} scored "
                f"{best_score:.0f}%, {second_score:.0f}%. Rejecting."
            )
            return None

    LOGGER.info(f"Found grid mention '{best_name}' in text (score={best_score:.0f}%)")
    return best_name


def parse_multi_site_args(raw_args: str) -> List[str]:
    """Parse comma-separated site/grid names from command args.

    If commas are present, splits on commas. Otherwise returns
    the entire string as a single item (preserving multi-word names).

    Args:
        raw_args: Raw arguments string (e.g., "Site1, Site2, Site3")

    Returns:
        List of trimmed, non-empty site names
    """
    if "," in raw_args:
        items = [item.strip() for item in raw_args.split(",")]
    else:
        items = [raw_args.strip()]
    return [item for item in items if item]


def normalize_grid_name(name: str) -> str:
    """
    Normalize a grid name for consistent comparison.

    - Strips leading/trailing whitespace
    - Normalizes multiple spaces to single space

    Args:
        name: Grid name to normalize

    Returns:
        Normalized grid name
    """
    if not name:
        return ""
    # Normalize whitespace
    return " ".join(name.split())
