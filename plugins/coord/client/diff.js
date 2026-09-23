/**
 * Unified diffs of two texts (like `diff -u`), for `coord doc patch --from`: only the change is
 * sent, and the server can merge it onto a newer revision. Myers' O((N+M)D) algorithm on lines;
 * lines split on "\n" only (a CRLF document keeps its "\r"), GNU "\ No newline at end of file".
 */
const lines = (text) => {
    const parts = text.split("\n");
    return parts.length && parts[parts.length - 1] === "" ? parts.slice(0, -1) : parts;
};
/** Shortest edit script between a and b (Myers). */
function script(a, b) {
    const n = a.length, m = b.length, max = n + m, off = max;
    const v = new Int32Array(2 * max + 2);
    const trace = [];
    let found = false;
    for (let d = 0; d <= max && !found; d++) {
        trace.push(v.slice());
        for (let k = -d; k <= d; k += 2) {
            let x = k === -d || (k !== d && v[off + k - 1] < v[off + k + 1]) ? v[off + k + 1] : v[off + k - 1] + 1;
            let y = x - k;
            while (x < n && y < m && a[x] === b[y]) {
                x++;
                y++;
            }
            v[off + k] = x;
            if (x >= n && y >= m) {
                found = true;
                break;
            }
        }
    }
    const ops = [];
    let x = n, y = m;
    for (let d = trace.length - 1; d >= 0; d--) {
        const t = trace[d], k = x - y;
        const prevK = k === -d || (k !== d && t[off + k - 1] < t[off + k + 1]) ? k + 1 : k - 1;
        const px = t[off + prevK], py = px - prevK;
        while (x > px && y > py) {
            x--;
            y--;
            ops.push({ tag: " ", a: x, b: y });
        }
        if (d > 0) {
            if (x === px) {
                y--;
                ops.push({ tag: "+", a: x, b: y });
            }
            else {
                x--;
                ops.push({ tag: "-", a: x, b: y });
            }
        }
    }
    return ops.reverse();
}
/** `diff -u` of old and new text; "" when they are equal. */
export function unifiedDiff(oldText, newText, context = 3, names = ["a", "b"]) {
    if (oldText === newText)
        return "";
    const a = lines(oldText), b = lines(newText);
    const aNoNl = oldText !== "" && !oldText.endsWith("\n"), bNoNl = newText !== "" && !newText.endsWith("\n");
    // a last line without "\n" differs from the same text with one (diff -u shows it as a change)
    const key = (xs, noNl) => (noNl ? [...xs.slice(0, -1), xs[xs.length - 1] + "\u0000"] : xs);
    const ops = script(key(a, aNoNl), key(b, bNoNl));
    const out = [`--- ${names[0]}`, `+++ ${names[1]}`];
    let i = 0;
    while (i < ops.length) {
        while (i < ops.length && ops[i].tag === " ")
            i++;
        if (i >= ops.length)
            break;
        let start = Math.max(0, i - context), end = i;
        for (;;) { // extend over changes separated by <= 2*context equal lines
            while (end < ops.length && ops[end].tag !== " ")
                end++;
            let eq = end;
            while (eq < ops.length && ops[eq].tag === " ")
                eq++;
            if (eq < ops.length && eq - end <= 2 * context) {
                end = eq;
                continue;
            }
            end = Math.min(ops.length, end + context);
            break;
        }
        const hunk = ops.slice(start, end);
        const oldN = hunk.filter((o) => o.tag !== "+").length, newN = hunk.filter((o) => o.tag !== "-").length;
        const first = hunk[0];
        const oldStart = oldN ? first.a + 1 : first.a, newStart = newN ? first.b + 1 : first.b;
        out.push(`@@ -${oldStart}${oldN === 1 ? "" : "," + oldN} +${newStart}${newN === 1 ? "" : "," + newN} @@`);
        for (const o of hunk) {
            out.push(o.tag + (o.tag === "+" ? b[o.b] : a[o.a]));
            const lastOld = o.tag !== "+" && o.a === a.length - 1 && aNoNl;
            const lastNew = o.tag !== "-" && o.b === b.length - 1 && bNoNl;
            if (o.tag === " " ? lastOld || lastNew : o.tag === "-" ? lastOld : lastNew)
                out.push("\\ No newline at end of file");
        }
        i = end;
    }
    return out.join("\n") + "\n";
}
