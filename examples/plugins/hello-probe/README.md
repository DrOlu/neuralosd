# hello-probe

Minimal probe pack. Enable it with:

```sh
neuralosd plugin test .
neuralosd plugin enable hello --instance-dir ../my-instance --path .
neuralosd ask --instance-dir ../my-instance "hello plugin"
```

The registered probe is namespaced as `hello_hello`.
