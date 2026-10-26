import { copyFile } from "node:fs/promises";

// TypeScript does not emit declaration inputs into outDir.
await copyFile(new URL("../src/schema.d.ts", import.meta.url),
  new URL("../dist/schema.d.ts", import.meta.url));
