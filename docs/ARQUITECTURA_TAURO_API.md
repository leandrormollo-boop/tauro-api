# ARQUITECTURA TAURO API — Documento de Handoff para Antigravity

*Tauro Solutions — Abril 2026*

---

## 1. Modelo de negocio

**Tauro Solutions** es un broker de envíos internacionales para eCommerce argentino (Shopify, TiendaNube).

- **Clientes directos:** tiendas eCommerce argentinas
- **Usuarios finales:** los compradores de esas tiendas (en el exterior)
- **Modelo invisible:** las tiendas nunca ven el costo real de FedEx; Tauro aplica un markup que queda oculto
- **Facturación MVP:** mensual a las tiendas
- **Carrier MVP:** FedEx International Priority
- **Carrier Fase 2:** DHL (la arquitectura ya lo contempla con `CarrierBase`)

---

## 2. Flujo de datos

```
Tienda (Shopify/TiendaNube)
        ↓  X-API-Key header
   [main.py / FastAPI]
        ↓
  [sheets_client.py]  ←→  Google Sheets (4 hojas)
        ↓
  [fedex_client.py]   ←→  FedEx API (OAuth2)
        ↓
  [email_sender.py]   →   Logística (PDF adjunto por SMTP)
```

---

## 3. Google Sheets — 4 hojas

### CONFIG
| CAMPO | VALOR |
|---|---|
| TIPO_CAMBIO_OFICIAL | 1400 |
| MARGEN_MINIMO_ARS | 5000 |

### PERFILES
| CLIENTE_ID | NOMBRE_COMPLETO | CUIT | DIRECCION_RETIRO | CODIGO_POSTAL | CIUDAD | PAIS | TELEFONO | EMAIL | API_KEY |
|---|---|---|---|---|---|---|---|---|---|
| JUAN_MENDEZ | Juan Mendez | 20-12345678-9 | Av. Corrientes 1234 | 1043 | BUENOS AIRES | AR | +5491112345678 | juan@tienda.com | tk_juanmendez_a3f9x |

### COTI
| CLIENTE_ID | PRODUCTO_ID | DESTINO_PAIS | PRECIO_ARS | COSTO_FEDEX_ARS | MARGEN_ARS |
|---|---|---|---|---|---|
| JUAN_MENDEZ | CARTERA_MINI | US | 45800 | 38000 | 7800 |

> **PRECIO_ARS** es el precio que cobra Tauro a la tienda (incluye markup).  
> **COSTO_FEDEX_ARS** se actualiza automáticamente cada lunes por el job.  
> **USD display** = PRECIO_ARS ÷ TIPO_CAMBIO_OFICIAL (nunca multiplicado).

### PRODUCTOS_CATALOGO
| CLIENTE_ID | PRODUCTO_ID | NOMBRE_ES | NOMBRE_EN | HS_CODE | VALOR_USD | UNIDADES | PESO_KG | LARGO | ANCHO | ALTO |
|---|---|---|---|---|---|---|---|---|---|---|
| JUAN_MENDEZ | CARTERA_MINI | Cartera de cuero mini | Mini leather handbag | 420221 | 150 | 1 | 0.4 | 28 | 18 | 8 |

---

## 4. Stack técnico

| Componente | Tecnología |
|---|---|
| Framework | FastAPI (Python) |
| Hosting recomendado | Railway |
| Base de datos MVP | Google Sheets (gspread) |
| Auth Google Sheets | Service Account (credenciales.json) |
| Email | SMTP Gmail con App Password |
| PDF | ReportLab |
| Scheduler | APScheduler (job semanal dentro del proceso FastAPI) |
| Carrier MVP | FedEx API (OAuth2) |

---

## 5. Archivos del proyecto

```
tauro-api/
├── main.py                  # FastAPI app, endpoints, scheduler
├── sheets_client.py         # Toda la lógica de Google Sheets
├── fedex_client.py          # Cliente FedEx con OAuth2 y retry
├── email_sender.py          # PDF con ReportLab + alertas de margen
├── credenciales.json        # Service Account Google (NO subir a GitHub)
├── .env                     # Variables de entorno (NO subir a GitHub)
├── requirements.txt         # Dependencias Python
└── .gitignore               # Excluye .env y credenciales.json
```

---

## 6. Variables de entorno (.env)

```env
# Google Sheets
GOOGLE_SHEET_URL=https://docs.google.com/spreadsheets/d/TU_ID_AQUI/edit

# Email (Gmail con App Password)
EMAIL_REMITENTE=logistica@taurosolutions.ar
EMAIL_PASSWORD=xxxx xxxx xxxx xxxx
EMAIL_DESTINO=logistica@taurosolutions.ar

# FedEx
FEDEX_API_KEY=tu_api_key
FEDEX_SECRET_KEY=tu_secret_key
FEDEX_ACCOUNT_NUMBER=tu_account_number
FEDEX_ENVIRONMENT=sandbox   # → production cuando esté listo

# Job semanal
MARGEN_MINIMO_ALERTA_ARS=5000
CRON_DIA=monday
CRON_HORA=6
```

