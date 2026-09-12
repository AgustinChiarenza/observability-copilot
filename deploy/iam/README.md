# Política IAM para el usuario del copiloto en Huawei Cloud

Sólo hace falta si el cliente enchufa Cloud Eye, BSS o SMN. Con Prometheus y un
webhook no hay credenciales de nube y este directorio no aplica.

El copiloto **no escribe nada** en la nube: lee métricas y alarmas (CES), lee
facturación (BSS) y publica en **un** topic de SMN. Esta política es exactamente
eso y nada más. Si el cliente tiene una política propia de sólo lectura, mejor:
que use la suya y le agregue `smn:topic:publish`.

## Cómo aplicarla

1. IAM → Usuarios → crear un usuario **programático** (sin consola), p.ej. `copilot`.
2. IAM → Permisos → Políticas → crear una política **personalizada** en modo
   JSON con [`huawei-readonly-policy.json`](huawei-readonly-policy.json).
3. Crear un grupo, asignarle la política sobre el proyecto de la región donde
   están los recursos, y meter al usuario en el grupo.
4. Generar un AK/SK para el usuario. Va **directo al Secret de Kubernetes**
   (`HW_AK`, `HW_SK`), no a un YAML ni a un chat.

Para acotar el `publish` a un solo topic, agregar al statement de SMN:

```json
"Resource": ["smn:*:*:topic:urn:smn:la-south-2:XXXX:copilot-avisos"]
```

## Cómo verificar que alcanza y que no sobra

```bash
kubectl -n <ns> exec deploy/<release>-observability-copilot -- python -m copilot preflight
```

`preflight` toca cada puerto con las credenciales reales: lista métricas en
CES, pide un período de facturación en BSS y lista las suscripciones del topic.
Si alguna acción falta, el error de IAM nombra la acción denegada y se agrega
esa, no `*`.

Los nombres de acción con comodín (`ces:*:get*`) son los que documenta Huawei
para políticas personalizadas; ante cualquier duda, la consola de IAM los
valida al guardar.
