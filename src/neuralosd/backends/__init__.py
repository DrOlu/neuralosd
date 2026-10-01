from .boxlite_backend import BoxLiteBackend
from .msb_backend import MSBBackend


def get_backend(name: str, **kwargs):
    if name == "boxlite":
        return BoxLiteBackend(**kwargs)
    if name == "msb":
        return MSBBackend(**kwargs)
    raise ValueError(f"unknown backend: {name}")


def available_backends():
    return {"boxlite": BoxLiteBackend.available(),
            "msb": MSBBackend.available()}
