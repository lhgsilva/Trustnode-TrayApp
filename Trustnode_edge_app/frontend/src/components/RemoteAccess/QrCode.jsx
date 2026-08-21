// Tiny dependency-free QR code encoder + SVG renderer.
//
// Scope (plan 2026-08-21 §3.4 "each URL with a QR code"): byte mode, error
// correction level M, versions 1–10 (up to 213 bytes — plenty for
// https://<hostname>:8443/trustnode/client/app/). No external package, no
// network. Algorithm follows ISO/IEC 18004 the same way the well-known
// compact encoders (qrcode-generator / Nayuki) do: Reed–Solomon over
// GF(256) with polynomial 0x11d, 8 mask patterns scored by the standard
// penalty rules, BCH-coded format + version information.

import { useMemo } from "react";

const EC_LEVEL_M_BITS = 0; // format-info level indicator: L=1, M=0, Q=3, H=2
const MAX_VERSION = 10;

// Reed–Solomon block structure for EC level M (ISO 18004 table 9):
// [blockCount, totalCodewordsPerBlock, dataCodewordsPerBlock] groups.
const RS_BLOCKS_M = {
  1: [[1, 26, 16]],
  2: [[1, 44, 28]],
  3: [[1, 70, 44]],
  4: [[2, 50, 32]],
  5: [[2, 67, 43]],
  6: [[4, 43, 27]],
  7: [[4, 49, 31]],
  8: [[2, 60, 38], [2, 61, 39]],
  9: [[3, 58, 36], [2, 59, 37]],
  10: [[4, 69, 43], [1, 70, 44]],
};

// Alignment pattern centre coordinates per version.
const ALIGN_POS = {
  1: [],
  2: [6, 18],
  3: [6, 22],
  4: [6, 26],
  5: [6, 30],
  6: [6, 34],
  7: [6, 22, 38],
  8: [6, 24, 42],
  9: [6, 26, 46],
  10: [6, 28, 50],
};

// ---- GF(256) arithmetic -------------------------------------------------
const GF_EXP = new Uint8Array(512);
const GF_LOG = new Uint8Array(256);
(function initGaloisTables() {
  let x = 1;
  for (let i = 0; i < 255; i += 1) {
    GF_EXP[i] = x;
    GF_LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i += 1) GF_EXP[i] = GF_EXP[i - 255];
})();

function gfMul(a, b) {
  if (a === 0 || b === 0) return 0;
  return GF_EXP[GF_LOG[a] + GF_LOG[b]];
}

// Generator polynomial of the given degree, coefficients highest-first,
// leading coefficient (x^degree) included as g[0] = 1.
function rsGenerator(degree) {
  let g = [1];
  for (let i = 0; i < degree; i += 1) {
    const next = new Array(g.length + 1).fill(0);
    for (let j = 0; j < g.length; j += 1) {
      next[j] ^= g[j];
      next[j + 1] ^= gfMul(g[j], GF_EXP[i]);
    }
    g = next;
  }
  return g;
}

function rsEncode(data, ecCount) {
  const gen = rsGenerator(ecCount);
  const result = new Array(ecCount).fill(0);
  for (let k = 0; k < data.length; k += 1) {
    const factor = data[k] ^ result[0];
    result.shift();
    result.push(0);
    if (factor !== 0) {
      for (let i = 0; i < ecCount; i += 1) {
        result[i] ^= gfMul(gen[i + 1], factor);
      }
    }
  }
  return result;
}

// ---- Data encoding -----------------------------------------------------
function utf8Bytes(text) {
  const s = String(text ?? "");
  try {
    if (typeof TextEncoder !== "undefined") return Array.from(new TextEncoder().encode(s));
  } catch (_) { /* fall through */ }
  // eslint-disable-next-line no-undef
  const bin = unescape(encodeURIComponent(s));
  const out = new Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i) & 0xff;
  return out;
}

function totalDataCodewords(version) {
  return RS_BLOCKS_M[version].reduce((acc, [count, , dataLen]) => acc + count * dataLen, 0);
}

function charCountBits(version) {
  return version < 10 ? 8 : 16; // byte mode: 8 bits for v1–9, 16 for v10–26
}

