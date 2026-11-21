#!/usr/bin/env python3
"""
A6xx-low (A610/A619) fix for ir3: make sure `load_subgroup_invocation`
(SubgroupLocalInvocationId) never reaches the backend in non-compute shaders
when the subgroup size is 1, so the backend does not emit an unsupported
`getfiberid`.  Replaces it with the constant 0.

Run from the root of a Mesa checkout:   python3 fix_a619_subgroups.py

Env:
  A6XX_SUBGROUP_MODE = auto      (default) gate on the subgroup_size used by the
                                 nir_lower_subgroups call; if that size is a
                                 constant != 1 the gate would never fire, so the
                                 script falls back to `graphics` and says so.
                     = graphics  no gate: every non-compute shader (test build)

On any mismatch the script prints diagnostics and exits non-zero, so the build
fails loudly instead of silently producing an unpatched driver.
"""
import os
import pathlib
import re
import sys

PATH = pathlib.Path("src/freedreno/ir3/ir3_nir.c")
MARKER = "ir3_nir_lower_subgroup_id_zero"
MODE = os.environ.get("A6XX_SUBGROUP_MODE", "auto").strip().lower()


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    show_context()
    sys.exit(code)


def show_context():
    try:
        lines = PATH.read_text().splitlines()
    except Exception:
        return
    hits = [(i + 1, l) for i, l in enumerate(lines) if re.search(r"subgroup", l, re.I)]
    print("--- lines mentioning 'subgroup' in %s (first 60):" % PATH, file=sys.stderr)
    for n, l in hits[:60]:
        print(f"{n:5d}: {l}", file=sys.stderr)


def match_paren(text, open_idx):
    """index of the ')' matching text[open_idx] == '('"""
    depth = 0
    i = open_idx
    while i < len(text):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def split_args(s):
    args, depth, cur = [], 0, []
    for c in s:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    if cur:
        args.append("".join(cur).strip())
    return args


if not PATH.exists():
    die(f"{PATH} not found (run from the Mesa root)")

text = PATH.read_text()
if MARKER in text:
    print("Already patched, nothing to do.")
    sys.exit(0)

# ---- locate the nir_lower_subgroups invocation -------------------------------
call_re = re.compile(r"\b(OPT_V|OPT|NIR_PASS_V|NIR_PASS)\s*\(|\bnir_lower_subgroups\s*\(")
found = None
for m in call_re.finditer(text):
    if m.group(0).startswith("nir_lower_subgroups"):
        open_idx = m.end() - 1
        close = match_paren(text, open_idx)
        if close < 0:
            continue
        args = split_args(text[open_idx + 1:close])
        found = dict(style="bare", macro=None, shader=args[0], opts=args[1] if len(args) > 1 else None,
                     start=m.start(), close=close, progress=None)
        break
    open_idx = m.end() - 1
    close = match_paren(text, open_idx)
    if close < 0:
        continue
    args = split_args(text[open_idx + 1:close])
    macro = m.group(1)
    if macro == "NIR_PASS":
        # NIR_PASS(progress, shader, pass, opts...)
        if len(args) >= 3 and args[2] == "nir_lower_subgroups":
            found = dict(style="NIR_PASS", macro=macro, progress=args[0], shader=args[1],
                         opts=args[3] if len(args) > 3 else None, start=m.start(), close=close)
            break
    else:
        # OPT / OPT_V / NIR_PASS_V (shader, pass, opts...)
        if len(args) >= 2 and args[1] == "nir_lower_subgroups":
            found = dict(style=macro, macro=macro, progress=None, shader=args[0],
                         opts=args[2] if len(args) > 2 else None, start=m.start(), close=close)
            break

if not found:
    die("nir_lower_subgroups call not found in ir3_nir.c")

# end of statement
semi = text.find(";", found["close"])
if semi < 0:
    die("could not find end of the nir_lower_subgroups statement")
stmt_end = semi + 1

shader = found["shader"]
print(f"Found nir_lower_subgroups call (style={found['style']}, shader var='{shader}')")

