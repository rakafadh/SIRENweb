// SIREN QR payload decoder, format version 1.
// Mirror of encoder/siren_qr/payload.py. Runs in the browser and in Node (tests).
// Layout: see the docstring of payload.py or docs/FORMAT.md.

export const VERSION = 1;
export const ALERTS = [
  "shock_index", "news2_rise", "spo2_low", "rr_high", "rr_low", "hr_high",
  "hr_low", "sbp_low", "temp_high", "temp_low", "new_confusion", "bp_trend",
];
const COLOURS = [null, "HIJAU", "KUNING", "MERAH"];
const SEXES = [null, "L", "P"];
const ACVPU = ["A", "C", "V", "P", "U"];
const SERIES = ["hr", "spo2", "rr", "temp", "news2", "colour"];
const TEXTS = ["code", "unit", "complaint"];
const MISSING = 255;
const TAG_PPG_BP = 1;

export class DecodeError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;   // "empty" | "format" | "checksum" | "version" | "browser"
  }
}

function digitsToBytes(digits) {
  if (!/^[0-9]+$/.test(digits)) throw new DecodeError("format", "fragment is not digits");
  let hex = BigInt(digits).toString(16);
  if (hex.length % 2) hex = "0" + hex;
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  if (bytes[0] !== 1) throw new DecodeError("format", "bad digit header");
  return bytes.subarray(1);
}

async function inflateRaw(bytes) {
  if (typeof DecompressionStream === "undefined") {
    throw new DecodeError("browser", "DecompressionStream not supported");
  }
  try {
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  } catch (err) {
    if (err instanceof TypeError && /deflate-raw|format/i.test(String(err.message))) {
      throw new DecodeError("browser", "deflate-raw not supported");
    }
    throw new DecodeError("format", "corrupt data");
  }
}

let crcTable = null;
export function crc32(bytes) {
  if (!crcTable) {
    crcTable = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      crcTable[n] = c >>> 0;
    }
  }
  let crc = 0xffffffff;
  for (const b of bytes) crc = crcTable[(crc ^ b) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

export function unpack(data) {
  if (data.length < 5) throw new DecodeError("checksum", "too short");
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  if (crc32(data.subarray(0, data.length - 4)) !== view.getUint32(data.length - 4)) {
    throw new DecodeError("checksum", "checksum mismatch");
  }
  if (data[0] !== VERSION) throw new DecodeError("version", `unsupported version ${data[0]}`);

  let pos = 0;
  const u8 = () => view.getUint8(pos++);
  const u16 = () => { const v = view.getUint16(pos); pos += 2; return v; };
  const i16 = () => { const v = view.getInt16(pos); pos += 2; return v; };
  const u32 = () => { const v = view.getUint32(pos); pos += 4; return v; };
  const opt = (v, scale = 1, offset = 0) => (v === MISSING ? null : Math.round((v / scale + offset) * 10) / 10);

  u8(); // version
  const flags = u8();
  const start = u32();
  const step = u16();
  const n = u8();
  const printed = i16();
  const age = u8(), sex = u8(), acvpu = u8(), pain = u8(), colours = u8();
  const news2 = u8(), measured = u8(), alerts = u16();

  const cuffs = [];
  for (let k = u8(); k > 0; k--) {
    const t = i16(), s = u8(), d = u8();
    cuffs.push({ t: start + t * 10, sys: s === MISSING ? null : s, dia: d === MISSING ? null : d });
  }
  const texts = {};
  const utf8 = new TextDecoder("utf-8");
  for (const key of TEXTS) {
    const len = u8();
    texts[key] = utf8.decode(data.subarray(pos, pos + len));
    pos += len;
  }
  const series = {};
  for (const key of SERIES) {
    const values = [];
    let prev = 0;
    for (let i = 0; i < n; i++) {
      prev = (prev + data[pos + i]) % 256;
      values.push(prev);
    }
    series[key] = values;
    pos += n;
  }
  // Optional tail: name, then tagged sections (unknown tags are skipped)
  let name = null;
  const end = data.length - 4;
  if (pos < end) {
    const len = u8();
    name = utf8.decode(data.subarray(pos, pos + len)) || null;
    pos += len;
  }
  series.sbp_ppg = new Array(n).fill(MISSING);
  series.dbp_ppg = new Array(n).fill(MISSING);
  while (pos + 3 <= end) {
    const tag = u8(), len = u16();
    const block = data.subarray(pos, pos + len);
    pos += len;
    if (tag === TAG_PPG_BP && len === 2 * n) {
      ["sbp_ppg", "dbp_ppg"].forEach((key, j) => {
        let prev = 0;
        for (let i = 0; i < n; i++) {
          prev = (prev + block[j * n + i]) % 256;
          series[key][i] = prev;
        }
      });
    }
  }

  const points = [];
  for (let i = 0; i < n; i++) {
    points.push({
      t: start + i * step,
      hr: opt(series.hr[i]), spo2: opt(series.spo2[i]), rr: opt(series.rr[i]),
      temp: opt(series.temp[i], 10, 25), news2: opt(series.news2[i]),
      colour: COLOURS[series.colour[i] & 3],
      sbp_ppg: opt(series.sbp_ppg[i]), dbp_ppg: opt(series.dbp_ppg[i]),
    });
  }

  return {
    version: VERSION,
    simulated: !!(flags & 1), on_oxygen: !!(flags & 2), handed_over: !!(flags & 4),
    start, step_s: step, printed_at: start + printed * 10,
    age: opt(age), sex: SEXES[sex] ?? null, acvpu: acvpu === MISSING ? null : ACVPU[acvpu],
    pain: opt(pain), colour: COLOURS[colours & 3], worst_colour: COLOURS[(colours >> 2) & 3],
    start_colour: COLOURS[(colours >> 4) & 3], news2: opt(news2), news2_measured: measured,
    alerts: ALERTS.filter((_, i) => (alerts >> i) & 1),
    cuffs, ...texts, name, points,
  };
}

export async function decodeFragment(fragment) {
  const digits = (fragment || "").replace(/^#/, "").trim();
  if (!digits) throw new DecodeError("empty", "no data in the address");
  return unpack(await inflateRaw(digitsToBytes(digits)));
}
