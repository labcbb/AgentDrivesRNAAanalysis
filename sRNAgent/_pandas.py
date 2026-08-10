"""Process-wide pandas compatibility settings."""
from __future__ import annotations


def configure_pandas_string_storage() -> None:
    """Avoid the Arrow string backend in long-lived agent processes.

    pandas 3 enables inferred Arrow strings by default when pyarrow is
    installed.  The UI process is long-lived and coordinates notebook, HTTP,
    and worker threads, so keep inferred strings as Python objects instead.
    """
    try:
        import pandas as pd
    except Exception:
        return

    try:
        pd.options.future.infer_string = False
    except Exception:
        pass
    try:
        pd.options.mode.string_storage = "python"
    except Exception:
        pass


configure_pandas_string_storage()
