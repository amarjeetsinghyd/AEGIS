import os

base_dir = r"C:\Users\Amarjeet Singh\quant-bot"
exclude_dir = "Astra_Version_1_Archive"

for root, dirs, files in os.walk(base_dir):
    if exclude_dir in root or '.git' in root or 'venv' in root:
        continue
    for file in files:
        if file.endswith(".md"):
            file_path = os.path.join(root, file)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                new_content = content.replace("RingBufferManager", "FootprintCompiler")
                new_content = new_content.replace("ring_buffer", "footprint_compiler")
                new_content = new_content.replace("ring buffer", "footprint compiler")
                new_content = new_content.replace("Ring Buffer", "Footprint Compiler")
                
                if new_content != content:
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    print(f"Updated docs in: {file_path}")
            except Exception as e:
                print(f"Error reading {file_path}: {e}")
