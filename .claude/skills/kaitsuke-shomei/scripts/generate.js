#!/usr/bin/env node
/*
 * 買付証明書（不動産購入申込書）.docx ジェネレーター
 * 使い方: node generate.js params.json [出力先.docx]
 * 設計方針: 常に A4 1ページ以内に収まるよう、余白・行間・文字サイズを圧縮。
 * params.json のキーは SKILL.md / params.sample.json を参照。
 */
const fs = require('fs');
const path = require('path');
const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
        AlignmentType, WidthType, BorderStyle, ShadingType, TabStopType,
        TabStopPosition } = require('docx');

const FONT = "Yu Mincho";
const border = { style: BorderStyle.SINGLE, size: 4, color: "888888" };
const borders = { top: border, bottom: border, left: border, right: border };
const SHADE = "DDEBF7";

// 半角英数記号 → 全角（金額・面積などを定型書式に合わせる。任意）
function toZenkaku(s) {
  if (s == null) return s;
  return String(s).replace(/[A-Za-z0-9!-/:-@\[-`{-~]/g, ch =>
    String.fromCharCode(ch.charCodeAt(0) + 0xFEE0)).replace(/ /g, '　');
}

function run(text, opts = {}) { return new TextRun({ text: String(text), font: FONT, ...opts }); }
function p(children, opts = {}) {
  return new Paragraph({ children: Array.isArray(children) ? children : [children], ...opts });
}
function leftTab(text) {
  return new Paragraph({
    children: [new TextRun({ text: "\t" + text, font: FONT, size: 20 })],
    tabStops: [{ type: TabStopType.LEFT, position: 5400 }],
    spacing: { after: 20, line: 252 },
  });
}

function kvRow(k, v, opts = {}) {
  const cellPara = (r) => new Paragraph({ children: [r], spacing: { before: 0, after: 0, line: 240 } });
  return new TableRow({ children: [
    new TableCell({ borders, width: { size: 3000, type: WidthType.DXA },
      shading: { fill: SHADE, type: ShadingType.CLEAR },
      margins: { top: 30, bottom: 30, left: 130, right: 130 },
      children: [cellPara(run(k, { bold: true, size: 20 }))] }),
    new TableCell({ borders, width: { size: 6360, type: WidthType.DXA },
      margins: { top: 30, bottom: 30, left: 130, right: 130 },
      children: [cellPara(run(v, { size: 20, bold: !!opts.bold, color: opts.color }))] }),
  ]});
}

function build(d) {
  const rowDefs = [
    ["物 件 名", d["物件名"]],
    ["所 在 地", d["所在地"]],
    ["物件種別", d["物件種別"]],
    ["土地面積", d["土地面積"]],
    ["建物面積", d["建物面積"]],
    ["築 年 月", d["築年月"]],
    ["購入希望価格", d["購入希望価格"], { bold: true, color: "C00000" }],
    ["手 付 金", d["手付金"]],
    ["残 代 金", d["残代金"]],
    ["融資特約", d["融資特約"]],
    ["引渡希望", d["引渡希望"]],
    ["有効期限", d["有効期限"]],
  ].filter(r => r[1] != null && String(r[1]).trim() !== "");

  const table = new Table({
    width: { size: 9360, type: WidthType.DXA }, columnWidths: [3000, 6360],
    rows: rowDefs.map(r => kvRow(r[0], r[1], r[2] || {})),
  });

  const notes = (d["特記事項"] && d["特記事項"].length) ? d["特記事項"] : [
    "上記期限までに融資の承認が得られないときは、本申込み及び締結後の売買契約を白紙解約とし、授受済みの金員は無利息にて返還するものとする。",
    "本書は購入の意思表示であり、売買契約の成立を約するものではありません。詳細条件は別途締結する売買契約書によるものとします。",
    "本物件は売主直売（仲介手数料不要）です。",
  ];

  const children = [
    p(run("買 付 証 明 書", { bold: true, size: 30 }),
      { alignment: AlignmentType.CENTER, spacing: { after: 120, line: 300 } }),
    new Paragraph({ children: [new TextRun({ text: "\t" + (d["作成日"] || ""), font: FONT, size: 20 })],
      tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }], spacing: { after: 120 } }),
    p(run(d["宛先"] || "御中", { size: 21, bold: true }), { spacing: { after: d["宛先担当"] ? 20 : 120 } }),
  ];
  if (d["宛先担当"]) children.push(p(run(d["宛先担当"], { size: 19 }), { spacing: { after: 120 } }));

  children.push(
    leftTab("買主（申込人）"),
    leftTab("住　所：" + (d["買主住所"] || "________________________________")),
    leftTab("氏　名：" + (d["買主氏名"] || "________________________") + "　㊞"),
    leftTab("連絡先：" + (d["連絡先"] || "") + (d["TEL"] ? "　TEL " + d["TEL"] : "")),
    p(run("　私は、下記不動産を下記条件にて購入したく、本書をもって買付の意思表示をいたします。", { size: 20 }),
      { spacing: { before: 120, after: 100, line: 252 } }),
    p(run("記", { bold: true, size: 21 }), { alignment: AlignmentType.CENTER, spacing: { after: 100 } }),
    table,
    p(run("【特記事項】", { bold: true, size: 19 }), { spacing: { before: 140, after: 40 } }),
  );
  notes.forEach(n => children.push(
    p(run("・" + n, { size: 17 }), { spacing: { after: 30, line: 234 } })));
  children.push(
    new Paragraph({ children: [new TextRun({ text: "\t以　上", font: FONT, size: 20 })],
      tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }], spacing: { before: 120 } }),
  );

  return new Document({
    styles: { default: { document: { run: { font: FONT, size: 20 } } } },
    sections: [{
      properties: { page: { size: { width: 11906, height: 16838 },
        margin: { top: 760, right: 1080, bottom: 680, left: 1080 } } },
      children,
    }],
  });
}

function main() {
  const paramsPath = process.argv[2];
  if (!paramsPath) { console.error("usage: node generate.js params.json [out.docx]"); process.exit(1); }
  const d = JSON.parse(fs.readFileSync(paramsPath, "utf-8"));
  if (d["全角変換"]) {
    ["土地面積", "建物面積", "購入希望価格", "手付金", "残代金"].forEach(k => { if (d[k]) d[k] = toZenkaku(d[k]); });
  }
  let out = process.argv[3] || d["出力ファイル名"];
  if (!out) {
    const ymd = (d["作成日"] || "").replace(/[^0-9]/g, "").slice(0, 8) || "buritsuke";
    out = `${ymd}_買付証明書.docx`;
  }
  const doc = build(d);
  Packer.toBuffer(doc).then(buf => { fs.writeFileSync(out, buf); console.log("saved:", path.resolve(out)); });
}
main();
