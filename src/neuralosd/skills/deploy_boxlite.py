#!/usr/bin/env python3
"""Deploy a neuralOS instance into a BoxLite box and serve it.

usage: deploy_boxlite.py --instance-dir ./my_instance --box-name my-brain
                         [--port 8877] [--packages "pymysql requests"]
"""
import argparse
import asyncio
import os

import boxlite


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance-dir", required=True)
    ap.add_argument("--box-name", required=True)
    ap.add_argument("--port", type=int, default=8877)
    ap.add_argument("--packages", default="pydantic")
    ap.add_argument("--serve-url")
    ap.add_argument("--api-key")
    a = ap.parse_args()

    if a.serve_url:
        opts = boxlite.BoxliteRestOptions(url=a.serve_url,
                                          credential=boxlite.ApiKeyCredential(a.api_key) if a.api_key else None)
        rt = boxlite.Boxlite.rest(opts)
    else:
        rt = boxlite.Boxlite.default()

    box = await rt.get_or_create(
        boxlite.BoxOptions(image="python:3.12-slim", auto_delete=0,
                           cpus=1, memory_mib=1024, disk_size_gb=6),
        name=a.box_name)
    await box.start()
    await box.copy_in(a.instance_dir, "/opt/chinook" if "chinook" in a.instance_dir else "/opt/instance")
    ex = await box.exec("pip", "install", "--no-cache-dir", "neuralos", *a.packages.split(), timeout_secs=600)
    out = []
    async for line in ex.stdout(): out.append(line)
    await ex.wait()
    print("deployed:", a.box_name)


if __name__ == "__main__":
    asyncio.run(main())
