import pathlib
import re
import sys

path = pathlib.Path("src/freedreno/ir3/ir3_nir.c")
text = path.read_text()

pattern = re.compile(r'(OPT\s*\(\s*([a-zA-Z0-9_]+)\s*,\s*nir_lower_subgroups\s*,[^;]+;\s*)')
match = pattern.search(text)

if not match:
    pattern = re.compile(r'(nir_lower_subgroups\s*\(\s*([a-zA-Z0-9_]+)\s*,[^;]+;\s*)')
    match = pattern.search(text)

if not match:
    print("ERROR: nir_lower_subgroups call not found in ir3_nir.c", file=sys.stderr)
    sys.exit(1)

shader_var = match.group(2)

helper_func = """
static bool
ir3_nir_lower_subgroup_id_zero_instr(nir_builder *b, nir_intrinsic_instr *intrin, void *data)
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

fn_pattern = re.compile(r'^[a-zA-Z0-9_*\s]+\bir3_nir_post_finalize\s*\(', re.MULTILINE)
fn_match = fn_pattern.search(text)
if not fn_match:
    fn_pattern = re.compile(r'^[a-zA-Z0-9_*\s]+\bir3_optimize_nir\s*\(', re.MULTILINE)
    fn_match = fn_pattern.search(text)

if not fn_match:
    print("ERROR: target insertion function not found", file=sys.stderr)
    sys.exit(1)

insert_pos = fn_match.start()
text = text[:insert_pos] + helper_func + "\n" + text[insert_pos:]

match = pattern.search(text)
inject_call = f"""
   if (subgroup_size == 1 && !mesa_shader_stage_is_compute({shader_var}->info.stage)) {{
      OPT({shader_var}, ir3_nir_lower_subgroup_id_zero);
   }}
"""

text = text[:match.end()] + inject_call + text[match.end():]
path.write_text(text)
print("Successfully patched ir3_nir.c for subgroup_size == 1")
