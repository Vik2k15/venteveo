# Catálogo Hornero Store (`Vik2k15/venteveo`)

Repositorio público conectado por defecto en **Hornero Store** (`Vik2k15/venteveo`, rama `main`) para distribuir y actualizar aplicaciones en TV Box.

## Cómo dejarlo andando en 2 pasos

1. Copiá todo el contenido de la carpeta `github-template/` (incluyendo la carpeta oculta `.github/`, `overrides.json` y `apks/`) a la raíz de `https://github.com/Vik2k15/venteveo` en la rama `main`.
2. Subí cualquier `.apk` de dos maneras:
   - **Opción recomendada (APKs de hasta 2 GB):** En GitHub andá a **Releases → Create a new release**, poné un tag (ej. `apps-v1` o `xuper-v7.2`), adjuntá tus `.apk` y tocá **Publish release**.
   - **Opción rápida (APKs < 25 MB por web o < 100 MB por git):** Subí los `.apk` dentro de la carpeta `apks/`.
3. A los ~20 segundos, el workflow de **GitHub Actions** (`Actualizar Catálogo Hornero Store`) lee cada `.apk` con `aapt2`, extrae el nombre real, `packageName`, `versionCode`, arquitectura, SHA-256 y el ícono `.png`, y actualiza automáticamente `apps.json` + `icons/`.
   - *(Tip: podés subir también el mismo `HorneroStore-v1.0.0.apk` en un Release si después querés que la propia tienda se actualice sola).*

## Nota sobre permisos de Actions en GitHub

Si GitHub Actions te llega a pedir permiso de escritura para actualizar `apps.json`, en tu repo andá a **Settings → Actions → General → Workflow permissions** y marcá **Read and write permissions**.

## Personalizar nombres, categorías o descripciones (opcional)

Si querés cambiar cómo aparece una app en la tienda, editá `overrides.json` usando su `packageName`:

```json
{
  "ignorePackages": [],
  "apps": {
    "com.ejemplo.app": {
      "name": "Mi App TV",
      "category": "Deportes",
      "description": "Descripción que va a aparecer en el TV Box.",
      "pinned": false
    }
  }
}
```