---

## 7. Autenticación

Todas las rutas excepto `GET /` requieren el header:
```
X-API-Key: tk_juanmendez_a3f9x
```
La API Key se valida contra la columna `API_KEY` de la hoja `PERFILES`.  
Si no existe → 403 Forbidden.  
La key identifica al cliente automáticamente — el body NO necesita mandar `cliente_id`.

---

## 8. Endpoints

### GET /
Health check. Sin autenticación.
```json
{ "mensaje": "API Tauro Solutions operativa." }
```

---

### POST /cotizar
```
Headers: X-API-Key: tk_juanmendez_a3f9x
```
```json
{
  "producto_id": "CARTERA_MINI",
  "destino_pais": "US"
}
```
**Response 200:**
```json
{
  "status": "success",
  "producto_id": "CARTERA_MINI",
  "destino_pais": "US",
  "precio_ars": 45800,
  "precio_usd": 32.71,
  "tipo_cambio_usado": 1400
}
```
**Response 404:** combinación no encontrada en COTI  
**Response 403:** API Key inválida

---

### POST /pedido
```
Headers: X-API-Key: tk_juanmendez_a3f9x
```
```json
{
  "producto_id": "CARTERA_MINI",
  "destino_pais": "US",
  "nombre_comprador": "John Smith",
  "direccion_exacta": "123 Main St Apt 4B",
  "ciudad": "New York",
  "estado": "NY",
  "zip_code": "10001",
  "pais": "US",
  "telefono": "+12125551234",
  "email_comprador": "john@email.com"
}
```
**Response 200:**
```json
{
  "status": "success",
  "mensaje": "Pedido recibido. PDF enviado a logística.",
  "referencia": "JUAN_MENDEZ-CARTERA_MINI-20260401-1430"
}
```

---

## 9. PDF de armado de guía

El PDF contiene 4 bloques:
1. **Remitente** — datos del cliente (tienda) desde PERFILES
2. **Destinatario** — datos del comprador final desde el request
3. **Aduana** — nombre EN, HS Code, valor USD, peso, dimensiones desde PRODUCTOS_CATALOGO
4. **Financiero (interno Tauro)** — precio cobrado, costo FedEx, margen

---

## 10. Job semanal

- Corre los **lunes a las 6am (hora Argentina)**
- Itera todas las filas de COTI
- Llama a `fedex_client.get_rates()` por cada combinación
- Actualiza `COSTO_FEDEX_ARS` y recalcula `MARGEN_ARS` en el Sheet
- Si el margen cae por debajo de `MARGEN_MINIMO_ARS` (CONFIG), envía email de alerta

---

## 11. Setup inicial (checklist para Antigravity)

- [ ] `pip install fastapi uvicorn gspread oauth2client python-dotenv reportlab apscheduler requests`
- [ ] Crear Service Account en Google Cloud → descargar `credenciales.json`
- [ ] Compartir el Google Sheet con el email del Service Account
- [ ] Crear las 4 hojas en el Sheet: `CONFIG`, `COTI`, `PERFILES`, `PRODUCTOS_CATALOGO`
- [ ] Cargar al menos 1 cliente de prueba en PERFILES con API Key
- [ ] Cargar al menos 1 producto en PRODUCTOS_CATALOGO
- [ ] Cargar al menos 1 fila en COTI con PRECIO_ARS
- [ ] Configurar `.env` con todas las variables
- [ ] Correr: `uvicorn main:app --reload`
- [ ] Probar `GET /` → debe devolver mensaje OK
- [ ] Probar `POST /cotizar` con API Key y producto de prueba

---

## 12. Deploy en Railway

1. Crear cuenta en Railway (railway.app)
2. Subir el código a GitHub (sin `.env` ni `credenciales.json`)
3. Conectar el repo a Railway → deploy automático
4. Cargar variables de entorno en Railway (reemplazan el `.env`)
5. Para `credenciales.json`: convertirlo a base64 y cargarlo como variable de entorno `GOOGLE_CREDENTIALS_B64`
6. La API queda disponible en `https://tu-proyecto.railway.app`

---

## 13. Roadmap de fases

### Fase 1 — MVP ✅ (este código)
- [x] Arquitectura completa
- [x] `sheets_client.py` con 4 hojas
- [x] `main.py` con auth y endpoints `/cotizar` + `/pedido`
- [x] `email_sender.py` con PDF ReportLab
- [x] `fedex_client.py` con OAuth2 y retry
- [x] Job semanal APScheduler
- [ ] Deploy en Railway

### Fase 2 — Automatización FedEx
- [ ] `POST /fedex/enviar` → genera etiqueta real
- [ ] Tracking automático + notificación al comprador
- [ ] Webhook a la tienda cuando la guía está lista

### Fase 3 — Plataforma SaaS
- [ ] Panel web por cliente
- [ ] Sistema de crédito prepago
- [ ] Plugin Shopify App Store
- [ ] Plugin TiendaNube
- [ ] Multi-carrier: DHL Express (CarrierBase ya implementada)