function pickVersion(byteLen) {
  for (let v = 1; v <= MAX_VERSION; v += 1) {
    const capacityBits = totalDataCodewords(v) * 8;
    const needBits = 4 + charCountBits(v) + byteLen * 8;
    if (needBits <= capacityBits) return v;
  }
  return 0;
}

function buildCodewords(bytes, version) {
  const bits = [];
  const put = (value, len) => {
    for (let i = len - 1; i >= 0; i -= 1) bits.push((value >>> i) & 1);
  };
  put(0x4, 4); // byte mode indicator
  put(bytes.length, charCountBits(version));
  for (const b of bytes) put(b, 8);
  const capacityBits = totalDataCodewords(version) * 8;
  // terminator (up to 4 zero bits), then pad to a byte boundary
  put(0, Math.min(4, capacityBits - bits.length));
  while (bits.length % 8 !== 0) bits.push(0);
  // pad codewords 0xEC / 0x11 alternating
  for (let pad = 0xec; bits.length < capacityBits; pad ^= 0xec ^ 0x11) put(pad, 8);

  const data = [];
  for (let i = 0; i < bits.length; i += 8) {
    let v = 0;
    for (let j = 0; j < 8; j += 1) v = (v << 1) | bits[i + j];
    data.push(v);
  }

  // Split into RS blocks, compute EC per block, then interleave.
  const blocks = [];
  let offset = 0;
  for (const [count, total, dataLen] of RS_BLOCKS_M[version]) {
    for (let b = 0; b < count; b += 1) {
      const chunk = data.slice(offset, offset + dataLen);
      offset += dataLen;
      blocks.push({ data: chunk, ec: rsEncode(chunk, total - dataLen) });
    }
  }
  const out = [];
  const maxData = Math.max(...blocks.map((b) => b.data.length));
  for (let i = 0; i < maxData; i += 1) {
    for (const b of blocks) if (i < b.data.length) out.push(b.data[i]);
  }
  const maxEc = Math.max(...blocks.map((b) => b.ec.length));
  for (let i = 0; i < maxEc; i += 1) {
    for (const b of blocks) if (i < b.ec.length) out.push(b.ec[i]);
  }
  return out;
}

// ---- Matrix construction ------------------------------------------------
function makeGrid(size, fill) {
  const g = new Array(size);
  for (let y = 0; y < size; y += 1) g[y] = new Array(size).fill(fill);
  return g;
}

function drawFunctionPatterns(modules, isFunction, version) {
  const size = modules.length;
  const set = (x, y, dark) => {
    if (x < 0 || y < 0 || x >= size || y >= size) return;
    modules[y][x] = dark ? 1 : 0;
    isFunction[y][x] = true;
  };
  // Finder patterns + separators
  const finder = (x0, y0) => {
    for (let dy = -1; dy <= 7; dy += 1) {
      for (let dx = -1; dx <= 7; dx += 1) {
        const dist = Math.max(Math.abs(dx - 3), Math.abs(dy - 3));
        set(x0 + dx, y0 + dy, dist !== 2 && dist !== 4);
      }
    }
  };
  finder(0, 0);
  finder(size - 7, 0);
  finder(0, size - 7);
  // Timing patterns
  for (let i = 8; i < size - 8; i += 1) {
    set(i, 6, i % 2 === 0);
    set(6, i, i % 2 === 0);
  }
  // Alignment patterns (skip the three that collide with finders)
  const pos = ALIGN_POS[version] || [];
  const last = pos.length - 1;
  for (let i = 0; i < pos.length; i += 1) {
    for (let j = 0; j < pos.length; j += 1) {
      if ((i === 0 && j === 0) || (i === 0 && j === last) || (i === last && j === 0)) continue;
      const cx = pos[i];
      const cy = pos[j];
      for (let dy = -2; dy <= 2; dy += 1) {
        for (let dx = -2; dx <= 2; dx += 1) {
          set(cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
        }
      }
    }
  }
  // Reserve format-information areas (drawn after masking)
  for (let i = 0; i < 9; i += 1) {
    if (!isFunction[8][i]) set(i, 8, false);
    if (!isFunction[i][8]) set(8, i, false);
  }
  for (let i = 0; i < 8; i += 1) {
    set(size - 1 - i, 8, false);
    set(8, size - 1 - i, false);
  }
  set(8, size - 8, true); // the always-dark module
  // Reserve version-information areas (v >= 7)
  if (version >= 7) {
    for (let i = 0; i < 18; i += 1) {
      const a = size - 11 + (i % 3);
      const b = Math.floor(i / 3);
      set(a, b, false);
      set(b, a, false);
    }
  }
}

function placeData(modules, isFunction, codewords) {
  const size = modules.length;
  const totalBits = codewords.length * 8;
  let i = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vert = 0; vert < size; vert += 1) {
      for (let j = 0; j < 2; j += 1) {
        const x = right - j;
        const upward = ((right + 1) & 2) === 0;
        const y = upward ? size - 1 - vert : vert;
        if (!isFunction[y][x] && i < totalBits) {
          modules[y][x] = (codewords[i >>> 3] >>> (7 - (i & 7))) & 1;
          i += 1;
        }
      }
    }
  }
}

function maskBit(mask, x, y) {
  switch (mask) {
    case 0: return (x + y) % 2 === 0;
    case 1: return y % 2 === 0;
    case 2: return x % 3 === 0;
    case 3: return (x + y) % 3 === 0;
    case 4: return (Math.floor(y / 2) + Math.floor(x / 3)) % 2 === 0;
    case 5: return ((x * y) % 2) + ((x * y) % 3) === 0;
    case 6: return (((x * y) % 2) + ((x * y) % 3)) % 2 === 0;
    case 7: return (((x + y) % 2) + ((x * y) % 3)) % 2 === 0;
    default: return false;
  }
}

function applyMask(modules, isFunction, mask) {
  const size = modules.length;
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      if (!isFunction[y][x] && maskBit(mask, x, y)) modules[y][x] ^= 1;
    }
  }
}

function drawFormatBits(modules, mask) {
  const size = modules.length;
  const data = (EC_LEVEL_M_BITS << 3) | mask;
  let rem = data;
  for (let i = 0; i < 10; i += 1) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
  const bits = ((data << 10) | rem) ^ 0x5412;
  const bit = (i) => ((bits >>> i) & 1);
  const set = (x, y, dark) => { modules[y][x] = dark ? 1 : 0; };
  for (let i = 0; i <= 5; i += 1) set(8, i, bit(i));
  set(8, 7, bit(6));
  set(8, 8, bit(7));
  set(7, 8, bit(8));
  for (let i = 9; i < 15; i += 1) set(14 - i, 8, bit(i));
  for (let i = 0; i < 8; i += 1) set(size - 1 - i, 8, bit(i));
  for (let i = 8; i < 15; i += 1) set(8, size - 15 + i, bit(i));
  set(8, size - 8, true);
}

function drawVersionBits(modules, version) {
  if (version < 7) return;
  const size = modules.length;
  let rem = version;
  for (let i = 0; i < 12; i += 1) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
  const bits = (version << 12) | rem;
  for (let i = 0; i < 18; i += 1) {
    const dark = ((bits >>> i) & 1) === 1;
    const a = size - 11 + (i % 3);
    const b = Math.floor(i / 3);
    modules[b][a] = dark ? 1 : 0;
    modules[a][b] = dark ? 1 : 0;
  }
}

