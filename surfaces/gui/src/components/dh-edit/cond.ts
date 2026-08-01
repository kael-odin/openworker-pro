// evalCondition — lightweight visible_if expression evaluator.
//
// Supports the three forms documented in spec/app-spec.md §4:
//   key == "value"   — userConfig[key] equals the string value
//   key != "value"   — userConfig[key] does not equal the string value
//   key               — userConfig[key] is truthy
//
// Unknown keys resolve to undefined (treated as not-equal to any value, truthy=false). Anything
// that doesn't parse (empty string) returns true (field is always visible). This is deliberately
// tiny — not a full expression engine — to match what the form editor needs.

export function evalCondition(
  expr: string | undefined,
  config: Record<string, unknown>,
): boolean {
  if (!expr || !expr.trim()) return true;
  const e = expr.trim();
  const eq = e.match(/^(\w+)\s*==\s*(.+)$/);
  if (eq) {
    const key = eq[1];
    const want = _stripQuotes(eq[2].trim());
    return String(config[key] ?? "") === String(want);
  }
  const neq = e.match(/^(\w+)\s*!=\s*(.+)$/);
  if (neq) {
    const key = neq[1];
    const want = _stripQuotes(neq[2].trim());
    return String(config[key] ?? "") !== String(want);
  }
  // bare key — truthy check
  if (/^\w+$/.test(e)) {
    return Boolean(config[e]);
  }
  // unrecognized expression — be permissive (show the field) rather than hide it
  return true;
}

function _stripQuotes(s: string): string {
  if (s.length >= 2 && ((s[0] === '"' && s[s.length - 1] === '"') || (s[0] === "'" && s[s.length - 1] === "'"))) {
    return s.slice(1, -1);
  }
  return s;
}
