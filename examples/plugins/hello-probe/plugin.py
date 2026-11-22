"""A minimal neuralOSd probe pack."""
from neuralosd import probe


@probe(description="Hello from a plugin", triggers=["hello plugin"])
def hello():
    return {"hello": "from the hello plugin"}


def register(ctx):
    ctx.register_probe(hello)