// Standard penalty score (ISO 18004 §7.8.3) used to pick the best mask.
function penaltyScore(modules) {
  const size = modules.length;
  let result = 0;
  const countPatterns = (hist) => {
    const n = hist[1];
    const core = n > 0 && hist[2] === n && hist[3] === n * 3 && hist[4] === n && hist[5] === n;
    return (core && hist[0] >= n * 4 && hist[6] >= n ? 1 : 0)
      + (core && hist[6] >= n * 4 && hist[0] >= n ? 1 : 0);
  };
  const addHistory = (runLen, hist) => {
    let len = runLen;
    if (hist[0] === 0) len += size;
    hist.pop();
    hist.unshift(len);
  };
  const terminateAndCount = (runColor, runLen, hist) => {
    let len = runLen;
    if (runColor) { addHistory(len, hist); len = 0; }
    len += size;
    addHistory(len, hist);
    return countPatterns(hist);
  };
  for (let y = 0; y < size; y += 1) {
    let runColor = 0; let runX = 0; const hist = [0, 0, 0, 0, 0, 0, 0];
    for (let x = 0; x < size; x += 1) {
      if (modules[y][x] === runColor) {
        runX += 1;
        if (runX === 5) result += 3; else if (runX > 5) result += 1;
      } else {
        addHistory(runX, hist);
        if (!runColor) result += countPatterns(hist) * 40;
        runColor = modules[y][x];
        runX = 1;
      }
    }
    result += terminateAndCount(runColor, runX, hist) * 40;
  }
  for (let x = 0; x < size; x += 1) {
    let runColor = 0; let runY = 0; const hist = [0, 0, 0, 0, 0, 0, 0];
    for (let y = 0; y < size; y += 1) {
      if (modules[y][x] === runColor) {
        runY += 1;
        if (runY === 5) result += 3; else if (runY > 5) result += 1;
      } else {
        addHistory(runY, hist);
        if (!runColor) result += countPatterns(hist) * 40;
        runColor = modules[y][x];
        runY = 1;
      }
    }
    result += terminateAndCount(runColor, runY, hist) * 40;
  }
  for (let y = 0; y < size - 1; y += 1) {
    for (let x = 0; x < size - 1; x += 1) {
      const c = modules[y][x];
      if (c === modules[y][x + 1] && c === modules[y + 1][x] && c === modules[y + 1][x + 1]) result += 3;
    }
  }
  let dark = 0;
  for (let y = 0; y < size; y += 1) for (let x = 0; x < size; x += 1) dark += modules[y][x];
  const total = size * size;
  const k = Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1;
  result += k * 10;
  return result;
}

// Returns { size, modules (rows of 0/1), version, mask } or null when the
// text does not fit in version 10 (level M).
export function encodeQr(text) {
  const bytes = utf8Bytes(text);
  const version = pickVersion(bytes.length);
  if (!version) return null;
  const size = version * 4 + 17;
  const codewords = buildCodewords(bytes, version);
  const base = makeGrid(size, 0);
  const isFunction = makeGrid(size, false);
  drawFunctionPatterns(base, isFunction, version);
  placeData(base, isFunction, codewords);

  let best = null;
  for (let mask = 0; mask < 8; mask += 1) {
    const m = base.map((row) => row.slice());
    applyMask(m, isFunction, mask);
    drawFormatBits(m, mask);
    drawVersionBits(m, version);
    const score = penaltyScore(m);
    if (!best || score < best.score) best = { score, mask, modules: m };
  }
  return { size, modules: best.modules, version, mask: best.mask };
}

// SVG path for the dark modules (one "M x y h1 v1 h-1 z" per module).
export function qrSvgPath(modules) {
  const parts = [];
  for (let y = 0; y < modules.length; y += 1) {
    const row = modules[y];
    for (let x = 0; x < row.length; x += 1) {
      if (row[x]) parts.push(`M${x} ${y}h1v1h-1z`);
    }
  }
  return parts.join("");
}

// <QrCode value="https://…" size={240} /> — renders an inline SVG with a
// 4-module quiet zone. Falls back to a short note when the payload is too
// long for version 10.
export function QrCode({ value, size = 220, quietZone = 4, title = "", style = {} }) {
  const encoded = useMemo(() => encodeQr(value), [value]);
  if (!encoded) {
    return <div className="muted" style={{ fontSize: 12 }}>QR code unavailable (text too long).</div>;
  }
  const dim = encoded.size + quietZone * 2;
  return (
    <svg
      role="img"
      aria-label={title || `QR code for ${String(value || "")}`}
      width={size}
      height={size}
      viewBox={`0 0 ${dim} ${dim}`}
      shapeRendering="crispEdges"
      style={{ display: "block", background: "#ffffff", borderRadius: 8, ...style }}
    >
      {title ? <title>{title}</title> : null}
      <rect width={dim} height={dim} fill="#ffffff" />
      <path transform={`translate(${quietZone} ${quietZone})`} d={qrSvgPath(encoded.modules)} fill="#000000" />
    </svg>
  );
}

export default QrCode;
