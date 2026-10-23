"""
Build-time PSA provider selection.

Exactly one provider is compiled into each RivetPanel binary - there is no
runtime picker. CI writes psa/_selected.py (gitignored) via
scripts/select_provider.py right before the PyInstaller build; PyInstaller's
modulegraph then only bundles that one concrete provider module. Local/dev
runs (no _selected.py present) default to RivetIT/RivetMSP.
"""


def get_provider_class():
    try:
        from . import _selected
        return _selected.PROVIDER_CLASS
    except ImportError:
        from .rivet import RivetProvider
        return RivetProvider


def get_provider_name():
    try:
        from . import _selected
        return _selected.PROVIDER_NAME
    except ImportError:
        return "rivet"
