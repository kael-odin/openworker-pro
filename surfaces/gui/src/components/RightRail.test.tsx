import { describe, expect, it } from "vitest";
import JSZip from "jszip";

import { parseXlsx, sandboxedSrcDoc } from "./RightRail";

// P0-01: HTML artifacts render in a bare sandbox with an injected restrictive CSP so that
// untrusted artifact content cannot run scripts, read the parent window's globals (sidecar
// token), or beacon out to the sidecar / external hosts — even as a static document.
describe("sandboxedSrcDoc (P0-01 artifact isolation)", () => {
  it("injects a restrictive CSP meta that blocks all network fetches", () => {
    const doc = sandboxedSrcDoc("<p>hi</p>");
    // CSP present and denies by default.
    expect(doc).toMatch(/Content-Security-Policy/);
    expect(doc).toMatch(/default-src 'none'/);
    // No connect/fetch to the sidecar or anywhere — connect-src is absent (inherits 'none').
    expect(doc).not.toMatch(/connect-src/);
    // Only inline styles and data:/blob: images are let through.
    expect(doc).toMatch(/style-src 'unsafe-inline'/);
    expect(doc).toMatch(/img-src data: blob:/);
    // Forms and base/navigation locked down.
    expect(doc).toMatch(/form-action 'none'/);
    expect(doc).toMatch(/base-uri 'none'/);
  });

  it("wraps a bare fragment in a full document with the CSP in <head>", () => {
    const doc = sandboxedSrcDoc("<p>hello</p>");
    expect(doc).toMatch(/^<!DOCTYPE html><html><head>/);
    expect(doc).toContain("<p>hello</p>");
  });

  it("injects the CSP into an existing <head> without duplicating the document", () => {
    const html = "<html><head><title>t</title></head><body><p>x</p></body></html>";
    const doc = sandboxedSrcDoc(html);
    // Still exactly one <head> and one <title>.
    expect(doc.match(/<head/gi)?.length).toBe(1);
    expect(doc.match(/<title/gi)?.length).toBe(1);
    expect(doc).toContain("<p>x</p>");
    expect(doc).toMatch(/Content-Security-Policy/);
  });

  it("injects into an <html>-only document that has no <head>", () => {
    const html = "<html><body><p>no head</p></body></html>";
    const doc = sandboxedSrcDoc(html);
    expect(doc).toMatch(/<head>.*Content-Security-Policy.*<\/head>/s);
    expect(doc).toContain("<p>no head</p>");
  });

  it("preserves the original body content intact (no escaping of the artifact)", () => {
    const body = '<div class="x" data-n="1">值 & <span>"quoted"</span></div>';
    const doc = sandboxedSrcDoc(body);
    expect(doc).toContain(body);
  });

  it("handles empty input as an empty static document", () => {
    const doc = sandboxedSrcDoc("");
    expect(doc).toMatch(/Content-Security-Policy/);
    expect(doc).not.toMatch(/<script/i);
  });
});

