"""Zero-drift verifier: every function/method in the ORIGINAL 20 files must exist in the consolidated modules with an
AST identical to the original after applying ONLY the documented, mechanical transforms. Anything else must be listed in
INTENDED (and is then reported, not hidden).

    python -m aegis_v1.tools.verify_zero_drift --orig /path/to/original/files --new /path/to/aegis_v1

Independent of the build codemod: transforms are re-implemented here on the AST (not on source text)."""
from __future__ import annotations
import argparse, ast, copy, os, sys

# original file -> new module that now contains it
HOME = {"price_action_engine": "pillars", "structural_registry": "structure", "dow_macro_shields": "structure",
        "option_order_flow": "flow", "wyckoff_volume_engine": "flow", "aegis_pipeline_orchestrator": "pipeline",
        "aegis_execution_manager": "execution", "dump_detector": "execution", "tick_compiler": "feed", "bar_book": "feed",
        "data_fetcher": "feed", "option_quality": "infra", "instrument_repository": "infra", "instrument_registry": "infra",
        "shadow_boundary_observer": "infra", "l2_options_archiver": "infra", "hot_structures": "infra", "l2_guard": "infra",
        "instrument_repository_1": "infra", "aegis_main": "main"}
LOGGER_RENAME = {"bar_book": "_log_bars", "tick_compiler": "_log_tick", "data_fetcher": "_log_fetch", "instrument_repository": "_log_repo",
                 "instrument_repository_1": "_log_repo", "l2_options_archiver": "_log_l2", "shadow_boundary_observer": "_log_shadow", "wyckoff_volume_engine": "_wyckoff_log"}
MIXINS = {"structural_registry": "StructuralRegistryMixin", "dow_macro_shields": "DowMacroShieldsMixin", "option_order_flow": "OptionOrderFlowMixin"}
# deliberate, reviewed behavioural-preserving rewrites (hot path / import isolation); everything else must be byte-equivalent
INTENDED = {"TickCompiler.on_message", "TickCompiler.__init__", "BarBook.__init__", "BarBook.on_tick", "BarBook.force_close_forming", "DataFetcher.__init__"}
# the two InstrumentRepository copies diverged; the unified class keeps the primary's bodies (see report)
DIVERGED_OK = {("instrument_repository_1", "InstrumentRepository.get_weekly_option_tokens"), ("instrument_repository_1", "InstrumentRepository._init_db")}

def dump(n): return ast.dump(n, annotate_fields=False, include_attributes=False)

class Norm(ast.NodeTransformer):
    def __init__(self, mode, funcs, static, logger=None, selfname="eng"):
        self.mode, self.funcs, self.static, self.logger, self.selfname = mode, funcs, static, logger, selfname
    def visit_Name(self, n):
        if self.logger and n.id == "logger": return ast.copy_location(ast.Name(self.logger, n.ctx), n)
        if self.mode == "mixin" and n.id == "self": return ast.copy_location(ast.Name("eng", n.ctx), n)
        return n
    def visit_arg(self, n):
        if self.mode == "mixin" and n.arg == "self": n.arg = "eng"
        return n
    def visit_Call(self, c):
        self.generic_visit(c)
        f = c.func
        if self.mode in ("mixin", "pae") and isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in ("self", "eng") and f.attr in self.funcs:
            args = list(c.args) if f.attr in self.static else [ast.Name("eng" if self.mode == "mixin" else "self", ast.Load())] + list(c.args)
            return ast.copy_location(ast.Call(ast.Name(f.attr, ast.Load()), args, c.keywords), c)
        return c

def index(tree):
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)): out[n.name] = n
        elif isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)): out[f"{n.name}.{m.name}"] = m
    return out

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--orig", required=True); ap.add_argument("--new", required=True); a = ap.parse_args()
    orig = {f[:-3]: ast.parse(open(os.path.join(a.orig, f), encoding="utf-8").read()) for f in os.listdir(a.orig) if f.endswith(".py") and f[:-3] in HOME}
    new = {m: index(ast.parse(open(os.path.join(a.new, m + ".py"), encoding="utf-8").read())) for m in set(HOME.values())}
    mixfn, static = set(), set()
    for f, cname in MIXINS.items():
        for n in orig[f].body:
            if isinstance(n, ast.ClassDef) and n.name == cname:
                for m in n.body:
                    if isinstance(m, ast.FunctionDef):
                        mixfn.add(m.name)
                        if any(ast.unparse(d) == "staticmethod" for d in m.decorator_list): static.add(m.name)
    tot = same = 0; intended, missing, mismatched, diverged = [], [], [], []
    for f, tree in sorted(orig.items()):
        home = new[HOME[f]]
        for q, node in index(tree).items():
            tot += 1; cls = q.split(".")[0]; fn = q.split(".")[-1]
            in_mixin = "." in q and cls == MIXINS.get(f)
            tgt_name = fn if in_mixin else q
            tgt = home.get(tgt_name)
            if f == "instrument_repository_1" and tgt is None: tgt = home.get(tgt_name)
            if tgt is None: missing.append((f, q)); continue
            if in_mixin: mode = "mixin"
            elif f == "price_action_engine" and cls == "PriceActionEngine": mode = "pae"
            else: mode = "plain"
            o = Norm(mode, mixfn, static, LOGGER_RENAME.get(f)).visit(copy.deepcopy(node))
            if in_mixin and fn in static: o.decorator_list = []
            nn = copy.deepcopy(tgt)
            if dump(o) == dump(nn): same += 1
            elif q in INTENDED: intended.append((f, q))
            elif (f, q) in DIVERGED_OK: diverged.append((f, q))
            else: mismatched.append((f, q))
    print(f"functions/methods in the 20 originals : {tot}")
    print(f"  AST-identical after mechanical norm : {same}")
    print(f"  intentionally rewritten (reviewed)  : {len(intended)}  {sorted(q for _, q in intended)}")
    print(f"  diverged duplicate (repo copy _1)   : {len(diverged)}  {sorted(q for _, q in diverged)}")
    print(f"  MISSING in consolidated modules     : {len(missing)}  {missing}")
    print(f"  UNEXPLAINED MISMATCH                : {len(mismatched)}  {mismatched}")
    sys.exit(1 if (missing or mismatched) else 0)

if __name__ == "__main__":
    main()
