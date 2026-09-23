// Copy the built client into plugins/coord/client so the plugin runs with node alone (no install).
import { copyFileSync, mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const dist = here("../dist/"), out = here("../../../plugins/coord/client/");
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
const files = readdirSync(dist).filter((f) => f.endsWith(".js"));
for (const f of files) copyFileSync(dist + f, out + f);
writeFileSync(out + "package.json", JSON.stringify({ type: "module", private: true }, null, 1) + "\n");
console.log(`plugins/coord/client: ${files.length} files`);
