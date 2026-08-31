# TAURO Nacional · NubeSDK

Extensión mínima de checkout para la aplicación pública de envíos de TAURO.
Corre dentro del Web Worker aislado de Tiendanube y personaliza únicamente la
opción creada por el Shipping Carrier de TAURO. No lee datos personales, no
manipula el DOM y no cotiza: las tarifas se resuelven exclusivamente en el
backend firmado de TAURO.

## Verificación local

```bash
npm ci
npm test
npm run typecheck
npm run build
```

El artefacto para cargar en el Portal de Partners queda en
`dist/main.min.js`. Al crear el script debe marcarse **Uses NubeSDK**.
