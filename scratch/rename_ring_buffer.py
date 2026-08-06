import os
import shutil

base_dir = r"C:\Users\Amarjeet Singh\quant-bot"
exclude_dir = "Astra_Version_1_Archive"

for root, dirs, files in os.walk(base_dir):
    if exclude_dir in root:
        continue
    for file in files:
        if file.endswith(".py"):
            file_path = os.path.join(root, file)
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                
            new_content = content.replace("FootprintCompiler", "FootprintCompiler")
            new_content = new_content.replace("footprint_compiler", "footprint_compiler")
            
            if new_content != content:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"Updated content in: {file_path}")

# Now rename any files named footprint_compiler.py to footprint_compiler.py
for root, dirs, files in os.walk(base_dir):
    if exclude_dir in root:
        continue
    for file in files:
        if file == "footprint_compiler.py":
            old_path = os.path.join(root, file)
            new_path = os.path.join(root, "footprint_compiler.py")
            os.rename(old_path, new_path)
            print(f"Renamed file: {old_path} -> {new_path}")
