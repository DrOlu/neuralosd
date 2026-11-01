#!/usr/bin/env node
// neuralosd CLI — installs the neuralOSd gateway/runtime binaries
// from the official GitHub releases for the current platform.

import { chmod, mkdir, writeFile } from "node:fs/promises";
import { readFileSync } from "node:fs";
import { createWriteStream } from "node:fs";
import { pipeline } from "node:stream/promises";
import { homedir, platform, arch } from "node:os";
import path from "node:path";
import { Readable } from "node:stream";

// Derived from package.json at runtime: a hardcoded literal here shipped
// stale binaries in the reactorpro launcher — drift is structurally impossible.
const VERSION = JSON.parse(
  readFileSync(new URL("../package.json", import.meta.url), "utf8"),
).version;
const OWNER = "DrOlu";
const REPO = "neuralosd";
const INSTALL_DIR = path.join(homedir(), ".neuralosd", "bin");

// npm platform key → release asset naming
const TARGETS = {
  "darwin-arm64": { asset: "macos-arm64", ext: "" },
  "darwin-x64": { asset: "macos-x64", ext: "" },
  "linux-x64": { asset: "linux-x64", ext: "" },
  "linux-arm64": { asset: "linux-arm64", ext: "" },
  "win32-x64": { asset: "windows-x64", ext: ".exe" },
};

const VARIANTS = ["base", "boxlite", "msb"];

function usage() {
  console.log(`neuralosd v${VERSION} — neuralOSd CLI

Usage:
  neuralosd install [variant]   Download + install neuralOSd (base | boxlite | msb; default base)
  neuralosd --version           Print the CLI version
  neuralosd help                Show this help

Binaries are downloaded from the official GitHub releases (v${VERSION})
and installed into ${INSTALL_DIR}.`);
}

function resolveTarget() {
  const key = `${platform()}-${arch()}`;
  const target = TARGETS[key];
  if (!target) return null;
  return { ...target, key };
}

function assetName(variant, target) {
  const base = variant === "base" ? "neuralosd" : `neuralosd-${variant}`;
  return `${base}-${target.asset}${target.ext}`;
}

async function downloadTo(url, destPath) {
  const response = await fetch(url, { redirect: "follow" });
  if (!response.ok || !response.body) {
    throw new Error(`download failed (HTTP ${response.status}) for ${url}`);
  }
  await pipeline(Readable.fromWeb(response.body), createWriteStream(destPath));
  if (platform() !== "win32") await chmod(destPath, 0o755);
}

async function install(variant) {
  const target = resolveTarget();
  if (!target) {
    console.error(`Unsupported platform: ${platform()}-${arch()}`);
    process.exitCode = 1;
    return;
  }
  const name = assetName(variant, target);
  const url = `https://github.com/${OWNER}/${REPO}/releases/download/v${VERSION}/${name}`;
  const suffix = variant === "base" ? "" : `-${variant}`;
  const destPath = path.join(INSTALL_DIR, `neuralosd${suffix}${target.ext}`);
  await mkdir(INSTALL_DIR, { recursive: true });
  process.stdout.write(`Installing neuralOSd ${variant} v${VERSION} (${target.key}) → ${destPath}\n`);
  try {
    await downloadTo(url, destPath);
  } catch (error) {
    console.error(error.message);
    console.error(`Check the release exists: https://github.com/${OWNER}/${REPO}/releases/tag/v${VERSION}`);
    process.exitCode = 1;
    return;
  }
  process.stdout.write(`Done. Binary at ${destPath}\n`);
  if (platform() === "win32") {
    process.stdout.write(`Run it:  ${destPath}\n`);
  } else {
    process.stdout.write(`Run it:  ${destPath} serve --instance-dir <dir>\n`);
  }
}

const [cmd, ...rest] = process.argv.slice(2);
const variant = VARIANTS.includes(rest[0]) ? rest[0] : "base";

if (cmd === "install") {
  await install(variant);
} else if (cmd === "--version" || cmd === "-v" || cmd === "version") {
  console.log(`neuralosd v${VERSION}`);
} else {
  usage();
}
