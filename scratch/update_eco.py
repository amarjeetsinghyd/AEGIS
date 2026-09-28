with open('/home/opc/ecosystem.config.js') as f:
    c = f.read()
if 'AEGIS_OPTION_QUALITY_MODE' not in c:
    c = c.replace('AEGIS_QUALITY_STACK_MODE: "SHADOW",', 'AEGIS_QUALITY_STACK_MODE: "SHADOW",\n        AEGIS_OPTION_QUALITY_MODE: "SHADOW",')
    with open('/home/opc/ecosystem.config.js', 'w') as f:
        f.write(c)
    print('Updated ecosystem.config.js with OPTION_QUALITY_MODE: SHADOW')
else:
    print('Already present')
