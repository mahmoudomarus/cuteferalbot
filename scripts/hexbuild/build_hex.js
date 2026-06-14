#!/usr/bin/env node
// Build a flashable hex: official MicroPython v2.1.1 runtime + main.py in
// the embedded filesystem. Bypasses serial entirely (drag-and-drop flash).
//
// Usage: node build_hex.js <script.py> <out.hex>

const fs = require("fs");
const path = require("path");
const { MicropythonFsHex, microbitBoardId } = require("@microbit/microbit-fs");

const [, , scriptPath, outPath] = process.argv;
if (!scriptPath || !outPath) {
  console.error("usage: node build_hex.js <script.py> <out.hex>");
  process.exit(1);
}

const runtime = fs.readFileSync(
  path.join(__dirname, "../../firmware/runtime/micropython-microbit-v2.1.1.hex"),
  "utf8"
);

const mpFs = new MicropythonFsHex([
  { hex: runtime, boardId: microbitBoardId.V2 },
]);
mpFs.write("main.py", fs.readFileSync(scriptPath, "utf8"));

fs.writeFileSync(outPath, mpFs.getIntelHex(microbitBoardId.V2));
console.log(`Wrote ${outPath} (main.py = ${fs.statSync(scriptPath).size} bytes)`);
