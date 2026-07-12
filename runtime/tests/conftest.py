import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolate_default_cockpit_db(tmp_path_factory):
    """Keep explicit default CockpitRuntime instances away from the live DB."""
    variable = "SPST_COCKPIT_DB_PATH"
    previous = os.environ.get(variable)
    isolated = tmp_path_factory.mktemp("default-cockpit") / "spst_cockpit.db"
    os.environ[variable] = str(isolated)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous
