import os
import difflib

def get_diff(file1_path, file2_path):
    with open(file1_path, 'r', encoding='utf-8') as f1:
        f1_text = f1.readlines()
    with open(file2_path, 'r', encoding='utf-8') as f2:
        f2_text = f2.readlines()
        
    diff = list(difflib.unified_diff(f1_text, f2_text, fromfile=file1_path, tofile=file2_path))
    return "".join(diff)

base_path = r"C:\Users\Amarjeet Singh\quant-bot\astra_bot"
gemini_path = os.path.join(base_path, "Gemini Code Improvement Suggestion")

deployed_micro = os.path.join(base_path, "core", "microstructure_engine.py")
gemini_micro_1 = os.path.join(gemini_path, "gemini-code-1785910333925.py")
gemini_micro_2 = os.path.join(gemini_path, "gemini-code-1785910396153.py")

deployed_ring = os.path.join(base_path, "footprint_compiler.py")
gemini_ring = os.path.join(gemini_path, "gemini-code-1785910348751.py")

print("=== DIFF: deployed microstructure vs gemini micro 1 ===")
print(get_diff(deployed_micro, gemini_micro_1)[:2000] + "\n... (truncated if too long)")

print("\n=== DIFF: deployed microstructure vs gemini micro 2 ===")
print(get_diff(deployed_micro, gemini_micro_2)[:2000] + "\n... (truncated if too long)")

print("\n=== DIFF: deployed footprint_compiler vs gemini footprint_compiler ===")
print(get_diff(deployed_ring, gemini_ring)[:2000] + "\n... (truncated if too long)")
