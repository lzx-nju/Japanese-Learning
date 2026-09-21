// 罗马字 → 假名 实时转换：英文键盘直接打 norikae → のりかえ，无需日文输入法。
// 规则（与主流 IME 一致的最小集）：
//   - 相邻匹配取最长（3 → 2 → 1 字符）
//   - n + 辅音 → ん（genki → げんき；konya → かにゃ，n 先收再转 nya）
//   - nn 后跟元音 → 只消费一个 n（konnichiwa → こんにちわ；注意 wa→わ，
//     「こんにちは」这类助词 は 的例外需自行用日文输入法打）
//   - 双辅音 → 促音 っ（gakkou → がっこう）
//   - "-" → 长音 ー；xtu/ltu → っ；n' → ん
//   - 外来语特殊音与主流 IME 的扩展罗马字一致：fa/fi/fe/fo → ふぁ行、
//     thi/dhi → てぃ/でぃ、she/che/je → しぇ/ちぇ/じぇ（ミーティング
//     打 mithingu → みてぃんぐ；ti 仍是 ち，这是 IME 惯例不是 bug）
//   - 未匹配的字符原样保留，可直接输入或粘贴假名，两者互不影响
const ROMA_TO_KANA = {
  a: "あ", i: "い", u: "う", e: "え", o: "お",
  ka: "か", ki: "き", ku: "く", ke: "け", ko: "こ",
  ga: "が", gi: "ぎ", gu: "ぐ", ge: "げ", go: "ご",
  sa: "さ", shi: "し", si: "し", su: "す", se: "せ", so: "そ",
  za: "ざ", ji: "じ", zi: "じ", zu: "ず", ze: "ぜ", zo: "ぞ",
  ta: "た", chi: "ち", ti: "ち", tsu: "つ", tu: "つ", te: "て", to: "と",
  da: "だ", di: "ぢ", du: "づ", de: "で", do: "ど",
  na: "な", ni: "に", nu: "ぬ", ne: "ね", no: "の",
  ha: "は", hi: "ひ", fu: "ふ", hu: "ふ", he: "へ", ho: "ほ",
  ba: "ば", bi: "び", bu: "ぶ", be: "べ", bo: "ぼ",
  pa: "ぱ", pi: "ぴ", pu: "ぷ", pe: "ぺ", po: "ぽ",
  ma: "ま", mi: "み", mu: "む", me: "め", mo: "も",
  ya: "や", yu: "ゆ", yo: "よ",
  ra: "ら", ri: "り", ru: "る", re: "れ", ro: "ろ",
  wa: "わ", wi: "ゐ", we: "ゑ", wo: "を",
  "n'": "ん",
  kya: "きゃ", kyu: "きゅ", kyo: "きょ",
  gya: "ぎゃ", gyu: "ぎゅ", gyo: "ぎょ",
  sha: "しゃ", shu: "しゅ", sho: "しょ",
  sya: "しゃ", syu: "しゅ", syo: "しょ",
  ja: "じゃ", ju: "じゅ", jo: "じょ",
  jya: "じゃ", jyu: "じゅ", jyo: "じょ",
  cha: "ちゃ", chu: "ちゅ", cho: "ちょ",
  cya: "ちゃ", cyu: "ちゅ", cyo: "ちょ",
  nya: "にゃ", nyu: "にゅ", nyo: "にょ",
  hya: "ひゃ", hyu: "ひゅ", hyo: "ひょ",
  bya: "びゃ", byu: "びゅ", byo: "びょ",
  pya: "ぴゃ", pyu: "ぴゅ", pyo: "ぴょ",
  mya: "みゃ", myu: "みゅ", myo: "みょ",
  rya: "りゃ", ryu: "りゅ", ryo: "りょ",
  xtu: "っ", ltu: "っ", xtsu: "っ", ltsu: "っ",
  xa: "ぁ", xi: "ぃ", xu: "ぅ", xe: "ぇ", xo: "ぉ",
  xya: "ゃ", xyu: "ゅ", xyo: "ょ",
  // 外来语特殊音（片假名外来语常见，如 ファンタジー/ミーティング/カフェ）。
  // 与主流 IME 的扩展罗马字一致：fa 系打 f+元音、ティ/ディ 打 thi/dhi、
  // シェ/チェ/ジェ 打 she/che/je、ツァ 系打 tsa 等。ti 保持 ち（IME 惯例）。
  fa: "ふぁ", fi: "ふぃ", fe: "ふぇ", fo: "ふぉ",
  va: "ゔぁ", vi: "ゔぃ", vu: "ゔ", ve: "ゔぇ", vo: "ゔぉ",
  thi: "てぃ", dhi: "でぃ",
  she: "しぇ", che: "ちぇ", je: "じぇ",
  tsa: "つぁ", tsi: "つぃ", tse: "つぇ", tso: "つぉ",
  twu: "とぅ", dwu: "どぅ",
  kwa: "くぁ", kwi: "くぃ", kwe: "くぇ", kwo: "くぉ",
};

function romajiToKana(input, finalize) {
  if (!input) return "";
  const s = String(input).toLowerCase();
  const VOWELS = "aeiou";
  const CONSONANTS = "bcdfghjklmnpqrstvwxyz";
  let out = "";
  let i = 0;
  while (i < s.length) {
    let matched = false;
    for (let len = 3; len >= 1; len--) {
      const chunk = s.slice(i, i + len);
      if (ROMA_TO_KANA[chunk] !== undefined) {
        out += ROMA_TO_KANA[chunk];
        i += len;
        matched = true;
        break;
      }
    }
    if (matched) continue;
    const c = s[i];
    const next = i + 1 < s.length ? s[i + 1] : "";
    const next2 = i + 2 < s.length ? s[i + 2] : "";
    if (c === "n") {
      if (next === "n") {
        // nn+元音：只消费一个 n，第二个 n 与元音组成下一音节（konnichiwa → こんにちわ）
        // 否则两个 n 合成 ん（sann → さん、konn → こん）
        if (next2 && VOWELS.includes(next2)) {
          out += "ん"; i += 1;
        } else {
          out += "ん"; i += 2;
        }
        continue;
      }
      // n+辅音（不含 y）：n 单独成 ん（genki → げんき）。
      // y 除外：ny… 是拗音，"nya/nyu/nyo" 已在映射表里由最长匹配处理
      //（konya → こにゃ，与主流 IME 一致；んや 要打 kon'ya）
      if (next && CONSONANTS.includes(next) && next !== "y") {
        out += "ん"; i += 1; continue;
      }
      // 后跟元音/y/结尾：交给映射或保持等待（san 提交时收为 さん）
      out += c; i += 1; continue;
    }
    if (CONSONANTS.includes(c) && next === c) {
      out += "っ"; i += 1; continue;  // 双辅音促音（gakkou → がっこう）
    }
    if (c === "-") {
      out += "ー"; i += 1; continue;
    }
    out += c;  // 未匹配字符原样保留
    i += 1;
  }
  // 提交时收尾：结尾悬着的单 n 定为 ん（与 IME 回车确认为 ん 的行为一致）
  if (finalize && out.endsWith("n")) {
    out = out.slice(0, -1) + "ん";
  }
  return out;
}
