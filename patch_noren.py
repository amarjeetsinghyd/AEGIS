import os
import re

path = '/home/opc/.local/lib/python3.9/site-packages/NorenRestApiPy/NorenApi.py'
with open(path, 'r') as f:
    content = f.read()

# Replace the block that returns None when stat is Not_Ok
content = re.sub(
    r"if resDict\['stat'\] != 'Ok':\s*return None", 
    "# Removed faulty None return", 
    content
)

with open(path, 'w') as f:
    f.write(content)
print('Patch applied successfully.')
