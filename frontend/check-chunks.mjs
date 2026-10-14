// Fails the build when a JavaScript chunk passes the size limit after minification, so a
// large chunk is split (or loaded on demand) instead of the limit being raised.
//   node check-chunks.mjs [directory]   (default: dist/assets)
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

/** Vite's warning limit, in bytes (Vite counts a kilobyte as 1000 bytes). */
export const LIMIT = 500_000;

/** The JavaScript files in a directory larger than the limit, largest first. */
export function oversized(directory, limit = LIMIT) {
  return fs.readdirSync(directory)
    .filter((name) => name.endsWith(".js"))
    .map((name) => ({ name, bytes: fs.statSync(path.join(directory, name)).size }))
    .filter((file) => file.bytes > limit)
    .sort((a, b) => b.bytes - a.bytes);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const directory = process.argv[2] ?? fileURLToPath(new URL("dist/assets", import.meta.url));
  const found = oversized(directory);
  for (const file of found) {
    console.error(`${file.name}: ${(file.bytes / 1000).toFixed(2)} kB, over the ${LIMIT / 1000} kB limit`);
  }
  if (found.length) {
    console.error("Split the chunk in vite.config.ts (manualChunks) or import less; do not raise the limit.");
    process.exit(1);
  }
  console.log(`chunks: every JavaScript chunk is within ${LIMIT / 1000} kB`);
}
