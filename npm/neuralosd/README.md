# neuralosd (npm launcher)

Installs the **neuralOSd** verified agent-runtime binaries from the official
GitHub releases for your platform, then hands every other command to the
installed binary.

```bash
npx neuralosd install                  # base runtime → ~/.neuralosd/bin
npx neuralosd serve --instance-dir ./my-instance
npx neuralosd ask --instance-dir ./my-instance "how many rows"
npx neuralosd update --check           # verified self-update
```

The first non-installer command installs the base binary if it is missing,
then runs it with your arguments and exits with its status.

Variants: `npx neuralosd install boxlite` · `npx neuralosd install msb`.

Documentation: https://drolu.github.io/neuralosd/
