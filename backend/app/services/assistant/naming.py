"""One-shot route naming: a single no-tools completion, not a turn.

Deliberately separate from turn.py's tool-calling loop. Naming a route needs
none of that machinery - no tools, no handles, one request and one reply -
and folding it into run_turn would mean threading a "no tools this time"
flag through code whose whole shape exists to support tool calls.

The only facts the model is given are the ones the caller measured: the
start/end place names (when the place index resolved them) and the route's
own distance and ascent. Nothing here lets the model state a number it was
not handed, mirroring SYSTEM_PROMPT's "never state a figure a tool did not
give you" rule from the chat assistant.
"""

from app.services.llm import LLMClient

MAX_NAME_CHARS = 60

_SYSTEM_PROMPT = """\
You name cycling routes for Moovelo, a route planner. Given a short set of \
facts about one route, reply with ONLY a short route name - no quotes, no \
punctuation wrapping it, no explanation, nothing before or after it.

Use only the facts given to you. Never state a distance, place name or any \
other figure that was not in the facts. If no place names were given, name \
the route from its distance instead of guessing at where it is.

Keep it under 8 words.\
"""


def _build_facts(
    distance_km: float,
    ascent_m: float,
    start_name: str | None,
    end_name: str | None,
) -> str:
    lines = [f"Distance: {distance_km:.1f} km", f"Ascent: {round(ascent_m)} m"]
    if start_name and end_name:
        if start_name == end_name:
            lines.append(f"Starts and ends near: {start_name} (a loop)")
        else:
            lines.append(f"Starts near: {start_name}")
            lines.append(f"Ends near: {end_name}")
    else:
        lines.append("No place names are available for this route.")
    return "\n".join(lines)


def build_messages(
    distance_km: float,
    ascent_m: float,
    start_name: str | None,
    end_name: str | None,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_facts(distance_km, ascent_m, start_name, end_name),
        },
    ]


def clean_name(raw: str) -> str:
    """Make a model reply safe to drop straight into a text input.

    Newlines and surrounding quotes are the two shapes models reliably slip
    in despite being told not to; the hard length cap is the backstop for
    everything else, cut on a word boundary where one exists so a truncated
    name does not end mid-word.
    """
    collapsed = " ".join(raw.split())
    stripped = collapsed.strip("\"'")
    if len(stripped) <= MAX_NAME_CHARS:
        return stripped
    truncated = stripped[:MAX_NAME_CHARS]
    last_space = truncated.rfind(" ")
    # Only trim to the word boundary when that does not throw away most of
    # the name - a name with no space near the cap is better cut hard than
    # reduced to a handful of characters.
    if last_space > MAX_NAME_CHARS // 2:
        truncated = truncated[:last_space]
    return truncated.strip()


async def suggest_name(
    llm: LLMClient,
    distance_km: float,
    ascent_m: float,
    start_name: str | None,
    end_name: str | None,
) -> str:
    """One no-tools completion, returning a cleaned, length-capped name.

    Raises LLMError on failure - the caller decides how to surface that,
    same as every other outbound call in this codebase.
    """
    messages = build_messages(distance_km, ascent_m, start_name, end_name)
    response = await llm.complete(messages)
    return clean_name(response.content)
