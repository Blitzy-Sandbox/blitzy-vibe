"""Interactive numbered LLM provider picker.

This module exposes :func:`select_provider`, the interactive prompt shown at
CLI startup when neither ``--resume`` (with an existing session) nor
``--provider`` is supplied.  The prompt offers three choices -- Blitzy (the
default), Mistral, and Anthropic -- and is rendered verbatim per the AAP user
example::

    Select LLM provider:
    [1] Blitzy  (default)
    [2] Mistral
    [3] Anthropic
    >

Per AAP section 0.8.1 rule 4, this picker MUST be displayed before any backend
constructor runs.  The entrypoint orchestration block in :mod:`vibe.cli.entrypoint`
enforces this ordering -- the resolved :class:`~vibe.core.config.Backend` value is
passed downstream to :func:`vibe.cli.cli.run_cli`, which forwards it to the
:data:`~vibe.core.llm.backend.factory.BACKEND_FACTORY` map.

Empty input (just Enter) selects Blitzy (rule 4 -- ``[1] Blitzy (default)``).
The parser is case-insensitive and accepts either the digit (``1``/``2``/``3``)
or the lowercase provider token (``blitzy``/``mistral``/``anthropic``).
Invalid input reprompts up to three times; after exhaustion,
:class:`KeyboardInterrupt` is raised to abort startup safely so that the
boundary contract from rule 4 (no backend constructor called when selection
fails) is honored by the caller.
"""

from __future__ import annotations

from vibe.core.config import Backend

# The four-line prompt header rendered verbatim per AAP section 0.6.3 (User
# Interface Design).  Note the EXACT spacing: ``[1] Blitzy  (default)`` has
# TWO spaces between "Blitzy" and "(default)" to match the user example.  The
# trailing ``> `` line is supplied separately to :func:`input` so the cursor
# sits on the same line as the prompt arrow.
_PROMPT = "Select LLM provider:\n[1] Blitzy  (default)\n[2] Mistral\n[3] Anthropic"

# Canonical mapping from accepted user tokens (digit or lowercase name) to the
# :class:`Backend` enum value.  Both numeric and alphabetic forms are accepted
# because the AAP user example explicitly shows ``[1] Blitzy`` -- operators may
# enter either the digit or the provider name.  The keys here mirror the
# argparse ``--provider`` choices and the ``Backend`` enum lowercase values
# (AAP rule 13 -- no orphaned strings).
_TOKEN_TO_BACKEND: dict[str, Backend] = {
    "1": Backend.BLITZY,
    "blitzy": Backend.BLITZY,
    "2": Backend.MISTRAL,
    "mistral": Backend.MISTRAL,
    "3": Backend.ANTHROPIC,
    "anthropic": Backend.ANTHROPIC,
}

# Bounded retry limit per AAP section 0.6.1 ("bounded retry").  After this many
# invalid attempts the picker raises :class:`KeyboardInterrupt` so that startup
# aborts cleanly and rule 4 is preserved (no backend constructor is invoked
# when selection is unsuccessful).
_MAX_RETRIES = 3

# Error message rendered after each invalid input per AAP section 0.6.3.  The
# wording is fixed and is asserted by the integration tests in
# ``tests/cli/test_provider_selection.py``.
_INVALID_MESSAGE = "Invalid choice. Please enter 1, 2, or 3."


def select_provider() -> Backend:
    """Display the interactive numbered LLM provider prompt and return the selection.

    The prompt is rendered verbatim per the AAP user example.  Empty input
    (just Enter) returns :attr:`Backend.BLITZY` -- the default option.  The
    parser is case-insensitive: ``1``, ``2``, ``3`` (digits) and
    ``blitzy``/``mistral``/``anthropic`` (lowercase tokens, after stripping
    and lowercasing the raw input) are the accepted forms.

    Invalid input reprompts up to :data:`_MAX_RETRIES` (three) times; on
    exhaustion :class:`KeyboardInterrupt` is raised so the caller can exit
    cleanly without instantiating any backend (AAP rule 4).  ``EOF`` from the
    underlying :func:`input` call (for example ``Ctrl-D`` or a piped empty
    stdin) is treated as cancellation and converted to
    :class:`KeyboardInterrupt` for uniform caller handling.

    Returns:
        The selected :class:`Backend` enum value -- one of
        :attr:`Backend.BLITZY`, :attr:`Backend.MISTRAL`, or
        :attr:`Backend.ANTHROPIC`.

    Raises:
        KeyboardInterrupt: When the user supplies invalid input on every
            attempt (bounded to :data:`_MAX_RETRIES`) or when stdin reaches
            EOF before a valid selection is made.  The exception is the
            caller's signal that no backend constructor must run.
    """
    # Emit the four-line prompt header.  ``print`` appends a trailing newline
    # so the cursor naturally lands on the next line, where ``input("> ")``
    # then prints the prompt arrow before reading the user response.
    print(_PROMPT)

    for _ in range(_MAX_RETRIES):
        try:
            # ``input("> ")`` writes ``> `` to stdout (without a newline) and
            # reads a single line from stdin, stripping the trailing newline.
            raw = input("> ")
        except EOFError as exc:
            # Ctrl-D or piped empty stdin -- treat as cancellation per AAP
            # rule 4 ("no backend constructor must run when selection is
            # cancelled").  Convert to ``KeyboardInterrupt`` so the entrypoint
            # has a single exception type to catch for both interactive
            # cancellation paths.
            raise KeyboardInterrupt("Provider selection cancelled (EOF)") from exc

        # Normalize the raw input: strip surrounding whitespace (including the
        # trailing carriage return some terminals append) and lowercase so the
        # token lookup is case-insensitive ("Blitzy", "BLITZY", "blitzy" all
        # match the same enum value).
        token = raw.strip().lower()

        # Empty input -- just pressing Enter -- selects Blitzy, the default
        # provider per AAP rule 4 ("[1] Blitzy (default)").  This case MUST be
        # checked before the dict lookup because "" is not a key.
        if token == "":
            return Backend.BLITZY

        # Direct lookup -- matches "1", "blitzy", "2", "mistral", "3",
        # "anthropic".  Any other token falls through to the invalid branch.
        if token in _TOKEN_TO_BACKEND:
            return _TOKEN_TO_BACKEND[token]

        # Invalid input -- report and reprompt.  The exact wording is asserted
        # by ``tests/cli/test_provider_selection.py::test_invalid_input_reprompts``.
        print(_INVALID_MESSAGE)

    # All ``_MAX_RETRIES`` attempts were invalid.  Abort startup cleanly --
    # the entrypoint's outer try/except in :func:`vibe.cli.entrypoint.main`
    # catches ``KeyboardInterrupt`` and exits without instantiating a backend.
    raise KeyboardInterrupt(
        f"Provider selection failed after {_MAX_RETRIES} invalid attempts"
    )


__all__ = ["select_provider"]