// Build a minimal-but-valid .xlsx (OOXML zip) in memory, then parse it back — verifies the
// SheetViewer's replacement for the vulnerable SheetJS dep reads sheet names, shared strings,
// inline strings, numbers, booleans and sparse cells without a binary fixture checked into git.
async function buildXlsx(
  sheets: { name: string; cells: { ref: string; t?: string; v?: string; inline?: string }[] }[],
): Promise<string> {
  const zip = new JSZip();
  // shared strings table — collect every t="s" value across sheets, dedup, and remember each
  // cell's index so the worksheet <v> holds the index (real OOXML), not the literal string.
  const stringIndex = new Map<string, number>();
  const strings: string[] = [];
  for (const s of sheets) {
    for (const c of s.cells) {
      if (c.t === "s" && c.v != null && !stringIndex.has(c.v)) {
        stringIndex.set(c.v, strings.length);
        strings.push(c.v);
      }
    }
  }
  const sst =
    strings.length > 0
      ? `<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="${strings.length}" uniqueCount="${strings.length}">` +
        strings.map((s) => `<si><t>${s}</t></si>`).join("") +
        `</sst>`
      : "";
  if (sst) zip.file("xl/sharedStrings.xml", sst);

  const sheetFiles: { name: string; rid: string }[] = [];
  sheets.forEach((s, i) => {
    const rid = `rId${i + 1}`;
    const path = `xl/worksheets/sheet${i + 1}.xml`;
    const body = s.cells
      .map((c) => {
        let inner = "";
        if (c.t === "inlineStr") inner = `<is><t>${c.inline ?? ""}</t></is>`;
        else if (c.t === "s" && c.v != null) inner = `<v>${stringIndex.get(c.v)}</v>`;
        else if (c.v != null) inner = `<v>${c.v}</v>`;
        const t = c.t ? ` t="${c.t}"` : "";
        return `<c r="${c.ref}"${t}>${inner}</c>`;
      })
      .join("");
    zip.file(
      path,
      `<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheetData>${body}</sheetData></worksheet>`,
    );
    sheetFiles.push({ name: s.name, rid });
  });

  zip.file(
    "xl/workbook.xml",
    `<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>` +
      sheetFiles.map((s) => `<sheet name="${s.name}" sheetId="1" r:id="${s.rid}"/>`).join("") +
      `</sheets></workbook>`,
  );
  zip.file(
    "xl/_rels/workbook.xml.rels",
    `<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
      sheetFiles
        .map((s, i) => `<Relationship Id="${s.rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet${i + 1}.xml"/>`)
        .join("") +
      `</Relationships>`,
  );
  zip.file(
    "[Content_Types].xml",
    `<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>` +
      sheetFiles
        .map(
          (_, i) =>
            `<Override PartName="/xl/worksheets/sheet${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`,
        )
        .join("") +
      (sst ? `<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>` : "") +
      `</Types>`,
  );

  const buf = await zip.generateAsync({ type: "base64" });
  return buf;
}

describe("parseXlsx (CVE-free SheetViewer backend)", () => {
  it("reads sheet names, shared strings, numbers, booleans and inline strings", async () => {
    const base64 = await buildXlsx([
      {
        name: "Sheet One",
        cells: [
          { ref: "A1", t: "s", v: "Name" }, // shared string "Name"
          { ref: "B1", t: "s", v: "Score" }, // shared string "Score"
          { ref: "A2", t: "s", v: "Alice" }, // shared string "Alice"
          { ref: "B2", v: "42" }, // number
          { ref: "C2", t: "b", v: "1" }, // boolean true
          { ref: "D2", t: "inlineStr", inline: "note" }, // inline string
        ],
      },
    ]);
    const out = await parseXlsx(base64);
    expect(out).toHaveLength(1);
    expect(out[0].name).toBe("Sheet One");
    const rows = out[0].rows;
    // header row is padded to the sheet's max column width (D), like SheetJS header:1+defval:""
    expect(rows[0]).toEqual(["Name", "Score", "", ""]);
    // data row: shared string, number, boolean true, inline string
    expect(rows[1]).toEqual(["Alice", 42, true, "note"]);
  });

  it("handles multiple sheets and sparse/gap cells with empty fill", async () => {
    const base64 = await buildXlsx([
      { name: "First", cells: [{ ref: "A1", t: "s", v: "hi" }] },
      { name: "Second", cells: [{ ref: "C3", v: "7" }] }, // gap at B3 and the whole first two rows
    ]);
    const out = await parseXlsx(base64);
    expect(out.map((s) => s.name)).toEqual(["First", "Second"]);
    expect(out[0].rows).toEqual([["hi"]]);
    // C3 placed at [col2]; the all-empty first two rows are dropped by the trailing filter, so
    // the lone data row is the only one left, normalized to the sheet's max column width.
    const second = out[1].rows;
    expect(second).toEqual([["", "", 7]]);
  });

  it("returns empty rows for a sheet with no cells", async () => {
    const base64 = await buildXlsx([{ name: "Empty", cells: [] }]);
    const out = await parseXlsx(base64);
    expect(out[0].name).toBe("Empty");
    expect(out[0].rows).toEqual([]);
  });
});