# ---- decide the gate ---------------------------------------------------------
gate = None  # C expression for the subgroup size, or None for no gate
if MODE == "graphics":
    print("Mode: graphics (no gate)")
else:
    opts = found["opts"]
    expr = None
    if opts:
        if opts.startswith("&") and re.fullmatch(r"&\s*[A-Za-z_]\w*", opts):
            expr = opts.lstrip("&").strip() + ".subgroup_size"
        elif re.fullmatch(r"[A-Za-z_]\w*", opts):
            expr = opts + "->subgroup_size"
        else:
            sm = re.search(r"\.subgroup_size\s*=\s*([^,}\n]+)", opts)
            if sm:
                expr = sm.group(1).strip()
    if not expr:
        die("could not work out the subgroup_size expression used by nir_lower_subgroups; "
            "rerun with A6XX_SUBGROUP_MODE=graphics for the test build, or send me this output", 2)

    # try to resolve the actual literal value (inline, or from the named options struct)
    literal = None
    if re.fullmatch(r"\d+", expr):
        literal = int(expr)
    elif opts and re.fullmatch(r"&?\s*[A-Za-z_]\w*", opts):
        name = opts.lstrip("&").strip()
        im = re.search(r"\b" + re.escape(name) + r"\b\s*=\s*\{(.*?)\};", text, re.S)
        if im:
            vm = re.search(r"\.subgroup_size\s*=\s*(\d+)\s*[,}\n]", im.group(1) + "\n")
            if vm:
                literal = int(vm.group(1))

    if literal is not None and literal != 1:
        print(f"NOTE: subgroup_size here is the constant {literal}, a '== 1' gate would never fire "
              f"-> falling back to graphics mode (no gate).")
        gate = None
    else:
        gate = expr
        print(f"Mode: auto, gate = ({gate}) == 1")

# ---- helper pass -------------------------------------------------------------
helper = """
static bool
ir3_nir_lower_subgroup_id_zero_instr(nir_builder *b, nir_intrinsic_instr *intrin,
                                     void *data)
{
   if (intrin->intrinsic != nir_intrinsic_load_subgroup_invocation)
      return false;

   b->cursor = nir_before_instr(&intrin->instr);
   nir_def_rewrite_uses(&intrin->def, nir_imm_intN_t(b, 0, intrin->def.bit_size));
   nir_instr_remove(&intrin->instr);
   return true;
}

static bool
ir3_nir_lower_subgroup_id_zero(nir_shader *s)
{
   return nir_shader_intrinsics_pass(s, ir3_nir_lower_subgroup_id_zero_instr,
                                     nir_metadata_control_flow, NULL);
}
"""

# ---- build the call ----------------------------------------------------------
cond = f"{shader}->info.stage != MESA_SHADER_COMPUTE"
if gate:
    cond = f"({gate}) == 1 && {cond}"

style = found["style"]
if style in ("OPT", "OPT_V", "NIR_PASS_V"):
    call = f"{style}({shader}, ir3_nir_lower_subgroup_id_zero);"
elif style == "NIR_PASS":
    call = f"NIR_PASS({found['progress']}, {shader}, ir3_nir_lower_subgroup_id_zero);"
else:
    call = f"ir3_nir_lower_subgroup_id_zero({shader});"

inject = (f"\n\n   /* A6xx-low: never leave SubgroupLocalInvocationId for the backend\n"
          f"    * (it would emit an unsupported getfiberid). */\n"
          f"   if ({cond}) {{\n      {call}\n   }}\n")

# ---- apply (call first, then helper, so offsets stay valid) ------------------
text = text[:stmt_end] + inject + text[stmt_end:]

# helper goes right after the last #include of the file header
inc = list(re.finditer(r"^[ \t]*#[ \t]*include[^\n]*\n", text, re.M))
if not inc:
    die("no #include found in ir3_nir.c")
pos = inc[-1].end()
extra = ""
if "nir_builder.h" not in text:
    extra = '#include "compiler/nir/nir_builder.h"\n'
text = text[:pos] + extra + helper + text[pos:]

PATH.write_text(text)
print("Successfully patched ir3_nir.c (non-compute shaders, subgroup size 1)")
