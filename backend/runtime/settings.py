"""Runtime settings, with support for environment keys from older deployments."""

import os


def get_setting(name, default=None):
    value = os.environ.get(f"ZHIXING_{name}")
    if value is None:
        return os.environ.get(f"MUSE_{name}", default)
    return value
