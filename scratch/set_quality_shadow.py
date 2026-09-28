content = open('/home/opc/ecosystem.config.js').read()

changes = {
    'AEGIS_QUALITY_STACK_MODE: "ACTIVE"': 'AEGIS_QUALITY_STACK_MODE: "SHADOW"',
}

for old, new in changes.items():
    if old in content:
        content = content.replace(old, new)
        print(f"Changed: {old} -> {new}")
    else:
        print(f"NOT FOUND: {old}")

open('/home/opc/ecosystem.config.js', 'w').write(content)

print("\nVerification:")
for line in open('/home/opc/ecosystem.config.js'):
    if any(k in line for k in ['QUALITY_STACK', 'ML_GATEKEEPER', 'OPTION_QUALITY']):
        print(" ", line.strip())
