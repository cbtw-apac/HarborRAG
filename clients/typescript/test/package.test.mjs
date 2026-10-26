import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

test("packed client includes exported generated wire declarations", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "harbor-client-package-"));
  const root = fileURLToPath(new URL("..", import.meta.url));
  try {
    const environment = { ...process.env, npm_config_cache: directory };
    delete environment.NODE_TEST_CONTEXT;
    execFileSync("npm", ["pack", "--pack-destination", directory], {
      cwd: root, encoding: "utf8", env: environment,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const archives = (await readdir(directory)).filter((name) => name.endsWith(".tgz"));
    assert.equal(archives.length, 1);
    execFileSync("tar", ["-xzf", path.join(directory, archives[0]), "-C", directory]);
    const schema = await readFile(path.join(directory, "package/dist/schema.d.ts"), "utf8");
    assert.match(schema, /export interface paths/);
    const entry = await readFile(path.join(directory, "package/dist/index.d.ts"), "utf8");
    assert.match(entry, /export type \{ paths, components, operations \} from "\.\/schema.js"/);
    execFileSync(path.join(root, "node_modules/.bin/tsc"), [
      "--noEmit", "--skipLibCheck", "false", "--lib", "ES2022,DOM",
      path.join(directory, "package/dist/index.d.ts"),
    ], { cwd: root, stdio: "pipe" });
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
