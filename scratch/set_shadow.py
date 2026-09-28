content = open('/home/opc/ecosystem.config.js').read()
old = 'AEGIS_ML_GATEKEEPER_MODE: "ACTIVE"'
new = 'AEGIS_ML_GATEKEEPER_MODE: "SHADOW"'
if old in content:
    content = content.replace(old, new)
    open('/home/opc/ecosystem.config.js', 'w').write(content)
    print("Changed ACTIVE -> SHADOW")
else:
    print("ERROR: pattern not found. Current line:")
    for line in content.split('\n'):
        if 'ML_GATEKEEPER' in line:
            print(repr(line))

# Verify
for line in open('/home/opc/ecosystem.config.js'):
    if 'ML_GATEKEEPER' in line:
        print("Verified:", line.strip())
