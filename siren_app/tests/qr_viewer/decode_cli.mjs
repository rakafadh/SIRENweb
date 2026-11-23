// Decode QR digits with the viewer's own decoder (copied from thaniyuck/siren docs/decode.js).
//   node tests/qr_viewer/decode_cli.mjs <digits>
import { decodeFragment } from "./decode.js";

try {
  console.log(JSON.stringify(await decodeFragment(process.argv[2])));
} catch (err) {
  console.log(JSON.stringify({ error: err.code || "unknown", message: err.message }));
}
