"""Sandbox backends, resolved through the plugin registry.

The shipped backends are registered exactly like third-party ones, so a
plugin can add a backend without a core fork.
"""
def _record():
    from ..plugins.builtins import builtin_record
    return builtin_record()


def get_backend(name: str, **kwargs):
    factory = _record().backends.get(name)
    if factory is None:
        raise ValueError(f"unknown backend: {name}")
    return factory(**kwargs)


def available_backends():
    return {name: factory.available()
            for name, factory in _record().backends.items()}
