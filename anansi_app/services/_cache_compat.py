"""A ``cache_data`` decorator that works with or without Streamlit.

Under the Streamlit app this is exactly ``streamlit.cache_data`` (behavior
unchanged). Under the NiceGUI app — which does not install ``streamlit`` — it
degrades to a no-op passthrough so the shared ``services/`` layer imports
cleanly. NiceGUI-side result caching can be layered on later if it proves
necessary; correctness does not depend on it.
"""

from __future__ import annotations

try:
    import streamlit as st

    cache_data = st.cache_data
except ModuleNotFoundError:

    def cache_data(*dargs, **dkwargs):  # type: ignore[misc]
        """No-op stand-in supporting both ``@cache_data`` and ``@cache_data(...)``."""
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return dargs[0]

        def decorator(func):
            return func

        return decorator
